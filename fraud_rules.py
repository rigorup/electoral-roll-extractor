"""Layer A: deterministic fraud-detection rules (SQL, no ML, high precision).

Every rule writes rows into `flags`. A flag is a LEAD for human review, never a
verdict — false positives here would strike legitimate voters off a roll.

Deliberate fairness note: rules that compare `relation_name` are gender-aware.
A woman's recorded relation legitimately changes (father -> husband) across
revisions and after marriage, so matching on it blindly over-flags women. Rules
below either exclude relation_name or require corroborating fields.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Callable

from psycopg.types.json import Json

from dbx import connect, init_schema, norm_name, phonetic

# ---------------------------------------------------------------- similarity
# Two complementary record-linkage methods over ALL of a voter's data points
# (name + relation + house + age + gender). Both build a candidate set with the
# trigram index (cheap) and then re-score it; they differ in the similarity
# they apply, so they flag genuinely different pairs:
#
#   fuzzy_dup   pg_trgm similarity() — trigram SET overlap (Jaccard-like),
#               weighted across the fields, entirely in SQL.
#   cosine_dup  TF-weighted trigram VECTOR cosine on the concatenated profile,
#               computed in Python (frequency-aware, magnitude-normalised).
#
# Both BLOCK on the metaphone key (name_phonetic, indexed) to stay tractable at
# scale — a full trigram self-join over tens of thousands of voters does not
# finish in interactive time. Blocking only decides which pairs are *compared*;
# the score still weighs every data point (name, relation, house, age, gender)
# and can flag likely duplicates across different houses / parts / constituencies.
#
# Fairness note (see module docstring): name-based matching over-flags migrants
# and married women, so both stay 'medium' leads and surface every field that
# fed the score, for the reviewer to judge.
FUZZY_THRESHOLD = 0.70      # composite weighted score to raise a fuzzy_dup flag
COSINE_THRESHOLD = 0.80     # trigram-vector cosine to raise a cosine_dup flag
CAND_AGE_WINDOW = 8         # blocking: only compare voters within this many years


def _profile(name_norm: str, relation_norm: str, house_norm: str,
             age, gender: str) -> str:
    """The single string that represents one voter for cosine comparison —
    every data point we hold, joined so trigrams span field boundaries."""
    return " ".join(str(x) for x in (
        name_norm or "", relation_norm or "", house_norm or "",
        "" if age is None else age, gender or "")).strip()


def _trigrams(s: str) -> "Counter[str]":
    s = f"  {s} "                      # pad so start/end characters count
    if len(s) < 3:
        return Counter([s])
    return Counter(s[i:i + 3] for i in range(len(s) - 2))


def _cosine(a: "Counter[str]", b: "Counter[str]") -> float:
    if not a or not b:
        return 0.0
    dot = sum(cnt * b.get(tri, 0) for tri, cnt in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _run_cosine(c, year: int) -> None:
    """Callable rule: TF-weighted trigram-vector cosine over the full voter
    profile. Candidates come from the trigram index; cosine re-scores them."""
    cand = c.execute(
        """
        SELECT a.id AS aid, b.id AS bid, a.name AS aname, b.name AS bname,
               a.name_norm AS an, b.name_norm AS bn,
               coalesce(a.relation_name_norm,'') AS ar,
               coalesce(b.relation_name_norm,'') AS br,
               a.house_norm AS ah, b.house_norm AS bh,
               a.age AS aa, b.age AS ba, a.gender AS ag, b.gender AS bg,
               a.epic_no AS ae, b.epic_no AS be
        FROM voters a JOIN voters b
          ON a.name_phonetic = b.name_phonetic
         AND a.id < b.id
         AND abs(coalesce(a.age,0) - coalesce(b.age,0)) <= %(win)s
         AND a.year = %(year)s AND b.year = %(year)s
        WHERE a.name_phonetic <> ''
        """,
        {"win": CAND_AGE_WINDOW, "year": year},
    ).fetchall()

    batch = []
    for r in cand:
        ta = _trigrams(_profile(r["an"], r["ar"], r["ah"], r["aa"], r["ag"]))
        tb = _trigrams(_profile(r["bn"], r["br"], r["bh"], r["ba"], r["bg"]))
        cos = _cosine(ta, tb)
        if cos >= COSINE_THRESHOLD:
            batch.append((
                round(cos, 3), r["aid"], r["bid"],
                Json({"method": "cosine", "cosine": round(cos, 3),
                      "name_a": r["aname"], "name_b": r["bname"],
                      "age_a": r["aa"], "age_b": r["ba"],
                      "gender_a": r["ag"], "gender_b": r["bg"],
                      "same_house": bool(r["ah"] and r["ah"] == r["bh"]),
                      "epic_a": r["ae"], "epic_b": r["be"]}),
            ))
    if batch:
        with c.cursor() as cur:          # executemany is a cursor method, not a
            cur.executemany(             # connection method, in psycopg 3
                """INSERT INTO flags (rule, severity, score, voter_id,
                                      related_voter_id, details)
                   VALUES ('cosine_dup', 'medium', %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                batch,
            )


# =====================================================================
# new_model_1 — probabilistic identity resolution over the VERIFIED record
# =====================================================================
# The single-signal rules above each look at one clue. new_model_1 is a
# record-linkage MODEL: for a candidate pair it sums independent evidence
# weights (log-evidence "points", the Fellegi–Sunter idea) across every data
# point held for a voter, and flags the pair only when the total clears a
# decision threshold. It differs from the other rules in three ways that matter
# for this data:
#
#   1. It matches on the ECINET **verified_name**, not the OCR'd roll text.
#      The two disagree on ~10% of enriched rows, so the roll name alone both
#      misses real duplicates (OCR garbled one copy) and invents false ones.
#   2. It is **quality-aware**. Two ECINET fields are dirty in characteristic
#      ways and are explicitly discounted:
#        • verified_dob defaults to `YYYY-01-01` when only the birth *year* is
#          known — a placeholder that collides across unrelated people, so an
#          01-Jan match scores far below a real-date match.
#        • one mobile_no is routinely shared by a whole family / CSC operator
#          (seen on up to 30 EPICs here), so a match on a high-multiplicity
#          number is near-worthless and is down-weighted to almost nothing.
#   3. It records **why**: every signal that fired — positive evidence and
#      counter-evidence — is stored on the flag, so a reviewer sees the case,
#      not just a number. Aadhaar reference agreement alone clears the bar;
#      weaker clues must corroborate; disagreements (different real DOB,
#      different gender, different Aadhaar) subtract so a mere phonetic name
#      clash between two clearly different people does not flag.
NM1_THRESHOLD        = 4.0   # total evidence points needed to raise a flag
NM1_HIGH             = 6.0   # >= this total -> 'high' severity
NM1_MOBILE_SHARE_CAP = 4     # a mobile on more EPICs than this is a shared phone
NM1_NAME_STRONG_SIM  = 0.85  # trigram-cosine over verified names: near-identical
NM1_NAME_WEAK_SIM    = 0.62  # …similar enough to be weak corroboration

# agreement weights (evidence points added when the fields AGREE)
_NM1_W = {
    "aadhaar":         6.0,   # verified Aadhaar reference — near-certain identity
    "dob_exact":       3.0,   # identical real birth date
    "dob_placeholder": 1.0,   # identical 01-Jan default (birth-year only)
    "dob_year":        0.4,   # birth year agrees, day differs / is a default
    "name_exact":      2.5,
    "name_strong":     1.6,
    "name_phonetic":   0.9,
    "name_weak":       0.5,
    "mobile":          1.8,   # same mobile, held by few people
    "mobile_shared":   0.3,   # same mobile, but it is a shared/booth number
    "relation_epic":   2.0,   # same parent/relation EPIC
    "parent_name":     0.6,   # father-or-guardian / mother name agrees
    "spouse_name":     0.6,
    "age_exact":       0.7,   # only used when DOB is absent on a side
    "age_close":       0.4,
    "house":           0.6,   # same normalised house in the same constituency
    "gender_same":     0.25,
}
# counter-evidence (points SUBTRACTED when the fields disagree)
_NM1_P = {
    "dob_conflict":     -3.0,  # two different real birth dates -> different people
    "aadhaar_conflict": -1.5,
    "gender_diff":      -1.2,
    "relation_conflict":-0.5,
    "age_far":          -0.8,
    "name_conflict":    -1.0,
}

# Human phrasing for the flag's `reason`, richest evidence first.
_NM1_PHRASE = {
    "aadhaar": "same Aadhaar reference (near-certain)",
    "dob_exact": "identical verified DOB",
    "relation_epic": "same parent/relation EPIC",
    "name_exact": "verified names identical",
    "mobile": "same non-shared mobile",
    "name_strong": "verified names near-identical",
    "dob_placeholder": "same default (01-Jan) DOB",
    "name_phonetic": "names sound identical",
    "father_name_match": "same father/guardian",
    "mother_name_match": "same mother",
    "spouse_name_match": "same spouse",
    "name_weak": "verified names similar",
    "age_exact": "identical age",
    "same_house": "same house & constituency",
    "dob_year": "same birth year",
    "age_close": "near-identical age",
    "mobile_shared": "same (shared) mobile",
    "gender_same": "same gender",
}
_NM1_COUNTER = {
    "dob_conflict": "different birth dates",
    "aadhaar_conflict": "different Aadhaar reference",
    "gender_diff": "different gender",
    "relation_conflict": "different parent EPIC",
    "age_far": "ages far apart",
    "name_conflict": "verified names differ",
}

# The columns new_model_1 scores on — verified fields first, roll fields as
# fallback / corroboration.
_NM1_COLS = (
    "id, epic_no, name, verified_name, gender, age, verified_age, "
    "verified_dob, mobile_no, aadhaar_ref_no, relation_epic, "
    "father_or_guardian_name, mother_name, spouse_name, "
    "house_norm, constituency_no"
)

# Blocking: build the candidate pairs cheaply from five indexed keys, union
# them, and score the union. Each key is a way two records of the SAME person
# can be found; scoring then decides. All comparisons stay WITHIN one year and
# require a DIFFERENT EPIC (same-EPIC duplicates are dup_epic's job).
_NM1_CANDIDATES_SQL = f"""
WITH shared_mobiles AS (
    SELECT mobile_no
      FROM voters
     WHERE year = %(year)s AND coalesce(mobile_no,'') <> ''
     GROUP BY mobile_no
    HAVING count(DISTINCT epic_no) > %(mobcap)s
),
pairs AS (
    -- B1: same Aadhaar reference
    SELECT a.id AS aid, b.id AS bid
      FROM voters a JOIN voters b
        ON a.aadhaar_ref_no = b.aadhaar_ref_no AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s
       AND coalesce(a.aadhaar_ref_no,'') <> ''
       AND a.epic_no IS DISTINCT FROM b.epic_no
    UNION
    -- B2: same real (non-placeholder) verified DOB
    SELECT a.id, b.id
      FROM voters a JOIN voters b
        ON a.verified_dob = b.verified_dob AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s
       AND coalesce(a.verified_dob,'') <> '' AND a.verified_dob NOT LIKE '%%-01-01'
       AND a.epic_no IS DISTINCT FROM b.epic_no
    UNION
    -- B3: same mobile, excluding shared/booth numbers
    SELECT a.id, b.id
      FROM voters a JOIN voters b
        ON a.mobile_no = b.mobile_no AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s
       AND coalesce(a.mobile_no,'') <> ''
       AND a.mobile_no NOT IN (SELECT mobile_no FROM shared_mobiles)
       AND a.epic_no IS DISTINCT FROM b.epic_no
    UNION
    -- B4: same parent/relation EPIC
    SELECT a.id, b.id
      FROM voters a JOIN voters b
        ON a.relation_epic = b.relation_epic AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s
       AND coalesce(a.relation_epic,'') <> ''
       AND a.epic_no IS DISTINCT FROM b.epic_no
    UNION
    -- B5: same name phonetic key, within an age window (catches OCR/spelling
    -- drift the exact keys above miss)
    SELECT a.id, b.id
      FROM voters a JOIN voters b
        ON a.name_phonetic = b.name_phonetic AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s
       AND a.name_phonetic <> ''
       AND abs(coalesce(a.age,0) - coalesce(b.age,0)) <= %(win)s
       AND a.epic_no IS DISTINCT FROM b.epic_no
)
SELECT DISTINCT aid, bid FROM pairs
"""


def _nm1_name(v: dict) -> tuple[str, str]:
    """The name to match on: the ECINET verified name if we have it, else the
    roll (OCR) name — plus which source it came from, for the flag."""
    vn = (v.get("verified_name") or "").strip()
    if vn:
        return vn, "ecinet_verified"
    return (v.get("name") or "").strip(), "roll_ocr"


def _nm1_is_placeholder_dob(d: str) -> bool:
    return bool(d) and d.strip().endswith("-01-01")


def _nm1_score(a: dict, b: dict, mobile_share: dict) -> tuple[float, list[dict]]:
    """Sum the evidence for 'a and b are the same person'. Returns the total
    points and the list of signals that fired (each a dict recording its own
    contribution) — that list is the flag's explanation."""
    sig: list[dict] = []
    pts = 0.0

    def add(name: str, points: float, **extra):
        nonlocal pts
        pts += points
        sig.append({"signal": name, "points": round(points, 2), **extra})

    # ---- name (verified preferred) -----------------------------------
    an, asrc = _nm1_name(a)
    bn, bsrc = _nm1_name(b)
    an_n, bn_n = norm_name(an), norm_name(bn)
    if an_n and bn_n:
        if an_n == bn_n:
            add("name_exact", _NM1_W["name_exact"], a=an, b=bn)
        else:
            sim = _cosine(_trigrams(an_n), _trigrams(bn_n))
            if sim >= NM1_NAME_STRONG_SIM:
                add("name_strong", _NM1_W["name_strong"], a=an, b=bn, sim=round(sim, 3))
            elif phonetic(an) and phonetic(an) == phonetic(bn):
                add("name_phonetic", _NM1_W["name_phonetic"], a=an, b=bn, sim=round(sim, 3))
            elif sim >= NM1_NAME_WEAK_SIM:
                add("name_weak", _NM1_W["name_weak"], a=an, b=bn, sim=round(sim, 3))
            else:
                add("name_conflict", _NM1_P["name_conflict"], a=an, b=bn, sim=round(sim, 3))

    # ---- date of birth (quality-aware) / age fallback ----------------
    da = (a.get("verified_dob") or "").strip()
    db_ = (b.get("verified_dob") or "").strip()
    if da and db_:
        if da == db_:
            if _nm1_is_placeholder_dob(da):
                add("dob_placeholder", _NM1_W["dob_placeholder"], value=da)
            else:
                add("dob_exact", _NM1_W["dob_exact"], value=da)
        else:
            ph = _nm1_is_placeholder_dob(da) or _nm1_is_placeholder_dob(db_)
            ya, yb = da[:4], db_[:4]
            if ph and ya == yb:
                add("dob_year", _NM1_W["dob_year"], a=da, b=db_)
            elif not ph:
                add("dob_conflict", _NM1_P["dob_conflict"], a=da, b=db_)
    else:  # no DOB on at least one side -> lean on age
        aa = a.get("verified_age") if a.get("verified_age") is not None else a.get("age")
        ba = b.get("verified_age") if b.get("verified_age") is not None else b.get("age")
        if aa is not None and ba is not None:
            diff = abs(int(aa) - int(ba))
            if diff == 0:
                add("age_exact", _NM1_W["age_exact"], a=aa, b=ba)
            elif diff <= 2:
                add("age_close", _NM1_W["age_close"], a=aa, b=ba)
            elif diff >= 8:
                add("age_far", _NM1_P["age_far"], a=aa, b=ba)

    # ---- Aadhaar reference (value never exposed; only agree/disagree) --
    aad_a = (a.get("aadhaar_ref_no") or "").strip()
    aad_b = (b.get("aadhaar_ref_no") or "").strip()
    if aad_a and aad_b:
        if aad_a == aad_b:
            add("aadhaar", _NM1_W["aadhaar"])
        else:
            add("aadhaar_conflict", _NM1_P["aadhaar_conflict"])

    # ---- mobile (shared numbers discounted; value not exposed) --------
    ma = (a.get("mobile_no") or "").strip()
    mb = (b.get("mobile_no") or "").strip()
    if ma and mb and ma == mb:
        n = mobile_share.get(ma, 1)
        if n > NM1_MOBILE_SHARE_CAP:
            add("mobile_shared", _NM1_W["mobile_shared"], shared_across_epics=n)
        else:
            add("mobile", _NM1_W["mobile"])

    # ---- parent/relation EPIC ----------------------------------------
    ra = (a.get("relation_epic") or "").strip()
    rb = (b.get("relation_epic") or "").strip()
    if ra and rb:
        if ra == rb:
            add("relation_epic", _NM1_W["relation_epic"], epic=ra)
        else:
            add("relation_conflict", _NM1_P["relation_conflict"])

    # ---- parent / spouse names ---------------------------------------
    for col, name, w in (("father_or_guardian_name", "father_name_match", _NM1_W["parent_name"]),
                         ("mother_name", "mother_name_match", _NM1_W["parent_name"]),
                         ("spouse_name", "spouse_name_match", _NM1_W["spouse_name"])):
        x, y = norm_name(a.get(col)), norm_name(b.get(col))
        if x and y and x == y:
            add(name, w, value=x)

    # ---- same house in same constituency -----------------------------
    ha, hb = (a.get("house_norm") or ""), (b.get("house_norm") or "")
    if ha and ha == hb and (a.get("constituency_no") or "") == (b.get("constituency_no") or ""):
        add("same_house", _NM1_W["house"], house=ha)

    # ---- gender ------------------------------------------------------
    ga = (a.get("gender") or "").strip().upper()[:1]
    gb = (b.get("gender") or "").strip().upper()[:1]
    if ga and gb:
        if ga == gb:
            add("gender_same", _NM1_W["gender_same"])
        else:
            add("gender_diff", _NM1_P["gender_diff"], a=ga, b=gb)

    return pts, sig


def _nm1_reason(sig: list[dict]) -> str:
    """A one-line, reviewer-facing explanation built from the signals."""
    pos = [s["signal"] for s in sig if s["points"] > 0]
    neg = [s["signal"] for s in sig if s["points"] < 0]
    pos.sort(key=lambda s: list(_NM1_PHRASE).index(s) if s in _NM1_PHRASE else 99)
    parts = [_NM1_PHRASE[s] for s in pos if s in _NM1_PHRASE]
    reason = "Same person suspected — " + "; ".join(parts) if parts else \
             "Weak duplicate signal"
    if neg:
        cnt = [_NM1_COUNTER[s] for s in neg if s in _NM1_COUNTER]
        if cnt:
            reason += ".  Counter-evidence: " + "; ".join(cnt)
    return reason + ".  (Different EPIC on each record.)"


def _run_new_model_1(c, year: int) -> None:
    """Callable rule: score every candidate pair with the identity model and
    flag those clearing NM1_THRESHOLD, recording the evidence on each flag."""
    share_rows = c.execute(
        """SELECT mobile_no, count(DISTINCT epic_no) AS n
             FROM voters
            WHERE year = %(year)s AND coalesce(mobile_no,'') <> ''
            GROUP BY mobile_no""",
        {"year": year},
    ).fetchall()
    mobile_share = {r["mobile_no"]: r["n"] for r in share_rows}

    pairs = c.execute(_NM1_CANDIDATES_SQL,
                      {"year": year, "mobcap": NM1_MOBILE_SHARE_CAP,
                       "win": CAND_AGE_WINDOW}).fetchall()
    if not pairs:
        return

    ids = {p["aid"] for p in pairs} | {p["bid"] for p in pairs}
    rows = c.execute(
        f"SELECT {_NM1_COLS} FROM voters WHERE id = ANY(%s)", (list(ids),)
    ).fetchall()
    by_id = {r["id"]: r for r in rows}

    batch = []
    for p in pairs:
        a, b = by_id.get(p["aid"]), by_id.get(p["bid"])
        if not a or not b:
            continue
        pts, sig = _nm1_score(a, b, mobile_share)
        if pts < NM1_THRESHOLD:
            continue
        an, asrc = _nm1_name(a)
        bn, bsrc = _nm1_name(b)
        sev = "high" if pts >= NM1_HIGH else "medium"
        # squash the unbounded point total into [0,1] for ordering; the raw
        # points and a rough match-probability live in details.
        norm_score = round(min(1.0, pts / 8.0), 3)
        conf = round(1.0 / (1.0 + 2.0 ** (-(pts - NM1_THRESHOLD))), 3)
        details = {
            "method": "new_model_1",
            "model": "probabilistic identity resolution (Fellegi-Sunter), "
                     "ECINET-verified",
            "score_points": round(pts, 2),
            "match_confidence": conf,
            "name_a": an, "name_b": bn,
            "name_source_a": asrc, "name_source_b": bsrc,
            "epic_a": a["epic_no"], "epic_b": b["epic_no"],
            "signals": sig,
            "reason": _nm1_reason(sig),
        }
        batch.append((sev, norm_score, a["id"], b["id"], Json(details)))

    if batch:
        with c.cursor() as cur:
            cur.executemany(
                """INSERT INTO flags (rule, severity, score, voter_id,
                                      related_voter_id, details)
                   VALUES ('new_model_1', %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                batch,
            )


# =====================================================================
# fuzzy_new — weighted fuzzy-similarity duplicate model with a side-by-side
#             attribute comparison (for the PDF compare view)
# =====================================================================
# Where new_model_1 sums exact-match evidence, fuzzy_new is a *similarity*
# model: every shared attribute is scored 0..1 with fuzzy (trigram) matching
# and folded into one weighted score. It is built for the roll+ECINET record
# together and deliberately brings in the PROGENY/relation data (the ECINET
# reference person: relation EPIC + relation name) so two entries that link to
# the same parent reinforce each other, plus the other demographics — verified
# DOB, father, mother, spouse, house, age, gender.
#
# The voter name comes from the ECINET **verified_name** (roll name only as a
# fallback). Every attribute it checks — matched OR not — is recorded on the
# flag as a side-by-side {attribute, a, b, similarity, status} row, so a
# reviewer (and the comparison PDF) can see exactly which fields agreed and
# which differed, next to each other.
FUZZY_NEW_THRESHOLD = 0.72   # weighted similarity needed to raise a flag
FUZZY_NEW_HIGH      = 0.88   # >= this -> 'high' severity
FUZZY_NEW_MIN_ATTRS = 3      # need at least this many comparable attributes

# Relative importance of each attribute in the weighted average. The average is
# taken only over attributes present on BOTH records, so missing enrichment
# degrades gracefully instead of dragging every score down.
_FN_WEIGHTS = {
    "name": 0.30, "dob": 0.16, "father": 0.12, "mother": 0.10,
    "progeny_epic": 0.10, "progeny_name": 0.08, "spouse": 0.06,
    "house": 0.04, "age": 0.02, "gender": 0.02,
}

_FN_COLS = (
    "id, epic_no, name, verified_name, gender, age, verified_age, "
    "verified_dob, relation_type, relation_name, relation_name_verified, "
    "relation_epic, father_or_guardian_name, mother_name, spouse_name, "
    "house_number, house_norm, constituency_no"
)

# Candidates: fuzzy-name block (phonetic + age window) UNION progeny block
# (same relation EPIC) UNION real-DOB block. Within one year, different EPIC.
_FN_CANDIDATES_SQL = """
WITH pairs AS (
    SELECT a.id AS aid, b.id AS bid
      FROM voters a JOIN voters b
        ON a.name_phonetic = b.name_phonetic AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s AND a.name_phonetic <> ''
       AND abs(coalesce(a.age,0) - coalesce(b.age,0)) <= %(win)s
       AND a.epic_no IS DISTINCT FROM b.epic_no
    UNION
    SELECT a.id, b.id
      FROM voters a JOIN voters b
        ON a.relation_epic = b.relation_epic AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s
       AND coalesce(a.relation_epic,'') <> ''
       AND a.epic_no IS DISTINCT FROM b.epic_no
    UNION
    SELECT a.id, b.id
      FROM voters a JOIN voters b
        ON a.verified_dob = b.verified_dob AND a.id < b.id
       AND a.year = %(year)s AND b.year = %(year)s
       AND coalesce(a.verified_dob,'') <> '' AND a.verified_dob NOT LIKE '%%-01-01'
       AND a.epic_no IS DISTINCT FROM b.epic_no
)
SELECT DISTINCT aid, bid FROM pairs
"""


def _fn_s0(v) -> str | None:
    s = (str(v).strip() if v is not None else "")
    return s or None


def _fn_sim(a, b) -> float | None:
    """Trigram-cosine similarity of two names, or None if either is missing."""
    an, bn = norm_name(a), norm_name(b)
    if not an or not bn:
        return None
    if an == bn:
        return 1.0
    return round(_cosine(_trigrams(an), _trigrams(bn)), 3)


def _fn_status(s: float | None) -> str:
    if s is None:
        return "—"           # not comparable (missing on a side)
    if s >= 0.995:
        return "exact"
    if s >= 0.85:
        return "strong"
    if s >= 0.62:
        return "partial"
    if s > 0:
        return "weak"
    return "differ"


def _fn_score(a: dict, b: dict) -> tuple[float, list[dict], int]:
    """Weighted fuzzy similarity over every comparable attribute. Returns the
    composite score, the full side-by-side comparison, and how many attributes
    were actually comparable."""
    comp: list[dict] = []
    wsum = ssum = 0.0
    n = 0

    def use(label: str, sim: float | None, weight: float, va, vb):
        nonlocal wsum, ssum, n
        comp.append({"attribute": label, "a": va, "b": vb,
                     "similarity": None if sim is None else round(sim, 3),
                     "weight": weight, "status": _fn_status(sim)})
        if sim is not None:
            wsum += weight
            ssum += weight * sim
            n += 1

    # ---- verified name (roll name only as fallback) ------------------
    an, _ = _nm1_name(a)
    bn, _ = _nm1_name(b)
    use("Verified name", _fn_sim(an, bn), _FN_WEIGHTS["name"], an or None, bn or None)

    # ---- date of birth (01-Jan placeholder discounted) ---------------
    da = (a.get("verified_dob") or "").strip()
    db_ = (b.get("verified_dob") or "").strip()
    dsim = None
    if da and db_:
        if da == db_:
            dsim = 0.6 if _nm1_is_placeholder_dob(da) else 1.0
        else:
            dsim = 0.5 if da[:4] == db_[:4] else 0.0
    use("Date of birth", dsim, _FN_WEIGHTS["dob"], da or None, db_ or None)

    # ---- parents / spouse --------------------------------------------
    use("Father / guardian",
        _fn_sim(a.get("father_or_guardian_name"), b.get("father_or_guardian_name")),
        _FN_WEIGHTS["father"], _fn_s0(a.get("father_or_guardian_name")),
        _fn_s0(b.get("father_or_guardian_name")))
    use("Mother", _fn_sim(a.get("mother_name"), b.get("mother_name")),
        _FN_WEIGHTS["mother"], _fn_s0(a.get("mother_name")), _fn_s0(b.get("mother_name")))
    use("Spouse", _fn_sim(a.get("spouse_name"), b.get("spouse_name")),
        _FN_WEIGHTS["spouse"], _fn_s0(a.get("spouse_name")), _fn_s0(b.get("spouse_name")))

    # ---- progeny / relation (ECINET reference person) ----------------
    ra = (a.get("relation_epic") or "").strip()
    rb = (b.get("relation_epic") or "").strip()
    pesim = (1.0 if ra == rb else 0.0) if (ra and rb) else None
    use("Progeny / relation EPIC", pesim, _FN_WEIGHTS["progeny_epic"],
        ra or None, rb or None)
    pna = a.get("relation_name_verified") or a.get("relation_name")
    pnb = b.get("relation_name_verified") or b.get("relation_name")
    use("Progeny / relation name", _fn_sim(pna, pnb), _FN_WEIGHTS["progeny_name"],
        _fn_s0(pna), _fn_s0(pnb))

    # ---- household ---------------------------------------------------
    ha, hb = (a.get("house_norm") or ""), (b.get("house_norm") or "")
    hsim = (1.0 if ha == hb else 0.0) if (ha and hb) else None
    use("House", hsim, _FN_WEIGHTS["house"], _fn_s0(a.get("house_number")),
        _fn_s0(b.get("house_number")))

    # ---- age (only when DOB isn't on both sides) ---------------------
    if not (da and db_):
        aa = a.get("verified_age") if a.get("verified_age") is not None else a.get("age")
        ba = b.get("verified_age") if b.get("verified_age") is not None else b.get("age")
        asim = (max(0.0, 1 - abs(int(aa) - int(ba)) / 10.0)
                if aa is not None and ba is not None else None)
        use("Age", asim, _FN_WEIGHTS["age"], aa, ba)

    # ---- gender ------------------------------------------------------
    ga = (a.get("gender") or "").strip().upper()[:1]
    gb = (b.get("gender") or "").strip().upper()[:1]
    gsim = (1.0 if ga == gb else 0.0) if (ga and gb) else None
    use("Gender", gsim, _FN_WEIGHTS["gender"], _fn_s0(a.get("gender")),
        _fn_s0(b.get("gender")))

    composite = (ssum / wsum) if wsum > 0 else 0.0
    return composite, comp, n


# A flag needs at least one identity ANCHOR — a signal strong enough to tie two
# records to the same person. Without one, a high score is just a coincidence of
# common name + village house + similar age (already covered by dup_identity /
# fuzzy_dup on the roll fields). The anchor is what makes fuzzy_new the
# verified-record + progeny model rather than a roll-field rehash. A DOB anchor
# must be a REAL date: the 01-Jan placeholder only scores 'partial', never here.
_FN_ANCHOR_EXACT = {"Date of birth", "Progeny / relation EPIC"}
_FN_ANCHOR_NAME = {"Father / guardian", "Mother", "Spouse"}


def _fn_has_anchor(comp: list[dict]) -> bool:
    for c in comp:
        if c["attribute"] in _FN_ANCHOR_EXACT and c["status"] == "exact":
            return True
        if c["attribute"] in _FN_ANCHOR_NAME and c["status"] in ("exact", "strong"):
            return True
    return False


def _fn_reason(comp: list[dict], composite: float) -> str:
    """One-line summary naming the attributes that agreed and those that
    differed — the same evidence the side-by-side table shows."""
    agree = [c for c in comp if c["status"] in ("exact", "strong")]
    agree.sort(key=lambda c: -c["weight"])
    parts = [f"{c['attribute'].lower()} {c['status']}" for c in agree]
    diff = [c["attribute"].lower() for c in comp if c["status"] == "differ"]
    r = f"Fuzzy match {composite:.0%} — " + ("; ".join(parts) if parts
                                             else "weak partial signals")
    if diff:
        r += ".  Differs on: " + "; ".join(diff)
    return r + ".  (Different EPIC on each record.)"


def _run_fuzzy_new(c, year: int) -> None:
    """Callable rule: score candidate pairs by weighted fuzzy similarity and
    flag those clearing FUZZY_NEW_THRESHOLD, storing the side-by-side compare."""
    pairs = c.execute(_FN_CANDIDATES_SQL,
                      {"year": year, "win": CAND_AGE_WINDOW}).fetchall()
    if not pairs:
        return
    # Load the year's voters once (a single indexed scan) rather than fetching
    # the involved ids with `id = ANY(<huge array>)` — that array form is a
    # planner cliff on this dataset and can run for minutes.
    rows = c.execute(f"SELECT {_FN_COLS} FROM voters WHERE year = %s",
                     (year,)).fetchall()
    by_id = {r["id"]: r for r in rows}

    batch = []
    for p in pairs:
        a, b = by_id.get(p["aid"]), by_id.get(p["bid"])
        if not a or not b:
            continue
        composite, comp, n = _fn_score(a, b)
        # need a comparable name and enough evidence to trust the score
        name_row = comp[0]
        if name_row["similarity"] is None or n < FUZZY_NEW_MIN_ATTRS:
            continue
        if composite < FUZZY_NEW_THRESHOLD:
            continue
        # robustness gate: a high score on thin evidence (common name + same
        # house + similar age, no verified corroboration) is a coincidence, not
        # a duplicate — require a real identity anchor.
        if not _fn_has_anchor(comp):
            continue
        an, asrc = _nm1_name(a)
        bn, bsrc = _nm1_name(b)
        sev = "high" if composite >= FUZZY_NEW_HIGH else "medium"
        details = {
            "method": "fuzzy_new",
            "model": "weighted fuzzy similarity (roll + ECINET + progeny)",
            "similarity": round(composite, 3),
            "attributes_compared": n,
            "name_a": an, "name_b": bn,
            "name_source_a": asrc, "name_source_b": bsrc,
            "epic_a": a["epic_no"], "epic_b": b["epic_no"],
            "comparison": comp,          # side-by-side, for the compare PDF
            "reason": _fn_reason(comp, composite),
        }
        batch.append((sev, round(composite, 3), a["id"], b["id"], Json(details)))

    if batch:
        with c.cursor() as cur:
            cur.executemany(
                """INSERT INTO flags (rule, severity, score, voter_id,
                                      related_voter_id, details)
                   VALUES ('fuzzy_new', %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                batch,
            )


# =====================================================================
# cosine_new — TF-weighted trigram-vector cosine over the verified record,
#              category and demographics, with a side-by-side comparison
# =====================================================================
# Where fuzzy_new averages per-attribute similarities, cosine_new is the cosine
# counterpart (the cosine_dup idea, extended): it builds ONE TF-weighted
# trigram VECTOR per voter from all their fields — verified name, verified DOB,
# father, mother, spouse, the ECINET CATEGORY (self / progeny / na) and relation
# type, progeny/relation EPIC, house, gender — and scores a pair by the cosine
# between the two vectors. Cosine is frequency-aware and magnitude-normalised,
# so it ranks pairs differently from the fuzzy average.
#
# The voter name comes from the ECINET verified_name (roll name as fallback).
# Category is a light corroborator (only 3 values), never an anchor. As with
# fuzzy_new, a flag needs a real identity anchor (real DOB / progeny-EPIC /
# parent-name match) and stores a side-by-side {attribute, a, b, similarity,
# status} comparison of every field checked, for the comparison PDF.
COSINE_NEW_THRESHOLD  = 0.80  # combined-profile cosine needed to raise a flag
COSINE_NEW_HIGH       = 0.90  # >= this -> 'high' severity
COSINE_NEW_MIN_ATTRS  = 3
# A duplicate is the SAME person, so the verified names must actually match.
# Without this floor the combined-vector cosine stays high whenever the other
# demographics agree, flagging siblings / same-DOB strangers whose names differ.
COSINE_NEW_NAME_FLOOR = 0.85

# Field weights for the combined profile vector (each field's trigram counts are
# scaled by its weight before summing). Name and DOB dominate.
_CN_VEC_W = {
    "name": 3.0, "dob": 2.0, "father": 1.5, "mother": 1.2, "spouse": 0.8,
    "progeny_epic": 0.6, "house": 0.5, "category": 0.5, "relation_type": 0.4,
    "gender": 0.3,
}
_CN_COLS = (
    "id, epic_no, name, verified_name, gender, age, verified_age, "
    "verified_dob, relation_type, relation_name, relation_name_verified, "
    "relation_epic, relation_type_code, category_type, "
    "father_or_guardian_name, mother_name, spouse_name, "
    "house_number, house_norm, constituency_no"
)


def _cn_fields(v: dict) -> dict:
    """The per-field normalised strings that make up a voter's profile."""
    an, _ = _nm1_name(v)
    return {
        "name": norm_name(an),
        "dob": (v.get("verified_dob") or "").strip(),
        "father": norm_name(v.get("father_or_guardian_name")),
        "mother": norm_name(v.get("mother_name")),
        "spouse": norm_name(v.get("spouse_name")),
        "progeny_epic": (v.get("relation_epic") or "").strip().upper(),
        "house": v.get("house_norm") or "",
        "category": (v.get("category_type") or "").strip().upper(),
        "relation_type": (v.get("relation_type_code") or "").strip().upper(),
        "gender": (v.get("gender") or "").strip().upper()[:1],
    }


def _cn_vec(fields: dict) -> "Counter[str]":
    """Weighted sum of the fields' trigram counters. Each field is name-spaced
    (`field:value`) so trigrams from different fields never collide — the cosine
    then measures agreement field-by-field, weighted."""
    vec: "Counter[str]" = Counter()
    for f, w in _CN_VEC_W.items():
        s = fields.get(f) or ""
        if not s:
            continue
        for tri, cnt in _trigrams(f"{f}:{s}").items():
            vec[tri] += cnt * w
    return vec


def _cn_score(a: dict, b: dict, va: "Counter[str]", vb: "Counter[str]"
              ) -> tuple[float, list[dict], int]:
    """Cosine of the two (pre-built) profile vectors, plus the per-attribute
    side-by-side comparison and the count of comparable attributes. Vectors are
    passed in so the caller can build each voter's vector once, not per pair."""
    composite = _cosine(va, vb)
    comp: list[dict] = []
    n = 0

    def use(label: str, sim: float | None, va, vb):
        nonlocal n
        comp.append({"attribute": label, "a": va, "b": vb,
                     "similarity": None if sim is None else round(sim, 3),
                     "status": _fn_status(sim)})
        if sim is not None:
            n += 1

    an, _ = _nm1_name(a)
    bn, _ = _nm1_name(b)
    use("Verified name", _fn_sim(an, bn), an or None, bn or None)

    da = (a.get("verified_dob") or "").strip()
    db_ = (b.get("verified_dob") or "").strip()
    dsim = None
    if da and db_:
        dsim = ((0.6 if _nm1_is_placeholder_dob(da) else 1.0) if da == db_
                else (0.5 if da[:4] == db_[:4] else 0.0))
    use("Date of birth", dsim, da or None, db_ or None)

    use("Father / guardian",
        _fn_sim(a.get("father_or_guardian_name"), b.get("father_or_guardian_name")),
        _fn_s0(a.get("father_or_guardian_name")), _fn_s0(b.get("father_or_guardian_name")))
    use("Mother", _fn_sim(a.get("mother_name"), b.get("mother_name")),
        _fn_s0(a.get("mother_name")), _fn_s0(b.get("mother_name")))
    use("Spouse", _fn_sim(a.get("spouse_name"), b.get("spouse_name")),
        _fn_s0(a.get("spouse_name")), _fn_s0(b.get("spouse_name")))

    # ---- category / relation type (the requested "category" connection) ----
    ca = (a.get("category_type") or "").strip()
    cb = (b.get("category_type") or "").strip()
    csim = (1.0 if ca.upper() == cb.upper() else 0.0) if (ca and cb) else None
    use("Category", csim, ca or None, cb or None)
    rta = (a.get("relation_type_code") or "").strip()
    rtb = (b.get("relation_type_code") or "").strip()
    rtsim = (1.0 if rta.upper() == rtb.upper() else 0.0) if (rta and rtb) else None
    use("Relation type", rtsim, rta or None, rtb or None)

    # ---- progeny/relation EPIC (link + anchor) -----------------------
    pea = (a.get("relation_epic") or "").strip()
    peb = (b.get("relation_epic") or "").strip()
    pesim = (1.0 if pea == peb else 0.0) if (pea and peb) else None
    use("Progeny / relation EPIC", pesim, pea or None, peb or None)

    ha, hb = (a.get("house_norm") or ""), (b.get("house_norm") or "")
    hsim = (1.0 if ha == hb else 0.0) if (ha and hb) else None
    use("House", hsim, _fn_s0(a.get("house_number")), _fn_s0(b.get("house_number")))

    if not (da and db_):
        aa = a.get("verified_age") if a.get("verified_age") is not None else a.get("age")
        ba = b.get("verified_age") if b.get("verified_age") is not None else b.get("age")
        asim = (max(0.0, 1 - abs(int(aa) - int(ba)) / 10.0)
                if aa is not None and ba is not None else None)
        use("Age", asim, aa, ba)

    ga = (a.get("gender") or "").strip().upper()[:1]
    gb = (b.get("gender") or "").strip().upper()[:1]
    gsim = (1.0 if ga == gb else 0.0) if (ga and gb) else None
    use("Gender", gsim, _fn_s0(a.get("gender")), _fn_s0(b.get("gender")))

    return composite, comp, n


def _cn_reason(comp: list[dict], composite: float) -> str:
    agree = [c for c in comp if c["status"] in ("exact", "strong")]
    agree.sort(key=lambda c: -(c["similarity"] or 0))
    parts = [f"{c['attribute'].lower()} {c['status']}" for c in agree]
    diff = [c["attribute"].lower() for c in comp if c["status"] == "differ"]
    r = f"Cosine match {composite:.0%} — " + ("; ".join(parts) if parts
                                              else "weak partial signals")
    if diff:
        r += ".  Differs on: " + "; ".join(diff)
    return r + ".  (Different EPIC on each record.)"


def _run_cosine_new(c, year: int) -> None:
    """Callable rule: cosine over the combined profile vector; flag pairs that
    clear COSINE_NEW_THRESHOLD and carry an identity anchor."""
    pairs = c.execute(_FN_CANDIDATES_SQL,
                      {"year": year, "win": CAND_AGE_WINDOW}).fetchall()
    if not pairs:
        return
    rows = c.execute(f"SELECT {_CN_COLS} FROM voters WHERE year = %s",
                     (year,)).fetchall()
    by_id = {r["id"]: r for r in rows}
    # Build each voter's profile vector ONCE (not per candidate pair).
    vecs = {r["id"]: _cn_vec(_cn_fields(r)) for r in rows}

    batch = []
    for p in pairs:
        a, b = by_id.get(p["aid"]), by_id.get(p["bid"])
        if not a or not b:
            continue
        composite, comp, n = _cn_score(a, b, vecs[a["id"]], vecs[b["id"]])
        if comp[0]["similarity"] is None or n < COSINE_NEW_MIN_ATTRS:
            continue
        if composite < COSINE_NEW_THRESHOLD:
            continue
        # a duplicate must be the same person: the verified names must match
        if comp[0]["similarity"] < COSINE_NEW_NAME_FLOOR:
            continue
        if not _fn_has_anchor(comp):
            continue
        # two real, different birth dates => different people (siblings sharing
        # a surname + parents, not a duplicate). Reject the DOB conflict.
        if any(c["attribute"] == "Date of birth" and c["status"] == "differ"
               for c in comp):
            continue
        an, asrc = _nm1_name(a)
        bn, bsrc = _nm1_name(b)
        sev = "high" if composite >= COSINE_NEW_HIGH else "medium"
        details = {
            "method": "cosine_new",
            "model": "TF-weighted trigram-vector cosine (verified record + "
                     "category + demographics)",
            "cosine": round(composite, 3),
            "similarity": round(composite, 3),
            "attributes_compared": n,
            "name_a": an, "name_b": bn,
            "name_source_a": asrc, "name_source_b": bsrc,
            "epic_a": a["epic_no"], "epic_b": b["epic_no"],
            "comparison": comp,          # side-by-side, for the compare PDF
            "reason": _cn_reason(comp, composite),
        }
        batch.append((sev, round(composite, 3), a["id"], b["id"], Json(details)))

    if batch:
        with c.cursor() as cur:
            cur.executemany(
                """INSERT INTO flags (rule, severity, score, voter_id,
                                      related_voter_id, details)
                   VALUES ('cosine_new', %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                batch,
            )


# Each rule: id -> (severity, human description, SQL string OR callable(cursor)).
# A callable does its own INSERTs into `flags`; a string is executed as-is.
RULES: dict[str, tuple[str, str, str | Callable]] = {

    # ---- exact duplicate EPIC: the same voter ID card number twice.
    "dup_epic": ("high", "Same EPIC number on more than one record", """
        INSERT INTO flags (rule, severity, score, voter_id, related_voter_id, details)
        SELECT 'dup_epic', 'high', 1.0, a.id, b.id,
               jsonb_build_object('epic', a.epic_no, 'name_a', a.name, 'name_b', b.name)
        FROM voters a JOIN voters b
          ON a.epic_no = b.epic_no AND a.id < b.id
         AND a.year = %(year)s AND b.year = %(year)s
        WHERE a.epic_no <> ''
        ON CONFLICT DO NOTHING;
    """),

    # ---- same person, same household, same age: near-certain double entry.
    # Uses name + house + age. Relation name intentionally NOT required.
    "dup_identity": ("high", "Same name, same house, near-same age (different EPIC)", """
        INSERT INTO flags (rule, severity, score, voter_id, related_voter_id, details)
        SELECT 'dup_identity', 'high', 0.9, a.id, b.id,
               jsonb_build_object('name', a.name, 'house', a.house_number,
                                  'age_a', a.age, 'age_b', b.age,
                                  'epic_a', a.epic_no, 'epic_b', b.epic_no)
        FROM voters a JOIN voters b
          ON a.name_norm = b.name_norm
         AND a.house_norm = b.house_norm
         AND a.constituency_no = b.constituency_no
         AND abs(coalesce(a.age,0) - coalesce(b.age,0)) <= 1
         AND a.epic_no <> b.epic_no
         AND a.id < b.id
         AND a.year = %(year)s AND b.year = %(year)s
        WHERE a.name_norm <> '' AND a.house_norm <> ''
        ON CONFLICT DO NOTHING;
    """),

    # ---- same name+father+age in DIFFERENT parts: classic multi-booth entry.
    # Requires relation match here because cross-part needs corroboration; still
    # only a lead (common names + common father names do collide legitimately).
    "cross_part_dup": ("medium", "Same name + relation + age enrolled in another part", """
        INSERT INTO flags (rule, severity, score, voter_id, related_voter_id, details)
        SELECT 'cross_part_dup', 'medium', 0.7, a.id, b.id,
               jsonb_build_object('name', a.name, 'relation', a.relation_name,
                                  'part_a', a.part_no, 'part_b', b.part_no,
                                  'epic_a', a.epic_no, 'epic_b', b.epic_no)
        FROM voters a JOIN voters b
          ON a.name_norm = b.name_norm
         AND a.relation_name_norm = b.relation_name_norm
         AND abs(coalesce(a.age,0) - coalesce(b.age,0)) <= 1
         AND a.part_no <> b.part_no
         AND a.id < b.id
         AND a.year = %(year)s AND b.year = %(year)s
        WHERE a.name_norm <> '' AND a.relation_name_norm <> ''
        ON CONFLICT DO NOTHING;
    """),

    # ---- phonetic near-duplicate in the same house (BASFOR/BASPHOR/BUSFOR).
    "phonetic_dup": ("medium", "Phonetically identical name in same house, similar age", """
        INSERT INTO flags (rule, severity, score, voter_id, related_voter_id, details)
        SELECT 'phonetic_dup', 'medium', 0.6, a.id, b.id,
               jsonb_build_object('name_a', a.name, 'name_b', b.name,
                                  'house', a.house_number,
                                  'epic_a', a.epic_no, 'epic_b', b.epic_no)
        FROM voters a JOIN voters b
          ON a.name_phonetic = b.name_phonetic
         AND a.house_norm = b.house_norm
         AND a.constituency_no = b.constituency_no
         AND a.name_norm <> b.name_norm           -- spelt differently
         AND abs(coalesce(a.age,0) - coalesce(b.age,0)) <= 2
         AND a.id < b.id
         AND a.year = %(year)s AND b.year = %(year)s
        WHERE a.name_phonetic <> '' AND a.house_norm <> ''
        ON CONFLICT DO NOTHING;
    """),

    # ---- the same photograph under two identities: strongest single signal.
    "photo_reuse": ("high", "Identical photograph used on two different voters", """
        INSERT INTO flags (rule, severity, score, voter_id, related_voter_id, details)
        SELECT 'photo_reuse', 'high', 0.95, pa.voter_id, pb.voter_id,
               jsonb_build_object('phash', pa.phash,
                                  'name_a', va.name, 'name_b', vb.name,
                                  'epic_a', va.epic_no, 'epic_b', vb.epic_no)
        FROM photos pa
        JOIN photos pb ON pa.phash = pb.phash AND pa.voter_id < pb.voter_id
        JOIN voters va ON va.id = pa.voter_id AND va.year = %(year)s
        JOIN voters vb ON vb.id = pb.voter_id AND vb.year = %(year)s
        WHERE pa.phash IS NOT NULL AND va.epic_no <> vb.epic_no
        ON CONFLICT DO NOTHING;
    """),

    # ---- implausible household size (roll stuffing into one address).
    # ONE flag per house (voter_id = lowest id there as representative); the
    # review UI expands it into all occupants + a reconstructed family tree.
    "house_overload": ("medium", "Unusually many electors in one house (grouped per house, with family-tree analysis)", """
        INSERT INTO flags (rule, severity, score, voter_id, details)
        SELECT 'house_overload', 'medium',
               least(0.9, 0.5 + h.n / 100.0), h.rep_id,
               jsonb_build_object('house', h.house, 'house_norm', h.house_norm,
                                  'constituency_no', h.constituency_no,
                                  'occupants', h.n)
        FROM (SELECT constituency_no, house_norm,
                     min(id) AS rep_id, count(*) AS n,
                     min(house_number) AS house
              FROM voters WHERE house_norm <> '' AND year = %(year)s
              GROUP BY 1, 2 HAVING count(*) > 15) h
        ON CONFLICT DO NOTHING;
    """),

    # ---- age impossibilities / data integrity.
    "age_outlier": ("low", "Age below 18 or implausibly high", """
        INSERT INTO flags (rule, severity, score, voter_id, details)
        SELECT 'age_outlier', 'low', 0.4, id,
               jsonb_build_object('age', age, 'name', name)
        FROM voters
        WHERE age IS NOT NULL AND (age < 18 OR age > 105)
          AND year = %(year)s
        ON CONFLICT DO NOTHING;
    """),

    # ---- malformed EPIC (3 letters + 7 digits is the standard form).
    "epic_malformed": ("low", "EPIC number does not match the expected format", """
        INSERT INTO flags (rule, severity, score, voter_id, details)
        SELECT 'epic_malformed', 'low', 0.3, id,
               jsonb_build_object('epic', epic_no, 'name', name)
        FROM voters
        WHERE epic_no <> '' AND epic_no !~ '^[A-Z]{3}[0-9]{7}$'
          AND year = %(year)s
        ON CONFLICT DO NOTHING;
    """),

    # ---- FUZZY method: weighted trigram-similarity across every data point.
    # Trigram SET overlap (pg_trgm similarity(), Jaccard-like) on name + relation,
    # combined with same-house, age closeness and gender agreement. Catches
    # spelling drift / OCR noise that exact and phonetic rules miss.
    "fuzzy_dup": ("medium",
                  "Fuzzy method — weighted trigram similarity across name, "
                  "relation, house, age and gender", f"""
        INSERT INTO flags (rule, severity, score, voter_id, related_voter_id, details)
        SELECT 'fuzzy_dup', 'medium', p.score, p.aid, p.bid, p.details
        FROM (
            SELECT a.id AS aid, b.id AS bid,
                   ( 0.45 * similarity(a.name_norm, b.name_norm)
                   + 0.20 * similarity(coalesce(a.relation_name_norm,''),
                                       coalesce(b.relation_name_norm,''))
                   + 0.15 * (a.house_norm = b.house_norm AND a.house_norm <> '')::int
                   + 0.10 * greatest(0, 1 - abs(coalesce(a.age,0)
                                                - coalesce(b.age,0)) / 10.0)
                   + 0.10 * (a.gender = b.gender)::int )::real AS score,
                   jsonb_build_object(
                       'method', 'fuzzy',
                       'name_a', a.name, 'name_b', b.name,
                       'name_sim', round(similarity(a.name_norm, b.name_norm)::numeric, 3),
                       'relation_sim', round(similarity(
                           coalesce(a.relation_name_norm,''),
                           coalesce(b.relation_name_norm,''))::numeric, 3),
                       'same_house', (a.house_norm = b.house_norm AND a.house_norm <> ''),
                       'age_a', a.age, 'age_b', b.age,
                       'gender_a', a.gender, 'gender_b', b.gender,
                       'epic_a', a.epic_no, 'epic_b', b.epic_no) AS details
            FROM voters a JOIN voters b
              ON a.name_phonetic = b.name_phonetic
             AND a.id < b.id
             AND abs(coalesce(a.age,0) - coalesce(b.age,0)) <= {CAND_AGE_WINDOW}
             AND a.year = %(year)s AND b.year = %(year)s
            WHERE a.name_phonetic <> ''
        ) p
        WHERE p.score >= {FUZZY_THRESHOLD}
        ON CONFLICT DO NOTHING;
    """),

    # ---- COSINE method: TF-weighted trigram-vector cosine over the full
    # voter profile (name+relation+house+age+gender). Frequency-aware and
    # magnitude-normalised, so it ranks differently from the set-based fuzzy
    # rule. Implemented in Python (see _run_cosine).
    "cosine_dup": ("medium",
                   "Cosine method — TF-weighted trigram-vector cosine over the "
                   "full voter profile", _run_cosine),

    # ---- new_model_1: probabilistic identity resolution on the VERIFIED
    # (ECINET) record — weighs Aadhaar ref, verified DOB (01-Jan defaults
    # discounted), verified name, mobile (shared numbers discounted), parent
    # EPIC, parent/spouse names, house, age and gender into one score, and
    # records the per-signal evidence ('why') on every flag.
    "new_model_1": ("high",
                    "new_model_1 — probabilistic identity match over the "
                    "ECINET-verified record (Aadhaar / verified DOB / verified "
                    "name / mobile / parent-EPIC), quality-aware, with a "
                    "per-signal reason on each flag", _run_new_model_1),

    # ---- fuzzy_new: weighted fuzzy-similarity over the roll + ECINET record,
    # including progeny/relation links and the other demographics (verified
    # DOB, father, mother, spouse, house, age, gender). Uses the verified name,
    # and records a side-by-side per-attribute comparison on every flag for the
    # duplicate-comparison PDF.
    "fuzzy_new": ("medium",
                  "fuzzy_new — weighted fuzzy similarity over verified name + "
                  "DOB + father/mother/spouse + progeny (relation) + house/age/"
                  "gender, with a side-by-side attribute comparison on each "
                  "flag", _run_fuzzy_new),

    # ---- cosine_new: TF-weighted trigram-vector cosine over the verified
    # record + ECINET category + demographics (DOB, father, mother, spouse,
    # relation type, progeny EPIC, house, gender). Verified name; identity-
    # anchor gate; side-by-side per-attribute comparison for the compare PDF.
    "cosine_new": ("medium",
                   "cosine_new — TF-weighted trigram-vector cosine over verified "
                   "name + DOB + father/mother/spouse + category + progeny + "
                   "house/gender, with a side-by-side attribute comparison on "
                   "each flag", _run_cosine_new),
}


def run_rules(selected: list[str] | None = None,
              year: int | None = None) -> dict[str, int]:
    """Run rules over one revision year and return {rule: new_flags_added}.

    Every rule compares voters *within* `year` only: the same person legitimately
    reappears in the next year's roll, so cross-year comparison would flag the
    whole electorate."""
    init_schema()
    if year is None:
        raise ValueError("run_rules needs a year to scope the comparison")
    year = int(year)
    names = selected or list(RULES)
    added: dict[str, int] = {}
    with connect() as c:
        for name in names:
            if name not in RULES:
                continue
            before = c.execute("SELECT count(*) n FROM flags WHERE rule=%s",
                               (name,)).fetchone()["n"]
            spec = RULES[name][2]
            if callable(spec):
                spec(c, year)                # callable rule does its own INSERTs
            else:
                c.execute(spec, {"year": year})
            after = c.execute("SELECT count(*) n FROM flags WHERE rule=%s",
                              (name,)).fetchone()["n"]
            added[name] = after - before
        c.commit()
    return added


def clear_flags(year: int | None = None) -> None:
    """Drop flags. With a year, only that year's flags go (a flag belongs to
    the year of the voter it points at), so clearing 2026 leaves 2025 intact."""
    with connect() as c:
        if year is None:
            c.execute("DELETE FROM flags")
        else:
            c.execute(
                """DELETE FROM flags f USING voters va
                   WHERE va.id = f.voter_id AND va.year = %s""", (int(year),))
        c.commit()


def flag_summary(year: int | None = None):
    q = """
        SELECT f.rule, f.severity, count(*) AS flags,
               count(r.id) AS reviewed
        FROM flags f
        JOIN voters va ON va.id = f.voter_id
        LEFT JOIN reviews r ON r.flag_id = f.id
        WHERE TRUE
    """
    params: list = []
    if year is not None:
        q += " AND va.year = %s"
        params.append(int(year))
    q += " GROUP BY 1,2 ORDER BY flags DESC"
    with connect() as c:
        return c.execute(q, params).fetchall()


def flag_counts_by_constituency(year: int | None = None,
                                rule: str | None = None):
    """Total flags raised per constituency, most flags first.

    A flag is attributed to the constituency of voter A (the flag's primary
    voter). A cross-constituency pair therefore counts once, under A's AC —
    it is one lead, not two."""
    q = """
        SELECT coalesce(nullif(va.constituency_no, ''), '(unknown)')
                   AS constituency_no,
               max(va.constituency_name)                    AS constituency_name,
               count(DISTINCT f.id)                         AS flags,
               count(DISTINCT f.id) FILTER (WHERE f.severity = 'high')   AS high,
               count(DISTINCT f.id) FILTER (WHERE f.severity = 'medium') AS medium,
               count(DISTINCT f.id) FILTER (WHERE f.severity = 'low')    AS low,
               count(DISTINCT r.flag_id)                    AS reviewed
        FROM flags f
        JOIN voters va ON va.id = f.voter_id
        LEFT JOIN reviews r ON r.flag_id = f.id
        WHERE TRUE
    """
    params: list = []
    if year is not None:
        q += " AND va.year = %s"
        params.append(int(year))
    if rule:
        q += " AND f.rule = %s"
        params.append(rule)
    q += " GROUP BY 1 ORDER BY flags DESC"
    with connect() as c:
        return c.execute(q, params).fetchall()


def flag_counts_by_constituency_rule(year: int | None = None):
    """Flags per (constituency, rule) — the long form behind the
    model-by-constituency matrix. Same attribution as
    flag_counts_by_constituency: the flag counts under voter A's AC."""
    q = """
        SELECT coalesce(nullif(va.constituency_no, ''), '(unknown)')
                   AS constituency_no,
               max(va.constituency_name) AS constituency_name,
               f.rule                    AS rule,
               count(DISTINCT f.id)      AS flags
        FROM flags f
        JOIN voters va ON va.id = f.voter_id
        WHERE TRUE
    """
    params: list = []
    if year is not None:
        q += " AND va.year = %s"
        params.append(int(year))
    q += " GROUP BY 1, f.rule ORDER BY 1, f.rule"
    with connect() as c:
        return c.execute(q, params).fetchall()


def flag_entry_counts_by_constituency_rule(year: int | None = None):
    """Per (constituency, rule): how many flagged voter ENTRIES belong to that
    AC, counting BOTH sides of a pair in their own AC.

    flag_counts_by_constituency counts a pair once (under voter A). This counts
    the names instead: a pair contributes one entry to A's AC and one to B's,
    so a cross-AC duplicate shows up in both seats. Totals therefore come to
    2 x paired flags + 1 x single-voter flags.

    `entries` counts appearances (a voter in three pairs counts three times);
    `voters` counts how many distinct people are actually implicated."""
    side_a = """
        SELECT coalesce(nullif(va.constituency_no, ''), '(unknown)') AS ac,
               max(va.constituency_name) OVER (PARTITION BY va.constituency_no)
                   AS ac_name,
               f.rule AS rule, f.voter_id AS vid
        FROM flags f JOIN voters va ON va.id = f.voter_id
        WHERE %(year)s IS NULL OR va.year = %(year)s
    """
    side_b = """
        SELECT coalesce(nullif(vb.constituency_no, ''), '(unknown)') AS ac,
               max(vb.constituency_name) OVER (PARTITION BY vb.constituency_no)
                   AS ac_name,
               f.rule AS rule, f.related_voter_id AS vid
        FROM flags f JOIN voters vb ON vb.id = f.related_voter_id
        WHERE %(year)s IS NULL OR vb.year = %(year)s
    """
    q = f"""
        SELECT ac AS constituency_no, max(ac_name) AS constituency_name,
               rule, count(*) AS entries, count(DISTINCT vid) AS voters
        FROM ( {side_a} UNION ALL {side_b} ) t
        GROUP BY ac, rule ORDER BY ac, rule
    """
    with connect() as c:
        return c.execute(q, {"year": None if year is None else int(year)}
                         ).fetchall()


def flagged_constituencies(year: int | None = None,
                           rule: str | None = None) -> list[str]:
    """Constituency numbers that actually have flags — drives the per-AC
    download picker."""
    return [r["constituency_no"]
            for r in flag_counts_by_constituency(year, rule)]


def open_flags(rule: str | None = None, limit: int = 200,
               year: int | None = None):
    """Flags awaiting human review, most severe first."""
    q = """
        SELECT f.id, f.rule, f.severity, f.score, f.details,
               va.name AS name_a, va.epic_no AS epic_a, va.part_no AS part_a,
               va.house_number AS house_a, va.age AS age_a, va.gender AS gender_a,
               va.constituency_no AS const_a, va.serial_no AS serial_a,
               vb.name AS name_b, vb.epic_no AS epic_b, vb.part_no AS part_b,
               vb.house_number AS house_b, vb.age AS age_b, vb.gender AS gender_b,
               vb.constituency_no AS const_b, vb.serial_no AS serial_b,
               f.voter_id, f.related_voter_id
        FROM flags f
        JOIN voters va ON va.id = f.voter_id
        LEFT JOIN voters vb ON vb.id = f.related_voter_id
        LEFT JOIN reviews r ON r.flag_id = f.id
        WHERE r.id IS NULL
    """
    params: list = []
    if year is not None:
        q += " AND va.year = %s"
        params.append(int(year))
    if rule:
        q += " AND f.rule = %s"
        params.append(rule)
    q += """ ORDER BY CASE f.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                       ELSE 3 END, f.score DESC NULLS LAST, f.id
             LIMIT %s"""
    params.append(limit)
    with connect() as c:
        return c.execute(q, params).fetchall()


def house_members(constituency_no: str | None, house_norm: str,
                  year: int | None = None):
    """Every elector registered at one (constituency, normalised house)."""
    q = """SELECT id, name, name_norm, relation_type, relation_name,
                  relation_name_norm, age, gender, serial_no, part_no,
                  house_number, epic_no, constituency_no
           FROM voters
           WHERE coalesce(constituency_no,'') = coalesce(%s,'')
             AND house_norm = %s"""
    params: list = [constituency_no, house_norm]
    if year is not None:
        q += " AND year = %s"
        params.append(int(year))
    q += " ORDER BY part_no, serial_no NULLS LAST, id"
    with connect() as c:
        return c.execute(q, params).fetchall()


# Latest verdict wins when a flag was re-adjudicated.
_LATEST_REVIEW = """
    JOIN LATERAL (SELECT verdict, reviewer, notes, reviewed_at
                  FROM reviews WHERE flag_id = f.id
                  ORDER BY reviewed_at DESC, id DESC LIMIT 1) r ON TRUE
"""


def reviewed_flags(verdict: str | None = None, rule: str | None = None,
                   limit: int = 200, year: int | None = None):
    """Flags that already have a verdict, grouped confirmed -> legitimate ->
    needs_info (newest review first inside each group), so they can be
    revisited later."""
    q = f"""
        SELECT f.id, f.rule, f.severity, f.score, f.details,
               f.voter_id, f.related_voter_id,
               r.verdict, r.reviewer, r.notes, r.reviewed_at,
               va.name AS name_a, va.epic_no AS epic_a, va.part_no AS part_a,
               va.house_number AS house_a, va.age AS age_a, va.gender AS gender_a,
               va.constituency_no AS const_a, va.serial_no AS serial_a,
               vb.name AS name_b, vb.epic_no AS epic_b, vb.part_no AS part_b,
               vb.house_number AS house_b, vb.age AS age_b, vb.gender AS gender_b,
               vb.constituency_no AS const_b, vb.serial_no AS serial_b
        FROM flags f
        {_LATEST_REVIEW}
        JOIN voters va ON va.id = f.voter_id
        LEFT JOIN voters vb ON vb.id = f.related_voter_id
        WHERE TRUE
    """
    params: list = []
    if year is not None:
        q += " AND va.year = %s"
        params.append(int(year))
    if verdict:
        q += " AND r.verdict = %s"
        params.append(verdict)
    if rule:
        q += " AND f.rule = %s"
        params.append(rule)
    q += """ ORDER BY CASE r.verdict WHEN 'confirmed' THEN 1
                       WHEN 'legitimate' THEN 2 ELSE 3 END,
             r.reviewed_at DESC, f.id
             LIMIT %s"""
    params.append(limit)
    with connect() as c:
        return c.execute(q, params).fetchall()


def reviewed_summary(year: int | None = None):
    """{verdict: count} using each flag's latest verdict."""
    q = f"""
        SELECT r.verdict, count(*) AS n
        FROM flags f {_LATEST_REVIEW}
        JOIN voters va ON va.id = f.voter_id
        WHERE TRUE
    """
    params: list = []
    if year is not None:
        q += " AND va.year = %s"
        params.append(int(year))
    q += " GROUP BY 1"
    with connect() as c:
        rows = c.execute(q, params).fetchall()
    return {row["verdict"]: row["n"] for row in rows}


def reopen_flag(flag_id: int) -> None:
    """Wipe a flag's reviews so it returns to the open queue."""
    with connect() as c:
        c.execute("DELETE FROM reviews WHERE flag_id = %s", (flag_id,))
        c.commit()


def record_review(flag_id: int, verdict: str, reviewer: str, notes: str = ""):
    with connect() as c:
        c.execute(
            """INSERT INTO reviews (flag_id, verdict, reviewer, notes)
               VALUES (%s,%s,%s,%s)""",
            (flag_id, verdict, reviewer, notes),
        )
        c.commit()


# Every flag (open or reviewed), latest verdict if any -> used for the "download
# all flags" export so it mirrors exactly what the review UI shows per side.
_LATEST_REVIEW_LEFT = """
    LEFT JOIN LATERAL (SELECT verdict, reviewer, notes, reviewed_at
                       FROM reviews WHERE flag_id = f.id
                       ORDER BY reviewed_at DESC, id DESC LIMIT 1) r ON TRUE
"""


def all_flags_for_export(rule: str | None = None, year: int | None = None,
                         constituency: str | None = None):
    """Every flag, both sides' full voter details, and latest verdict (if
    reviewed) — most severe / most recent first. Feeds the PDF export.
    `constituency` restricts to flags attributed to that AC (voter A's)."""
    q = f"""
        SELECT f.id, f.rule, f.severity, f.score, f.details,
               f.voter_id, f.related_voter_id,
               r.verdict, r.reviewer, r.notes, r.reviewed_at,
               va.name AS name_a, va.epic_no AS epic_a, va.relation_type AS relation_type_a,
               va.relation_name AS relation_name_a, va.part_no AS part_a,
               va.house_number AS house_a, va.age AS age_a, va.gender AS gender_a,
               va.constituency_no AS const_a, va.serial_no AS serial_a,
               vb.name AS name_b, vb.epic_no AS epic_b, vb.relation_type AS relation_type_b,
               vb.relation_name AS relation_name_b, vb.part_no AS part_b,
               vb.house_number AS house_b, vb.age AS age_b, vb.gender AS gender_b,
               vb.constituency_no AS const_b, vb.serial_no AS serial_b
        FROM flags f
        {_LATEST_REVIEW_LEFT}
        JOIN voters va ON va.id = f.voter_id
        LEFT JOIN voters vb ON vb.id = f.related_voter_id
        WHERE TRUE
    """
    params: list = []
    if year is not None:
        q += " AND va.year = %s"
        params.append(int(year))
    if rule:
        q += " AND f.rule = %s"
        params.append(rule)
    if constituency:
        q += " AND coalesce(nullif(va.constituency_no, ''), '(unknown)') = %s"
        params.append(constituency)
    q += """ ORDER BY CASE f.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                       ELSE 3 END, f.score DESC NULLS LAST, f.id"""
    with connect() as c:
        return c.execute(q, params).fetchall()


def house_overload_members_for_export(rule: str | None = None,
                                      year: int | None = None,
                                      constituency: str | None = None):
    """One row per occupant for every open house_overload flag — mirrors the
    'All electors in this house' table in the review UI."""
    if rule not in (None, "house_overload"):
        return []
    fq = """SELECT f.id AS flag_id, f.details FROM flags f
            JOIN voters va ON va.id = f.voter_id
            WHERE f.rule = 'house_overload'"""
    fp: list = []
    if year is not None:
        fq += " AND va.year = %s"
        fp.append(int(year))
    if constituency:
        fq += " AND coalesce(nullif(va.constituency_no, ''), '(unknown)') = %s"
        fp.append(constituency)
    with connect() as c:
        flags = c.execute(fq, fp).fetchall()
        out = []
        for fl in flags:
            d = fl["details"] or {}
            mq = """SELECT id, name, relation_type, relation_name, age, gender,
                           serial_no, part_no, house_number, epic_no,
                           constituency_no
                    FROM voters
                    WHERE coalesce(constituency_no,'') = coalesce(%s,'')
                      AND house_norm = %s"""
            mp: list = [d.get("constituency_no"), d.get("house_norm")]
            if year is not None:
                mq += " AND year = %s"
                mp.append(int(year))
            mq += " ORDER BY part_no, serial_no NULLS LAST, id"
            members = c.execute(mq, mp).fetchall()
            for m in members:
                out.append({"flag_id": fl["flag_id"], "house": d.get("house"),
                           "constituency_no": d.get("constituency_no"), **m})
        return out


def get_photo(voter_id: int) -> bytes | None:
    with connect() as c:
        r = c.execute("SELECT image FROM photos WHERE voter_id=%s",
                      (voter_id,)).fetchone()
    return bytes(r["image"]) if r and r["image"] else None


def get_photos(voter_ids) -> dict[int, bytes]:
    """Batch photo lookup — one query for many voters (the PDF export needs
    thousands; one-by-one round trips would be far too slow)."""
    ids = [v for v in {*voter_ids} if v]
    if not ids:
        return {}
    with connect() as c:
        rows = c.execute(
            "SELECT voter_id, image FROM photos WHERE voter_id = ANY(%s)",
            (ids,),
        ).fetchall()
    return {r["voter_id"]: bytes(r["image"]) for r in rows if r["image"]}
