"""End-to-end processing: batched OCR -> extraction/repair -> ZIP bundle.

`process_pdf` is the single entry point used by the UI. It reports progress
through a callback so the front-end can show what is happening live.
"""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import pandas as pd

from extractor import build_rows, COLUMNS
from ocr_providers import PageText
from pdf_utils import get_page_count, sub_pdf

# Large documents are OCR'd in page batches for reliability/accuracy.
BATCH_THRESHOLD = 25    # only batch when the PDF has MORE than this many pages
BATCH_SIZE = 15         # pages per batch

Progress = Callable[[str, float | None], None]


def _batches(n: int, size: int) -> list[list[int]]:
    return [list(range(i, min(i + size, n))) for i in range(0, n, size)]


def ocr_batched(provider, pdf_bytes: bytes, include_images: bool,
                progress: Progress) -> list[PageText]:
    """OCR the whole PDF, splitting big documents into page batches.
    Returned PageText objects always carry the GLOBAL page index."""
    n = get_page_count(pdf_bytes)

    if n <= BATCH_THRESHOLD:
        progress(f"Running OCR on {n} page(s)…", 0.1)
        result = provider.ocr_pdf(pdf_bytes, include_images=include_images)
        return [PageText(index=i, markdown=p.markdown, images=p.images)
                for i, p in enumerate(result)]

    batches = _batches(n, BATCH_SIZE)
    sizes = ", ".join(str(len(b)) for b in batches)
    progress(f"{n} pages (> {BATCH_THRESHOLD}) → {len(batches)} batches "
             f"of {sizes} pages for higher accuracy…", 0.05)

    pages: list[PageText] = []
    for bi, idxs in enumerate(batches):
        progress(f"OCR batch {bi + 1}/{len(batches)} — "
                 f"pages {idxs[0] + 1}–{idxs[-1] + 1}…",
                 0.05 + (bi / len(batches)) * 0.6)
        sub = sub_pdf(pdf_bytes, idxs)
        if not sub:
            continue
        result = provider.ocr_pdf(sub, include_images=include_images)
        for local_i, p in enumerate(result):
            gidx = idxs[local_i] if local_i < len(idxs) else idxs[0] + local_i
            pages.append(PageText(index=gidx, markdown=p.markdown, images=p.images))
    return pages


def make_zip(base: str, pdf_bytes: bytes, df: pd.DataFrame,
             photos_dir: Path | None) -> bytes:
    """Bundle the original PDF, the Excel, and any photos into one ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{base}/{base}.pdf", pdf_bytes)

        xbuf = io.BytesIO()
        with pd.ExcelWriter(xbuf, engine="openpyxl") as xl:
            df.to_excel(xl, index=False, sheet_name="Voters")
        z.writestr(f"{base}/{base}.xlsx", xbuf.getvalue())

        if photos_dir and photos_dir.exists():
            for f in sorted(photos_dir.iterdir()):
                if f.is_file():
                    z.write(f, f"{base}/photos/{f.name}")
    return buf.getvalue()


def process_pdf(pdf_bytes: bytes, filename: str, method: str,
                include_photos: bool, provider,
                progress: Progress | None = None):
    """Full pipeline. Returns (df, issues, zip_bytes, base_name)."""
    progress = progress or (lambda msg, frac=None: None)
    base = Path(filename).stem or "electoral_roll"

    pages = ocr_batched(provider, pdf_bytes, include_photos, progress)

    progress("Extracting & verifying voter records…", 0.7)
    with tempfile.TemporaryDirectory() as td:
        photos_dir = Path(td) / "photos" if include_photos else None
        rows, issues = build_rows(pages, method=method, photos_dir=photos_dir,
                                  pdf_bytes=pdf_bytes, provider=provider)

        # Rewrite Photo_Path to a portable, zip-internal location.
        for r in rows:
            if r.get("Photo_Id"):
                r["Photo_Path"] = f"{base}/photos/{r['Photo_Id']}"
        df = pd.DataFrame(rows, columns=COLUMNS)

        progress("Packaging ZIP (PDF + Excel + photos)…", 0.9)
        zip_bytes = make_zip(base, pdf_bytes, df, photos_dir)

    progress("Done.", 1.0)
    return df, issues, zip_bytes, base
