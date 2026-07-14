"""Electoral Roll PDF -> Excel/ZIP converter (lightweight Streamlit UI)."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from pdf_utils import get_page_count, trim_pages
from ocr_providers import get_provider
from pipeline import process_pdf, BATCH_THRESHOLD, BATCH_SIZE
from extractor import COLUMNS

load_dotenv()

st.set_page_config(page_title="Electoral Roll → Excel", page_icon="🗳️",
                   layout="centered")
st.title("🗳️ Electoral Roll → Excel")
st.caption("Upload an electoral-roll PDF → get an Excel + photos, bundled as a "
           "single ZIP. Large PDFs are OCR'd in batches for accuracy.")

with st.sidebar:
    st.header("Settings")
    st.caption("Cover / map / summary pages are skipped automatically — they "
               "hold no voter records.")
    trim = st.toggle(
        "Also force-drop pages before OCR",
        value=False,
        help="Optional: physically remove N first/last pages before OCR. Leave "
             "OFF unless sure — the app auto-retries with all pages if this "
             "empties the result.",
    )
    drop_first = st.number_input("Pages to drop at start", 0, 5, 2, disabled=not trim)
    drop_last = st.number_input("Pages to drop at end", 0, 5, 2, disabled=not trim)
    method = st.radio(
        "Structuring method",
        ["regex", "llm"],
        format_func=lambda m: {"regex": "Parser (fast, free, exact)",
                               "llm": "LLM (extra Mistral calls)"}[m],
        help="The parser is tuned to the roll layout. Both paths run a repair "
             "pass that fills gaps and empty fields.",
    )
    include_photos = st.toggle(
        "Include voter photos",
        value=True,
        help="Crops each voter's photo from the scan and adds them to the ZIP "
             "(with Photo_Id + Photo_Path columns in the Excel).",
    )
    st.caption(f"OCR provider: **{os.getenv('OCR_PROVIDER', 'mistral')}** · "
               f"batches of {BATCH_SIZE} when > {BATCH_THRESHOLD} pages")
    if not os.getenv("MISTRAL_API_KEY"):
        st.warning("MISTRAL_API_KEY not set — add it to your .env file.")

uploaded = st.file_uploader("Choose a PDF", type=["pdf"])

if uploaded is not None:
    pdf_bytes = uploaded.read()
    total = get_page_count(pdf_bytes)
    note = (f" — will batch into {BATCH_SIZE}-page chunks"
            if total > BATCH_THRESHOLD else "")
    st.info(f"**{uploaded.name}** — {total} pages{note}")

    if st.button("Convert & build ZIP", type="primary", use_container_width=True):
        work = trim_pages(pdf_bytes, drop_first, drop_last) if trim else pdf_bytes

        status = st.status("Starting…", expanded=True)
        bar = st.progress(0.0)

        def progress(msg: str, frac: float | None = None):
            status.update(label=msg)
            status.write(msg)
            if frac is not None:
                bar.progress(min(max(frac, 0.0), 1.0))

        try:
            provider = get_provider()
            df, issues, zip_bytes, base = process_pdf(
                work, uploaded.name, method, include_photos, provider, progress)

            # Auto-retry on the full PDF if trimming emptied the result.
            if df.empty and trim:
                progress("No records after trimming — retrying with ALL pages…")
                df, issues, zip_bytes, base = process_pdf(
                    pdf_bytes, uploaded.name, method, include_photos,
                    provider, progress)

            status.update(label="Done.", state="complete", expanded=False)
        except Exception as e:  # noqa: BLE001
            status.update(label="Failed.", state="error")
            st.exception(e)
            st.stop()

        if df.empty:
            st.error("No voter records were found. Check that this is an "
                     "English electoral-roll PDF.")
            st.stop()

        st.success(f"Extracted {len(df)} voter records.")

        # ---- integrity checks -------------------------------------------
        exp, miss = issues["expected_max_serial"], issues["missing_serials"]
        incomplete = issues["incomplete_rows"]
        if exp:
            st.caption(f"Serial range {issues['min_serial']}–{exp} · "
                       f"{len(df)} rows.")
        if not miss and not incomplete:
            st.success("✅ Integrity check passed — no missing serials, no "
                       "empty fields.")
        if miss:
            st.error(f"⚠️ {len(miss)} serial(s) still missing after re-OCR: "
                     f"{miss[:40]}" + (" …" if len(miss) > 40 else ""))
        if incomplete:
            with st.expander(f"⚠️ {len(incomplete)} row(s) with empty field(s)"):
                st.dataframe(pd.DataFrame(incomplete), use_container_width=True)

        n_photos = sum(1 for p in df["Photo_Id"] if p) if include_photos else 0
        contents = [f"`{base}.pdf`", f"`{base}.xlsx`"]
        if include_photos:
            contents.append(f"`photos/` ({n_photos} images)")
        st.info("ZIP contains: " + " · ".join(contents))

        st.download_button(
            "⬇️ Download everything (ZIP)",
            data=zip_bytes,
            file_name=f"{base}.zip",
            mime="application/zip",
            use_container_width=True,
        )
        st.dataframe(df, use_container_width=True, height=400)
