"""Postgres + pgvector layer: connection, schema, normalisation, ingest.

The schema is created idempotently on first use, so a fresh deploy needs no
manual migration step.
"""
from __future__ import annotations

import os
import re

import psycopg
from psycopg.rows import dict_row


# ---------------------------------------------------------------- connection
def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set.")
    # psycopg wants postgresql://, Coolify hands out postgres://
    return re.sub(r"^postgres://", "postgresql://", dsn)


def connect():
    return psycopg.connect(get_dsn(), row_factory=dict_row, connect_timeout=10)


def db_ready() -> tuple[bool, str]:
    try:
        with connect() as c:
            v = c.execute("select version()").fetchone()["version"]
        return True, v.split(",")[0]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ---------------------------------------------------------------- schema
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS ingests (
    id                SERIAL PRIMARY KEY,
    source_file       TEXT NOT NULL,
    constituency_no   TEXT,
    constituency_name TEXT,
    part_no           TEXT,
    row_count         INT DEFAULT 0,
    photo_count       INT DEFAULT 0,
    ingested_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS voters (
    id                 BIGSERIAL PRIMARY KEY,
    ingest_id          INT REFERENCES ingests(id) ON DELETE CASCADE,
    year               INT,
    constituency_no    TEXT,
    constituency_name  TEXT,
    part_no            TEXT,
    serial_no          INT,
    epic_no            TEXT,
    name               TEXT,
    relation_type      TEXT,
    relation_name      TEXT,
    house_number       TEXT,
    age                INT,
    gender             TEXT,
    photo_id           TEXT,
    -- normalised forms: linkage quality is won or lost here
    name_norm          TEXT,
    relation_name_norm TEXT,
    house_norm         TEXT,
    name_phonetic      TEXT,
    created_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS voters_epic_idx      ON voters (epic_no);
CREATE INDEX IF NOT EXISTS voters_name_norm_idx ON voters (name_norm);
CREATE INDEX IF NOT EXISTS voters_house_idx     ON voters (constituency_no, house_norm);
CREATE INDEX IF NOT EXISTS voters_phon_idx      ON voters (name_phonetic);
CREATE INDEX IF NOT EXISTS voters_name_trgm_idx ON voters USING gin (name_norm gin_trgm_ops);

-- Photos live in the DB (bytea): ~15KB x 50k = ~1GB, keeps the app stateless.
CREATE TABLE IF NOT EXISTS photos (
    id         BIGSERIAL PRIMARY KEY,
    voter_id   BIGINT UNIQUE REFERENCES voters(id) ON DELETE CASCADE,
    photo_id   TEXT,
    image      BYTEA,
    phash      BIGINT,          -- perceptual (dHash) -> catches reused images
    embedding  vector(512),     -- face embedding, filled by a later batch job
    face_found BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS photos_phash_idx ON photos (phash);

-- One row per suspicious finding. A flag is a LEAD, never a verdict.
CREATE TABLE IF NOT EXISTS flags (
    id               BIGSERIAL PRIMARY KEY,
    rule             TEXT NOT NULL,
    severity         TEXT,
    score            REAL,
    voter_id         BIGINT REFERENCES voters(id) ON DELETE CASCADE,
    related_voter_id BIGINT REFERENCES voters(id) ON DELETE CASCADE,
    details          JSONB,
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (rule, voter_id, related_voter_id)
);
CREATE INDEX IF NOT EXISTS flags_rule_idx ON flags (rule);

-- Human adjudication. These become the training labels for a later ranker.
CREATE TABLE IF NOT EXISTS reviews (
    id          BIGSERIAL PRIMARY KEY,
    flag_id     BIGINT REFERENCES flags(id) ON DELETE CASCADE,
    verdict     TEXT,   -- confirmed | legitimate | needs_info
    reviewer    TEXT,
    notes       TEXT,
    reviewed_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reviews_flag_idx ON reviews (flag_id);

-- The plain UNIQUE above treats NULL related_voter_id as distinct, so
-- re-running single-voter rules used to duplicate flags. De-duplicate any
-- legacy rows, then enforce uniqueness with NULL coalesced.
DELETE FROM flags a USING flags b
 WHERE a.id > b.id AND a.rule = b.rule AND a.voter_id = b.voter_id
   AND coalesce(a.related_voter_id, -1) = coalesce(b.related_voter_id, -1);
CREATE UNIQUE INDEX IF NOT EXISTS flags_dedup_idx
    ON flags (rule, voter_id, coalesce(related_voter_id, -1));

-- house_overload moved from one-flag-per-voter to one-flag-per-house: drop
-- old-format flags (no 'house_norm' key in details) nobody has reviewed yet.
DELETE FROM flags f
 WHERE f.rule = 'house_overload' AND NOT (f.details ? 'house_norm')
   AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.flag_id = f.id);

-- ---------------------------------------------------------------- year split
-- Each roll belongs to a revision year; the detection rules run within one
-- year at a time (comparing 2025 against 2026 would flag every returning
-- voter as a duplicate).
ALTER TABLE voters  ADD COLUMN IF NOT EXISTS year INT;
ALTER TABLE ingests ADD COLUMN IF NOT EXISTS year INT;

-- Everything loaded before this column existed is the 2025 roll.
UPDATE voters  SET year = 2025 WHERE year IS NULL;
UPDATE ingests SET year = 2025 WHERE year IS NULL;
ALTER TABLE voters ALTER COLUMN year SET NOT NULL;

-- The original key omitted year, so re-uploading the same seat for a later
-- year would UPDATE the previous year's row instead of adding a new one --
-- silently destroying the earlier roll. The key must include year.
ALTER TABLE voters
    DROP CONSTRAINT IF EXISTS voters_constituency_no_part_no_serial_no_epic_no_key;
CREATE UNIQUE INDEX IF NOT EXISTS voters_year_ac_part_serial_epic_idx
    ON voters (year, constituency_no, part_no, serial_no, epic_no);
CREATE INDEX IF NOT EXISTS voters_year_idx ON voters (year);

-- ------------------------------------------------------- ECINET enrichment
-- The roll PDF only carries what is printed on the page. The ERO-side ECINET
-- record holds the verified enumeration details (DOB, mobile, parents, the
-- exact part/serial) plus two document images. These columns are filled by the
-- EPIC Enrichment page, one lookup per unique EPIC.
ALTER TABLE voters ADD COLUMN IF NOT EXISTS epic_lookup_status  TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS epic_lookup_at      TIMESTAMPTZ;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS epic_id             TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS lookup_ac_no        TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS lookup_officer      TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS verified_name       TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS verified_dob        TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS verified_age        INT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS mobile_no           TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS father_or_guardian_name TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS mother_name         TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS spouse_name         TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS verified_house_no   TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS verified_part_no    TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS part_serial_no      TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS part_name           TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS ac_name             TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS category_type       TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS relation_type_code  TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS relation_epic       TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS relation_name_verified TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS district_cd         TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS state_cd            TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS survey_channel      TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS submitted_for_recommendation TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS enum_created_on     TEXT;
ALTER TABLE voters ADD COLUMN IF NOT EXISTS enum_modified_on    TEXT;
-- Sensitive: only written when the operator explicitly opts in.
ALTER TABLE voters ADD COLUMN IF NOT EXISTS aadhaar_ref_no      TEXT;

CREATE INDEX IF NOT EXISTS voters_epic_lookup_idx
    ON voters (epic_lookup_status);

-- The two ECINET images (EF photo + enumeration form page 1). Keyed by EPIC,
-- not voter_id: the same elector appears in several years/rows but the
-- document is one file, so this stores exactly one copy per EPIC. phash is
-- kept so these can feed the existing photo-reuse detection.
CREATE TABLE IF NOT EXISTS epic_documents (
    id         BIGSERIAL PRIMARY KEY,
    epic_no    TEXT NOT NULL,
    doc_type   TEXT NOT NULL,          -- 'photo' | 'sr_form'
    image      BYTEA,
    ext        TEXT,
    phash      BIGINT,
    fetched_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (epic_no, doc_type)
);
CREATE INDEX IF NOT EXISTS epic_documents_epic_idx  ON epic_documents (epic_no);
CREATE INDEX IF NOT EXISTS epic_documents_phash_idx ON epic_documents (phash);

-- Small key/value store for operational settings that must survive a redeploy
-- and be editable from the UI. Holds the ECINET session config: those ERO
-- tokens expire roughly every 30h, so keeping them here means a refresh is a
-- paste into the app, not a rebuild.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""


def init_schema() -> None:
    with connect() as c:
        c.execute(SCHEMA)
        c.commit()


# ------------------------------------------------------------ app settings
def get_setting(key: str) -> str | None:
    """Read one operational setting, or None. Never raises."""
    try:
        with connect() as c:
            row = c.execute("SELECT value FROM app_settings WHERE key=%s",
                            (key,)).fetchone()
        return row["value"] if row else None
    except Exception:  # noqa: BLE001 — table may not exist yet
        return None


def set_setting(key: str, value: str) -> None:
    init_schema()
    with connect() as c:
        c.execute(
            """INSERT INTO app_settings (key, value) VALUES (%s,%s)
               ON CONFLICT (key) DO UPDATE
                 SET value = EXCLUDED.value, updated_at = now()""",
            (key, value))
        c.commit()


def setting_updated_at(key: str):
    try:
        with connect() as c:
            row = c.execute("SELECT updated_at FROM app_settings WHERE key=%s",
                            (key,)).fetchone()
        return row["updated_at"] if row else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- normalise
_HONORIFICS = r"^(SMT|SHRI|SRI|MR|MRS|MS|DR|LATE)\.?\s+"


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = str(s).upper().strip()
    s = re.sub(r"[^A-Z ]", " ", s)          # drop punctuation/digits
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(_HONORIFICS, "", s).strip()
    return s


def norm_house(s: str | None) -> str:
    """E-72 / E 72 / E72 must all collapse to E72."""
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def phonetic(s: str | None) -> str:
    """Metaphone of the full name — tolerant of Indian transliteration drift
    (BASFOR / BASPHOR / BUSFOR collapse together)."""
    n = norm_name(s)
    if not n:
        return ""
    try:
        import jellyfish
        return " ".join(jellyfish.metaphone(tok) for tok in n.split())
    except Exception:
        return n


def to_int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- photo hash
def dhash(image_bytes: bytes) -> int | None:
    """64-bit difference hash. Identical/near-identical images collide, which
    is exactly what 'same photo reused under two identities' looks like."""
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        img = cv2.resize(img, (9, 8), interpolation=cv2.INTER_AREA)
        diff = img[:, 1:] > img[:, :-1]
        bits = 0
        for b in diff.flatten():
            bits = (bits << 1) | int(b)
        # store as signed BIGINT
        return bits - (1 << 64) if bits >= (1 << 63) else bits
    except Exception:
        return None


# ---------------------------------------------------------------- ingest
VOTER_COLS = [
    "year",
    "constituency_no", "constituency_name", "part_no", "serial_no", "epic_no",
    "name", "relation_type", "relation_name", "house_number", "age", "gender",
    "photo_id", "name_norm", "relation_name_norm", "house_norm", "name_phonetic",
]

DEFAULT_YEAR = 2025          # what the pre-year-column data was


def year_from_filename(name: str) -> int | None:
    """Rolls are named like '2025-EROLLGEN-S02-58-FinalRoll-...' — pull the
    revision year out so the ingest form can pre-fill it."""
    m = re.search(r"(19|20)\d{2}", str(name or ""))
    return int(m.group(0)) if m else None


_YEARS_SQL = ("SELECT DISTINCT year FROM voters WHERE year IS NOT NULL "
              "ORDER BY year DESC")


def available_years() -> list[int]:
    """Years that actually have voters loaded, newest first.

    The review pages call this before anything else touches the schema, so on a
    database predating the year column the first call migrates and retries
    rather than erroring the page."""
    try:
        with connect() as c:
            return [r["year"] for r in c.execute(_YEARS_SQL).fetchall()]
    except psycopg.errors.UndefinedColumn:
        init_schema()
        with connect() as c:
            return [r["year"] for r in c.execute(_YEARS_SQL).fetchall()]


def ingest_dataframe(df, source_file: str, photos: dict[str, bytes] | None = None,
                     year: int | None = None):
    """Load one extracted roll (Excel rows + optional photo bytes keyed by
    Photo_Id) into Postgres under a revision `year`. Idempotent per year:
    re-ingesting the same roll for the same year updates rather than
    duplicates, while the same seat for a different year is a separate row.
    Returns (ingest_id, n_voters, n_photos)."""
    photos = photos or {}
    year = int(year or year_from_filename(source_file) or DEFAULT_YEAR)
    init_schema()

    def g(row, key):
        v = row.get(key)
        return "" if v is None or str(v) == "nan" else str(v).strip()

    with connect() as c:
        first = df.iloc[0].to_dict() if len(df) else {}
        ing = c.execute(
            """INSERT INTO ingests (source_file, constituency_no,
                                    constituency_name, part_no, year)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (source_file, g(first, "Constituency_No"),
             g(first, "Constituency_Name"), g(first, "Part_No"), year),
        ).fetchone()["id"]

        n_v = n_p = 0
        for _, r in df.iterrows():
            row = r.to_dict()
            name = g(row, "Name")
            if not name:
                continue
            rel_name = g(row, "Relation_Name")
            house = g(row, "House_Number")

            vid = c.execute(
                f"""INSERT INTO voters (ingest_id, {','.join(VOTER_COLS)})
                    VALUES (%s,{','.join(['%s'] * len(VOTER_COLS))})
                    ON CONFLICT (year, constituency_no, part_no, serial_no,
                                 epic_no)
                    DO UPDATE SET name = EXCLUDED.name,
                                  ingest_id = EXCLUDED.ingest_id
                    RETURNING id""",
                (ing, year, g(row, "Constituency_No"), g(row, "Constituency_Name"),
                 g(row, "Part_No"), to_int(row.get("Serial_No")),
                 g(row, "EPIC_No"), name, g(row, "Relation_Type"), rel_name,
                 house, to_int(row.get("Age")), g(row, "Gender"),
                 g(row, "Photo_Id"), norm_name(name), norm_name(rel_name),
                 norm_house(house), phonetic(name)),
            ).fetchone()["id"]
            n_v += 1

            pid = g(row, "Photo_Id")
            blob = photos.get(pid)
            if blob:
                c.execute(
                    """INSERT INTO photos (voter_id, photo_id, image, phash)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (voter_id) DO UPDATE
                         SET image = EXCLUDED.image, phash = EXCLUDED.phash""",
                    (vid, pid, blob, dhash(blob)),
                )
                n_p += 1

        c.execute("UPDATE ingests SET row_count=%s, photo_count=%s WHERE id=%s",
                  (n_v, n_p, ing))
        c.commit()
    return ing, n_v, n_p
