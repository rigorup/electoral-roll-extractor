"""PDF helpers: trim boilerplate pages and re-serialize to bytes."""
from __future__ import annotations

import fitz  # PyMuPDF


def get_page_count(pdf_bytes: bytes) -> int:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.page_count


def sub_pdf(pdf_bytes: bytes, indices: list[int]) -> bytes:
    """Return a new PDF containing only `indices` (0-based), in order.
    Used to OCR a large document in smaller, more reliable batches."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        valid = [i for i in indices if 0 <= i < doc.page_count]
        if not valid:
            return b""
        doc.select(valid)
        return doc.tobytes()


def trim_pages(pdf_bytes: bytes, drop_first: int = 2, drop_last: int = 2) -> bytes:
    """Return a new PDF with the first `drop_first` and last `drop_last` pages removed.

    Electoral rolls put a cover + map/photos on the first two pages and a
    summary + legend on the last two, so those carry no voter records.
    Falls back to the original bytes if the document is too short to trim.
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        total = doc.page_count
        if total <= drop_first + drop_last:
            return pdf_bytes  # nothing safe to trim
        keep_start = drop_first
        keep_end = total - drop_last - 1  # inclusive
        doc.select(list(range(keep_start, keep_end + 1)))
        return doc.tobytes()
