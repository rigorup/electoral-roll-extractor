"""Adjudicated flags, grouped by verdict, so past decisions can be revisited."""
from __future__ import annotations

import streamlit as st
from dotenv import load_dotenv

from auth import require_auth
from dbx import available_years, db_ready
from fraud_rules import (RULES, record_review, reopen_flag, reviewed_flags,
                         reviewed_summary)
from ui_helpers import (flag_card, flag_title, infinite_limit,
                        infinite_scroll_sentinel)

load_dotenv()

st.set_page_config(page_title="Reviewed Flags", page_icon="📋", layout="wide")

require_auth()   # nothing below runs for an unauthenticated visitor
st.title("📋 Reviewed Flags")
st.caption("Every flag that already has a verdict — grouped confirmed → "
           "legitimate → needs info. Re-adjudicate here, or reopen a flag to "
           "send it back to the review queue.")

ok, msg = db_ready()
if not ok:
    st.error(f"Database not reachable: {msg}")
    st.stop()

VERDICTS = {
    "confirmed":  ("🚩", "Confirmed fraud leads"),
    "legitimate": ("✅", "Confirmed legitimate"),
    "needs_info": ("❓", "Needs more info"),
}

# ---------------------------------------------------------------- year scope
years = available_years()
if not years:
    st.info("No rolls loaded yet — ingest data on the Ingest page first.")
    st.stop()
with st.sidebar:
    st.header("Revision year")
    year = st.selectbox("Data year", years, index=0)

# ---------------------------------------------------------------- summary
summary = reviewed_summary(year)
if not summary:
    st.info(f"Nothing reviewed for {year} yet — adjudicate flags on the "
            "Fraud Review page first.")
    st.stop()

c1, c2, c3 = st.columns(3)
c1.metric("🚩 Confirmed", summary.get("confirmed", 0))
c2.metric("✅ Legitimate", summary.get("legitimate", 0))
c3.metric("❓ Needs info", summary.get("needs_info", 0))

# ---------------------------------------------------------------- filters
fc1, fc2 = st.columns(2)
verdict_filter = fc1.selectbox(
    "Verdict", ["(all)"] + list(VERDICTS),
    format_func=lambda v: v if v == "(all)" else f"{VERDICTS[v][0]} {VERDICTS[v][1]}")
rule_filter = fc2.selectbox("Rule", ["(all)"] + list(RULES))

with st.sidebar:
    reviewer = st.text_input("Reviewer name", value="adi")

# ---------------------------------------------------------------- list
scroll_key = f"reviewed_pages::{year}::{verdict_filter}::{rule_filter}"
limit = infinite_limit(scroll_key)
rows = reviewed_flags(None if verdict_filter == "(all)" else verdict_filter,
                      None if rule_filter == "(all)" else rule_filter,
                      limit=limit + 1, year=year)
has_more = len(rows) > limit
rows = rows[:limit]

if not rows:
    st.info("No reviewed flags match this filter.")
    st.stop()

st.caption(f"Showing {len(rows)} reviewed flag(s)"
           + (" — scroll down for more." if has_more else "."))

last_verdict = None
for f in rows:
    if verdict_filter == "(all)" and f["verdict"] != last_verdict:
        icon, label = VERDICTS.get(f["verdict"], ("•", f["verdict"]))
        st.subheader(f"{icon} {label}")
        last_verdict = f["verdict"]

    icon = VERDICTS.get(f["verdict"], ("•",))[0]
    with st.expander(f"{icon} {flag_title(f)}"):
        st.caption(f"Verdict **{f['verdict']}** by **{f['reviewer']}** on "
                   f"{f['reviewed_at']:%Y-%m-%d %H:%M}"
                   + (f" — “{f['notes']}”" if f["notes"] else ""))
        flag_card(f, year)

        notes = st.text_input("New notes (for re-adjudication)", key=f"rn{f['id']}")
        b1, b2, b3, b4 = st.columns(4)
        if b1.button("🚩 Confirmed", key=f"rc{f['id']}", use_container_width=True,
                     disabled=f["verdict"] == "confirmed"):
            record_review(f["id"], "confirmed", reviewer, notes)
            st.rerun()
        if b2.button("✅ Legitimate", key=f"rl{f['id']}", use_container_width=True,
                     disabled=f["verdict"] == "legitimate"):
            record_review(f["id"], "legitimate", reviewer, notes)
            st.rerun()
        if b3.button("❓ Needs info", key=f"ri{f['id']}", use_container_width=True,
                     disabled=f["verdict"] == "needs_info"):
            record_review(f["id"], "needs_info", reviewer, notes)
            st.rerun()
        if b4.button("↩️ Reopen (back to queue)", key=f"ro{f['id']}",
                     use_container_width=True):
            reopen_flag(f["id"])
            st.rerun()

infinite_scroll_sentinel(scroll_key, has_more)
