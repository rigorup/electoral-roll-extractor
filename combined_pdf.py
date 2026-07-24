"""PDF export for the Combined Model — two facilities.

1. build_comprehensive_pdf  — the *report*: every qualifying voter, in priority
   order, with all findings, methods, reasons and duplicate-comparison logic laid
   out as flowing text (a small identifying photo per voter). No per-page cap;
   one voter can span pages.

2. build_dossier_pdf / build_dossier_zip — the *dossier*: a full case file per
   doubtful voter — every stored data point, every photo, and the EF
   (enumeration) form rendered LARGE so its contents are legible — followed by
   the same complete file for each cosine/fuzzy duplicate of that voter. Voters
   are sequenced highest-possibility first, and the set is split across numbered
   PDFs (part 01 = the strongest leads) so no single file becomes unopenable.

Images: ECINET EF photos are ~0.7 MB PNGs, so they are decoded and re-encoded to
sized JPEGs before embedding (keeps the files openable); the EF form is kept at
high resolution because reading it is the point.
"""
from __future__ import annotations

import io
import zipfile

import fitz

from combined_model import (all_rows_for_epics, documents_for_epics, roll_photos,
                            voter_name)

# ---------------------------------------------------------------- page geometry
PW, PH = 595.28, 841.89          # A4 points
M = 34                           # margin
TOP = 54                         # first content baseline
BOTTOM = PH - 30
CONTENT_W = PW - 2 * M

_SEV_TAG = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
_SEV_COL = {"high": (.78, .13, .13), "medium": (.80, .52, .05),
            "low": (.72, .60, .05)}
_TIER_COL = {0: (.78, .13, .13), 1: (.13, .38, .68), 2: (.35, .35, .40)}

# The verified (ECINET) fields shown as the enrichment block, in a sensible order.
_ENR_FIELDS = [
    ("Verified name", "verified_name"), ("Verified DOB", "verified_dob"),
    ("Verified age", "verified_age"), ("Mobile", "mobile_no"),
    ("Father / guardian", "father_or_guardian_name"), ("Mother", "mother_name"),
    ("Spouse", "spouse_name"), ("Category", "category_type"),
    ("Relation type", "relation_type_code"),
    ("Relation / progeny EPIC", "relation_epic"),
    ("Relation / progeny name", "relation_name_verified"),
    ("Verified house", "verified_house_no"), ("Verified part", "verified_part_no"),
    ("Part serial", "part_serial_no"), ("Part name", "part_name"),
    ("AC name", "ac_name"), ("District", "district_cd"), ("State", "state_cd"),
    ("Survey channel", "survey_channel"),
    ("Submitted for recommendation", "submitted_for_recommendation"),
    ("Aadhaar ref", "aadhaar_ref_no"), ("Enum created", "enum_created_on"),
    ("Enum modified", "enum_modified_on"), ("Lookup officer", "lookup_officer"),
    ("Lookup AC", "lookup_ac_no"), ("Enrichment status", "epic_lookup_status"),
]
_ROLL_FIELDS = [
    ("Year", "year"), ("AC", "constituency_no"), ("AC name", "constituency_name"),
    ("Part", "part_no"), ("Serial", "serial_no"), ("EPIC", "epic_no"),
    ("Roll name", "name"), ("Relation", "relation_type"),
    ("Relation name", "relation_name"), ("House", "house_number"),
    ("Age", "age"), ("Gender", "gender"),
]

_CHECK_LABEL = {
    "progeny_overload": "Progeny overload (>= 6)",
    "father_name_conflict": "Father-name conflict",
    "parent_age_under_15": "Parent age gap < 15",
    "parent_age_over_50": "Parent age gap > 50",
    "grandparent_age_le_40": "Grandparent age gap <= 40",
    "age_dob_gap": "Roll-age vs DOB-age gap > 5",
}


# ---------------------------------------------------------------- text/image utils
def _safe(s) -> str:
    """base-14 fonts are latin-1; normalise the few unicode punctuation marks
    that appear and drop anything else to a safe glyph."""
    s = "" if s is None else str(s)
    for a, b in (("—", " - "), ("–", "-"), ("…", ".."), ("’", "'"),
                 ("‘", "'"), ("“", '"'), ("”", '"'), ("•", "-")):
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def _prep_image(raw: bytes | None, max_side: int = 1000, quality: int = 80):
    """Decode -> resize to <= max_side -> JPEG. Returns (bytes, w, h) or None."""
    if not raw:
        return None
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(raw, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        sc = max_side / float(max(h, w))
        if sc < 1:
            img = cv2.resize(img, (max(1, int(w * sc)), max(1, int(h * sc))),
                             interpolation=cv2.INTER_AREA)
            h, w = img.shape[:2]
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return (buf.tobytes(), w, h) if ok else None
    except Exception:
        return None


def _wrap(text: str, font: str, size: float, maxw: float) -> list[str]:
    """Greedy word-wrap to a pixel width, breaking over-long tokens."""
    out: list[str] = []
    for raw_line in _safe(text).split("\n"):
        words = raw_line.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if fitz.get_text_length(trial, fontname=font, fontsize=size) <= maxw:
                cur = trial
                continue
            if cur:
                out.append(cur)
            # hard-break a single token longer than the line
            while fitz.get_text_length(w, fontname=font, fontsize=size) > maxw and len(w) > 1:
                cut = len(w)
                while cut > 1 and fitz.get_text_length(
                        w[:cut], fontname=font, fontsize=size) > maxw:
                    cut -= 1
                out.append(w[:cut])
                w = w[cut:]
            cur = w
        out.append(cur)
    return out or [""]


# ---------------------------------------------------------------- flow document
class Flow:
    """A paginating A4 document with a top-to-bottom cursor. Every draw call
    breaks to a new page when the content would cross the bottom margin, so a
    single record can flow across as many pages as it needs."""

    def __init__(self, title: str):
        self.doc = fitz.open()
        self.title = _safe(title)
        self.page = None
        self.y = TOP
        self._new_page()

    def _new_page(self):
        self.page = self.doc.new_page(width=PW, height=PH)
        self.page.insert_text((M, 30), self.title, fontsize=9, fontname="hebo",
                              color=(.3, .3, .3))
        self.page.insert_text((PW - M - 60, 30), f"p. {len(self.doc)}",
                              fontsize=8, fontname="helv", color=(.5, .5, .5))
        self.page.draw_line(fitz.Point(M, 38), fitz.Point(PW - M, 38),
                            color=(.7, .7, .7), width=.6)
        self.y = TOP

    def ensure(self, h: float):
        if self.y + h > BOTTOM:
            self._new_page()

    def space(self, h: float = 4):
        self.y += h

    def line(self, color=(.6, .6, .6), width=.6, gap=4):
        self.ensure(gap * 2)
        self.page.draw_line(fitz.Point(M, self.y), fitz.Point(PW - M, self.y),
                            color=color, width=width)
        self.y += gap

    def para(self, text, size=8, font="helv", color=(0, 0, 0), x=M, width=None,
             leading=1.35, gap=1.5):
        width = width or (PW - M - x)
        lh = size * leading
        for ln in _wrap(text, font, size, width):
            self.ensure(lh)
            self.page.insert_text((x, self.y + size), ln, fontsize=size,
                                  fontname=font, color=color)
            self.y += lh
        self.y += gap

    def bullet(self, text, tag=None, tag_col=(0, 0, 0), size=8, x=M):
        """A '- ' bullet with an optional coloured [TAG] prefix; the body wraps
        under a hanging indent."""
        bx = x + 8
        lh = size * 1.35
        prefix = f"[{tag}] " if tag else ""
        lines = _wrap(prefix + _safe(text), "helv", size, PW - M - bx)
        for i, ln in enumerate(lines):
            self.ensure(lh)
            if i == 0:
                self.page.insert_text((x, self.y + size), "-", fontsize=size,
                                      fontname="hebo", color=(.4, .4, .4))
                if tag:
                    self.page.insert_text((bx, self.y + size), f"[{tag}]",
                                          fontsize=size, fontname="hebo",
                                          color=tag_col)
                    off = fitz.get_text_length(f"[{tag}] ", fontname="hebo",
                                               fontsize=size)
                    body = ln[len(f"[{tag}] "):]
                    self.page.insert_text((bx + off, self.y + size), body,
                                          fontsize=size, fontname="helv")
                else:
                    self.page.insert_text((bx, self.y + size), ln, fontsize=size,
                                          fontname="helv")
            else:
                self.page.insert_text((bx, self.y + size), ln, fontsize=size,
                                      fontname="helv")
            self.y += lh
        self.y += 1

    def heading(self, text, size=9.5, color=(.1, .1, .1), fill=None, gap_before=5):
        self.space(gap_before)
        self.ensure(size + 6)
        if fill:
            self.page.draw_rect(fitz.Rect(M, self.y, PW - M, self.y + size + 4),
                                fill=fill, color=fill)
            self.page.insert_text((M + 3, self.y + size), _safe(text),
                                  fontsize=size, fontname="hebo", color=(1, 1, 1))
            self.y += size + 6
        else:
            self.page.insert_text((M, self.y + size), _safe(text), fontsize=size,
                                  fontname="hebo", color=color)
            self.y += size + 3

    def kv_block(self, pairs, size=7.5, label_w=118, x=M, width=None):
        """A list of (label, value) rows: bold label column, wrapped value."""
        width = width or (PW - M - x)
        vx = x + label_w
        lh = size * 1.32
        for label, val in pairs:
            val = "-" if val in (None, "") else str(val)
            lines = _wrap(val, "helv", size, width - label_w)
            self.ensure(lh)
            self.page.insert_text((x, self.y + size), _safe(label), fontsize=size,
                                  fontname="hebo", color=(.25, .25, .25))
            for i, ln in enumerate(lines):
                if i:
                    self.ensure(lh)
                self.page.insert_text((vx, self.y + size), ln, fontsize=size,
                                      fontname="helv")
                self.y += lh
        self.y += 1

    def comparison_table(self, comp, size=6.8):
        """The side-by-side {attribute, a, b, similarity, status} logic table."""
        if not comp:
            return
        c_attr, c_match = 120, 78
        c_val = (CONTENT_W - c_attr - c_match) / 2
        xs = [M, M + c_attr, M + c_attr + c_val, M + c_attr + c_val * 2]
        ws = [c_attr, c_val, c_val, c_match]
        rh = size * 1.55

        def clip(s, w):
            s = _safe("-" if s is None else str(s))
            while s and fitz.get_text_length(s, fontname="helv", fontsize=size) > w - 3:
                s = s[:-1]
            return s

        self.ensure(rh + 2)
        self.page.draw_rect(fitz.Rect(M, self.y, PW - M, self.y + rh),
                            fill=(.92, .92, .95), color=(.8, .8, .8), width=.3)
        for x, t in zip(xs, ("Attribute", "Voter A", "Voter B", "Match")):
            self.page.insert_text((x + 2, self.y + size + 1), t, fontsize=size,
                                  fontname="hebo")
        self.y += rh
        for row in comp:
            status = row.get("status", "")
            sim = row.get("similarity")
            match = status + (f" {sim}" if sim is not None else "")
            cells = [row.get("attribute", ""), row.get("a"), row.get("b"), match]
            cols = [(0, 0, 0), (0, 0, 0), (0, 0, 0),
                    _SEV_COL.get("high") if status == "differ"
                    else (.11, .47, .11) if status in ("exact", "strong")
                    else (.3, .3, .3)]
            fonts = ["helv", "helv", "helv", "hebo"]
            self.ensure(rh)
            for x, w, txt, fnt, col in zip(xs, ws, cells, fonts, cols):
                self.page.insert_text((x + 2, self.y + size + 1), clip(txt, w),
                                      fontsize=size, fontname=fnt, color=col)
            self.page.draw_line(fitz.Point(M, self.y + rh), fitz.Point(PW - M, self.y + rh),
                                color=(.92, .92, .92), width=.25)
            self.y += rh
        self.y += 2

    def image(self, raw, box_w, box_h, caption=None, max_side=1000, quality=80,
              x=None):
        """Fit an image inside box_w x box_h (preserving aspect), paginating if
        it will not fit in the space left. Returns the width actually used."""
        prep = _prep_image(raw, max_side=max_side, quality=quality)
        cap_h = 9 if caption else 0
        if not prep:
            if caption:
                self.ensure(box_h + cap_h)
                self.page.draw_rect(fitz.Rect(M, self.y, M + 60, self.y + 40),
                                    color=(.8, .8, .8), width=.5)
                self.page.insert_text((M + 4, self.y + 52), _safe(caption),
                                      fontsize=7, fontname="helv", color=(.5, .5, .5))
                self.y += 40 + cap_h
            return 0
        data, iw, ih = prep
        sc = min(box_w / iw, box_h / ih, 1.0) if iw and ih else 1.0
        dw, dh = iw * sc, ih * sc
        self.ensure(dh + cap_h + 4)
        ix = M if x is None else x
        rect = fitz.Rect(ix, self.y, ix + dw, self.y + dh)
        try:
            self.page.insert_image(rect, stream=data, keep_proportion=True)
        except Exception:
            self.page.draw_rect(rect, color=(.8, .8, .8), width=.5)
        self.y += dh
        if caption:
            self.page.insert_text((ix, self.y + 7), _safe(caption), fontsize=6.5,
                                  fontname="helv", color=(.45, .45, .45))
            self.y += cap_h
        self.y += 3
        return dw

    def image_row(self, items, box_h, gap=8, max_side=700, quality=78):
        """Lay several (raw, caption) images left-to-right on one band."""
        items = [it for it in items if it and it[0]]
        if not items:
            return
        preps = [(_prep_image(r, max_side=max_side, quality=quality), cap)
                 for r, cap in items]
        preps = [(p, cap) for p, cap in preps if p]
        if not preps:
            return
        # scale each to box_h, then place; wrap to a new band if the row overflows
        self.ensure(box_h + 12)
        x = M
        row_top = self.y
        maxh = 0
        for prep, cap in preps:
            data, iw, ih = prep
            sc = min(box_h / ih, 1.0)
            dw, dh = iw * sc, ih * sc
            if x + dw > PW - M and x > M:
                self.y = row_top + maxh + 12
                self.ensure(box_h + 12)
                x, row_top, maxh = M, self.y, 0
            rect = fitz.Rect(x, row_top, x + dw, row_top + dh)
            try:
                self.page.insert_image(rect, stream=data, keep_proportion=True)
            except Exception:
                self.page.draw_rect(rect, color=(.8, .8, .8), width=.5)
            if cap:
                self.page.insert_text((x, row_top + dh + 7), _safe(cap),
                                      fontsize=6.5, fontname="helv",
                                      color=(.45, .45, .45))
            maxh = max(maxh, dh + 9)
            x += dw + gap
        self.y = row_top + maxh + 4

    def bytes(self) -> bytes:
        return self.doc.tobytes(deflate=True, garbage=3)


# ---------------------------------------------------------------- shared bits
def _voter_header(flow: Flow, rec: dict, idx: int):
    tier_col = _TIER_COL.get(rec["tier"], (.3, .3, .3))
    flow.space(4)
    flow.ensure(20)
    flow.page.draw_rect(fitz.Rect(M, flow.y, PW - M, flow.y + 16),
                        fill=(.96, .96, .98), color=tier_col, width=.7)
    head = (f"#{idx}  [{_SEV_TAG.get(rec['severity'], '')}]  {rec['name']}  "
            f"({rec['epic_no'] or 'no EPIC'})")
    flow.page.insert_text((M + 4, flow.y + 11.5), _safe(head), fontsize=9.5,
                          fontname="hebo", color=tier_col)
    right = f"{rec['tier_label']}"
    rw = fitz.get_text_length(_safe(right), fontname="hebo", fontsize=7.5)
    flow.page.insert_text((PW - M - rw - 4, flow.y + 11.5), _safe(right),
                          fontsize=7.5, fontname="hebo", color=tier_col)
    flow.y += 19
    v = rec["voter"]
    meta = (f"AC {rec['constituency_no'] or '?'} · Part {rec['part_no'] or '?'} · "
            f"Serial {rec['serial_no'] if rec['serial_no'] is not None else '?'} · "
            f"House {rec['house_number'] or '?'} · Age {rec['age'] if rec['age'] is not None else '?'} · "
            f"{rec['gender'] or '?'}"
            + (f" · roll name '{rec['roll_name']}'" if rec['roll_name'] and rec['roll_name'] != rec['name'] else ""))
    flow.para(meta, size=7.5, color=(.3, .3, .3))
    if rec["signals_summary"]:
        flow.para("Signals: " + rec["signals_summary"], size=7.5, font="hebo",
                  color=(.2, .2, .2))


def _findings_section(flow: Flow, rec: dict):
    if rec["logical"]:
        flow.heading("Logical discrepancies", size=8.5, color=(.55, .18, .18))
        for f in rec["logical"]:
            label = _CHECK_LABEL.get(f["check"], f["check"])
            flow.bullet(f"{label}: {f['how']}", tag=_SEV_TAG.get(f["severity"]),
                        tag_col=_SEV_COL.get(f["severity"], (0, 0, 0)))
    if rec["no_mapping"]:
        flow.heading("No mapping in categories", size=8.5, color=(.35, .35, .4))
        flow.bullet("ECINET category_type = 'na' — this voter could not be mapped "
                    "to a self or progeny SIR entry.", tag="LOW",
                    tag_col=_SEV_COL["low"])


def _dups_section(flow: Flow, rec: dict, comprehensive: bool):
    for label, key, colour in (("Cosine duplicates (cosine_new)", "cosine", (.10, .40, .55)),
                               ("Fuzzy duplicates (fuzzy_new)", "fuzzy", (.20, .45, .20))):
        dups = rec[key]
        if not dups:
            continue
        flow.heading(f"{label} — {len(dups)}", size=8.5, color=colour)
        for k, d in enumerate(dups, 1):
            metric = d.get("metric")
            head = (f"Duplicate {k}: {d.get('partner_name') or '?'} "
                    f"({d.get('partner_epic') or 'no EPIC'}) — "
                    f"{'cosine' if key == 'cosine' else 'fuzzy'} match "
                    f"{metric if metric is not None else d.get('score')}")
            flow.para(head, size=8, font="hebo", color=colour)
            if d.get("reason"):
                flow.para("Why: " + d["reason"], size=7.5, color=(.3, .3, .3))
            flow.comparison_table(d.get("comparison") or [])


# ---------------------------------------------------------------- comprehensive
def build_comprehensive_pdf(records: list[dict], year: int,
                            scope_label: str = "all constituencies",
                            with_photo: bool = True,
                            start_index: int = 1) -> bytes:
    """Facility 1 — the full report of every qualifying voter and every reason,
    method and duplicate-comparison, in priority order. `start_index` continues
    the running voter number across chunked parts."""
    flow = Flow(f"Combined Model — comprehensive report — {year} — {scope_label}")
    flow.para(f"{len(records)} voter(s) with at least one signal, ordered "
              f"highest-possibility first (fuzzy/cosine duplicate leads, then "
              f"logical discrepancies, then no-mapping).", size=8,
              color=(.3, .3, .3))
    flow.line()

    photos = roll_photos([r["voter_id"] for r in records]) if with_photo else {}

    for idx, rec in enumerate(records, start_index):
        _voter_header(flow, rec, idx)
        if with_photo:
            ph = photos.get(rec["voter_id"])
            if ph:
                flow.image(ph, box_w=70, box_h=84, caption="roll photo",
                           max_side=300, quality=70)
        _findings_section(flow, rec)
        _dups_section(flow, rec, comprehensive=True)
        flow.line(color=(.75, .75, .8), width=.7, gap=6)

    if not records:
        flow.para("No voters matched any signal for this scope.", size=10)
    return flow.bytes()


# ---------------------------------------------------------------- dossier
def _voter_all_data(flow: Flow, epic: str, rows_by_epic: dict):
    """Every stored voter row (per year) + the ECINET enrichment, in full."""
    rows = rows_by_epic.get(epic or "", [])
    if not rows:
        flow.para("No stored roll record for this EPIC.", size=8, color=(.5, .5, .5))
        return
    flow.para("Roll records (all revision years):", size=8, font="hebo",
              color=(.25, .25, .25))
    for r in rows:
        flow.kv_block([(lbl, r.get(col)) for lbl, col in _ROLL_FIELDS], size=7)
        flow.space(1)
    enriched = next((r for r in rows if r.get("epic_lookup_status") == "Found"), rows[0])
    pairs = [(lbl, enriched.get(col)) for lbl, col in _ENR_FIELDS
             if enriched.get(col) not in (None, "")]
    if pairs:
        flow.para("ECINET enrichment:", size=8, font="hebo", color=(.25, .25, .25))
        flow.kv_block(pairs, size=7)


def _voter_media(flow: Flow, epic: str, voter_ids: list[int], photos: dict,
                 docs_by_epic: dict):
    """All images for a voter: roll photo(s), the ECINET photo, and the EF form
    (enumeration form) rendered LARGE for legibility."""
    roll = [(photos.get(vid), f"roll photo (voter {vid})") for vid in voter_ids
            if photos.get(vid)]
    docs = docs_by_epic.get(epic or "", [])
    ecinet_photo = next((d["image"] for d in docs if d["doc_type"] == "photo"), None)
    ef_form = next((d["image"] for d in docs if d["doc_type"] == "sr_form"), None)

    band = [it for it in roll]
    if ecinet_photo:
        band.append((ecinet_photo, "ECINET photo"))
    if band:
        flow.para("Photos:", size=8, font="hebo", color=(.25, .25, .25))
        flow.image_row(band, box_h=110, max_side=420, quality=78)

    if ef_form:
        flow.para("EF form (enumeration form):", size=8, font="hebo",
                  color=(.25, .25, .25))
        # large: full content width, up to most of a page — this is the point.
        flow.image(ef_form, box_w=CONTENT_W, box_h=560, caption=None,
                   max_side=1700, quality=82)
    else:
        flow.para("EF form: not stored for this EPIC.", size=7.5, color=(.5, .5, .5))


def build_dossier_pdf(records: list[dict], year: int,
                      scope_label: str = "", start_index: int = 1) -> bytes:
    """Facility 2 — a full case file per doubtful voter (all data + all photos +
    large EF form), followed by the same for every cosine/fuzzy duplicate of the
    voter. One voter per fresh page; spans multiple pages as needed.

    `records` is expected already in priority order; `start_index` continues the
    running voter number across chunked parts."""
    title = f"Combined Model — full dossier — {year}" + (f" — {scope_label}" if scope_label else "")
    flow = Flow(title)

    # Gather every EPIC and voter id we will show (primary + all duplicates),
    # then batch-load their rows, documents and roll photos once.
    epics: set[str] = set()
    vids: set[int] = set()
    for rec in records:
        if rec["epic_no"]:
            epics.add(rec["epic_no"])
        vids.add(rec["voter_id"])
        for d in rec["cosine"] + rec["fuzzy"]:
            if d.get("partner_epic"):
                epics.add(d["partner_epic"])
            if d.get("partner_id"):
                vids.add(d["partner_id"])
    rows_by_epic = all_rows_for_epics(epics)
    docs_by_epic = documents_for_epics(epics)
    # include every year-instance voter id so all roll photos are available
    for rws in rows_by_epic.values():
        for r in rws:
            vids.add(r["id"])
    photos = roll_photos(vids)

    def epic_vids(epic):
        return [r["id"] for r in rows_by_epic.get(epic or "", [])]

    for offset, rec in enumerate(records):
        idx = start_index + offset
        if offset:
            flow._new_page()          # one voter dossier per fresh page
        _voter_header(flow, rec, idx)
        _findings_section(flow, rec)

        flow.heading("Primary voter — all stored data", size=9,
                     fill=(.20, .28, .40))
        _voter_all_data(flow, rec["epic_no"], rows_by_epic)
        _voter_media(flow, rec["epic_no"], epic_vids(rec["epic_no"]) or [rec["voter_id"]],
                     photos, docs_by_epic)

        dups = rec["cosine"] + rec["fuzzy"]
        if dups:
            flow.heading(f"Duplicate voters — {len(dups)} (full record for each)",
                         size=9, fill=(.55, .20, .20))
            for k, d in enumerate(dups, 1):
                metric = d.get("metric")
                flow.heading(
                    f"Duplicate {k}: {d.get('partner_name') or '?'} "
                    f"({d.get('partner_epic') or 'no EPIC'}) — "
                    f"{d['model']} match {metric if metric is not None else d.get('score')}",
                    size=8.5, color=(.5, .18, .18))
                if d.get("reason"):
                    flow.para("Why: " + d["reason"], size=7.5, color=(.3, .3, .3))
                flow.comparison_table(d.get("comparison") or [])
                _voter_all_data(flow, d.get("partner_epic"), rows_by_epic)
                _voter_media(flow, d.get("partner_epic"),
                             epic_vids(d.get("partner_epic")) or ([d["partner_id"]] if d.get("partner_id") else []),
                             photos, docs_by_epic)

    if not records:
        flow.para("No voters matched any signal for this scope.", size=10)
    return flow.bytes()


def build_dossier_zip(records: list[dict], year: int, per_file: int = 50,
                      scope_label: str = "", progress=None) -> bytes:
    """Split the dossier across numbered PDFs (part 01 = strongest leads), so no
    single file becomes too large to open. At most `per_file` voters per PDF
    (capped at 50). Returns a ZIP of the parts."""
    per_file = max(1, min(int(per_file), 50))
    buf = io.BytesIO()
    n = len(records)
    parts = max(1, (n + per_file - 1) // per_file)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in range(parts):
            chunk = records[p * per_file:(p + 1) * per_file]
            if progress:
                progress(p + 1, parts, len(chunk))
            pdf = build_dossier_pdf(chunk, year, scope_label=scope_label,
                                    start_index=p * per_file + 1)
            lo = p * per_file + 1
            hi = p * per_file + len(chunk)
            z.writestr(f"dossier_part{p + 1:02d}_rank{lo:05d}-{hi:05d}.pdf", pdf)
    return buf.getvalue()


def build_comprehensive_zip(records_by_ac: dict[str, list[dict]], year: int,
                            progress=None) -> bytes:
    """One comprehensive report PDF per constituency, bundled into a ZIP."""
    buf = io.BytesIO()
    acs = list(records_by_ac)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, ac in enumerate(acs, 1):
            if progress:
                progress(i, len(acs), ac)
            recs = records_by_ac[ac]
            safe = str(ac).replace("/", "-").replace(" ", "")
            z.writestr(f"combined_{year}_AC{safe}.pdf",
                       build_comprehensive_pdf(recs, year, scope_label=f"AC {ac}"))
    return buf.getvalue()


def build_comprehensive_zip_chunked(records: list[dict], year: int,
                                    per_file: int = 50,
                                    scope_label: str = "all constituencies",
                                    progress=None) -> bytes:
    """Split the comprehensive report across numbered PDFs of at most `per_file`
    voters each (capped at 50; part 01 = strongest leads), so no single file is
    too large to download or open. Voters stay in the same priority order across
    parts and keep a continuous rank number. Returns a ZIP of the parts."""
    per_file = max(1, min(int(per_file), 50))
    buf = io.BytesIO()
    n = len(records)
    parts = max(1, (n + per_file - 1) // per_file)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in range(parts):
            chunk = records[p * per_file:(p + 1) * per_file]
            if progress:
                progress(p + 1, parts, len(chunk))
            lo = p * per_file + 1
            hi = p * per_file + len(chunk)
            label = f"{scope_label} — part {p + 1}/{parts} (rank {lo}-{hi})"
            pdf = build_comprehensive_pdf(chunk, year, scope_label=label,
                                          start_index=lo)
            z.writestr(
                f"comprehensive_part{p + 1:02d}_rank{lo:05d}-{hi:05d}.pdf", pdf)
    return buf.getvalue()
