"""Fill verified ECINET enumeration details onto voters, keyed by EPIC number."""
from __future__ import annotations

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import eci_client
import epic_enrich
from auth import require_auth
from dbx import available_years, connect, db_ready, init_schema

load_dotenv()

st.set_page_config(page_title="EPIC Enrichment", page_icon="🪪", layout="wide")

require_auth()   # nothing below runs for an unauthenticated visitor
st.title("🪪 EPIC Enrichment")
st.caption("Looks each EPIC number up on ECINET and fills the verified "
           "enumeration record onto the voter row — DOB, mobile, parents, the "
           "exact part/serial — plus the two document images.")

ok, msg = db_ready()
if not ok:
    st.error(f"Database not reachable: {msg}")
    st.stop()
init_schema()

ok_cfg, cfg_msg = eci_client.config_available()
if ok_cfg:
    st.success(f"Database connected · ECINET sessions ready — {cfg_msg}")
else:
    st.error(f"EPIC lookup not configured: {cfg_msg}")
    st.caption("Point `EPIC_LOOKUP_CONFIG` at your config.json, or place one "
               "beside the app. Each ERO token only sees its own AC and "
               "expires roughly every 30 hours.")

# ---------------------------------------------------------------- year scope
years = available_years()
if not years:
    st.info("No data yet — ingest a roll first.")
    st.stop()

with st.sidebar:
    st.header("Revision year")
    year = st.selectbox("Data year", years, index=0,
                        help="Only voters in this year are enriched.")

# ---------------------------------------------------------------- what's left
summary = epic_enrich.pending_summary(year)
if not summary:
    st.info(f"No EPIC numbers found in {year}.")
    st.stop()

sdf = pd.DataFrame(summary)
tot_u = int(sdf["unique_epics"].sum())
tot_d = int(sdf["done"].sum())
tot_p = int(sdf["pending"].sum())

m1, m2, m3 = st.columns(3)
m1.metric("Unique EPICs", f"{tot_u:,}")
m2.metric("Already filled", f"{tot_d:,}")
m3.metric("Still pending", f"{tot_p:,}")

st.markdown("**Per constituency**")
st.dataframe(sdf, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- controls
st.divider()
st.subheader("Run a batch")

ac_options = [str(r["constituency_no"]) for r in summary
              if r["constituency_no"] is not None]
c1, c2 = st.columns(2)
with c1:
    chosen = st.multiselect(
        "Constituencies (blank = all)", ac_options,
        help="Only ACs whose ERO token is in config.json can resolve; others "
             "come back as Not found.")
    per_ac_cap = st.number_input(
        "Max EPICs per constituency (per click)", 1, 5000, 100,
        help="Default 100 — one click will not pull an entire roll. Click "
             "again to continue where it stopped.")
with c2:
    include_images = st.toggle(
        "Fetch the 2 images", value=True,
        help="EF photo + enumeration form page 1, stored in epic_documents "
             "(one copy per EPIC).")
    delay = st.number_input("Delay between lookups (sec)", 0.0, 5.0, 0.0,
                            step=0.1, help="Be gentle with the live ECI API.")
    include_aadhaar = st.toggle(
        "Include Aadhaar reference", value=False,
        help="Sensitive personal data. Off by default.")

if include_aadhaar:
    st.warning("⚠️ Aadhaar reference numbers will be written into the voters "
               "table. Enable only if you are authorised to store them, and "
               "handle the database accordingly.")

planned = min(tot_p, int(per_ac_cap) * len(chosen or ac_options))
st.caption(f"This click will look up at most **{planned:,}** unique EPIC(s) — "
           f"{per_ac_cap} per constituency. Rows already marked *Found* are "
           "skipped, so nothing is fetched twice.")

if st.button("🔎 Fetch & fill details", type="primary",
             use_container_width=True, disabled=not ok_cfg or tot_p == 0):
    status = st.status("Starting lookups…", expanded=True)
    bar = st.progress(0.0)

    def progress(msg: str, frac: float | None = None):
        status.update(label=msg)
        status.write(msg)
        if frac is not None:
            bar.progress(min(max(frac, 0.0), 1.0))

    try:
        stats = epic_enrich.enrich_pending(
            year=int(year),
            acs=chosen or None,
            per_ac_cap=int(per_ac_cap),
            include_images=include_images,
            include_aadhaar=include_aadhaar,
            delay=float(delay),
            progress=progress,
        )
        status.update(label="Done.", state="complete", expanded=False)
    except Exception as e:  # noqa: BLE001
        status.update(label="Failed.", state="error")
        st.exception(e)
        st.stop()

    st.session_state["epic_enrich_stats"] = stats

# ---------------------------------------------------------------- results
if "epic_enrich_stats" in st.session_state:
    stats = st.session_state["epic_enrich_stats"]
    st.divider()
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Filled", stats["found"])
    r2.metric("Not found", stats["not_found"])
    r3.metric("Rows updated", stats["rows_updated"])
    r4.metric("Images stored", stats["images_saved"])

    st.caption(f"{stats['api_calls']} API call(s) for {stats['unique_epics']} "
               "unique EPIC(s) — duplicate rows share one lookup.")
    if stats["per_ac"]:
        st.caption("Per AC this run: " + " · ".join(
            f"AC {k}: {v}" for k, v in sorted(stats["per_ac"].items())))

    if stats["stopped_early"] or any("expired" in str(m).lower()
                                     for m in stats["messages"]):
        st.error("🔑 A token has expired. Paste a fresh one into config.json — "
                 "it is re-read on the next click, no restart needed.")
        for m in stats["messages"][:5]:
            st.caption(f"· {m}")

    if stats["image_errors"]:
        with st.expander(f"⚠️ {len(stats['image_errors'])} image(s) not fetched"):
            for m in stats["image_errors"][:50]:
                st.caption(f"· {m}")

# ---------------------------------------------------------------- spot check
st.divider()
st.subheader("Spot-check a record")
probe = st.text_input("EPIC number", placeholder="e.g. BPR0299776").strip().upper()
if probe:
    with connect() as c:
        rows = c.execute(
            """SELECT year, constituency_no, part_no, serial_no, name,
                      epic_lookup_status, verified_name, verified_dob,
                      verified_age, mobile_no, father_or_guardian_name,
                      mother_name, part_serial_no, ac_name, epic_lookup_at
               FROM voters WHERE epic_no = %s ORDER BY year DESC""",
            (probe,)).fetchall()
        docs = c.execute(
            "SELECT doc_type, ext, length(image) AS bytes, fetched_at "
            "FROM epic_documents WHERE epic_no = %s", (probe,)).fetchall()

    if not rows:
        st.info("That EPIC is not in the database.")
    else:
        st.dataframe(pd.DataFrame(rows), use_container_width=True,
                     hide_index=True)
        if docs:
            st.caption("Stored documents")
            cols = st.columns(len(docs))
            with connect() as c:
                for col, d in zip(cols, docs):
                    blob = c.execute(
                        "SELECT image FROM epic_documents "
                        "WHERE epic_no=%s AND doc_type=%s",
                        (probe, d["doc_type"])).fetchone()["image"]
                    col.image(bytes(blob), caption=f"{d['doc_type']} "
                                                   f"({d['bytes']:,} bytes)")
        else:
            st.caption("No documents stored for this EPIC yet.")
