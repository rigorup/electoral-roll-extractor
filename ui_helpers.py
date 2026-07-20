"""Shared Streamlit pieces for the review pages: flag cards, the grouped
house_overload view with family-tree reconstruction, and infinite scroll."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from family import analyse_household, cluster_dot
from fraud_rules import (all_flags_for_export, get_photo, get_photos,
                         house_members)

# ---------------------------------------------------------------- infinite scroll
PAGE_STEP = 100

_autoload = components.declare_component(
    "autoload", path=str(Path(__file__).parent / "components" / "autoload"))


def infinite_limit(state_key: str) -> int:
    """How many rows to fetch right now for this list."""
    return PAGE_STEP * (1 + st.session_state.get(state_key, 0))


def infinite_scroll_sentinel(state_key: str, has_more: bool) -> None:
    """Place at the very bottom of the list. Scrolling it into view bumps the
    page counter and reruns, which loads PAGE_STEP more rows."""
    fire = _autoload(has_more=has_more, key=f"sentinel::{state_key}", default=0)
    if has_more:
        st.caption("Loading more as you scroll…")
        # The component sends a fresh timestamp each time the sentinel is
        # visible; any value we haven't seen yet means "load another page".
        seen_key = f"{state_key}::seen"
        if fire and fire != st.session_state.get(seen_key):
            st.session_state[seen_key] = fire
            st.session_state[state_key] = st.session_state.get(state_key, 0) + 1
            st.rerun()
    else:
        st.caption("— end of list —")


# ---------------------------------------------------------------- flag cards
def flag_title(f) -> str:
    sev_icon = {"high": "🔴", "medium": "🟠"}.get(f["severity"], "🟡")
    d = f.get("details") or {}
    if f["rule"] == "house_overload" and d.get("house_norm"):
        return (f"{sev_icon} **house_overload** — House {d.get('house') or '?'} "
                f"(AC {d.get('constituency_no') or '?'}) — "
                f"{d.get('occupants', '?')} electors")
    return (f"{sev_icon} **{f['rule']}** — {f['name_a']} "
            f"({f['epic_a'] or 'no EPIC'})"
            + (f"  ↔  {f['name_b']} ({f['epic_b'] or 'no EPIC'})"
               if f["name_b"] else ""))


def _voter_md(f, side: str) -> str:
    return (f"**{f['name_' + side]}**  \n"
            f"EPIC: `{f['epic_' + side]}`  \n"
            f"AC {f['const_' + side] or '?'} · Part {f['part_' + side]} · "
            f"Serial {f['serial_' + side] if f['serial_' + side] is not None else '?'}  \n"
            f"House {f['house_' + side]}  \n"
            f"Age {f['age_' + side]} · {f['gender_' + side]}")


def flag_card(f, year: int | None = None) -> None:
    """Body of one flag expander: pair of voter cards, or — for a grouped
    house_overload flag — every occupant plus the reconstructed family tree."""
    d = f.get("details") or {}
    if f["rule"] == "house_overload" and d.get("house_norm"):
        _house_overload_card(f, d, year)
        return

    cols = st.columns([2, 1, 2, 1]) if f["name_b"] else st.columns([2, 1])
    cols[0].markdown(_voter_md(f, "a"))
    pa = get_photo(f["voter_id"])
    if pa:
        cols[1].image(pa, width=110)
    if f["name_b"]:
        cols[2].markdown(_voter_md(f, "b"))
        pb = get_photo(f["related_voter_id"])
        if pb:
            cols[3].image(pb, width=110)
    st.json(f["details"], expanded=False)


def _house_overload_card(f, d: dict, year: int | None = None) -> None:
    members = house_members(d.get("constituency_no"), d["house_norm"], year)
    if not members:
        st.warning("No electors found for this house any more (data re-ingested?).")
        st.json(d, expanded=False)
        return

    hh = analyse_household(members)
    by_id = {m["id"]: m for m in hh.members}

    st.markdown(f"### 🏠 House `{d.get('house') or f['house_a']}` — "
                f"AC {d.get('constituency_no') or '?'} — "
                f"**{len(members)} electors** at this address")
    for line in hh.signals:
        st.markdown(f"- {line}")

    # ---- reconstructed family groups (tree per group)
    fams = [c for c in hh.clusters if len(c) >= 2]
    if fams:
        st.markdown("**Family groups** (arrows: parent → child, "
                    "purple line: spouses, red: anomaly):")
        for i, cluster in enumerate(fams, 1):
            with st.expander(f"Family group {i} — {len(cluster)} members",
                             expanded=len(fams) <= 3):
                st.graphviz_chart(cluster_dot(hh, cluster))

    # ---- the prime suspects: nobody in the house is family to them
    if hh.unlinked:
        st.markdown("**⚠️ Unattached electors** — no family link to anyone "
                    "here (verify these first):")
        st.dataframe(_members_df([by_id[v] for v in hh.unlinked], hh),
                     use_container_width=True, hide_index=True)

    # ---- everyone, grouped, in one table
    with st.expander(f"All {len(members)} electors in this house", expanded=False):
        group_of = {vid: i for i, c in enumerate(hh.clusters, 1)
                    for vid in c if len(c) >= 2}
        df = _members_df(hh.members, hh)
        df.insert(0, "Family", [group_of.get(m["id"], "—") for m in hh.members])
        st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------- PDF export
_PDF_PER_PAGE = 5           # comparison blocks per A4 page
_A4 = (595.28, 841.89)      # points


def _pdf_voter_lines(f, side: str) -> list[str]:
    """The same fields the review card shows, one voter, as text lines."""
    g = lambda k: f.get(f"{k}_{side}")
    serial = g("serial")
    rel = f"{g('relation_type') or ''} {g('relation_name') or ''}".strip()
    return [
        (g("name") or "—"),
        f"EPIC: {g('epic') or 'no EPIC'}",
        f"AC {g('const') or '?'} · Part {g('part') or '?'} · "
        f"Serial {serial if serial is not None else '?'}",
        f"House {g('house') or '?'} · Age "
        f"{g('age') if g('age') is not None else '?'} · {g('gender') or '?'}",
        f"Relation: {rel or '—'}",
    ]


def _pdf_draw_voter(page, x: float, y: float, w: float, h: float,
                    lines: list[str], photo: bytes | None) -> None:
    """One voter panel: photo on the left, detail lines to its right."""
    import fitz
    pw, ph = 46, 56
    prect = fitz.Rect(x + 3, y + 3, x + 3 + pw, y + 3 + ph)
    if photo:
        try:
            page.insert_image(prect, stream=photo, keep_proportion=True)
        except Exception:
            page.draw_rect(prect, color=(.7, .7, .7), width=.5)
    else:
        page.draw_rect(prect, color=(.8, .8, .8), width=.5)
        page.insert_textbox(prect, "no\nphoto", fontsize=6,
                            color=(.5, .5, .5), align=fitz.TEXT_ALIGN_CENTER)
    trect = fitz.Rect(x + 3 + pw + 5, y + 1, x + w - 3, y + h - 1)
    # first line (name) bold, rest regular — draw name then the block below it
    page.insert_textbox(trect, lines[0] + "\n", fontsize=8, fontname="hebo")
    body = fitz.Rect(trect.x0, trect.y0 + 11, trect.x1, trect.y1)
    page.insert_textbox(body, "\n".join(lines[1:]), fontsize=7, fontname="helv")


def build_flags_pdf(rule_filter: str | None, year: int | None = None) -> bytes:
    """PDF of every flag matching the filter: each flag is one side-by-side
    comparison (voter A vs voter B) with both photos and all details;
    _PDF_PER_PAGE comparisons per A4 page."""
    import fitz
    rows = all_flags_for_export(rule_filter, year)
    ids = set()
    for f in rows:
        ids.add(f["voter_id"])
        if f["related_voter_id"]:
            ids.add(f["related_voter_id"])
    photos = get_photos(ids)

    doc = fitz.open()
    pw, phg = _A4
    M, top = 28, 52
    usable_h = phg - top - M
    row_h = usable_h / _PDF_PER_PAGE
    col_w = (pw - 2 * M) / 2
    sev_icon = {"high": "[HIGH]", "medium": "[MED]", "low": "[LOW]"}

    page = None
    for i, f in enumerate(rows):
        slot = i % _PDF_PER_PAGE
        if slot == 0:
            page = doc.new_page(width=pw, height=phg)
            page.insert_textbox(
                fitz.Rect(M, 20, pw - M, 44),
                f"Fraud flags - {rule_filter or 'all rules'}"
                f"{f' - {year}' if year else ''}   "
                f"(page {len(doc)},  {len(rows)} flag(s) total)",
                fontsize=11, fontname="hebo")
            page.draw_line(fitz.Point(M, 46), fitz.Point(pw - M, 46),
                           color=(.6, .6, .6), width=.7)

        y0 = top + slot * row_h
        page.draw_rect(fitz.Rect(M, y0, pw - M, y0 + row_h - 4),
                       color=(.85, .85, .85), width=.5)
        d = f.get("details") or {}
        extra = ""
        if d.get("name_sim") is not None:
            extra = f"   name_sim={d['name_sim']}"
        elif d.get("cosine") is not None:
            extra = f"   cosine={d['cosine']}"
        score = f["score"]
        verdict = f" · {f['verdict']}" if f.get("verdict") else ""
        page.insert_text(
            fitz.Point(M + 4, y0 + 10),
            f"{sev_icon.get(f['severity'], '')} {f['rule']}"
            f"   score={round(score, 3) if score is not None else '?'}{extra}{verdict}",
            fontsize=8, fontname="hebo", color=(.15, .15, .15))

        body_y = y0 + 14
        body_h = row_h - 4 - 14
        _pdf_draw_voter(page, M, body_y, col_w, body_h,
                        _pdf_voter_lines(f, "a"), photos.get(f["voter_id"]))
        # vertical divider
        page.draw_line(fitz.Point(M + col_w, body_y + 1),
                       fitz.Point(M + col_w, y0 + row_h - 6),
                       color=(.8, .8, .8), width=.5)
        if f["name_b"]:
            _pdf_draw_voter(page, M + col_w, body_y, col_w, body_h,
                            _pdf_voter_lines(f, "b"),
                            photos.get(f["related_voter_id"]))
        else:
            note = "House-overload group"
            if d.get("occupants"):
                note += f" - {d['occupants']} electors at House {d.get('house') or '?'}"
            page.insert_textbox(
                fitz.Rect(M + col_w + 6, body_y + 4, pw - M - 3, y0 + row_h - 6),
                note, fontsize=8, fontname="helv", color=(.4, .4, .4))

    if page is None:                                   # no flags at all
        page = doc.new_page(width=pw, height=phg)
        page.insert_textbox(fitz.Rect(M, 40, pw - M, 80),
                            "No flags to export.", fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


def _members_df(members: list, hh) -> "pd.DataFrame":
    return pd.DataFrame([{
        "Serial": m.get("serial_no"),
        "Part": m.get("part_no"),
        "Name": m.get("name"),
        "Age": m.get("age"),
        "G": m.get("gender"),
        "Relation": f"{m.get('relation_type') or ''} {m.get('relation_name') or ''}".strip(),
        "EPIC": m.get("epic_no"),
        "Notes": "; ".join(hh.anomalies.get(m["id"], [])),
    } for m in members])
