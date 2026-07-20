"""Load extracted rolls (Excel, or the ZIP the extractor produces) into Postgres."""
from __future__ import annotations

import io
import zipfile

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from auth import require_auth
from dbx import (connect, db_ready, init_schema, ingest_dataframe,
                 year_from_filename)

load_dotenv()

st.set_page_config(page_title="Ingest → Database", page_icon="📥", layout="wide")

require_auth()   # nothing below runs for an unauthenticated visitor
st.title("📥 Ingest into Database")
st.caption("Load extracted rolls into Postgres so they can be cross-checked "
           "against every other roll for duplicates and photo reuse.")

ok, msg = db_ready()
if not ok:
    st.error(f"Database not reachable: {msg}")
    st.stop()
st.success(f"Connected — {msg}")
init_schema()

st.markdown(
    "Upload either:\n"
    "- the **ZIP** the extractor produces (Excel **+ photos** — preferred, "
    "photo reuse is the strongest fraud signal), or\n"
    "- one or more **.xlsx** files (text only, no photo checks)."
)

files = st.file_uploader("Roll files", type=["zip", "xlsx"],
                         accept_multiple_files=True)

# ---- revision year: rolls are named '2025-EROLLGEN-...', so pre-fill from the
# filename but let it be overridden. Each year is kept as a separate dataset.
detected = next((y for f in (files or [])
                 if (y := year_from_filename(f.name))), None)
yc1, yc2 = st.columns([1, 3])
with yc1:
    year = st.number_input("Revision year", min_value=1990, max_value=2100,
                           value=detected or 2026, step=1,
                           help="Which roll revision this upload belongs to. "
                                "Detection rules compare within one year only.")
with yc2:
    if detected:
        st.caption(f"Detected **{detected}** from the file name — change it "
                   "above if that is wrong.")
    else:
        st.caption("No year found in the file name — set it manually.")
    st.caption("Re-uploading the same seat for the **same** year updates those "
               "rows; a **different** year is stored as a separate dataset.")

if files and st.button("Ingest", type="primary", use_container_width=True):
    total_v = total_p = 0
    for f in files:
        try:
            if f.name.lower().endswith(".zip"):
                zf = zipfile.ZipFile(io.BytesIO(f.read()))
                xlsx = [n for n in zf.namelist() if n.endswith(".xlsx")]
                if not xlsx:
                    st.warning(f"{f.name}: no .xlsx inside, skipped.")
                    continue
                df = pd.read_excel(io.BytesIO(zf.read(xlsx[0])))
                photos = {
                    n.split("/")[-1]: zf.read(n)
                    for n in zf.namelist()
                    if "/photos/" in n and not n.endswith("/")
                }
            else:
                df = pd.read_excel(f)
                photos = {}

            _, n_v, n_p = ingest_dataframe(df, f.name, photos, int(year))
            total_v += n_v
            total_p += n_p
            st.write(f"✅ **{f.name}** — {n_v} voters, {n_p} photos ({year})")
        except Exception as e:  # noqa: BLE001
            st.error(f"{f.name}: {e}")

    st.success(f"Ingested {total_v} voters and {total_p} photos into {year}.")

# ---------------------------------------------------------------- current state
st.divider()
st.subheader("What's in the database")
with connect() as c:
    stats = c.execute("""
        SELECT (SELECT count(*) FROM voters)  AS voters,
               (SELECT count(*) FROM photos)  AS photos,
               (SELECT count(DISTINCT constituency_no) FROM voters) AS constituencies,
               (SELECT count(DISTINCT (constituency_no, part_no)) FROM voters) AS parts,
               (SELECT count(*) FROM flags)   AS flags
    """).fetchone()
    by_year = c.execute("""
        SELECT year,
               count(*) AS voters,
               count(DISTINCT constituency_no) AS constituencies,
               count(DISTINCT (constituency_no, part_no)) AS parts
        FROM voters GROUP BY year ORDER BY year DESC
    """).fetchall()
    ingests = c.execute("""
        SELECT year, source_file, constituency_no, part_no, row_count,
               photo_count, ingested_at
        FROM ingests ORDER BY ingested_at DESC LIMIT 25
    """).fetchall()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Voters", f"{stats['voters']:,}")
c2.metric("Photos", f"{stats['photos']:,}")
c3.metric("Constituencies", stats["constituencies"])
c4.metric("Parts", stats["parts"])
c5.metric("Flags", f"{stats['flags']:,}")

if by_year:
    st.markdown("**By revision year** — each year is a separate dataset the "
                "detection rules run on independently.")
    st.dataframe(pd.DataFrame(by_year), use_container_width=True,
                 hide_index=True)

if ingests:
    st.dataframe(pd.DataFrame(ingests), use_container_width=True)
else:
    st.info("Nothing ingested yet.")
