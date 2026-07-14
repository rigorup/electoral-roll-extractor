"""Run the detection rules and adjudicate the flags they raise."""
from __future__ import annotations

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from dbx import db_ready
from fraud_rules import (RULES, clear_flags, flag_summary, get_photo,
                         open_flags, record_review, run_rules)

load_dotenv()

st.set_page_config(page_title="Fraud Review", page_icon="🔍", layout="wide")
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

# ---------------------------------------------------------------- run rules
with st.sidebar:
    st.header("Detection rules")
    for rid, (sev, desc, _) in RULES.items():
        st.caption(f"**{rid}** ({sev}) — {desc}")

    chosen = st.multiselect("Rules to run", list(RULES), default=list(RULES))
    if st.button("▶️ Run rules", type="primary", use_container_width=True):
        with st.spinner("Scanning…"):
            added = run_rules(chosen)
        st.success("New flags: " + ", ".join(f"{k}: {v}" for k, v in added.items()))
    if st.button("Clear all flags", use_container_width=True):
        clear_flags()
        st.info("Flags cleared (reviews are kept).")

    reviewer = st.text_input("Reviewer name", value="adi")

# ---------------------------------------------------------------- summary
summary = flag_summary()
if summary:
    st.subheader("Flags by rule")
    st.dataframe(pd.DataFrame(summary), use_container_width=True)
else:
    st.info("No flags yet — run the rules from the sidebar.")
    st.stop()

# ---------------------------------------------------------------- queue
st.divider()
st.subheader("Review queue")
rule_filter = st.selectbox("Filter by rule", ["(all)"] + list(RULES))
rows = open_flags(None if rule_filter == "(all)" else rule_filter, limit=100)

if not rows:
    st.success("Nothing left to review in this filter. ✅")
    st.stop()

st.caption(f"{len(rows)} flag(s) awaiting review — most severe first.")

for f in rows:
    sev_icon = {"high": "🔴", "medium": "🟠"}.get(f["severity"], "🟡")
    with st.expander(
        f"{sev_icon} **{f['rule']}** — {f['name_a']} "
        f"({f['epic_a'] or 'no EPIC'})"
        + (f"  ↔  {f['name_b']} ({f['epic_b'] or 'no EPIC'})"
           if f["name_b"] else "")
    ):
        cols = st.columns([2, 1, 2, 1]) if f["name_b"] else st.columns([2, 1])

        cols[0].markdown(
            f"**{f['name_a']}**  \nEPIC: `{f['epic_a']}`  \n"
            f"Part {f['part_a']} · House {f['house_a']}  \n"
            f"Age {f['age_a']} · {f['gender_a']}"
        )
        pa = get_photo(f["voter_id"])
        if pa:
            cols[1].image(pa, width=110)

        if f["name_b"]:
            cols[2].markdown(
                f"**{f['name_b']}**  \nEPIC: `{f['epic_b']}`  \n"
                f"Part {f['part_b']} · House {f['house_b']}  \n"
                f"Age {f['age_b']} · {f['gender_b']}"
            )
            pb = get_photo(f["related_voter_id"])
            if pb:
                cols[3].image(pb, width=110)

        st.json(f["details"], expanded=False)

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
