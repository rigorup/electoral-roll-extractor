"""Read-only search/browse layer over the voters database.

Everything the Explore page needs: build a WHERE clause from a bag of filters,
count matches, page through them 100 at a time, and pull one voter's full
record with its photo and ECINET document images. All queries are parameterised
and ordering is whitelisted, so nothing here interpolates user input into SQL.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbx import connect, norm_name

PAGE_SIZE = 100

# Enrichment status choices shown in the UI. "Pending" means never looked up
# (NULL), which needs its own IS NULL branch rather than an = match.
STATUS_PENDING = "Pending"
STATUS_CHOICES = ["Found", "Not found", "Token expired", "Error", STATUS_PENDING]

# label -> ORDER BY fragment. Whitelisted: the UI can only pick a key here.
_PART_NUM = "NULLIF(regexp_replace(part_no, '\\D', '', 'g'), '')::int"
SORTS = {
    "Constituency · part · serial": f"constituency_no, {_PART_NUM} NULLS LAST, "
                                    "serial_no NULLS LAST",
    "Name (A–Z)": "name_norm, serial_no",
    "Age (low → high)": "age NULLS LAST, name_norm",
    "Age (high → low)": "age DESC NULLS LAST, name_norm",
    "Serial number": f"constituency_no, {_PART_NUM} NULLS LAST, serial_no",
    "Recently enriched": "epic_lookup_at DESC NULLS LAST, name_norm",
}

# Columns returned for the results table / gallery cards.
LIST_COLS = [
    "id", "year", "constituency_no", "part_no", "serial_no", "epic_no", "name",
    "relation_type", "relation_name", "house_number", "age", "gender",
    "epic_lookup_status", "verified_name", "verified_dob", "mobile_no",
    "father_or_guardian_name", "mother_name", "part_serial_no", "ac_name",
    "category_type", "epic_lookup_at",
]


@dataclass
class Filters:
    year: int | None = None
    acs: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    genders: list[str] = field(default_factory=list)
    relation_types: list[str] = field(default_factory=list)
    category_types: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    age_min: int | None = None
    age_max: int | None = None
    has_mobile: bool = False
    has_photo: bool = False
    query: str = ""


def _where(f: Filters) -> tuple[str, list]:
    """Turn a Filters into a parameterised WHERE clause + params list."""
    clauses: list[str] = []
    params: list = []

    if f.year is not None:
        clauses.append("year = %s")
        params.append(f.year)
    if f.acs:
        clauses.append("constituency_no = ANY(%s)")
        params.append(list(f.acs))
    if f.parts:
        clauses.append("part_no = ANY(%s)")
        params.append(list(f.parts))
    if f.genders:
        clauses.append("gender = ANY(%s)")
        params.append(list(f.genders))
    if f.relation_types:
        clauses.append("relation_type = ANY(%s)")
        params.append(list(f.relation_types))
    if f.category_types:
        clauses.append("category_type = ANY(%s)")
        params.append(list(f.category_types))

    if f.statuses:
        real = [s for s in f.statuses if s != STATUS_PENDING]
        ors = []
        if real:
            ors.append("epic_lookup_status = ANY(%s)")
            params.append(real)
        if STATUS_PENDING in f.statuses:
            ors.append("epic_lookup_status IS NULL")
        clauses.append("(" + " OR ".join(ors) + ")")

    if f.age_min is not None:
        clauses.append("age >= %s")
        params.append(f.age_min)
    if f.age_max is not None:
        clauses.append("age <= %s")
        params.append(f.age_max)
    if f.has_mobile:
        clauses.append("mobile_no IS NOT NULL AND mobile_no <> ''")
    if f.has_photo:
        clauses.append("EXISTS (SELECT 1 FROM photos p WHERE p.voter_id = v.id)")

    q = (f.query or "").strip()
    if q:
        like = f"%{q}%"
        nlike = f"%{norm_name(q)}%"
        # name_norm uses the gin_trgm index; the rest are ordinary ILIKEs.
        clauses.append(
            "(name_norm ILIKE %s OR name ILIKE %s OR epic_no ILIKE %s "
            "OR relation_name ILIKE %s OR house_number ILIKE %s "
            "OR mobile_no ILIKE %s OR verified_name ILIKE %s "
            "OR father_or_guardian_name ILIKE %s OR mother_name ILIKE %s)")
        params += [nlike, like, like, like, like, like, like, like, like]

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def count(f: Filters) -> int:
    where, params = _where(f)
    with connect() as c:
        return c.execute(f"SELECT count(*) n FROM voters v {where}",
                         params).fetchone()["n"]


def page_rows(f: Filters, *, sort: str = "Constituency · part · serial",
              page: int = 1, page_size: int = PAGE_SIZE) -> list[dict]:
    """Just the rows for one page (no COUNT) — cheap enough to run on every
    page turn. `page` is 1-based."""
    where, params = _where(f)
    order = SORTS.get(sort, next(iter(SORTS.values())))
    offset = (max(1, page) - 1) * page_size
    cols = ", ".join(LIST_COLS)
    with connect() as c:
        return c.execute(
            f"SELECT {cols} FROM voters v {where} "
            f"ORDER BY {order} LIMIT %s OFFSET %s",
            [*params, page_size, offset]).fetchall()


def search(f: Filters, *, sort: str = "Constituency · part · serial",
           page: int = 1, page_size: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """Return (rows, total). Convenience for callers that want both at once."""
    return page_rows(f, sort=sort, page=page, page_size=page_size), count(f)


def export_rows(f: Filters, *, sort: str, limit: int = 5000) -> list[dict]:
    """Rows for CSV export, bounded so a stray 'all voters' can't build a
    100k-row file in memory."""
    where, params = _where(f)
    order = SORTS.get(sort, next(iter(SORTS.values())))
    cols = ", ".join(LIST_COLS)
    with connect() as c:
        return c.execute(
            f"SELECT {cols} FROM voters v {where} ORDER BY {order} LIMIT %s",
            [*params, limit]).fetchall()


def voter_full(voter_id: int) -> dict | None:
    with connect() as c:
        return c.execute("SELECT * FROM voters WHERE id = %s",
                         (voter_id,)).fetchone()


def epic_documents(epic_no: str) -> list[dict]:
    """The stored ECINET document images for one EPIC (photo, sr_form)."""
    if not epic_no:
        return []
    with connect() as c:
        rows = c.execute(
            "SELECT doc_type, ext, image, length(image) AS bytes, fetched_at "
            "FROM epic_documents WHERE epic_no = %s ORDER BY doc_type",
            (epic_no,)).fetchall()
    for r in rows:
        r["image"] = bytes(r["image"]) if r["image"] else None
    return rows


def filter_options(year: int | None = None) -> dict:
    """Distinct values present in the data, for populating the filter widgets.
    Scoped to `year` when given so the choices match what's actually there."""
    year_clause = "year = %s AND " if year is not None else ""
    year_param = [year] if year is not None else []
    out: dict[str, list] = {}
    with connect() as c:
        for key, col in (("acs", "constituency_no"), ("genders", "gender"),
                         ("relation_types", "relation_type"),
                         ("category_types", "category_type")):
            rows = c.execute(
                f"SELECT DISTINCT {col} v FROM voters "
                f"WHERE {year_clause}{col} IS NOT NULL AND {col} <> ''",
                year_param).fetchall()
            out[key] = sorted(str(r["v"]) for r in rows)
        rng_where = "WHERE year = %s" if year is not None else ""
        rng = c.execute(
            f"SELECT min(age) mn, max(age) mx FROM voters {rng_where}",
            year_param).fetchone()
    out["age_min"] = rng["mn"] if rng and rng["mn"] is not None else 0
    out["age_max"] = rng["mx"] if rng and rng["mx"] is not None else 120
    return out


def parts_for(year: int | None, acs: list[str]) -> list[str]:
    """Distinct part numbers, optionally scoped to year + chosen ACs, sorted
    numerically where possible."""
    clauses, params = [], []
    if year is not None:
        clauses.append("year = %s")
        params.append(year)
    if acs:
        clauses.append("constituency_no = ANY(%s)")
        params.append(list(acs))
    clauses.append("part_no IS NOT NULL AND part_no <> ''")
    where = "WHERE " + " AND ".join(clauses)
    with connect() as c:
        rows = c.execute(f"SELECT DISTINCT part_no v FROM voters {where}",
                         params).fetchall()
    def key(p: str):
        digits = "".join(ch for ch in p if ch.isdigit())
        return (0, int(digits)) if digits else (1, p)
    return sorted((str(r["v"]) for r in rows), key=key)
