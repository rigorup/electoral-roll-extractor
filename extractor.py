"""Turn per-page OCR text into structured voter records.

Strategies:
  * "regex" -> local parsing tuned to how Mistral OCR actually renders roll
               pages (markdown tables, one voter per cell, fields inline).
  * "llm"   -> Mistral chat model returns strict JSON per page, with retries.
               Falls back to regex per page and keeps whichever found more.
Both paths are designed to never raise: worst case they return fewer rows.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from ocr_providers import PageText

# ---------------------------------------------------------------- data model
COLUMNS = [
    "Constituency_No", "Constituency_Name", "Part_No",
    "Serial_No", "EPIC_No", "Name",
    "Relation_Type", "Relation_Name",
    "House_Number", "Age", "Gender", "Page", "Photo_Path", "Photo_Id",
]

# Fields every genuine voter record must have (photo fields excluded). Used by
# the repair pass to decide a record is "incomplete" and needs another parse.
REQUIRED_FIELDS = [
    "Serial_No", "EPIC_No", "Name", "Relation_Type",
    "Relation_Name", "House_Number", "Age", "Gender",
]


@dataclass
class Voter:
    Serial_No: str = ""
    EPIC_No: str = ""
    Name: str = ""
    Relation_Type: str = ""
    Relation_Name: str = ""
    House_Number: str = ""
    Age: str = ""
    Gender: str = ""
    Page: int = 0
    Photo_Path: str = ""
    Photo_Id: str = ""


def _serial_key(v: "Voter") -> int:
    return int(v.Serial_No) if v.Serial_No.isdigit() else 10 ** 9


def _completeness(v: "Voter") -> int:
    return sum(1 for f in REQUIRED_FIELDS if getattr(v, f))


def _combine(a: "Voter", b: "Voter") -> "Voter":
    """Merge two records for the same voter: keep the more complete one and
    fill any still-empty field from the other. Never loses data."""
    base, other = (a, b) if _completeness(a) >= _completeness(b) else (b, a)
    for f in REQUIRED_FIELDS + ["Photo_Path", "Photo_Id"]:
        if not getattr(base, f) and getattr(other, f):
            setattr(base, f, getattr(other, f))
    if not base.Page:
        base.Page = other.Page
    return base


def _best_by_serial(voter_lists: list[list["Voter"]]) -> dict[str, "Voter"]:
    """Union several parses of the same content, keyed by serial number,
    keeping the most complete record for each (fields filled across parses)."""
    best: dict[str, "Voter"] = {}
    for vl in voter_lists:
        for v in vl:
            key = v.Serial_No if v.Serial_No.isdigit() else f"n:{v.Name}:{v.Page}"
            best[key] = _combine(best[key], v) if key in best else v
    return best


# ---------------------------------------------------------------- header bits
_CONST_RE = re.compile(
    r"Assembly\s*Constituency\s*No\.?\s*and\s*Name\s*[:：]?\s*(\d+)\s*[-–]\s*([A-Za-z ().]+)",
    re.I,
)
_PART_RE = re.compile(r"Part\s*No\.?\s*[:：]?\s*(\d+)", re.I)


def extract_header(pages: list[PageText]) -> dict:
    """Constituency number/name + part number from anywhere in the document."""
    joined = "\n".join(p.markdown for p in pages)
    const_no = const_name = part_no = ""
    m = _CONST_RE.search(joined)
    if m:
        const_no = m.group(1).strip()
        const_name = m.group(2).strip().rstrip(" .")
    mp = _PART_RE.search(joined)
    if mp:
        part_no = mp.group(1).strip()
    return {
        "Constituency_No": const_no,
        "Constituency_Name": const_name,
        "Part_No": part_no,
    }


# ---------------------------------------------------------------- regex parse
#
# Mistral OCR renders roll pages as markdown tables. A voter record looks like:
#   **95** Name : SHANTI BASFOR Husbands Name : NAGINA BASFOR House Number :
#   E-72 Age : 49 Gender : Female | CRC0248286 |
# Bold markers / spacing / case vary, EPIC may land in the same or next cell.
# We split the text on serial-number markers and field-parse each chunk.

_EPIC_RE = re.compile(r"\b([A-Z]{2,4}[0-9O]{6,8})\b")
# A serial marker: a bold or bare number right before "Name :"
_SERIAL_SPLIT_RE = re.compile(
    r"(?:\*{1,2}|\b)(\d{1,4})(?:\*{1,2}|\b)\s*(?=Name\s*[:：])", re.I
)
_REL_WORDS = r"(?:Father|Husband|Mother|Wife|Other)s?['’]?"
# Field value ends at the next field keyword, a table pipe, or end of chunk.
_VALUE_END = (
    r"(?=\s*(?:" + _REL_WORDS + r"\s*(?:Name)?\s*[:：]"
    r"|House\s*(?:Number|No)\.?\s*[:：]"
    r"|Age\s*[:：]|Gender\s*[:：]"
    r"|Photo\b\s*(?:is)?\s*(?:Not\s*)?(?:Available)?\s*[:：]?"
    r"|\||$))"
)
_NAME_FIELD_RE = re.compile(r"Name\s*[:：]\s*(.*?)" + _VALUE_END, re.I | re.S)
_REL_FIELD_RE = re.compile(
    r"(" + _REL_WORDS + r")\s*(?:Name)?\s*[:：]\s*(.*?)" + _VALUE_END, re.I | re.S
)
_HOUSE_RE = re.compile(
    r"House\s*(?:Number|No)\.?\s*[:：]\s*(.*?)" + _VALUE_END, re.I | re.S
)
_AGE_RE = re.compile(r"Age\s*[:：]\s*(\d{1,3})", re.I)
_GENDER_RE = re.compile(r"Gender\s*[:：]\s*(Male|Female|Third\s*Gender|M\b|F\b)", re.I)

_REL_CANON = {"father": "Father", "husband": "Husband", "mother": "Mother",
              "wife": "Wife", "other": "Other"}


def _clean(s: str) -> str:
    s = re.sub(r"[*|#]", " ", s)          # markdown bold / table debris
    s = re.sub(r"\s+", " ", s)
    return s.strip(" :：.,-")


def _canon_rel(word: str) -> str:
    base = re.sub(r"s?['’]?$", "", word.lower())
    return _REL_CANON.get(base, word.title())


def parse_page_regex(page: PageText) -> list[Voter]:
    text = page.markdown or ""
    voters: list[Voter] = []
    marks = list(_SERIAL_SPLIT_RE.finditer(text))
    for i, m in enumerate(marks):
        chunk_end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[m.start():chunk_end]

        v = Voter(Page=page.index + 1, Serial_No=m.group(1))

        # Elector name: first "Name :" that is NOT part of "<Relation>s Name :".
        rel_m = _REL_FIELD_RE.search(chunk)
        for nm in _NAME_FIELD_RE.finditer(chunk):
            if rel_m and rel_m.start() <= nm.start() < rel_m.end():
                continue
            v.Name = _clean(nm.group(1))
            break
        if rel_m:
            v.Relation_Type = _canon_rel(rel_m.group(1))
            v.Relation_Name = _clean(rel_m.group(2))

        hm = _HOUSE_RE.search(chunk)
        if hm:
            v.House_Number = _clean(hm.group(1))
        am = _AGE_RE.search(chunk)
        if am:
            v.Age = am.group(1)
        gm = _GENDER_RE.search(chunk)
        if gm:
            g = gm.group(1).upper()
            v.Gender = {"M": "Male", "F": "Female"}.get(
                g, re.sub(r"\s+", " ", gm.group(1)).title())
        em = _EPIC_RE.search(chunk)
        if em:
            v.EPIC_No = em.group(1)

        if v.Name:
            voters.append(v)
    return voters


def parse_regex(pages: list[PageText]) -> list[Voter]:
    out: list[Voter] = []
    for p in pages:
        try:
            out.extend(parse_page_regex(p))
        except Exception:
            continue  # never let one bad page kill the run
    return out


# ---- lenient parser: anchors on every elector "Name :", recovers records the
# ---- strict serial-split parser can miss when a serial marker is malformed.
_NAME_ANCHOR_RE = re.compile(r"Name\s*[:：]", re.I)
_REL_PREFIX_RE = re.compile(
    r"(?:Father|Husband|Mother|Wife|Other)s?['’]?\s*$", re.I)
_STANDALONE_INT_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,4})(?![0-9])")
# Every "<serial> Name :" marker actually present in the OCR text (ground truth
# for how many voters the page holds), independent of what the parser recovered.
_SERIAL_MARKER_RE = re.compile(r"(?:\*{0,2})(\d{1,4})(?:\*{0,2})\s*Name\s*[:：]", re.I)


def _page_serial_markers(text: str) -> set[int]:
    return {int(m.group(1)) for m in _SERIAL_MARKER_RE.finditer(text or "")}


def parse_page_regex_lenient(page: PageText) -> list[Voter]:
    text = page.markdown or ""
    anchors = []
    for m in _NAME_ANCHOR_RE.finditer(text):
        pre = text[max(0, m.start() - 14):m.start()]
        if _REL_PREFIX_RE.search(pre):
            continue  # this is "<Relation>s Name :", not an elector name
        anchors.append(m)

    voters: list[Voter] = []
    for i, m in enumerate(anchors):
        start = anchors[i - 1].end() if i else 0
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        before = text[start:m.start()]
        forward = text[m.start():end]

        v = Voter(Page=page.index + 1)
        ints = _STANDALONE_INT_RE.findall(before)
        if ints:
            v.Serial_No = ints[-1]

        nmv = _NAME_FIELD_RE.search(forward)
        if nmv:
            v.Name = _clean(nmv.group(1))
        rel = _REL_FIELD_RE.search(forward)
        if rel:
            v.Relation_Type = _canon_rel(rel.group(1))
            v.Relation_Name = _clean(rel.group(2))
        hm = _HOUSE_RE.search(forward)
        if hm:
            v.House_Number = _clean(hm.group(1))
        am = _AGE_RE.search(forward)
        if am:
            v.Age = am.group(1)
        gm = _GENDER_RE.search(forward)
        if gm:
            g = gm.group(1).upper()
            v.Gender = {"M": "Male", "F": "Female"}.get(
                g, re.sub(r"\s+", " ", gm.group(1)).title())
        em = _EPIC_RE.search(forward) or _EPIC_RE.search(before)
        if em:
            v.EPIC_No = em.group(1)

        if v.Name:
            voters.append(v)
    return voters


# ---------------------------------------------------------------- llm parse
_LLM_SYSTEM = (
    "You extract voter records from OCR text of an Indian electoral roll page. "
    "The text renders a grid of voter boxes, often as a markdown table with "
    "several voters per row. Each voter has: a serial number (bold, before "
    "'Name'), an EPIC number (alphanumeric id like CRC0141572, often in the "
    "next table cell), Name, a relation (Father/Husband/Mother/Other) with "
    "that person's name, House Number, Age, and Gender. Return ONLY JSON: "
    '{"voters":[{"serial_no":"","epic_no":"","name":"","relation_type":"",'
    '"relation_name":"","house_number":"","age":"","gender":""}]}. '
    "relation_type must be one of Father/Husband/Mother/Wife/Other. Copy "
    "values verbatim; use empty string if a field is missing. Extract EVERY "
    "voter on the page. Do not invent voters."
)


def _llm_call_with_retry(client, model: str, text: str, retries: int = 3):
    delay = 2.0
    for attempt in range(retries):
        try:
            resp = client.chat.complete(
                model=model,
                messages=[{"role": "system", "content": _LLM_SYSTEM},
                          {"role": "user", "content": text}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            return resp.choices[0].message.content
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(delay)
            delay *= 2
    return None


def _llm_page(page: PageText, client, model: str) -> list[Voter]:
    """Pure LLM extraction of one page (may be empty on failure)."""
    if not page.markdown.strip():
        return []
    content = _llm_call_with_retry(client, model, page.markdown)
    llm_rows: list[Voter] = []
    if content:
        try:
            data = json.loads(content)
            for r in data.get("voters", []):
                llm_rows.append(Voter(
                    Serial_No=str(r.get("serial_no", "") or ""),
                    EPIC_No=str(r.get("epic_no", "") or ""),
                    Name=str(r.get("name", "") or ""),
                    Relation_Type=str(r.get("relation_type", "") or ""),
                    Relation_Name=str(r.get("relation_name", "") or ""),
                    House_Number=str(r.get("house_number", "") or ""),
                    Age=str(r.get("age", "") or ""),
                    Gender=str(r.get("gender", "") or ""),
                    Page=page.index + 1,
                ))
        except (json.JSONDecodeError, TypeError):
            pass
    return llm_rows


def parse_page_llm(page: PageText, client, model: str) -> list[Voter]:
    regex_rows = parse_page_regex(page)
    llm_rows = _llm_page(page, client, model)
    # Keep whichever parse recovered more voters for this page.
    return llm_rows if len(llm_rows) >= len(regex_rows) else regex_rows


def parse_llm(pages: list[PageText]) -> list[Voter]:
    try:
        from mistralai import Mistral
        client = Mistral(api_key=os.getenv("MISTRAL_API_KEY"))
    except Exception:
        return parse_regex(pages)  # SDK/key problem -> still succeed via regex
    model = os.getenv("STRUCTURE_MODEL", "mistral-small-latest")
    out: list[Voter] = []
    for p in pages:
        try:
            out.extend(parse_page_llm(p, client, model))
        except Exception:
            out.extend(parse_page_regex(p))
    return out


# ---------------------------------------------------------------- photos
def _safe_stem(*parts: str) -> str:
    stem = "_".join(p for p in parts if p) or "voter"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", stem)


def save_photos(pages: list[PageText], voters: list[Voter], out_dir: Path) -> int:
    """Save OCR-provided page images (if any) mapped to voters in order.
    Returns number of photos written."""
    voters_by_page: dict[int, list[Voter]] = {}
    for v in voters:
        voters_by_page.setdefault(v.Page, []).append(v)

    written = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        page_voters = voters_by_page.get(page.index + 1, [])
        for voter, image in zip(page_voters, page.images):
            try:
                raw = base64.b64decode(image.base64_data)
            except Exception:
                continue
            ext = Path(image.id).suffix or ".jpeg"
            fname = _safe_stem(voter.EPIC_No, voter.Serial_No, voter.Name) + ext
            path = out_dir / fname
            path.write_bytes(raw)
            voter.Photo_Path = str(path)
            written += 1
    return written


def save_photos_cv(pdf_bytes: bytes, voters: list[Voter], out_dir: Path) -> int:
    """Crop voter photos straight from the scanned pages (OpenCV grid
    detection) and save them, mapped to voters in reading order per page.
    Used when the OCR provider returns no embedded images (typical for scans).
    Returns number of photos written."""
    from photo_extract import extract_photos_from_pdf

    crops_by_page = extract_photos_from_pdf(pdf_bytes)
    voters_by_page: dict[int, list[Voter]] = {}
    for v in voters:
        voters_by_page.setdefault(v.Page, []).append(v)

    written = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for page_no, page_voters in voters_by_page.items():
        # sort by serial so order matches the top-to-bottom, left-to-right grid
        def _key(v: Voter):
            return int(v.Serial_No) if v.Serial_No.isdigit() else 10 ** 9
        page_voters = sorted(page_voters, key=_key)
        crops = crops_by_page.get(page_no - 1, [])
        for voter, jpg in zip(page_voters, crops):
            if not jpg:
                continue
            fname = _safe_stem(voter.EPIC_No, voter.Serial_No, voter.Name) + ".jpg"
            path = out_dir / fname
            path.write_bytes(jpg)
            voter.Photo_Path = str(path)
            written += 1
    return written


# ---------------------------------------------------------------- repair
def _make_client():
    if not os.getenv("MISTRAL_API_KEY"):
        return None, None
    try:
        from mistralai import Mistral
        client = Mistral(api_key=os.getenv("MISTRAL_API_KEY"))
    except Exception:
        return None, None
    return client, os.getenv("STRUCTURE_MODEL", "mistral-small-latest")


def _extract_page(page, method, client, model) -> list[Voter]:
    """First-pass extraction for a page with the chosen method."""
    if method == "llm" and client is not None:
        try:
            return parse_page_llm(page, client, model)
        except Exception:
            pass
    return parse_page_regex(page)


def _repair_page(page, client, model) -> list[Voter]:
    """Re-extract a page every way we can and merge into the most complete set.
    Recovers voters the first pass missed and fills empty fields."""
    lists = [parse_page_regex(page), parse_page_regex_lenient(page)]
    if client is not None:
        try:
            lists.append(_llm_page(page, client, model))
        except Exception:
            pass
    merged = _best_by_serial(lists)
    return sorted(merged.values(), key=_serial_key)


def _single_page_pdf(pdf_bytes: bytes, idx: int) -> bytes:
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if idx < 0 or idx >= doc.page_count:
            return b""
        doc.select([idx])
        return doc.tobytes()


def _fill_serial_gaps(voters, pages, provider, pdf_bytes, client, model):
    """Check 1: the serial numbers must be contiguous. For any gap, find the
    page it belongs to and re-OCR just that page to recover the record.
    Returns (voters, still_missing_serials)."""
    by_serial: dict[int, Voter] = {}
    others: list[Voter] = []
    for v in voters:
        if v.Serial_No.isdigit():
            s = int(v.Serial_No)
            by_serial[s] = _combine(by_serial[s], v) if s in by_serial else v
        else:
            others.append(v)

    if not by_serial:
        return voters, []

    lo, hi = min(by_serial), max(by_serial)
    missing = [s for s in range(lo, hi + 1) if s not in by_serial]

    if missing and provider is not None and pdf_bytes:
        # Which page holds each missing serial? Use the text markers / neighbours.
        marker_page: dict[int, int] = {}
        for p in pages:
            for s in _page_serial_markers(p.markdown):
                marker_page[s] = p.index
        pages_to_reocr = set()
        for s in missing:
            idx = next((marker_page[k] for k in (s, s - 1, s + 1)
                        if k in marker_page), None)
            if idx is not None:
                pages_to_reocr.add(idx)

        for idx in pages_to_reocr:
            try:
                sub = _single_page_pdf(pdf_bytes, idx)
                if not sub:
                    continue
                md = provider.ocr_pdf(sub)[0].markdown
                newpage = PageText(index=idx, markdown=md)
                recovered = _repair_page(newpage, client, model)
                for v in recovered:
                    if v.Serial_No.isdigit():
                        s = int(v.Serial_No)
                        by_serial[s] = _combine(by_serial[s], v) if s in by_serial else v
            except Exception:
                continue
        missing = [s for s in range(lo, hi + 1) if s not in by_serial]

    return list(by_serial.values()) + others, missing


# ---------------------------------------------------------------- assemble
def build_rows(
    pages: list[PageText],
    method: str = "regex",
    photos_dir: Path | None = None,
    pdf_bytes: bytes | None = None,
    provider=None,
) -> tuple[list[dict], dict]:
    """Extract voter rows with a self-checking repair pass.

    Returns (rows, issues). `issues` reports anything that could not be fully
    recovered so the UI can surface it instead of silently dropping data.
    """
    header = extract_header(pages)
    client, model = _make_client()

    # 1) first pass per page
    page_voters: dict[int, list[Voter]] = {}
    for p in pages:
        page_voters[p.index] = _extract_page(p, method, client, model)

    # 2) per-page self-check (Checks 1 & 2, local): repair a page if the OCR
    #    text shows more voter markers than we parsed, or any field is empty.
    for p in pages:
        vs = page_voters[p.index]
        markers = _page_serial_markers(p.markdown)
        got = {int(v.Serial_No) for v in vs if v.Serial_No.isdigit()}
        incomplete = any(_completeness(v) < len(REQUIRED_FIELDS) for v in vs)
        if (markers - got) or incomplete or len(vs) < len(markers):
            page_voters[p.index] = _repair_page(p, client, model)

    voters = [v for idx in sorted(page_voters) for v in page_voters[idx]]

    # 3) global serial-gap fill via targeted re-OCR (Check 1, cross-page)
    voters, missing_serials = _fill_serial_gaps(
        voters, pages, provider, pdf_bytes, client, model)

    # 4) de-duplicate by serial, keeping the most complete record
    merged = _best_by_serial([voters])
    voters = sorted(merged.values(), key=_serial_key)

    # 5) photos (Prefer OCR-embedded images; else crop from the scan)
    if photos_dir is not None:
        written = save_photos(pages, voters, photos_dir)
        if written == 0 and pdf_bytes is not None:
            save_photos_cv(pdf_bytes, voters, photos_dir)
    for v in voters:
        v.Photo_Id = Path(v.Photo_Path).name if v.Photo_Path else ""

    # 6) build the issue report
    serials = [int(v.Serial_No) for v in voters if v.Serial_No.isdigit()]
    check_fields = REQUIRED_FIELDS + (["Photo_Path"] if photos_dir else [])
    incomplete_rows = [
        {"serial": v.Serial_No, "name": v.Name,
         "missing": [f for f in check_fields if not getattr(v, f)]}
        for v in voters if any(not getattr(v, f) for f in check_fields)
    ]
    issues = {
        "extracted": len(voters),
        "expected_max_serial": max(serials) if serials else 0,
        "min_serial": min(serials) if serials else 0,
        "missing_serials": missing_serials,
        "incomplete_rows": incomplete_rows,
    }

    rows = []
    for v in voters:
        row = {**header, **asdict(v)}
        rows.append({c: row.get(c, "") for c in COLUMNS})
    return rows, issues
