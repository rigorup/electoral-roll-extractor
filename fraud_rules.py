"""Layer A: deterministic fraud-detection rules (SQL, no ML, high precision).

Every rule writes rows into `flags`. A flag is a LEAD for human review, never a
verdict — false positives here would strike legitimate voters off a roll.

Deliberate fairness note: rules that compare `relation_name` are gender-aware.
A woman's recorded relation legitimately changes (father -> husband) across
revisions and after marriage, so matching on it blindly over-flags women. Rules
below either exclude relation_name or require corroborating fields.
"""
from __future__ import annotations

from dbx import connect, init_schema

# Each rule: id -> (severity, human description, SQL inserting into flags)
RULES: dict[str, tuple[str, str, str]] = {

    # ---- exact duplicate EPIC: the same voter ID card number twice.
    "dup_epic": ("high", "Same EPIC number on more than one record", """
        INSERT INTO flags (rule, severity, score, voter_id, related_voter_id, details)
        SELECT 'dup_epic', 'high', 1.0, a.id, b.id,
               jsonb_build_object('epic', a.epic_no, 'name_a', a.name, 'name_b', b.name)
        FROM voters a JOIN voters b
          ON a.epic_no = b.epic_no AND a.id < b.id
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
        JOIN voters va ON va.id = pa.voter_id
        JOIN voters vb ON vb.id = pb.voter_id
        WHERE pa.phash IS NOT NULL AND va.epic_no <> vb.epic_no
        ON CONFLICT DO NOTHING;
    """),

    # ---- implausible household size (roll stuffing into one address).
    "house_overload": ("medium", "Unusually many electors registered to one house", """
        INSERT INTO flags (rule, severity, score, voter_id, details)
        SELECT 'house_overload', 'medium', 0.5, v.id,
               jsonb_build_object('house', v.house_number, 'occupants', h.n)
        FROM voters v
        JOIN (SELECT constituency_no, house_norm, count(*) n
              FROM voters WHERE house_norm <> ''
              GROUP BY 1,2 HAVING count(*) > 15) h
          ON h.constituency_no = v.constituency_no AND h.house_norm = v.house_norm
        ON CONFLICT DO NOTHING;
    """),

    # ---- age impossibilities / data integrity.
    "age_outlier": ("low", "Age below 18 or implausibly high", """
        INSERT INTO flags (rule, severity, score, voter_id, details)
        SELECT 'age_outlier', 'low', 0.4, id,
               jsonb_build_object('age', age, 'name', name)
        FROM voters
        WHERE age IS NOT NULL AND (age < 18 OR age > 105)
        ON CONFLICT DO NOTHING;
    """),

    # ---- malformed EPIC (3 letters + 7 digits is the standard form).
    "epic_malformed": ("low", "EPIC number does not match the expected format", """
        INSERT INTO flags (rule, severity, score, voter_id, details)
        SELECT 'epic_malformed', 'low', 0.3, id,
               jsonb_build_object('epic', epic_no, 'name', name)
        FROM voters
        WHERE epic_no <> '' AND epic_no !~ '^[A-Z]{3}[0-9]{7}$'
        ON CONFLICT DO NOTHING;
    """),
}


def run_rules(selected: list[str] | None = None) -> dict[str, int]:
    """Run rules and return {rule: new_flags_added}."""
    init_schema()
    names = selected or list(RULES)
    added: dict[str, int] = {}
    with connect() as c:
        for name in names:
            if name not in RULES:
                continue
            before = c.execute("SELECT count(*) n FROM flags WHERE rule=%s",
                               (name,)).fetchone()["n"]
            c.execute(RULES[name][2])
            after = c.execute("SELECT count(*) n FROM flags WHERE rule=%s",
                              (name,)).fetchone()["n"]
            added[name] = after - before
        c.commit()
    return added


def clear_flags() -> None:
    with connect() as c:
        c.execute("DELETE FROM flags")
        c.commit()


def flag_summary():
    with connect() as c:
        return c.execute("""
            SELECT f.rule, f.severity, count(*) AS flags,
                   count(r.id) AS reviewed
            FROM flags f LEFT JOIN reviews r ON r.flag_id = f.id
            GROUP BY 1,2 ORDER BY flags DESC
        """).fetchall()


def open_flags(rule: str | None = None, limit: int = 200):
    """Flags awaiting human review, most severe first."""
    q = """
        SELECT f.id, f.rule, f.severity, f.score, f.details,
               va.name AS name_a, va.epic_no AS epic_a, va.part_no AS part_a,
               va.house_number AS house_a, va.age AS age_a, va.gender AS gender_a,
               vb.name AS name_b, vb.epic_no AS epic_b, vb.part_no AS part_b,
               vb.house_number AS house_b, vb.age AS age_b, vb.gender AS gender_b,
               f.voter_id, f.related_voter_id
        FROM flags f
        JOIN voters va ON va.id = f.voter_id
        LEFT JOIN voters vb ON vb.id = f.related_voter_id
        LEFT JOIN reviews r ON r.flag_id = f.id
        WHERE r.id IS NULL
    """
    params: list = []
    if rule:
        q += " AND f.rule = %s"
        params.append(rule)
    q += """ ORDER BY CASE f.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                       ELSE 3 END, f.score DESC NULLS LAST, f.id
             LIMIT %s"""
    params.append(limit)
    with connect() as c:
        return c.execute(q, params).fetchall()


def record_review(flag_id: int, verdict: str, reviewer: str, notes: str = ""):
    with connect() as c:
        c.execute(
            """INSERT INTO reviews (flag_id, verdict, reviewer, notes)
               VALUES (%s,%s,%s,%s)""",
            (flag_id, verdict, reviewer, notes),
        )
        c.commit()


def get_photo(voter_id: int) -> bytes | None:
    with connect() as c:
        r = c.execute("SELECT image FROM photos WHERE voter_id=%s",
                      (voter_id,)).fetchone()
    return bytes(r["image"]) if r and r["image"] else None
