"""FastAPI command-center backend for the electoral-roll fraud-detection tool.

This is a full replacement for the old Streamlit UI. It exposes the existing
engine modules (``dbx``, ``fraud_rules``, ``combined_model``, ``explore``,
``epic_enrich``, ``eci_client``, ``pipeline``, ``reports``, ``combined_pdf``)
over a JSON/REST API described in ``API_CONTRACT.md`` and serves the vanilla-JS
SPA that lives in ``web/``.

Design rules that make this module safe to import and boot with NO database and
NONE of the heavy native dependencies (psycopg / cv2 / fitz / mistralai)
installed:

* **Lazy imports.** Only ``fastapi`` / ``starlette`` / ``webauth`` / stdlib are
  imported at module top. Every engine module is imported *inside* the handler
  (or a small helper) that needs it, so ``import server`` never drags in a
  native dependency.
* **DB errors never 500.** Every database-touching handler runs through
  :func:`db_call`, which turns any connection/query error into a clean
  ``503 {"detail": ...}`` instead of leaking a stack trace.
* **Blocking work off the event loop.** All engine calls (they use blocking
  psycopg / OCR / PDF libraries) run in a threadpool via
  ``fastapi.concurrency.run_in_threadpool``.
"""
from __future__ import annotations

import csv
import io
import os
import secrets
import threading
import uuid
from typing import Any, Callable

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               Response)
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import webauth

# Load a local .env for development (`uvicorn server:app --reload`); in Docker
# the environment is passed directly. Guarded so a missing python-dotenv never
# breaks import.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

APP_NAME = "Electoral Roll Command Center"

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)

# Signed-cookie sessions hold the auth state (see webauth). The secret is taken
# from the environment when present so cookies survive a restart; otherwise a
# per-process random secret is used (sessions drop on restart, which is fine).
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("APP_SESSION_SECRET") or secrets.token_hex(32),
    https_only=False,
    same_site="lax",
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
async def db_call(fn: Callable, *args, **kwargs):
    """Run a blocking engine call in a threadpool and normalise failures.

    Any exception raised by a DB-touching call becomes a ``503`` with the error
    message as ``detail`` so the SPA can show a clean "database unreachable"
    banner instead of a 500 stack trace. ``HTTPException`` raised deliberately
    inside ``fn`` (e.g. a 404/409) is passed through untouched.
    """
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — any DB/driver error -> 503
        raise HTTPException(status_code=503, detail=str(e))


def _sanitize(obj: Any) -> Any:
    """Recursively convert non-JSON-native values (notably ``set``) to lists so
    responses built by hand can be serialised. Datetimes are left to
    ``jsonable_encoder`` at the response layer."""
    if isinstance(obj, set):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
}


def _image_response(data: bytes, ext: str | None) -> Response:
    """Raw image bytes with a private, hour-long cache."""
    media = _CONTENT_TYPES.get((ext or ".jpg").lower(), "image/jpeg")
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "private, max-age=3600"})


# --------------------------------------------------------------------------- #
# Auth & meta                                                                  #
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@app.get("/api/config")
async def api_config():
    """Public: is auth configured, and the app name (for the login screen)."""
    ok, msg = webauth.auth_configured()
    return {"auth_configured": ok, "message": msg, "app_name": APP_NAME}


@app.get("/api/me")
async def api_me(request: Request):
    """Public: current session identity."""
    user = webauth.current_user(request)
    return {"authenticated": bool(user), "user": user}


@app.post("/api/login")
async def api_login(request: Request, body: LoginBody):
    ok, msg = webauth.attempt_login(request, body.username, body.password)
    if not ok:
        raise HTTPException(status_code=401, detail=msg)
    return {"user": msg}


@app.post("/api/logout")
async def api_logout(request: Request):
    webauth.logout(request)
    return {"ok": True}


@app.get("/api/health")
async def api_health(user: str = Depends(webauth.require_auth)):
    def work():
        from dbx import db_ready
        return db_ready()

    # db_ready never raises, but importing dbx can (e.g. driver missing) — fold
    # that into a clean "not ready" answer rather than a 500.
    try:
        ok, msg = await run_in_threadpool(work)
    except Exception as e:  # noqa: BLE001
        ok, msg = False, str(e)
    return {"db_ready": ok, "message": msg}


@app.get("/api/years")
async def api_years(user: str = Depends(webauth.require_auth)):
    def work():
        from dbx import available_years
        return available_years()

    return {"years": await db_call(work)}


@app.get("/api/rules")
async def api_rules(user: str = Depends(webauth.require_auth)):
    def work():
        # Engine import happens inside the threadpool call so a missing native
        # dependency surfaces as a clean 503, not a 500.
        from fraud_rules import RULES
        return [{"id": rid, "severity": spec[0], "description": spec[1]}
                for rid, spec in RULES.items()]

    return {"rules": await db_call(work)}


# --------------------------------------------------------------------------- #
# Overview / dashboard                                                         #
# --------------------------------------------------------------------------- #
@app.get("/api/overview")
async def api_overview(year: int, user: str = Depends(webauth.require_auth)):
    def work():
        from dbx import connect
        from fraud_rules import (flag_counts_by_constituency, flag_summary,
                                 reviewed_summary)

        by_rule = flag_summary(year)
        flags_total = sum(r["flags"] for r in by_rule)
        by_severity = {"high": 0, "medium": 0, "low": 0}
        for r in by_rule:
            sev = r["severity"] if r["severity"] in by_severity else "low"
            by_severity[sev] += r["flags"]

        rev = reviewed_summary(year)
        confirmed = rev.get("confirmed", 0)
        legitimate = rev.get("legitimate", 0)
        needs_info = rev.get("needs_info", 0)
        open_ct = max(0, flags_total - (confirmed + legitimate + needs_info))

        with connect() as c:
            voters_total = c.execute(
                "SELECT count(*) n FROM voters WHERE year=%s", (year,)
            ).fetchone()["n"]

        top = flag_counts_by_constituency(year)[:12]
        return {
            "year": year,
            "voters_total": voters_total,
            "flags_total": flags_total,
            "by_severity": by_severity,
            "reviewed": {"confirmed": confirmed, "legitimate": legitimate,
                         "needs_info": needs_info, "open": open_ct},
            "by_rule": by_rule,
            "top_constituencies": top,
        }

    return jsonable_encoder(await db_call(work))


# --------------------------------------------------------------------------- #
# Suspects — the combined-model cluster view                                   #
# --------------------------------------------------------------------------- #
# In-memory cache of built combined-model reports, one per year.
_combined_cache: dict[int, dict] = {}
_combined_lock = threading.Lock()


def _cached_records(year: int) -> list[dict]:
    """Return the cached combined records for a year, or 409 if not built."""
    with _combined_lock:
        entry = _combined_cache.get(int(year))
    if not entry:
        raise HTTPException(status_code=409, detail="not_built")
    return entry["records"]


@app.post("/api/suspects/build")
async def api_suspects_build(request: Request,
                             user: str = Depends(webauth.require_auth)):
    from datetime import datetime, timezone

    body = await request.json()
    year = int(body["year"])

    def work():
        from combined_model import build_combined, combined_summary
        records = build_combined(year)
        return records, combined_summary(records)

    records, summary = await db_call(work)
    built_at = datetime.now(timezone.utc).isoformat()
    with _combined_lock:
        _combined_cache[year] = {"records": records, "built_at": built_at}
    return jsonable_encoder({"total": len(records), "built_at": built_at,
                             "summary": summary})


@app.get("/api/suspects/summary")
async def api_suspects_summary(year: int,
                               user: str = Depends(webauth.require_auth)):
    with _combined_lock:
        entry = _combined_cache.get(int(year))
    if not entry:
        return {"built": False}

    def work():
        from combined_model import combined_summary, constituencies_in
        records = entry["records"]
        return combined_summary(records), constituencies_in(records)

    summary, constituencies = await run_in_threadpool(work)
    return jsonable_encoder({"built": True, "built_at": entry["built_at"],
                             "summary": summary,
                             "constituencies": constituencies})


@app.get("/api/suspects")
async def api_suspects(
    year: int,
    ac: str | None = None,
    severity: str | None = None,
    signal: str | None = None,
    min_matches: int = 0,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: str = Depends(webauth.require_auth),
):
    records = _cached_records(year)  # raises 409 if not built

    ql = (q or "").strip().lower()

    def matches(r: dict) -> bool:
        if severity and r.get("severity") != severity:
            return False
        if signal:
            if signal == "cosine" and not r.get("cosine"):
                return False
            if signal == "fuzzy" and not r.get("fuzzy"):
                return False
            if signal == "logical" and not r.get("logical"):
                return False
            if signal == "nomap" and not r.get("no_mapping"):
                return False
        if min_matches and (r.get("n_dups") or 0) < min_matches:
            return False
        if ac and (r.get("constituency_no") or "") != ac:
            return False
        if ql:
            hay = " ".join(str(r.get(k) or "") for k in
                           ("name", "epic_no", "constituency_no",
                            "constituency_name")).lower()
            if ql not in hay:
                return False
        return True

    filtered = [r for r in records if matches(r)]
    total = len(filtered)
    sliced = filtered[offset:offset + limit]
    return jsonable_encoder({"total": total, "offset": offset, "limit": limit,
                             "records": sliced})


# --------------------------------------------------------------------------- #
# Review queue                                                                 #
# --------------------------------------------------------------------------- #
@app.get("/api/flags")
async def api_flags(
    year: int,
    rule: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: str = Depends(webauth.require_auth),
):
    def work():
        from fraud_rules import open_flags
        # Pull one extra past the window to know whether more remain.
        rows = open_flags(rule, limit=offset + limit + 1, year=year)
        window = rows[offset:offset + limit]
        has_more = len(rows) > offset + limit
        return {"flags": window, "has_more": has_more}

    return jsonable_encoder(await db_call(work))


@app.get("/api/flags/summary")
async def api_flags_summary(year: int,
                            user: str = Depends(webauth.require_auth)):
    def work():
        from fraud_rules import (RULES, flag_counts_by_constituency,
                                 flag_counts_by_constituency_rule,
                                 flag_entry_counts_by_constituency_rule,
                                 flag_summary)
        return {
            "by_rule": flag_summary(year),
            "by_constituency": flag_counts_by_constituency(year),
            "matrix": {
                "rules": list(RULES),
                "measure_flags": flag_counts_by_constituency_rule(year),
                "measure_entries": flag_entry_counts_by_constituency_rule(year),
            },
        }

    return jsonable_encoder(await db_call(work))


class ReviewBody(BaseModel):
    verdict: str
    reviewer: str
    notes: str = ""


@app.post("/api/flags/{flag_id}/review")
async def api_flag_review(flag_id: int, body: ReviewBody,
                          user: str = Depends(webauth.require_auth)):
    def work():
        from fraud_rules import record_review
        record_review(flag_id, body.verdict, body.reviewer, body.notes)

    await db_call(work)
    return {"ok": True}


@app.post("/api/flags/{flag_id}/reopen")
async def api_flag_reopen(flag_id: int,
                          user: str = Depends(webauth.require_auth)):
    def work():
        from fraud_rules import reopen_flag
        reopen_flag(flag_id)

    await db_call(work)
    return {"ok": True}


class RunRulesBody(BaseModel):
    year: int
    rules: list[str] | None = None


@app.post("/api/rules/run")
async def api_rules_run(body: RunRulesBody,
                        user: str = Depends(webauth.require_auth)):
    def work():
        from fraud_rules import run_rules
        return run_rules(body.rules, body.year)

    return {"added": await db_call(work)}


class ClearFlagsBody(BaseModel):
    year: int


@app.post("/api/flags/clear")
async def api_flags_clear(body: ClearFlagsBody,
                          user: str = Depends(webauth.require_auth)):
    def work():
        from fraud_rules import clear_flags
        clear_flags(body.year)

    await db_call(work)
    return {"ok": True}


@app.get("/api/house")
async def api_house(cn: str, house_norm: str, year: int,
                    user: str = Depends(webauth.require_auth)):
    def work():
        from fraud_rules import house_members
        return house_members(cn, house_norm, year)

    return jsonable_encoder({"members": await db_call(work)})


# --------------------------------------------------------------------------- #
# Reviewed history                                                             #
# --------------------------------------------------------------------------- #
@app.get("/api/reviewed")
async def api_reviewed(
    year: int,
    verdict: str | None = None,
    rule: str | None = None,
    limit: int = 200,
    user: str = Depends(webauth.require_auth),
):
    def work():
        from fraud_rules import reviewed_flags, reviewed_summary
        return {"flags": reviewed_flags(verdict, rule, limit, year),
                "summary": reviewed_summary(year)}

    return jsonable_encoder(await db_call(work))


# --------------------------------------------------------------------------- #
# Explore / search                                                             #
# --------------------------------------------------------------------------- #
@app.get("/api/explore/options")
async def api_explore_options(year: int,
                              user: str = Depends(webauth.require_auth)):
    def work():
        from explore import (PAGE_SIZE, SORTS, STATUS_CHOICES, filter_options,
                             parts_for)
        return {
            "options": filter_options(year),
            "parts": parts_for(year, []),
            "sorts": list(SORTS),
            "statuses": STATUS_CHOICES,
            "page_size": PAGE_SIZE,
        }

    return jsonable_encoder(await db_call(work))


@app.get("/api/explore/parts")
async def api_explore_parts(year: int, ac: list[str] = Query(default=[]),
                            user: str = Depends(webauth.require_auth)):
    def work():
        from explore import parts_for
        return parts_for(year, ac)

    return {"parts": await db_call(work)}


@app.get("/api/explore")
async def api_explore(
    year: int | None = None,
    ac: list[str] = Query(default=[]),
    part: list[str] = Query(default=[]),
    gender: list[str] = Query(default=[]),
    relation_type: list[str] = Query(default=[]),
    category_type: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    age_min: int | None = None,
    age_max: int | None = None,
    has_mobile: bool = False,
    has_photo: bool = False,
    q: str = "",
    sort: str = "",
    page: int = 1,
    user: str = Depends(webauth.require_auth),
):
    def work():
        from explore import PAGE_SIZE, SORTS, Filters, search
        f = Filters(
            year=year, acs=ac, parts=part, genders=gender,
            relation_types=relation_type, category_types=category_type,
            statuses=status, age_min=age_min, age_max=age_max,
            has_mobile=has_mobile, has_photo=has_photo, query=q,
        )
        chosen = sort if sort in SORTS else next(iter(SORTS))
        rows, total = search(f, sort=chosen, page=page)
        return {"rows": rows, "total": total, "page": page,
                "page_size": PAGE_SIZE}

    return jsonable_encoder(await db_call(work))


@app.get("/api/voter/{voter_id}")
async def api_voter(voter_id: int, user: str = Depends(webauth.require_auth)):
    def work():
        from explore import voter_full
        return voter_full(voter_id)

    row = await db_call(work)
    if not row:
        raise HTTPException(status_code=404, detail="voter not found")
    return jsonable_encoder({"voter": row})


@app.get("/api/person/{epic}")
async def api_person(epic: str, user: str = Depends(webauth.require_auth)):
    def work():
        from explore import person_profile
        return person_profile(epic)

    profile = await db_call(work)
    # Strip the raw image bytes from each document — they are served separately
    # via /api/epic-doc/{epic}/{doc_type}; the JSON keeps only the metadata.
    docs = []
    for d in profile.get("documents", []):
        docs.append({"doc_type": d.get("doc_type"), "ext": d.get("ext"),
                     "bytes": d.get("bytes"), "fetched_at": d.get("fetched_at")})
    profile["documents"] = docs
    return jsonable_encoder(profile)


@app.get("/api/explore/export.csv")
async def api_explore_export(
    year: int | None = None,
    ac: list[str] = Query(default=[]),
    part: list[str] = Query(default=[]),
    gender: list[str] = Query(default=[]),
    relation_type: list[str] = Query(default=[]),
    category_type: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    age_min: int | None = None,
    age_max: int | None = None,
    has_mobile: bool = False,
    has_photo: bool = False,
    q: str = "",
    sort: str = "",
    user: str = Depends(webauth.require_auth),
):
    def work():
        from explore import LIST_COLS, SORTS, Filters, export_rows
        f = Filters(
            year=year, acs=ac, parts=part, genders=gender,
            relation_types=relation_type, category_types=category_type,
            statuses=status, age_min=age_min, age_max=age_max,
            has_mobile=has_mobile, has_photo=has_photo, query=q,
        )
        chosen = sort if sort in SORTS else next(iter(SORTS))
        rows = export_rows(f, sort=chosen)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=LIST_COLS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return buf.getvalue()

    csv_text = await db_call(work)
    fname = f"voters_{year or 'all'}.csv"
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{fname}"'})


# --------------------------------------------------------------------------- #
# Media                                                                        #
# --------------------------------------------------------------------------- #
@app.get("/api/photo/{voter_id}")
async def api_photo(voter_id: int, user: str = Depends(webauth.require_auth)):
    def work():
        from fraud_rules import get_photo
        return get_photo(voter_id)

    data = await db_call(work)
    if not data:
        raise HTTPException(status_code=404, detail="no photo")
    return _image_response(data, ".jpg")


@app.get("/api/epic-doc/{epic}/{doc_type}")
async def api_epic_doc(epic: str, doc_type: str,
                       user: str = Depends(webauth.require_auth)):
    def work():
        from explore import epic_documents
        return epic_documents(epic)

    docs = await db_call(work)
    match = next((d for d in docs
                  if d.get("doc_type") == doc_type and d.get("image")), None)
    if not match:
        raise HTTPException(status_code=404, detail="no document")
    return _image_response(match["image"], match.get("ext"))


# --------------------------------------------------------------------------- #
# Ingest (PDF -> Excel/ZIP -> DB)                                              #
# --------------------------------------------------------------------------- #
# Token-keyed cache of extraction results, bounded to the most recent entries.
_ingest_cache: dict[str, dict] = {}
_ingest_lock = threading.Lock()
_INGEST_MAX = 8


def _ingest_store(token: str, entry: dict) -> None:
    with _ingest_lock:
        _ingest_cache[token] = entry
        while len(_ingest_cache) > _INGEST_MAX:
            # drop the oldest inserted entry (insertion order preserved by dict)
            _ingest_cache.pop(next(iter(_ingest_cache)))


def _ingest_get(token: str) -> dict:
    with _ingest_lock:
        entry = _ingest_cache.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="unknown or expired token")
    return entry


@app.get("/api/ingest/meta")
async def api_ingest_meta(user: str = Depends(webauth.require_auth)):
    def work():
        from extractor import COLUMNS
        from pipeline import BATCH_SIZE, BATCH_THRESHOLD
        return {
            "batch_threshold": BATCH_THRESHOLD,
            "batch_size": BATCH_SIZE,
            "ocr_provider": os.getenv("OCR_PROVIDER", "mistral"),
            "mistral_key_set": bool(os.getenv("MISTRAL_API_KEY")),
            "methods": ["regex", "llm"],
            "columns": COLUMNS,
        }

    return await db_call(work)


@app.post("/api/ingest/extract")
async def api_ingest_extract(
    file: UploadFile = File(...),
    method: str = Form("regex"),
    include_photos: bool = Form(False),
    trim: bool = Form(False),
    drop_first: int = Form(2),
    drop_last: int = Form(2),
    user: str = Depends(webauth.require_auth),
):
    filename = file.filename or "electoral_roll.pdf"
    pdf_bytes = await file.read()

    def work():
        import zipfile

        import pandas as pd

        from dbx import year_from_filename
        from ocr_providers import get_provider
        from pdf_utils import trim_pages
        from pipeline import process_pdf

        progress = lambda msg, frac=None: None  # noqa: E731 — no-op progress
        provider = get_provider()
        work_bytes = (trim_pages(pdf_bytes, drop_first, drop_last)
                      if trim else pdf_bytes)

        df, issues, zip_bytes, base = process_pdf(
            work_bytes, filename, method, include_photos, provider, progress)

        # Auto-retry on the full PDF if trimming emptied the result.
        if df.empty and trim:
            df, issues, zip_bytes, base = process_pdf(
                pdf_bytes, filename, method, include_photos, provider, progress)

        # Reconstruct the photos dict (Photo_Id -> bytes) from the ZIP the
        # pipeline built, exactly as the old ingest page did.
        photos: dict[str, bytes] = {}
        if include_photos and zip_bytes:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            photos = {n.split("/")[-1]: zf.read(n) for n in zf.namelist()
                      if "/photos/" in n and not n.endswith("/")}

        # Build a JSON-safe preview from the first 50 rows.
        head = df.head(50)
        head = head.astype(object).where(pd.notnull(head), None)
        preview = head.to_dict("records")

        n_photos = (len(photos) if photos
                    else int(sum(1 for p in df.get("Photo_Id", []) if p)))
        return {
            "df": df, "photos": photos, "zip_bytes": zip_bytes, "base": base,
            "issues": issues, "preview": preview, "n_photos": n_photos,
            "columns": list(df.columns),
            "year_guess": year_from_filename(filename),
            "rows": int(len(df)),
        }

    try:
        result = await run_in_threadpool(work)
    except Exception as e:  # noqa: BLE001 — OCR/extraction failure
        raise HTTPException(status_code=500, detail=str(e))

    if result["rows"] == 0:
        raise HTTPException(
            status_code=422,
            detail="No voter records found. Check that this is an English "
                   "electoral-roll PDF.")

    token = uuid.uuid4().hex
    _ingest_store(token, {
        "df": result["df"], "photos": result["photos"],
        "zip_bytes": result["zip_bytes"], "base": result["base"],
        "issues": result["issues"], "filename": filename,
        "year_guess": result["year_guess"],
    })

    return jsonable_encoder({
        "token": token,
        "filename": filename,
        "rows": result["rows"],
        "columns": result["columns"],
        "preview": result["preview"],
        "issues": result["issues"],
        "n_photos": result["n_photos"],
        "year_guess": result["year_guess"],
    })


@app.get("/api/ingest/download/{token}")
async def api_ingest_download(token: str,
                              user: str = Depends(webauth.require_auth)):
    entry = _ingest_get(token)
    zip_bytes = entry.get("zip_bytes")
    if not zip_bytes:
        raise HTTPException(status_code=404, detail="no bundle for this token")
    base = entry.get("base") or "electoral_roll"
    return Response(content=zip_bytes, media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{base}.zip"'})


class ToDbBody(BaseModel):
    token: str
    year: int


@app.post("/api/ingest/to_db")
async def api_ingest_to_db(body: ToDbBody,
                           user: str = Depends(webauth.require_auth)):
    entry = _ingest_get(body.token)

    def work():
        from dbx import ingest_dataframe
        return ingest_dataframe(entry["df"], entry["filename"],
                                entry.get("photos"), body.year)

    ingest_id, n_v, n_p = await db_call(work)
    return {"ingest_id": ingest_id, "voters": n_v, "photos": n_p,
            "year": body.year}


# --------------------------------------------------------------------------- #
# Enrichment (ECINET)                                                          #
# --------------------------------------------------------------------------- #
@app.get("/api/enrich/summary")
async def api_enrich_summary(year: int,
                             user: str = Depends(webauth.require_auth)):
    def work():
        import eci_client
        from epic_enrich import pending_summary
        available, message = eci_client.config_available()
        return {
            "pending": pending_summary(year),
            "config": {
                "available": available,
                "message": message,
                "source": eci_client.config_source(),
                "acs": eci_client.configured_acs(),
            },
        }

    return jsonable_encoder(await db_call(work))


class EnrichConfigBody(BaseModel):
    # The wire field is "json" (per the contract); the attribute is renamed to
    # avoid shadowing BaseModel.json.
    config_json: str = Field(alias="json")


@app.post("/api/enrich/config")
async def api_enrich_config(body: EnrichConfigBody,
                            user: str = Depends(webauth.require_auth)):
    import json as _json

    import eci_client

    raw = body.config_json
    try:
        cfg = _json.loads(raw)
    except Exception as e:  # noqa: BLE001 — bad JSON from the operator
        return {"ok": False, "message": f"config is not valid JSON: {e}"}

    ok, message = eci_client.validate_config(cfg)
    if not ok:
        return {"ok": False, "message": message}

    key = eci_client.SETTING_KEY

    def work():
        from dbx import set_setting
        set_setting(key, raw)

    await db_call(work)
    return {"ok": True, "message": message}


class EnrichRunBody(BaseModel):
    year: int
    acs: list[str] | None = None
    per_ac_cap: int = 100
    include_images: bool = True
    include_aadhaar: bool = False


@app.post("/api/enrich/run")
async def api_enrich_run(body: EnrichRunBody,
                         user: str = Depends(webauth.require_auth)):
    def work():
        from epic_enrich import enrich_pending
        return enrich_pending(
            year=body.year, acs=body.acs, per_ac_cap=body.per_ac_cap,
            include_images=body.include_images,
            include_aadhaar=body.include_aadhaar)

    stats = await db_call(work)
    return jsonable_encoder({"stats": _sanitize(stats)})


# --------------------------------------------------------------------------- #
# Reports / exports                                                            #
# --------------------------------------------------------------------------- #
def _pdf_response(data: bytes, filename: str) -> Response:
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


def _zip_response(data: bytes, filename: str) -> Response:
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


# The model rules whose reports use the richer side-by-side comparison builder.
_COMPARE_RULES = {"fuzzy_new", "cosine_new"}


@app.get("/api/reports/flags.pdf")
async def api_reports_flags_pdf(
    year: int,
    rule: str | None = None,
    ac: str | None = None,
    user: str = Depends(webauth.require_auth),
):
    def work():
        from reports import build_compare_pdf, build_flags_pdf
        builder = build_compare_pdf if rule in _COMPARE_RULES else build_flags_pdf
        return builder(rule, year, ac)

    data = await db_call(work)
    return _pdf_response(data, f"fraud_flags_{year}_{ac or 'all'}.pdf")


@app.get("/api/reports/flags.zip")
async def api_reports_flags_zip(
    year: int,
    rule: str | None = None,
    user: str = Depends(webauth.require_auth),
):
    def work():
        from reports import (build_compare_pdf, build_flags_pdf,
                             build_flags_pdf_zip)
        builder = build_compare_pdf if rule in _COMPARE_RULES else build_flags_pdf
        return build_flags_pdf_zip(rule, year, builder=builder)

    data = await db_call(work)
    return _zip_response(data, f"fraud_flags_{year}_by_constituency.zip")


@app.get("/api/reports/combined_comprehensive.zip")
async def api_reports_combined_comprehensive(
    year: int,
    ac: str | None = None,
    top: int | None = None,
    per_file: int = 50,
    user: str = Depends(webauth.require_auth),
):
    records = _cached_records(year)  # 409 if not built
    per_file = max(1, min(per_file, 50))  # at most 50 voters per PDF

    def work():
        from combined_pdf import build_comprehensive_zip_chunked
        recs = records
        if ac:
            recs = [r for r in recs if (r.get("constituency_no") or "") == ac]
            scope = f"AC {ac}"
        elif top:
            recs = recs[:top]
            scope = f"top {top}"
        else:
            scope = "all constituencies"
        return build_comprehensive_zip_chunked(
            recs, year, per_file=per_file, scope_label=scope)

    data = await db_call(work)
    return _zip_response(data, f"combined_comprehensive_{year}_{ac or 'all'}.zip")


@app.get("/api/reports/combined_dossier.zip")
async def api_reports_combined_dossier(
    year: int,
    ac: str | None = None,
    count: int | None = None,
    per_file: int = 50,
    user: str = Depends(webauth.require_auth),
):
    records = _cached_records(year)  # 409 if not built
    per_file = max(1, min(per_file, 50))  # at most 50 voters per PDF

    def work():
        from combined_pdf import build_dossier_zip
        recs = records
        if ac:
            recs = [r for r in recs if (r.get("constituency_no") or "") == ac]
            scope = f"AC {ac}"
        else:
            scope = ""
        if count:
            recs = recs[:count]
        return build_dossier_zip(recs, year, per_file=per_file,
                                 scope_label=scope)

    data = await db_call(work)
    return _zip_response(data, f"combined_dossier_{year}_{ac or 'all'}.zip")


# --------------------------------------------------------------------------- #
# Static SPA (registered LAST so all /api routes take precedence)             #
# --------------------------------------------------------------------------- #
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _safe_web_path(rel: str) -> str | None:
    """Resolve ``rel`` under web/, guarding against path traversal."""
    root = os.path.realpath(_WEB_DIR)
    full = os.path.realpath(os.path.join(root, rel))
    if full == root or full.startswith(root + os.sep):
        return full
    return None


@app.get("/{path:path}")
async def spa(path: str):
    """SPA fallback: serve real files under web/, otherwise index.html.

    Anything under ``/api/...`` that reaches here did not match a real API
    route, so it is a genuine 404 (returned as JSON, not the SPA shell)."""
    if path.startswith("api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    if path and not path.endswith("/"):
        full = _safe_web_path(path)
        if full and os.path.isfile(full):
            return FileResponse(full)

    index = os.path.join(_WEB_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return PlainTextResponse("frontend not built yet", status_code=200)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0",
                port=int(os.getenv("PORT", "8000")))
