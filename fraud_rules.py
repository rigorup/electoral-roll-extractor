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

from dbx import connect, init_schema

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


def all_flags_for_export(rule: str | None = None, year: int | None = None):
    """Every flag, both sides' full voter details, and latest verdict (if
    reviewed) — most severe / most recent first. Feeds the Excel export."""
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
    q += """ ORDER BY CASE f.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                       ELSE 3 END, f.score DESC NULLS LAST, f.id"""
    with connect() as c:
        return c.execute(q, params).fetchall()


def house_overload_members_for_export(rule: str | None = None,
                                      year: int | None = None):
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
