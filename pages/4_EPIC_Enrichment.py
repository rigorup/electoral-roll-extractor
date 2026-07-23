"""Fill verified ECINET enumeration details onto voters, keyed by EPIC number."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import eci_client
import epic_enrich
from auth import require_auth
from dbx import (available_years, connect, db_ready, init_schema, set_setting,
                 setting_updated_at)

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
    st.caption(f"Session config loaded from **{eci_client.config_source()}**.")
else:
    st.error(f"EPIC lookup not configured: {cfg_msg}")
    st.caption("Paste your ERO session config below to enable lookups.")

# ---------------------------------------------------- ECINET session tokens
# ERO tokens expire roughly every 30h. Storing them in the database means a
# refresh is a paste here — no rebuild, no redeploy, no container restart.
with st.expander("🔑 ECINET session tokens" +
                 ("" if ok_cfg else " — needs setting up"), expanded=not ok_cfg):
    updated = setting_updated_at(eci_client.SETTING_KEY)
    if updated:
        stamp = updated.strftime("%Y-%m-%d %H:%M UTC")
        st.caption(f"Stored in the database, last updated **{stamp}**. Tokens "
                   "expire about 30 hours after they are issued.")
    st.markdown(
        "Paste the whole `config.json` from your `epic_lookup` folder. It needs "
        "`userAgent`, `stateCd` and a `sessions` list, each with `acNo`, "
        "`token`, `atkn_bnd` and `rtkn_bnd`. Each ERO token only sees its own "
        "Assembly Constituency."
    )
    pasted = st.text_area("config.json", height=200, key="cfg_paste",
                          placeholder='{"userAgent": "...", "stateCd": "S02", '
                                      '"sessions": [...]}')
    if st.button("Save tokens", disabled=not pasted.strip()):
        try:
            parsed = json.loads(pasted)
        except json.JSONDecodeError as e:
            st.error(f"That isn't valid JSON: {e}")
        else:
            good, msg = eci_client.validate_config(parsed)
            if not good:
                st.error(f"Config rejected: {msg}")
            else:
                set_setting(eci_client.SETTING_KEY, json.dumps(parsed))
                st.success(f"Saved — {msg}. Reloading…")
                st.rerun()

if not ok_cfg:
    st.stop()

# ---------------------------------------------------------------- year scope
years = available_years()
if not years:
    st.info("No data yet — ingest a roll first.")
    st.stop()

st.divider()
yc1, yc2 = st.columns([1, 3])
with yc1:
    year = st.selectbox("📅 Revision year", years, index=0,
                        help="Everything on this page — the pending counts and "
                             "the batch itself — covers only this year.")
with yc2:
    st.caption(f"Counts and lookups below are scoped to **{year}**. Each "
               "revision year is a separate dataset, so the same elector in "
               "another year is enriched separately.")

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
                            step=0.1, help="Per worker. Be gentle with the live "
                                           "ECI API.")
    include_aadhaar = st.toggle(
        "Include Aadhaar reference", value=False,
        help="Sensitive personal data. Off by default.")

n_target_acs = len(chosen or ac_options)
workers = st.slider(
    "Parallel constituencies (lookups at once)", 1, max(n_target_acs, 1),
    min(3, n_target_acs) or 1,
    help="One worker per constituency runs concurrently — 3 selected means 3 "
         "lookups in flight at once. Within a constituency the calls stay "
         "sequential, because that is one shared ERO token.")

if include_aadhaar:
    st.warning("⚠️ Aadhaar reference numbers will be written into the voters "
               "table. Enable only if you are authorised to store them, and "
               "handle the database accordingly.")

planned = min(tot_p, int(per_ac_cap) * n_target_acs)
st.caption(f"This click will look up at most **{planned:,}** unique EPIC(s) — "
           f"{per_ac_cap} per constituency, **{workers}** at a time. Rows "
           "already marked *Found* are skipped, so nothing is fetched twice.")

if st.button("🔎 Fetch & fill details", type="primary",
             use_container_width=True, disabled=tot_p == 0):
    status = st.status("Starting lookups…", expanded=True)
    bar = st.progress(0.0)

    def progress(msg: str, frac: float | None = None):
        # Only update the label + bar (called ~2×/sec): appending to the log
        # every tick would bury the page in hundreds of lines.
        status.update(label=msg)
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
            max_workers=int(workers),
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
               f"unique EPIC(s) across {stats.get('workers', 1)} parallel "
               "worker(s) — duplicate rows share one lookup.")
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
