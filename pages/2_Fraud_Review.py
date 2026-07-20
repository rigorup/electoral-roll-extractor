"""Run the detection rules and adjudicate the flags they raise."""
from __future__ import annotations

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from auth import require_auth
from dbx import available_years, db_ready
from fraud_rules import (RULES, clear_flags, flag_summary, open_flags,
                         record_review, run_rules)
from ui_helpers import (build_flags_pdf, flag_card, flag_title,
                        infinite_limit, infinite_scroll_sentinel)

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
if summary:
    st.subheader(f"Flags by rule — {year}")
    st.dataframe(pd.DataFrame(summary), use_container_width=True)
else:
    st.info(f"No flags for {year} yet — run the rules from the sidebar.")
    st.stop()

# ---------------------------------------------------------------- queue
st.divider()
st.subheader("Review queue")
rule_filter = st.selectbox("Filter by rule", ["(all)"] + list(RULES))

_filter = None if rule_filter == "(all)" else rule_filter
# PDF embeds photos for every flag, so it is heavy — build only on click,
# not on every rerun, and cache the bytes for the current filter + year.
pdf_key = f"flags_pdf::{year}::{rule_filter}"
if st.button("🧾 Prepare PDF (photos, 5 / page)",
             help="Side-by-side comparison with both photos and all "
                  "details for every flag matching the current filter and "
                  "year — 5 comparisons per A4 page."):
    with st.spinner(f"Building {year} PDF (embedding photos)…"):
        st.session_state[pdf_key] = build_flags_pdf(_filter, year)
if st.session_state.get(pdf_key):
    st.download_button(
        "⬇️ Download flags PDF",
        data=st.session_state[pdf_key],
        file_name=f"fraud_flags_{year}.pdf",
        mime="application/pdf",
    )

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
