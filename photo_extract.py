"""Crop voter photos directly from scanned roll pages using OpenCV.

Mistral OCR does not return embedded images for full-page scans, so we detect
the voter-box grid on each page image and cut out the photo region (right side
of each box). Crops are returned in reading order (top-to-bottom rows,
left-to-right), which matches the serial order the text parser produces.
"""
from __future__ import annotations

import numpy as np


def _page_image(doc, page_index: int, zoom: float = 2.0) -> np.ndarray:
    """Render a PDF page to an RGB numpy array (H, W, 3)."""
    import fitz

    page = doc[page_index]
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return img


def _detect_cells(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find voter-box rectangles via table-line morphology.

    Takes an RGB page image, detects the grid on a grayscale copy, and returns
    (x, y, w, h) boxes sorted in reading order. Empty list if the page has no
    detectable grid (cover pages etc.)."""
    import cv2

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 25, 15)

    horiz = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (w // 8, 1)))
    vert = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, h // 12)))
    grid = cv2.dilate(horiz | vert, np.ones((3, 3), np.uint8))

    contours, _ = cv2.findContours(grid, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    min_area = (w // 5) * (h // 30)      # a voter box is roughly w/3 x h/11
    max_area = (w // 2) * (h // 5)
    boxes = []
    for c in contours:
        x, y, bw_, bh_ = cv2.boundingRect(c)
        area = bw_ * bh_
        if min_area < area < max_area and bw_ > w // 5 and bh_ > h // 40:
            boxes.append((x, y, bw_, bh_))

    if not boxes:
        return []

    # Deduplicate nested/overlapping detections.
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for b in boxes:
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        inside = any(k[0] <= cx <= k[0] + k[2] and k[1] <= cy <= k[1] + k[3]
                     for k in kept)
        if not inside:
            kept.append(b)

    # Reading order: cluster into rows by y-center, then sort by x.
    kept.sort(key=lambda b: b[1] + b[3] / 2)
    rows: list[list[tuple[int, int, int, int]]] = []
    for b in kept:
        yc = b[1] + b[3] / 2
        if rows and abs(yc - (rows[-1][0][1] + rows[-1][0][3] / 2)) < b[3] * 0.5:
            rows[-1].append(b)
        else:
            rows.append([b])
    ordered: list[tuple[int, int, int, int]] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda b: b[0]))
    return ordered


def extract_photos_from_pdf(pdf_bytes: bytes) -> dict[int, list[bytes]]:
    """Return {0-based page index: [jpeg bytes per voter box, reading order]}.

    The photo occupies the right ~27% of each voter box. Pages where no grid
    is detected map to an empty list."""
    import cv2
    import fitz

    result: dict[int, list[bytes]] = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pi in range(doc.page_count):
            try:
                rgb = _page_image(doc, pi)
                cells = _detect_cells(rgb)
                crops: list[bytes] = []
                for (x, y, cw, ch) in cells:
                    # photo strip: right side of the box, inset from borders
                    px0 = x + int(cw * 0.72)
                    px1 = x + cw - max(2, int(cw * 0.01))
                    py0 = y + max(2, int(ch * 0.06))
                    py1 = y + ch - max(2, int(ch * 0.06))
                    crop = rgb[py0:py1, px0:px1]
                    if crop.size == 0:
                        crops.append(b"")
                        continue
                    # cv2 expects BGR when encoding a color image
                    bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
                    ok, buf = cv2.imencode(".jpg", bgr,
                                           [cv2.IMWRITE_JPEG_QUALITY, 92])
                    crops.append(buf.tobytes() if ok else b"")
                result[pi] = crops
            except Exception:
                result[pi] = []      # never let one page break the run
    return result
