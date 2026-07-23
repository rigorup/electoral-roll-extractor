"""Run the detection rules and adjudicate the flags they raise."""
from __future__ import annotations

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from auth import require_auth
from dbx import available_years, db_ready
from fraud_rules import (RULES, clear_flags, flag_counts_by_constituency,
                         flag_counts_by_constituency_rule, flag_summary,
                         flagged_constituencies, open_flags, record_review,
                         run_rules)
from ui_helpers import (build_flags_pdf, build_flags_pdf_zip, flag_card,
                        flag_title, infinite_limit, infinite_scroll_sentinel)

load_dotenv()

st.set_page_config(page_title="Fraud Review", page_icon="🔍", layout="wide")

require_auth()   # nothing below runs for an unauthenticated visitor
st.title("🔍 Fraud Detection & Review")

ok, msg = db_ready()
if not ok:
    st.error(f"Database not reachable: {msg}")
    st.stop()

st.warning(
    "**A flag is a lead, not a verdict.** Removing a legitimate voter is the "
    "worse error — migrants and married women are the usual casualties of "
    "name-based matching. Every flag needs human confirmation before any action.",
    icon="⚠️",
)

# ---------------------------------------------------------------- year scope
years = available_years()
if not years:
    st.info("No rolls loaded yet — ingest data on the Ingest page first.")
    st.stop()

with st.sidebar:
    st.header("Revision year")
    year = st.selectbox(
        "Data year", years, index=0,
        help="Every rule compares voters *within* the selected year only. "
             "The same person legitimately reappears in the next year's roll, "
             "so comparing across years would flag the whole electorate.")
    st.caption(f"Rules, queue and exports below are scoped to **{year}**.")

# ---------------------------------------------------------------- run rules
with st.sidebar:
    st.header("Detection rules")
    for rid, (sev, desc, _) in RULES.items():
        st.caption(f"**{rid}** ({sev}) — {desc}")

    chosen = st.multiselect("Rules to run", list(RULES), default=list(RULES))
    if st.button(f"▶️ Run rules on {year}", type="primary",
                 use_container_width=True):
        with st.spinner(f"Scanning {year} data…"):
            added = run_rules(chosen, year)
        st.success("New flags: " + ", ".join(f"{k}: {v}" for k, v in added.items()))
    if st.button(f"Clear {year} flags", use_container_width=True):
        clear_flags(year)
        st.info(f"{year} flags cleared (reviews are kept, other years intact).")

    reviewer = st.text_input("Reviewer name", value="adi")

# ---------------------------------------------------------------- summary
summary = flag_summary(year)
if not summary:
    st.info(f"No flags for {year} yet — run the rules from the sidebar.")
    st.stop()

s1, s2 = st.columns(2)
with s1:
    st.subheader(f"Flags by rule — {year}")
    st.dataframe(pd.DataFrame(summary), use_container_width=True,
                 hide_index=True)
with s2:
    st.subheader(f"Flags by constituency — {year}")
    ac_counts = flag_counts_by_constituency(year)
    ac_df = pd.DataFrame(ac_counts)
    if not ac_df.empty:
        ac_df = ac_df.rename(columns={
            "constituency_no": "AC No.", "constituency_name": "AC Name",
            "flags": "Total flags", "high": "High", "medium": "Medium",
            "low": "Low", "reviewed": "Reviewed"})
        st.dataframe(ac_df, use_container_width=True, hide_index=True)
        st.caption(f"**{int(ac_df['Total flags'].sum()):,}** flags across "
                   f"**{len(ac_df)}** constituenc"
                   f"{'y' if len(ac_df) == 1 else 'ies'}. A cross-AC pair is "
                   "counted once, under the first voter's AC.")
    else:
        st.caption("No constituency breakdown available.")

# ---- model (rule) x constituency matrix
st.subheader(f"Flags by model × constituency — {year}")
mx = pd.DataFrame(flag_counts_by_constituency_rule(year))
if mx.empty:
    st.caption("No flags to break down yet.")
else:
    label = {r["constituency_no"]: (f"AC {r['constituency_no']} — "
                                    f"{r['constituency_name']}"
                                    if r["constituency_name"]
                                    else f"AC {r['constituency_no']}")
             for r in flag_counts_by_constituency(year)}
    piv = (mx.pivot_table(index="constituency_no", columns="rule",
                          values="flags", aggfunc="sum", fill_value=0)
             .reindex(columns=list(RULES), fill_value=0))     # stable rule order
    piv.insert(0, "TOTAL", piv.sum(axis=1))
    piv.loc["ALL ACs"] = piv.sum(axis=0)                      # column totals
    piv.index = [label.get(i, i) for i in piv.index]
    piv.index.name = "Constituency"
    st.dataframe(piv, use_container_width=True)
    st.caption("Rows are constituencies (last row = all ACs), columns are "
               "detection models. TOTAL is that constituency's flags across "
               "every model.")

# ---------------------------------------------------------------- queue
st.divider()
st.subheader("Review queue")
rule_filter = st.selectbox("Filter by rule", ["(all)"] + list(RULES))

_filter = None if rule_filter == "(all)" else rule_filter

# ---------------------------------------------------------------- downloads
# PDFs embed photos, so they are heavy — build only on click, never on every
# rerun, and cache the bytes against the exact scope they were built for.
st.markdown("**Download flag report (PDF — photos, 5 comparisons / page)**")
d_tot, d_ac = st.tabs(["📄 Total (all constituencies)", "🗂️ Constituency-wise"])

with d_tot:
    tot_key = f"flags_pdf::{year}::{rule_filter}::ALL"
    if st.button("🧾 Prepare total PDF", key="prep_total",
                 help="One report covering every constituency for the "
                      "selected year and rule filter."):
        with st.spinner(f"Building {year} total PDF (embedding photos)…"):
            st.session_state[tot_key] = build_flags_pdf(_filter, year)
    if st.session_state.get(tot_key):
        st.download_button(
            "⬇️ Download total PDF",
            data=st.session_state[tot_key],
            file_name=f"fraud_flags_{year}_all_ACs.pdf",
            mime="application/pdf", key="dl_total")

with d_ac:
    acs = flagged_constituencies(year, _filter)
    if not acs:
        st.info("No constituencies with flags in this scope.")
    else:
        pick = st.selectbox("Constituency", acs,
                            format_func=lambda a: f"AC {a}")
        one_key = f"flags_pdf::{year}::{rule_filter}::{pick}"
        b1, b2 = st.columns(2)
        with b1:
            if st.button(f"🧾 Prepare AC {pick} PDF", key="prep_one",
                         use_container_width=True):
                with st.spinner(f"Building AC {pick} PDF…"):
                    st.session_state[one_key] = build_flags_pdf(
                        _filter, year, pick)
            if st.session_state.get(one_key):
                st.download_button(
                    f"⬇️ Download AC {pick} PDF",
                    data=st.session_state[one_key],
                    file_name=f"fraud_flags_{year}_AC{pick}.pdf",
                    mime="application/pdf", key="dl_one",
                    use_container_width=True)
        with b2:
            zip_key = f"flags_zip::{year}::{rule_filter}"
            if st.button(f"🗜️ Prepare all {len(acs)} ACs (ZIP)", key="prep_zip",
                         use_container_width=True,
                         help="One separate PDF per constituency, bundled "
                              "into a single ZIP."):
                bar = st.progress(0.0, text="Starting…")
                st.session_state[zip_key] = build_flags_pdf_zip(
                    _filter, year,
                    progress=lambda i, n, ac: bar.progress(
                        i / n, text=f"AC {ac} ({i}/{n})"))
                bar.empty()
            if st.session_state.get(zip_key):
                st.download_button(
                    "⬇️ Download per-AC ZIP",
                    data=st.session_state[zip_key],
                    file_name=f"fraud_flags_{year}_by_constituency.zip",
                    mime="application/zip", key="dl_zip",
                    use_container_width=True)

# Infinite scroll: fetch one page more than currently shown; the sentinel at
# the bottom bumps the limit when the user scrolls to it.
scroll_key = f"queue_pages::{year}::{rule_filter}"
limit = infinite_limit(scroll_key)
rows = open_flags(None if rule_filter == "(all)" else rule_filter,
                  limit=limit + 1, year=year)
has_more = len(rows) > limit
rows = rows[:limit]

if not rows:
    st.success("Nothing left to review in this filter. ✅")
    st.stop()

st.caption(f"Showing {len(rows)} open flag(s) — most severe first"
           + (", scroll down for more." if has_more else "."))

for f in rows:
    with st.expander(flag_title(f)):
        flag_card(f, year)

        notes = st.text_input("Notes", key=f"n{f['id']}")
        b1, b2, b3 = st.columns(3)
        if b1.button("🚩 Confirmed", key=f"c{f['id']}", use_container_width=True):
            record_review(f["id"], "confirmed", reviewer, notes)
            st.rerun()
        if b2.button("✅ Legitimate", key=f"l{f['id']}", use_container_width=True):
            record_review(f["id"], "legitimate", reviewer, notes)
            st.rerun()
        if b3.button("❓ Needs info", key=f"i{f['id']}", use_container_width=True):
            record_review(f["id"], "needs_info", reviewer, notes)
            st.rerun()

infinite_scroll_sentinel(scroll_key, has_more)
