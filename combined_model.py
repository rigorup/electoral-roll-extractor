"""Combined Model — one per-voter report that fuses four independent doubt
signals over the ECINET-enriched roll.

For every voter in a revision year we look for FOUR kinds of problem and, if any
of them fires, the voter enters the report with a full, itemised explanation of
*which* problems fired and *how*:

  a. Logical discrepancy — six deterministic family/identity checks:
       1. progeny_overload    — a parent/ancestor reference with >= 6 progeny
       2. father_name_conflict — one household reference lists >= 2 father names
       3. parent_age_under_15  — a resolved parent < 15 years older than the child
       4. parent_age_over_50   — a resolved parent > 50 years older than the child
       5. grandparent_age_le_40 — a resolved grandparent <= 40 years older
       6. age_dob_gap          — |roll age - age from verified DOB| > 5 years
  b. No mapping in categories — the ECINET category_type is 'na' (the voter
     could not be mapped to a self or progeny SIR entry).
  c. cosine_new duplicate — the voter is on either side of a cosine_new flag.
  d. fuzzy_new duplicate  — the voter is on either side of a fuzzy_new flag.

The voter name shown is always the ECINET **verified_name** (roll name only as a
fallback). A voter is included when AT LEAST ONE signal fires.

Ordering (priority, highest first): voters with a cosine/fuzzy duplicate come
first (an actual duplicate lead is the strongest signal), then voters with only
a logical discrepancy, then voters with only 'no mapping'. Any logical/no-mapping
voter that ALSO turns out to have a fuzzy/cosine match is promoted into the top
tier automatically — that is the soft cross-link the brief asks for (found via
the existing models; if nothing matches, no problem).

Everything is computed from a single indexed scan of the year's voters plus one
read of the fuzzy_new/cosine_new flags — no `id = ANY(<huge array>)` (a planner
cliff on this database, see the deploy notes).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from dbx import connect, norm_name
from fraud_rules import (_fam_name_clusters, _nm1_is_placeholder_dob, _nm1_name,
                         get_photos)

# ---------------------------------------------------------------- thresholds
PROGENY_MAX          = 6      # a reference with >= this many progeny is flagged
PARENT_MIN_GAP       = 15     # a parent < this many years older -> impossible
PARENT_MAX_GAP       = 50     # a parent > this many years older -> suspicious
GRANDPARENT_MAX_GAP  = 40     # a grandparent <= this many years older -> impossible
DOB_AGE_GAP_MAX      = 5      # roll age vs DOB-age may differ by at most this
NO_MAPPING_CATEGORY  = "na"   # ECINET category_type meaning 'not mapped'

_PARENT_CODES = {"FTHR", "MTHR", "FATHER", "MOTHER", "F", "M"}
_GRANDPARENT_CODES = {"GFTH", "GMTH"}

_SEV_RANK = {"low": 1, "medium": 2, "high": 3}

# The columns the combined model needs — verified (ECINET) fields first, roll
# fields for identity + the reference graph.
_COLS = (
    "id, epic_no, name, verified_name, gender, age, verified_age, verified_dob, "
    "category_type, relation_type_code, relation_epic, relation_name_verified, "
    "father_or_guardian_name, mother_name, spouse_name, house_number, house_norm, "
    "constituency_no, constituency_name, part_no, serial_no, mobile_no, "
    "epic_lookup_status"
)


# ---------------------------------------------------------------- small helpers
def voter_name(v: dict) -> str:
    """ECINET verified name if present, else the roll (OCR) name."""
    return _nm1_name(v)[0] or (v.get("name") or "")


def _real_dob(s) -> "date | None":
    """Parse a verified DOB, rejecting the YYYY-01-01 birth-year placeholder."""
    s = (s or "").strip()
    if not s or _nm1_is_placeholder_dob(s):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _age_on(dob: date, ref: date) -> int:
    """Whole years old on `ref` — the conventional roll-age computation."""
    return ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))


def _age(v: dict | None):
    """Best available age: verified age (erollAge) then roll age."""
    if not v:
        return None
    a = v.get("verified_age")
    a = a if a is not None else v.get("age")
    try:
        return int(a)
    except (TypeError, ValueError):
        return None


def _sev_max(*sevs) -> str:
    best = 0
    for s in sevs:
        best = max(best, _SEV_RANK.get(s, 0))
    for k, r in _SEV_RANK.items():
        if r == best:
            return k
    return "low"


def _ac_key(v: dict) -> str:
    return (v.get("constituency_no") or "").strip() or "(unknown)"


# ---------------------------------------------------------------- logical checks
def _logical_findings(v: dict, by_epic: dict, relref: dict,
                      relref_progeny: dict, relref_fathers: dict,
                      year: int) -> list[dict]:
    """Every logical discrepancy that fires for one voter, each as a dict with a
    machine `check`, a `severity`, structured fields, and a human `how`."""
    out: list[dict] = []
    rel = (v.get("relation_epic") or "").strip()
    rtype = (v.get("relation_type_code") or "").strip().upper()

    # ---- 1. progeny_overload: the reference person has >= 6 progeny ----------
    if rel:
        n = relref_progeny.get(rel, 0)
        if n >= PROGENY_MAX:
            sibs = relref.get(rel, [])
            names = [f"{voter_name(s)} ({s.get('epic_no') or 'no EPIC'})"
                     for s in sibs if s.get("id") != v.get("id")]
            out.append({
                "check": "progeny_overload",
                "severity": "high" if n >= 10 else "medium",
                "count": n, "reference_id": rel,
                "reference_name": v.get("relation_name_verified"),
                "siblings": names,
                "how": (f"This voter is registered as progeny of reference "
                        f"'{v.get('relation_name_verified') or rel}' "
                        f"({rel}), which has {n} registered progeny "
                        f"(>= {PROGENY_MAX}). An unusually large single-parent "
                        f"progeny set is a roll-stuffing lead."),
            })

        # ---- 2. father_name_conflict: >= 2 distinct father names on this ref -
        fathers = relref_fathers.get(rel, [])
        if len(fathers) >= 2:
            mine = norm_name(v.get("father_or_guardian_name"))
            out.append({
                "check": "father_name_conflict",
                "severity": "low",
                "father_names": fathers, "reference_id": rel,
                "this_father": v.get("father_or_guardian_name"),
                "how": (f"Voters sharing household reference {rel} list "
                        f"{len(fathers)} different father/guardian names "
                        f"({'; '.join(fathers)}). This voter's father is "
                        f"'{mine or '—'}'. Often a multi-generation household, "
                        f"but a mismatch worth confirming."),
            })

    # ---- 3 & 4. parent age (only when the reference resolves to a voter) -----
    parent = by_epic.get(rel) if rel else None
    if parent is not None and rtype in _PARENT_CODES:
        pa, ca = _age(parent), _age(v)
        if pa is not None and ca is not None:
            gap = pa - ca
            if gap < PARENT_MIN_GAP:
                out.append({
                    "check": "parent_age_under_15", "severity": "high",
                    "parent_name": voter_name(parent), "parent_epic": parent.get("epic_no"),
                    "parent_age": pa, "child_age": ca, "gap": gap,
                    "how": (f"Listed parent '{voter_name(parent)}' "
                            f"({parent.get('epic_no') or 'no EPIC'}) is age {pa}; "
                            f"this voter is age {ca} — only {gap} years apart "
                            f"(< {PARENT_MIN_GAP}). Impossible parent/child ages."),
                })
            elif gap > PARENT_MAX_GAP:
                out.append({
                    "check": "parent_age_over_50", "severity": "medium",
                    "parent_name": voter_name(parent), "parent_epic": parent.get("epic_no"),
                    "parent_age": pa, "child_age": ca, "gap": gap,
                    "how": (f"Listed parent '{voter_name(parent)}' "
                            f"({parent.get('epic_no') or 'no EPIC'}) is age {pa}; "
                            f"this voter is age {ca} — {gap} years apart "
                            f"(> {PARENT_MAX_GAP}). Suspicious parent/child gap."),
                })

    # ---- 5. grandparent age <= 40 -------------------------------------------
    gp = None
    gp_path = ""
    if rtype in _GRANDPARENT_CODES and parent is not None:
        # direct grandparent link (relation_type_code GFTH/GMTH)
        gp, gp_path = parent, "direct grandparent link"
    elif rtype in _PARENT_CODES and parent is not None:
        # two-hop: voter -> parent -> parent's reference (grandparent)
        prel = (parent.get("relation_epic") or "").strip()
        cand = by_epic.get(prel) if prel else None
        if cand is not None:
            gp, gp_path = cand, "via resolved parent"
    if gp is not None:
        ga, ca = _age(gp), _age(v)
        if ga is not None and ca is not None:
            gap = ga - ca
            if gap <= GRANDPARENT_MAX_GAP:
                out.append({
                    "check": "grandparent_age_le_40", "severity": "high",
                    "grandparent_name": voter_name(gp),
                    "grandparent_epic": gp.get("epic_no"),
                    "grandparent_age": ga, "child_age": ca, "gap": gap,
                    "path": gp_path,
                    "how": (f"Listed grandparent '{voter_name(gp)}' "
                            f"({gp.get('epic_no') or 'no EPIC'}) is age {ga}; "
                            f"this voter is age {ca} — only {gap} years apart "
                            f"(<= {GRANDPARENT_MAX_GAP}, {gp_path}). Impossible "
                            f"across two generations."),
                })

    # ---- 6. roll age vs age computed from the verified DOB -------------------
    dob = _real_dob(v.get("verified_dob"))
    roll_age = v.get("age")
    if dob is not None and roll_age is not None:
        dob_age = _age_on(dob, date(int(year), 1, 1))
        gap = abs(int(roll_age) - dob_age)
        if gap > DOB_AGE_GAP_MAX:
            sev = "high" if gap > 10 else "medium" if gap > 5 else "low"
            out.append({
                "check": "age_dob_gap", "severity": sev,
                "roll_age": int(roll_age), "dob_age": dob_age,
                "verified_dob": dob.isoformat(), "gap": gap,
                "how": (f"Roll age is {int(roll_age)} but the verified DOB "
                        f"{dob.isoformat()} gives age {dob_age} on 01-Jan-{year} "
                        f"— a {gap}-year gap (> {DOB_AGE_GAP_MAX}). Age/DOB "
                        f"inconsistency."),
            })

    return out


# ---------------------------------------------------------------- duplicates
def _dup_map(c, year: int) -> dict:
    """voter_id -> {'cosine': [...], 'fuzzy': [...]} built from the existing
    cosine_new / fuzzy_new flags for the year. Both sides of every pair are
    recorded, so each voter carries all of its duplicate partners."""
    rows = c.execute(
        """SELECT f.id, f.rule, f.severity, f.score, f.voter_id,
                  f.related_voter_id, f.details
             FROM flags f JOIN voters va ON va.id = f.voter_id
            WHERE f.rule IN ('cosine_new', 'fuzzy_new') AND va.year = %s""",
        (year,)).fetchall()
    dm: dict[int, dict] = defaultdict(lambda: {"cosine": [], "fuzzy": []})
    for f in rows:
        key = "cosine" if f["rule"] == "cosine_new" else "fuzzy"
        d = f["details"] or {}
        base = {"rule": f["rule"], "model": key, "flag_id": f["id"],
                "score": f["score"], "severity": f["severity"],
                "comparison": d.get("comparison") or [], "reason": d.get("reason"),
                "metric": d.get("cosine", d.get("similarity"))}
        if f["related_voter_id"]:
            dm[f["voter_id"]][key].append({**base, "partner_id": f["related_voter_id"]})
            dm[f["related_voter_id"]][key].append({**base, "partner_id": f["voter_id"]})
    return dm


def _attach_partner(dups: list[dict], by_id: dict) -> list[dict]:
    """Fill each duplicate entry with its partner's identity from the loaded
    voter map (highest score first)."""
    out = []
    for e in sorted(dups, key=lambda x: (x.get("score") or 0), reverse=True):
        p = by_id.get(e["partner_id"])
        out.append({**e,
                    "partner": p,
                    "partner_epic": (p or {}).get("epic_no"),
                    "partner_name": voter_name(p) if p else None})
    return out


# ---------------------------------------------------------------- record build
def _make_record(v: dict, logical: list[dict], no_mapping: bool,
                 cosine: list[dict], fuzzy: list[dict]) -> dict:
    dup_sevs = [d["severity"] for d in cosine + fuzzy]
    log_sevs = [f["severity"] for f in logical]
    nm_sev = ["low"] if no_mapping else []
    severity = _sev_max(*dup_sevs, *log_sevs, *nm_sev) if (dup_sevs or log_sevs or nm_sev) else "low"

    has_dup = bool(cosine or fuzzy)
    tier = 0 if has_dup else (1 if logical else 2)
    tier_label = ("Duplicate lead (fuzzy/cosine)" if tier == 0
                  else "Logical discrepancy" if tier == 1 else "No category mapping")
    best_dup = max([d.get("metric") or d.get("score") or 0 for d in cosine + fuzzy],
                   default=0.0)

    parts = []
    if cosine:
        parts.append(f"cosine×{len(cosine)}")
    if fuzzy:
        parts.append(f"fuzzy×{len(fuzzy)}")
    if logical:
        parts.append("logical: " + ", ".join(sorted({f['check'] for f in logical})))
    if no_mapping:
        parts.append("no-mapping (na)")

    return {
        "voter_id": v["id"], "epic_no": v.get("epic_no"),
        "name": voter_name(v), "roll_name": v.get("name"),
        "constituency_no": v.get("constituency_no"),
        "constituency_name": v.get("constituency_name"),
        "part_no": v.get("part_no"), "serial_no": v.get("serial_no"),
        "house_number": v.get("house_number"), "age": v.get("age"),
        "gender": v.get("gender"), "voter": v,
        "logical": logical, "no_mapping": no_mapping,
        "cosine": cosine, "fuzzy": fuzzy,
        "severity": severity, "tier": tier, "tier_label": tier_label,
        "best_dup": round(best_dup, 3), "n_dups": len(cosine) + len(fuzzy),
        "signals_summary": " · ".join(parts),
    }


def _priority_key(rec: dict):
    """Sort ascending: tier (dup < logical < no-map), then strongest first."""
    log_rank = max([_SEV_RANK.get(f["severity"], 0) for f in rec["logical"]],
                   default=0)
    return (rec["tier"], -rec["best_dup"], -rec["n_dups"], -log_rank,
            -len(rec["logical"]), rec.get("epic_no") or "")


# ---------------------------------------------------------------- public API
def build_combined(year: int, constituency: str | None = None) -> list[dict]:
    """The ordered Combined-Model report for one revision year (optionally one
    constituency). Each element is a per-voter record; see `_make_record`."""
    year = int(year)
    with connect() as c:
        rows = c.execute(f"SELECT {_COLS} FROM voters WHERE year = %s",
                         (year,)).fetchall()
        dupmap = _dup_map(c, year)

    by_id = {r["id"]: r for r in rows}
    by_epic: dict[str, dict] = {}
    for r in rows:
        e = (r.get("epic_no") or "").strip()
        if e and e not in by_epic:
            by_epic[e] = r

    # reference (relation_epic) groups: children pointing at the same reference
    relref: dict[str, list] = defaultdict(list)
    for r in rows:
        rel = (r.get("relation_epic") or "").strip()
        if rel:
            relref[rel].append(r)
    relref_progeny = {
        rel: len({(x.get("epic_no") or "").strip() for x in g
                  if (x.get("epic_no") or "").strip()})
        for rel, g in relref.items()}
    relref_fathers = {
        rel: _fam_name_clusters(sorted({norm_name(x.get("father_or_guardian_name"))
                                        for x in g
                                        if norm_name(x.get("father_or_guardian_name"))}))
        for rel, g in relref.items()}

    records: list[dict] = []
    for r in rows:
        if constituency and _ac_key(r) != constituency:
            continue
        logical = _logical_findings(r, by_epic, relref, relref_progeny,
                                    relref_fathers, year)
        no_mapping = ((r.get("category_type") or "").strip().lower()
                      == NO_MAPPING_CATEGORY)
        d = dupmap.get(r["id"])
        cosine = _attach_partner(d["cosine"], by_id) if d else []
        fuzzy = _attach_partner(d["fuzzy"], by_id) if d else []
        if not (logical or no_mapping or cosine or fuzzy):
            continue
        records.append(_make_record(r, logical, no_mapping, cosine, fuzzy))

    records.sort(key=_priority_key)
    return records


def combined_summary(records: list[dict]) -> dict:
    """Headline counts for the UI: totals by tier, by signal, by severity."""
    s = {"total": len(records),
         "tier_dup": 0, "tier_logical": 0, "tier_nomap": 0,
         "with_cosine": 0, "with_fuzzy": 0, "with_logical": 0, "with_nomap": 0,
         "high": 0, "medium": 0, "low": 0,
         "by_check": defaultdict(int)}
    for r in records:
        s["tier_dup"] += r["tier"] == 0
        s["tier_logical"] += r["tier"] == 1
        s["tier_nomap"] += r["tier"] == 2
        s["with_cosine"] += bool(r["cosine"])
        s["with_fuzzy"] += bool(r["fuzzy"])
        s["with_logical"] += bool(r["logical"])
        s["with_nomap"] += bool(r["no_mapping"])
        s[r["severity"]] += 1
        for f in r["logical"]:
            s["by_check"][f["check"]] += 1
    s["by_check"] = dict(s["by_check"])
    return s


def constituencies_in(records: list[dict]) -> list[str]:
    """Distinct AC codes present in a report, ordered by how many records each
    contributes (largest first) — drives the per-AC export picker."""
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        counts[_ac_key(r["voter"])] += 1
    return [ac for ac, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ---------------------------------------------------------------- media loader
def documents_for_epics(epics) -> dict[str, list[dict]]:
    """{epic_no: [{doc_type, ext, image}, ...]} for a bounded set of EPICs —
    the ECINET photo + EF (sr_form) images used by the dossier PDF."""
    keys = [e for e in {*(e for e in epics if e)} if e]
    if not keys:
        return {}
    out: dict[str, list[dict]] = defaultdict(list)
    with connect() as c:
        rows = c.execute(
            "SELECT epic_no, doc_type, ext, image FROM epic_documents "
            "WHERE epic_no = ANY(%s) ORDER BY epic_no, doc_type", (keys,)).fetchall()
    for r in rows:
        out[r["epic_no"]].append(
            {"doc_type": r["doc_type"], "ext": r["ext"],
             "image": bytes(r["image"]) if r["image"] else None})
    return dict(out)


def all_rows_for_epics(epics) -> dict[str, list[dict]]:
    """Every stored voter row (all years) for a bounded set of EPICs — so the
    dossier can show the complete record, not just the flagged year."""
    keys = [e for e in {*(e for e in epics if e)} if e]
    if not keys:
        return {}
    out: dict[str, list[dict]] = defaultdict(list)
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM voters WHERE epic_no = ANY(%s) "
            "ORDER BY epic_no, year DESC, id", (keys,)).fetchall()
    for r in rows:
        out[r["epic_no"]].append(r)
    return dict(out)


# roll photos come from the photos table via fraud_rules.get_photos (voter_id).
roll_photos = get_photos
