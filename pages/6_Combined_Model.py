"""Combined Model — one per-voter doubt report that fuses logical discrepancies,
'no category mapping', and the cosine_new / fuzzy_new duplicate models, with two
PDF export facilities (comprehensive report + full per-voter dossier)."""
from __future__ import annotations

from collections import defaultdict

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from auth import require_auth
from combined_model import (build_combined, combined_summary,
                            constituencies_in)
from combined_pdf import (build_comprehensive_pdf, build_comprehensive_zip,
                          build_dossier_pdf, build_dossier_zip)
from dbx import available_years, db_ready
from fraud_rules import flag_summary, run_rules

load_dotenv()

st.set_page_config(page_title="Combined Model", page_icon="🧬", layout="wide")

require_auth()
st.title("🧬 Combined Model")
st.caption("For every voter, four independent doubt signals are checked and any "
           "voter with **at least one** is reported — in priority order: "
           "fuzzy/cosine duplicate leads first, then logical discrepancies, then "
           "'no category mapping'. Names come from the ECINET verified record.")

ok, msg = db_ready()
if not ok:
    st.error(f"Database not reachable: {msg}")
    st.stop()

st.warning("**A flag is a lead, not a verdict.** Every voter here needs human "
           "confirmation before any action — migrants and married women are the "
           "usual casualties of name-based matching.", icon="⚠️")

years = available_years()
if not years:
    st.info("No rolls loaded yet — ingest data first.")
    st.stop()

with st.sidebar:
    st.header("Revision year")
    year = st.selectbox("Data year", years, index=0,
                        help="All four signals are computed within this year "
                             "only.")
    st.divider()
    st.header("Signals covered")
    st.caption("**Logical discrepancy** — progeny ≥ 6 · father-name conflict · "
               "parent age gap < 15 or > 50 · grandparent age gap ≤ 40 · "
               "roll-age vs DOB-age gap > 5.")
    st.caption("**No mapping** — ECINET `category_type = 'na'`.")
    st.caption("**cosine_new / fuzzy_new** — the existing duplicate models "
               "(read from their flags).")

# --------------------------------------------------- duplicate-model freshness
fs = flag_summary(year)
dup_counts = {r["rule"]: 0 for r in fs}
for r in fs:
    if r["rule"] in ("fuzzy_new", "cosine_new"):
        dup_counts[r["rule"]] = dup_counts.get(r["rule"], 0) + r["flags"]
n_fuzzy = dup_counts.get("fuzzy_new", 0)
n_cosine = dup_counts.get("cosine_new", 0)

c1, c2, c3 = st.columns([2, 2, 3])
c1.metric("fuzzy_new flags", f"{n_fuzzy:,}")
c2.metric("cosine_new flags", f"{n_cosine:,}")
with c3:
    st.caption("The duplicate signals are read from these two models. If they "
               "look stale or empty, refresh them first.")
    if st.button("↻ Run fuzzy_new + cosine_new now", use_container_width=True):
        with st.spinner("Running the two duplicate models…"):
            added = run_rules(["fuzzy_new", "cosine_new"], year)
        st.success("Added — " + ", ".join(f"{k}: {v}" for k, v in added.items()))
        st.session_state.pop(f"combined::{year}", None)   # force a rebuild

st.divider()

# ----------------------------------------------------------------- build report
build_key = f"combined::{year}"
b1, b2 = st.columns([1, 3])
with b1:
    if st.button(f"▶️ Build combined report — {year}", type="primary",
                 use_container_width=True):
        with st.spinner(f"Scanning {year} voters and fusing signals…"):
            recs = build_combined(year)
        st.session_state[build_key] = recs
        # drop any previously built export bytes for this year
        for k in list(st.session_state):
            if k.startswith(f"cm_pdf::{year}") or k.startswith(f"cm_zip::{year}"):
                del st.session_state[k]
with b2:
    st.caption("Builds the per-voter report for the whole year. Re-run after "
               "enriching more EPICs or refreshing the duplicate models.")

records = st.session_state.get(build_key)
if records is None:
    st.info("Click **Build combined report** to generate the list.")
    st.stop()

if not records:
    st.success("No voter triggered any of the four signals for this year. ✅")
    st.stop()

# ----------------------------------------------------------------- summary
summ = combined_summary(records)
st.subheader(f"{summ['total']:,} voters flagged — {year}")
m = st.columns(4)
m[0].metric("Duplicate leads (fuzzy/cosine)", f"{summ['tier_dup']:,}")
m[1].metric("Logical discrepancy only", f"{summ['tier_logical']:,}")
m[2].metric("No-mapping only", f"{summ['tier_nomap']:,}")
m[3].metric("High severity", f"{summ['high']:,}")

sig = st.columns(4)
sig[0].metric("with cosine_new dup", f"{summ['with_cosine']:,}")
sig[1].metric("with fuzzy_new dup", f"{summ['with_fuzzy']:,}")
sig[2].metric("with a logical issue", f"{summ['with_logical']:,}")
sig[3].metric("no mapping (na)", f"{summ['with_nomap']:,}")

if summ["by_check"]:
    label = {
        "progeny_overload": "Progeny ≥ 6", "father_name_conflict": "Father-name conflict",
        "parent_age_under_15": "Parent gap < 15", "parent_age_over_50": "Parent gap > 50",
        "grandparent_age_le_40": "Grandparent gap ≤ 40", "age_dob_gap": "Roll-age vs DOB-age > 5",
    }
    bc = pd.DataFrame([{"Logical check": label.get(k, k), "Voters": v}
                       for k, v in sorted(summ["by_check"].items(),
                                          key=lambda kv: -kv[1])])
    st.caption("Logical checks breakdown (a voter can hit several):")
    st.dataframe(bc, hide_index=True, use_container_width=True)

# ----------------------------------------------------------------- preview list
st.divider()
st.subheader("Priority list (preview)")
PREVIEW = 300
prev = records[:PREVIEW]
tier_name = {0: "Duplicate lead", 1: "Logical", 2: "No-mapping"}
df = pd.DataFrame([{
    "#": i + 1, "Severity": r["severity"], "Tier": tier_name[r["tier"]],
    "Name (ECINET)": r["name"], "EPIC": r["epic_no"],
    "AC": r["constituency_no"], "Part": r["part_no"], "Serial": r["serial_no"],
    "cosine": len(r["cosine"]), "fuzzy": len(r["fuzzy"]),
    "logical": len(r["logical"]), "no-map": "yes" if r["no_mapping"] else "",
    "Signals": r["signals_summary"],
} for i, r in enumerate(prev)])
st.dataframe(df, hide_index=True, use_container_width=True, height=460)
st.caption(f"Showing the top **{len(prev)}** of **{len(records):,}** — the full "
           "set is in the PDF exports below.")

# ----------------------------------------------------------------- helpers
def _ac_of(rec) -> str:
    return (rec.get("constituency_no") or "").strip() or "(unknown)"

def _by_ac(recs) -> dict:
    d: dict[str, list] = defaultdict(list)
    for r in recs:
        d[_ac_of(r)].append(r)
    return dict(d)

acs = constituencies_in(records)

# ================================================================ FACILITY 1
st.divider()
st.header("📄 Facility 1 — Comprehensive report (PDF)")
st.caption("Every qualifying voter with all findings, methods, reasons and the "
           "full duplicate-comparison logic, in priority order. No per-page "
           "limit; a voter may span pages. Large scopes make large files — use "
           "the per-constituency options for openable PDFs.")

f1 = st.columns([2, 2, 2])
with f1[0]:
    scope1 = st.radio("Scope", ["One constituency", "All qualifying"],
                      key="c_scope1")
with f1[1]:
    ac1 = st.selectbox("Constituency", acs, key="c_ac1",
                       format_func=lambda a: f"AC {a}") if scope1 == "One constituency" else None
with f1[2]:
    topn1 = st.number_input("Limit to top-N (0 = all in scope)", 0, len(records),
                            0, key="c_topn1",
                            help="Highest-priority voters first.")

def _scoped(recs, scope, ac, topn):
    out = recs if scope == "All qualifying" else [r for r in recs if _ac_of(r) == ac]
    return out[:topn] if topn else out

if st.button("🧾 Prepare comprehensive PDF", key="c_prep1"):
    sub = _scoped(records, scope1, ac1, int(topn1))
    lbl = "all constituencies" if scope1 == "All qualifying" else f"AC {ac1}"
    with st.spinner(f"Building comprehensive PDF ({len(sub):,} voters)…"):
        st.session_state[f"cm_pdf::{year}::comp"] = (
            build_comprehensive_pdf(sub, year, scope_label=lbl),
            f"combined_comprehensive_{year}_"
            + (ac1 if ac1 else "all") + (f"_top{topn1}" if topn1 else "") + ".pdf")
if st.session_state.get(f"cm_pdf::{year}::comp"):
    data, fname = st.session_state[f"cm_pdf::{year}::comp"]
    st.download_button(f"⬇️ Download ({len(data)/1024/1024:.1f} MB)", data=data,
                       file_name=fname, mime="application/pdf", key="c_dl1")

with st.expander("🗂️ One PDF per constituency (ZIP)"):
    if st.button("🧾 Prepare per-AC ZIP", key="c_prepzip1"):
        bar = st.progress(0.0, text="Starting…")
        by_ac = _by_ac(records)
        st.session_state[f"cm_zip::{year}::comp"] = build_comprehensive_zip(
            by_ac, year,
            progress=lambda i, n, ac: bar.progress(i / n, text=f"AC {ac} ({i}/{n})"))
        bar.empty()
    if st.session_state.get(f"cm_zip::{year}::comp"):
        st.download_button("⬇️ Download per-AC ZIP",
                           data=st.session_state[f"cm_zip::{year}::comp"],
                           file_name=f"combined_comprehensive_{year}_by_AC.zip",
                           mime="application/zip", key="c_dlzip1")

# ================================================================ FACILITY 2
st.divider()
st.header("🗃️ Facility 2 — Full dossier (PDF)")
st.caption("A complete case file per doubtful voter — every stored data point, "
           "every photo, and the **EF form rendered large** — plus the same full "
           "record for each cosine/fuzzy duplicate. Voters are sequenced "
           "highest-possibility first and split across numbered PDFs (part 01 = "
           "strongest leads).")

f2 = st.columns([2, 2, 2])
with f2[0]:
    scope2 = st.radio("Scope", ["One constituency", "All qualifying"],
                      key="c_scope2")
with f2[1]:
    ac2 = st.selectbox("Constituency", acs, key="c_ac2",
                       format_func=lambda a: f"AC {a}") if scope2 == "One constituency" else None
with f2[2]:
    scope_recs = records if scope2 == "All qualifying" else [r for r in records if _ac_of(r) == ac2]
    count2 = st.number_input("Voters (from top of priority list)", 1,
                             max(1, len(scope_recs)),
                             min(50, len(scope_recs)), key="c_count2")

per_file = st.slider("Voters per PDF file", 5, 100, 40, key="c_perfile",
                     help="Smaller files open faster. Each part is a run of "
                          "voters in priority order.")
st.caption(f"Will build **{int(count2):,}** dossier(s) across "
           f"~**{max(1, (int(count2) + per_file - 1) // per_file)}** PDF file(s). "
           "Dossiers embed many images, so this can take a while.")

if st.button("🗃️ Prepare dossier ZIP (priority-sequenced)", key="c_prep2"):
    sub = scope_recs[:int(count2)]
    lbl = "all" if scope2 == "All qualifying" else f"AC {ac2}"
    bar = st.progress(0.0, text="Starting…")
    zbytes = build_dossier_zip(
        sub, year, per_file=int(per_file), scope_label=lbl,
        progress=lambda i, n, k: bar.progress(i / n, text=f"part {i}/{n} ({k} voters)"))
    bar.empty()
    st.session_state[f"cm_zip::{year}::doss"] = (
        zbytes, f"combined_dossier_{year}_" + (ac2 if ac2 else "all")
        + f"_top{int(count2)}.zip")
if st.session_state.get(f"cm_zip::{year}::doss"):
    data, fname = st.session_state[f"cm_zip::{year}::doss"]
    st.download_button(f"⬇️ Download dossier ZIP ({len(data)/1024/1024:.1f} MB)",
                       data=data, file_name=fname, mime="application/zip",
                       key="c_dl2")

with st.expander("👤 Single voter dossier by EPIC"):
    epic = st.text_input("EPIC number", key="c_epic",
                         placeholder="e.g. BPR0305409").strip().upper()
    if epic:
        match = next((r for r in records if (r["epic_no"] or "").upper() == epic), None)
        if not match:
            st.info("That EPIC is not in the combined report for this year "
                    "(it triggered none of the four signals, or belongs to "
                    "another year).")
        else:
            st.caption(f"Found: **{match['name']}** — {match['signals_summary']}")
            if st.button("🗃️ Prepare this voter's dossier", key="c_prep3"):
                with st.spinner("Building dossier…"):
                    st.session_state[f"cm_pdf::{year}::one"] = (
                        build_dossier_pdf([match], year,
                                          scope_label=f"EPIC {epic}"),
                        f"dossier_{year}_{epic}.pdf")
            if st.session_state.get(f"cm_pdf::{year}::one"):
                data, fname = st.session_state[f"cm_pdf::{year}::one"]
                st.download_button(f"⬇️ Download ({len(data)/1024:.0f} KB)",
                                   data=data, file_name=fname,
                                   mime="application/pdf", key="c_dl3")
