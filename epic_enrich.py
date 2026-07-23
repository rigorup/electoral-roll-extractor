"""Batch-fill ECINET enumeration details onto the `voters` table, by EPIC no.

The roll PDF only carries what is printed on the page. This module walks the
voters that still need it, resolves each EPIC through `eci_client`, and writes
the verified record back plus the two document images (EF photo and
enumeration-form page 1) into `epic_documents`.

Design rules:

* **One lookup per unique EPIC.** The same elector occurs in several rows
  (multiple years, re-ingests). The EPIC is resolved once and the result is
  applied to every voter row sharing it — never re-fetched.
* **Idempotent.** Rows already marked `Found` are not selected again, so the
  button can be clicked repeatedly and only fills what is still missing.
* **Per-AC cap.** At most `per_ac_cap` unique EPICs per constituency per run
  (default 100), so one click cannot pull an entire roll.
* **Parallel across constituencies.** Each AC has its own ERO token and its own
  slice of EPICs, so one worker per AC runs them concurrently — three selected
  constituencies means three lookups in flight at once. Within an AC the calls
  stay sequential, because that is one shared government token.
* **Expired tokens stop that AC** instead of being retried row after row; other
  constituencies keep going.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import eci_client
from dbx import connect, dhash, init_schema, to_int

STATUS_FOUND = "Found"
STATUS_NOT_FOUND = "Not found"
STATUS_EXPIRED = "Token expired"
STATUS_ERROR = "Error"

# voters column  ->  field in the ECINET `details` payload
FIELD_MAP = {
    "verified_name": "epicName",
    "verified_dob": "dobVerified",
    "mobile_no": "mobileNo",
    "father_or_guardian_name": "fathersOrGuardianName",
    "mother_name": "mothersName",
    "spouse_name": "spouseName",
    "verified_house_no": "houseNo",
    "verified_part_no": "partNo",
    "part_serial_no": "partSerialNo",
    "part_name": "progenyPartName",
    "ac_name": "progenyAcName",
    "category_type": "categoryType",
    "relation_type_code": "relationType",
    "relation_epic": "relationPrgyEpic",
    "relation_name_verified": "relationPrgyName",
    "district_cd": "districtCd",
    "state_cd": "stateCd",
    "survey_channel": "surveyChannel",
    "submitted_for_recommendation": "submittedForRecommendation",
    "enum_created_on": "createdDttm",
    "enum_modified_on": "modifiedDttm",
}

Progress = Callable[[str, float | None], None]


def _s(v) -> str | None:
    """ECINET nulls come back in several shapes; normalise to None/str."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def pending_summary(year: int | None = None) -> list[dict]:
    """Per-constituency counts of what is filled vs still outstanding."""
    init_schema()
    where = "WHERE epic_no IS NOT NULL AND epic_no <> ''"
    params: list = []
    if year is not None:
        where += " AND year = %s"
        params.append(year)
    sql = f"""
        SELECT constituency_no,
               count(DISTINCT epic_no) AS unique_epics,
               count(DISTINCT epic_no) FILTER (
                   WHERE epic_lookup_status = %s) AS done,
               count(DISTINCT epic_no) FILTER (
                   WHERE epic_lookup_status IS DISTINCT FROM %s) AS pending
        FROM voters {where}
        GROUP BY constituency_no
        ORDER BY constituency_no
    """
    with connect() as c:
        return c.execute(sql, [STATUS_FOUND, STATUS_FOUND, *params]).fetchall()


def _select_pending(c, year: int | None, acs: list[str] | None,
                    per_ac_cap: int) -> list[dict]:
    """Unique EPICs still needing a lookup, already capped per constituency.

    The window function does the capping in SQL so a huge table never has to be
    pulled into Python just to throw most of it away.
    """
    where = ["epic_no IS NOT NULL", "epic_no <> ''",
             "epic_lookup_status IS DISTINCT FROM %s"]
    params: list = [STATUS_FOUND]
    if year is not None:
        where.append("year = %s")
        params.append(year)
    if acs:
        where.append("constituency_no = ANY(%s)")
        params.append(list(acs))

    sql = f"""
        WITH uniq AS (
            SELECT DISTINCT ON (epic_no)
                   epic_no, constituency_no
              FROM voters
             WHERE {' AND '.join(where)}
             ORDER BY epic_no, constituency_no
        ), ranked AS (
            SELECT epic_no, constituency_no,
                   row_number() OVER (PARTITION BY constituency_no
                                      ORDER BY epic_no) AS rn
              FROM uniq
        )
        SELECT epic_no, constituency_no FROM ranked
         WHERE rn <= %s
         ORDER BY constituency_no, epic_no
    """
    return c.execute(sql, [*params, per_ac_cap]).fetchall()


def _store_images(c, epic: str, res: dict) -> tuple[int, list[str]]:
    """Fetch + upsert the two ECINET documents for one EPIC. (saved, errors)."""
    details = res.get("details") or {}
    ac = res.get("acNo")
    saved, errs = 0, []

    for field, doc_type in (("photoUrl", "photo"), ("srFormPage1Url", "sr_form")):
        path = details.get(field)
        if not path:
            continue
        # Already stored from an earlier run -> don't re-download.
        seen = c.execute(
            "SELECT 1 FROM epic_documents WHERE epic_no=%s AND doc_type=%s",
            (epic, doc_type)).fetchone()
        if seen:
            continue

        data, ext, err = eci_client.fetch_image_for_ac(path, ac)
        if err or not data:
            errs.append(f"{epic} {doc_type}: {err or 'no data'}")
            continue
        c.execute(
            """INSERT INTO epic_documents (epic_no, doc_type, image, ext, phash)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (epic_no, doc_type) DO UPDATE
                 SET image = EXCLUDED.image, ext = EXCLUDED.ext,
                     phash = EXCLUDED.phash, fetched_at = now()""",
            (epic, doc_type, data, ext, dhash(data)),
        )
        saved += 1
    return saved, errs


def _apply(c, epic: str, res: dict, include_aadhaar: bool) -> int:
    """Write one resolved record onto every voter row sharing that EPIC."""
    details = res.get("details") or {}
    cols = {col: _s(details.get(field)) for col, field in FIELD_MAP.items()}
    cols["verified_age"] = to_int(details.get("erollAge"))
    cols["epic_id"] = _s(res.get("epicId"))
    cols["lookup_ac_no"] = _s(res.get("acNo"))
    cols["lookup_officer"] = _s(res.get("officer"))
    cols["epic_lookup_status"] = STATUS_FOUND
    if include_aadhaar:
        cols["aadhaar_ref_no"] = _s(details.get("aadharNo"))

    sets = ", ".join(f"{k} = %s" for k in cols)
    return c.execute(
        f"UPDATE voters SET {sets}, epic_lookup_at = now() WHERE epic_no = %s",
        [*cols.values(), epic],
    ).rowcount


def _mark(c, epic: str, status: str) -> int:
    return c.execute(
        "UPDATE voters SET epic_lookup_status=%s, epic_lookup_at=now() "
        "WHERE epic_no=%s", (status, epic)).rowcount


def _enrich_one_ac(ac: str, epics: list[str], cfg: dict, *,
                   include_images: bool, include_aadhaar: bool, delay: float,
                   stats: dict, lock: threading.Lock, counter: dict) -> None:
    """Process every pending EPIC for a single constituency, sequentially.

    Runs on its own thread with its own DB connection and only ever touches
    this AC's token, so parallel workers never contend. All writes to the
    shared `stats`/`counter` go through `lock`.
    """
    with connect() as c:
        for epic in epics:
            try:
                res = eci_client.lookup_in_ac(epic, ac, cfg)
            except Exception as e:  # noqa: BLE001
                with lock:
                    stats["errors"] += _mark(c, epic, STATUS_ERROR)
                    stats["messages"].append(f"{epic}: {e}")
                    counter["done"] += 1
                c.commit()
                continue

            with lock:
                stats["api_calls"] += 1

            if res.get("ok"):
                saved, errs = (0, [])
                if include_images:
                    saved, errs = _store_images(c, epic, res)
                updated = _apply(c, epic, res, include_aadhaar)
                c.commit()
                with lock:
                    stats["images_saved"] += saved
                    stats["image_errors"].extend(errs)
                    stats["rows_updated"] += updated
                    stats["found"] += 1
                    stats["per_ac"][ac] = stats["per_ac"].get(ac, 0) + 1
                    counter["done"] += 1
            elif res.get("expired"):
                _mark(c, epic, STATUS_EXPIRED)
                c.commit()
                with lock:
                    stats["errors"] += 1
                    stats["messages"].append(res.get("message", "token expired"))
                    stats["expired_acs"].add(ac)
                    counter["done"] += 1
                # This AC's token is dead — every remaining EPIC here fails the
                # same way. Stop this worker; other constituencies continue.
                return
            else:
                _mark(c, epic, STATUS_NOT_FOUND)
                c.commit()
                with lock:
                    stats["not_found"] += 1
                    counter["done"] += 1

            if delay:
                time.sleep(delay)


def enrich_pending(
    *,
    year: int | None = None,
    acs: list[str] | None = None,
    per_ac_cap: int = 100,
    include_images: bool = True,
    include_aadhaar: bool = False,
    delay: float = 0.0,
    max_workers: int | None = None,
    progress: Progress | None = None,
) -> dict:
    """Fill outstanding voters, running constituencies in parallel.

    One worker thread per AC (up to `max_workers`), so N selected
    constituencies do N lookups at once. Returns a stats dict for the UI.
    """
    progress = progress or (lambda msg, frac=None: None)
    init_schema()

    stats = {
        "unique_epics": 0, "rows_updated": 0, "api_calls": 0,
        "found": 0, "not_found": 0, "errors": 0,
        "images_saved": 0, "image_errors": [], "messages": [],
        "per_ac": {}, "expired_acs": set(), "stopped_early": False,
        "workers": 0,
    }

    with connect() as c:
        targets = _select_pending(c, year, acs, per_ac_cap)

    stats["unique_epics"] = len(targets)
    if not targets:
        progress("Nothing pending — every EPIC is already filled.", 1.0)
        stats["expired_acs"] = []
        return stats

    # Partition the work by constituency: one bucket per AC, each run by a
    # single worker so a shared token is never hit concurrently.
    by_ac: dict[str, list[str]] = defaultdict(list)
    for row in targets:
        by_ac[row["constituency_no"]].append(row["epic_no"])

    cfg = eci_client.load_config()
    lock = threading.Lock()
    counter = {"done": 0, "total": len(targets)}
    workers = min(max_workers or len(by_ac), len(by_ac))
    stats["workers"] = workers

    progress(f"0/{counter['total']} — {len(by_ac)} constituencies, "
             f"{workers} in parallel…", 0.0)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(_enrich_one_ac, ac, epics, cfg,
                      include_images=include_images,
                      include_aadhaar=include_aadhaar, delay=delay,
                      stats=stats, lock=lock, counter=counter)
            for ac, epics in by_ac.items()
        ]
        # Poll from THIS (main) thread so every Streamlit UI call stays on the
        # script thread — worker threads only touch shared counters.
        while any(not f.done() for f in futures):
            with lock:
                done = counter["done"]
                acs_live = ", ".join(f"AC {a}:{n}" for a, n in
                                     sorted(stats["per_ac"].items()))
            progress(f"{done}/{counter['total']} filled so far"
                     + (f" — {acs_live}" if acs_live else "") + " …",
                     done / counter["total"])
            time.sleep(0.4)
        for f in futures:
            f.result()  # re-raise any worker exception

    # All workers whose token expired -> nothing more can be done this run.
    if stats["expired_acs"] and stats["expired_acs"] >= set(by_ac):
        stats["stopped_early"] = True
    stats["expired_acs"] = sorted(stats["expired_acs"])
    progress(f"Done — {stats['found']} filled, {stats['not_found']} not found.",
             1.0)
    return stats
