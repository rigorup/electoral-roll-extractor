"""Load extracted rolls (Excel, or the ZIP the extractor produces) into Postgres."""
from __future__ import annotations

import io
import zipfile

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from dbx import connect, db_ready, init_schema, ingest_dataframe

load_dotenv()

st.set_page_config(page_title="Ingest → Database", page_icon="📥", layout="wide")
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

            _, n_v, n_p = ingest_dataframe(df, f.name, photos)
            total_v += n_v
            total_p += n_p
            st.write(f"✅ **{f.name}** — {n_v} voters, {n_p} photos")
        except Exception as e:  # noqa: BLE001
            st.error(f"{f.name}: {e}")

    st.success(f"Ingested {total_v} voters and {total_p} photos.")

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
    ingests = c.execute("""
        SELECT source_file, constituency_no, part_no, row_count, photo_count,
               ingested_at
        FROM ingests ORDER BY ingested_at DESC LIMIT 25
    """).fetchall()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Voters", f"{stats['voters']:,}")
c2.metric("Photos", f"{stats['photos']:,}")
c3.metric("Constituencies", stats["constituencies"])
c4.metric("Parts", stats["parts"])
c5.metric("Flags", f"{stats['flags']:,}")

if ingests:
    st.dataframe(pd.DataFrame(ingests), use_container_width=True)
else:
    st.info("Nothing ingested yet.")
