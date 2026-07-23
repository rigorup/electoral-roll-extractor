"""Explore the voter database: search, filter every field, browse photos."""
from __future__ import annotations

import io
import math

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import explore
from auth import require_auth
from dbx import available_years, db_ready, init_schema
from explore import Filters
from fraud_rules import get_photo, get_photos

load_dotenv()

st.set_page_config(page_title="Explore Database", page_icon="🔎", layout="wide")

require_auth()
st.title("🔎 Explore the Voter Database")

ok, msg = db_ready()
if not ok:
    st.error(f"Database not reachable: {msg}")
    st.stop()
init_schema()

PAGE_SIZE = explore.PAGE_SIZE

# ===================================================================== filters
years = available_years()
year_labels = ["All years"] + [str(y) for y in years]

with st.sidebar:
    st.header("Filters")
    year_sel = st.selectbox("Revision year", year_labels, index=1 if years else 0)
    year = None if year_sel == "All years" else int(year_sel)

    opts = explore.filter_options(year)

    acs = st.multiselect("Constituency", opts["acs"])
    parts = st.multiselect("Part number", explore.parts_for(year, acs),
                           help="Choose constituencies first to narrow the list.")
    genders = st.multiselect("Gender", opts["genders"])
    relation_types = st.multiselect("Relation type", opts["relation_types"])
    statuses = st.multiselect("Enrichment status", explore.STATUS_CHOICES,
                              help="'Pending' = never looked up on ECINET.")
    category_types = st.multiselect("Category (enriched)", opts["category_types"])

    lo, hi = int(opts["age_min"]), int(opts["age_max"])
    if lo >= hi:
        hi = lo + 1
    age_min, age_max = st.slider("Age range", lo, hi, (lo, hi))
    age_flt = (age_min != lo) or (age_max != hi)

    c1, c2 = st.columns(2)
    has_mobile = c1.toggle("Has mobile", value=False,
                           help="Only voters whose ECINET record has a mobile "
                                "number.")
    has_photo = c2.toggle("Has photo", value=False)

    if st.button("Reset filters", use_container_width=True):
        for k in list(st.session_state.keys()):
            if k.startswith("exp_"):
                del st.session_state[k]
        st.rerun()

# ---------------------------------------------------------------- search box
query = st.text_input(
    "Search", key="exp_query",
    placeholder="Name, EPIC, relation, house number, or mobile — e.g. "
                "'wangsu', 'BPR0299776', '7085…'")

flt = Filters(
    year=year, acs=acs, parts=parts, genders=genders,
    relation_types=relation_types, category_types=category_types,
    statuses=statuses, has_mobile=has_mobile, has_photo=has_photo,
    query=query,
    age_min=age_min if age_flt else None,
    age_max=age_max if age_flt else None,
)

# A signature of everything that changes the result set. When it changes we
# jump back to page 1 and recompute the (expensive) total once.
sig = str((year, tuple(acs), tuple(parts), tuple(genders), tuple(relation_types),
           tuple(category_types), tuple(statuses), has_mobile, has_photo, query,
           flt.age_min, flt.age_max))
st.session_state.setdefault("exp_page", 1)
if st.session_state.get("exp_sig") != sig:
    st.session_state["exp_sig"] = sig
    st.session_state["exp_page"] = 1
    st.session_state["exp_total"] = explore.count(flt)

total = st.session_state["exp_total"]
pages = max(1, math.ceil(total / PAGE_SIZE))
# Clamp before any page widget is instantiated (safe to assign here).
st.session_state["exp_page"] = max(1, min(st.session_state["exp_page"], pages))

# ---------------------------------------------------------------- toolbar
tb = st.columns([2, 3, 3, 2])
tb[0].metric("Matches", f"{total:,}")
sort = tb[1].selectbox("Sort by", list(explore.SORTS), key="exp_sort")
view = tb[2].radio("View", ["Table", "Gallery"], horizontal=True, key="exp_view")

if total == 0:
    st.info("No voters match these filters. Try widening them or clearing the "
            "search box.")
    st.stop()

# ---------------------------------------------------------------- pagination
# Page moves go through callbacks so button clicks and the jump box share one
# source of truth (st.session_state["exp_page"]). Mutating widget-backed state
# inside a callback is the supported pattern and avoids the number_input
# snapping back over a button press.
def _go(delta_or_target, *, absolute=False) -> None:
    cur = st.session_state["exp_page"]
    nxt = delta_or_target if absolute else cur + delta_or_target
    st.session_state["exp_page"] = max(1, min(int(nxt), pages))

def _jump_cb() -> None:
    st.session_state["exp_page"] = max(1, min(int(st.session_state["exp_jump"]),
                                              pages))

def _pager(suffix: str, *, with_jump: bool) -> None:
    p = st.session_state["exp_page"]
    cols = st.columns([1, 1, 3, 1, 1])
    cols[0].button("⏮ First", disabled=p <= 1, key=f"first_{suffix}",
                   use_container_width=True, on_click=_go, args=(1,),
                   kwargs={"absolute": True})
    cols[1].button("◀ Prev", disabled=p <= 1, key=f"prev_{suffix}",
                   use_container_width=True, on_click=_go, args=(-1,))
    if with_jump:
        # The number_input owns "exp_jump"; its callback pushes into exp_page.
        st.session_state["exp_jump"] = p
        cols[2].number_input(
            f"Page (1–{pages})", min_value=1, max_value=pages, step=1,
            key="exp_jump", on_change=_jump_cb, label_visibility="collapsed")
    else:
        cols[2].markdown(f"<div style='text-align:center;padding-top:6px'>"
                         f"page <b>{p}</b> / {pages}</div>",
                         unsafe_allow_html=True)
    cols[3].button("Next ▶", disabled=p >= pages, key=f"next_{suffix}",
                   use_container_width=True, on_click=_go, args=(1,))
    cols[4].button("Last ⏭", disabled=p >= pages, key=f"last_{suffix}",
                   use_container_width=True, on_click=_go, args=(pages,),
                   kwargs={"absolute": True})

page = st.session_state["exp_page"]
start = (page - 1) * PAGE_SIZE + 1
end = min(page * PAGE_SIZE, total)
st.caption(f"Showing **{start:,}–{end:,}** of **{total:,}** · page "
           f"**{page}** of **{pages}** · {PAGE_SIZE} per page")
_pager("top", with_jump=True)

rows = explore.page_rows(flt, sort=sort, page=page)

# ---------------------------------------------------------------- results
if view == "Table":
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True, height=560)
else:
    photos = get_photos([r["id"] for r in rows])
    per_row = 5
    for i in range(0, len(rows), per_row):
        cols = st.columns(per_row)
        for col, r in zip(cols, rows[i:i + per_row]):
            with col:
                img = photos.get(r["id"])
                if img:
                    st.image(img, use_container_width=True)
                else:
                    st.caption("_no photo_")
                badge = {"Found": "✅", "Not found": "❌"}.get(
                    r["epic_lookup_status"], "")
                st.markdown(f"**{r['name']}** {badge}")
                st.caption(f"{r['epic_no']} · AC {r['constituency_no']}/"
                           f"P{r['part_no']}/#{r['serial_no']}\n\n"
                           f"{r['gender']}, age {r['age']} · {r['relation_type']} "
                           f"{r['relation_name']}")

_pager("bottom", with_jump=False)

# ---------------------------------------------------------------- detail view
st.divider()
st.subheader("Voter detail")
label = {f"{r['name']} — {r['epic_no']} (AC {r['constituency_no']} "
         f"#{r['serial_no']})": r["id"] for r in rows}
pick = st.selectbox("Open a full record from this page", ["—"] + list(label))
if pick != "—":
    v = explore.voter_full(label[pick])
    dc = st.columns([1, 2])
    with dc[0]:
        img = get_photo(v["id"])
        if img:
            st.image(img, caption="Roll photo", use_container_width=True)
        else:
            st.caption("_no roll photo_")
    with dc[1]:
        roll = {k: v[k] for k in (
            "year", "constituency_no", "constituency_name", "part_no",
            "serial_no", "epic_no", "name", "relation_type", "relation_name",
            "house_number", "age", "gender")}
        st.markdown("**Roll record**")
        st.dataframe(pd.DataFrame(roll.items(), columns=["Field", "Value"]),
                     hide_index=True, use_container_width=True)

    if v["epic_lookup_status"] == "Found":
        enr = {k: v[k] for k in (
            "verified_name", "verified_dob", "verified_age", "mobile_no",
            "father_or_guardian_name", "mother_name", "spouse_name",
            "verified_house_no", "verified_part_no", "part_serial_no",
            "part_name", "ac_name", "category_type", "relation_epic",
            "relation_name_verified", "lookup_officer", "epic_lookup_at")
            if v[k] not in (None, "")}
        st.markdown("**ECINET enrichment**")
        st.dataframe(pd.DataFrame(enr.items(), columns=["Field", "Value"]),
                     hide_index=True, use_container_width=True)

        docs = explore.epic_documents(v["epic_no"])
        if docs:
            dcols = st.columns(len(docs))
            for col, d in zip(dcols, docs):
                if d["image"]:
                    col.image(d["image"],
                              caption=f"{d['doc_type']} ({d['bytes']:,} bytes)",
                              use_container_width=True)
    else:
        st.caption(f"Enrichment status: **{v['epic_lookup_status'] or 'Pending'}"
                   "** — no ECINET record fetched yet.")

# ---------------------------------------------------------------- export
st.divider()
with st.expander("⬇️ Export matches to CSV"):
    st.caption("The current page, or all matches up to a safety cap of 5,000 "
               "rows.")
    e1, e2 = st.columns(2)
    page_csv = pd.DataFrame(rows).to_csv(index=False).encode()
    e1.download_button(f"This page ({len(rows)} rows)", page_csv,
                       file_name=f"voters_page{page}.csv", mime="text/csv",
                       use_container_width=True)
    if e2.button("Build CSV of all matches (≤5,000)", use_container_width=True):
        allrows = explore.export_rows(flt, sort=sort, limit=5000)
        buf = io.BytesIO()
        pd.DataFrame(allrows).to_csv(buf, index=False)
        st.download_button(f"Download {len(allrows):,} rows", buf.getvalue(),
                           file_name="voters_filtered.csv", mime="text/csv",
                           use_container_width=True)
        if total > 5000:
            st.caption(f"⚠️ Capped at 5,000 of {total:,} matches. Narrow the "
                       "filters to export a specific slice.")
