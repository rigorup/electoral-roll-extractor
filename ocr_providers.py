"""Swappable OCR provider layer.

To use a different provider later, implement `OCRProvider.ocr_pdf` in a new
subclass and register it in `get_provider`. Nothing else in the app needs to
change -- the rest of the pipeline only sees a list of per-page markdown/text.
"""
from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PageImage:
    id: str              # e.g. "img-0.jpeg", used as the markdown placeholder
    base64_data: str      # raw base64 payload (no data: prefix)


@dataclass
class PageText:
    index: int                   # 0-based page index within the (trimmed) document
    markdown: str                # OCR text for that page
    images: list[PageImage] = None  # populated only when image extraction is requested

    def __post_init__(self):
        if self.images is None:
            self.images = []


class OCRProvider(ABC):
    name: str = "base"

    @abstractmethod
    def ocr_pdf(self, pdf_bytes: bytes, include_images: bool = False) -> list[PageText]:
        """Run OCR over the whole PDF and return one PageText per page."""
        raise NotImplementedError


class MistralOCRProvider(OCRProvider):
    """Uses Mistral's dedicated Document OCR endpoint (mistral-ocr-latest)."""

    name = "mistral"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("MISTRAL_API_KEY")
        self.model = model or os.getenv("OCR_MODEL", "mistral-ocr-latest")
        if not self.api_key:
            raise RuntimeError(
                "MISTRAL_API_KEY is not set. Add it to your .env file."
            )

    def ocr_pdf(self, pdf_bytes: bytes, include_images: bool = False) -> list[PageText]:
        # Imported lazily so the app can start even before the SDK is installed.
        from mistralai import Mistral

        client = Mistral(api_key=self.api_key)
        data_uri = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()
        resp = client.ocr.process(
            model=self.model,
            document={"type": "document_url", "document_url": data_uri},
            include_image_base64=include_images,
        )
        pages: list[PageText] = []
        for p in resp.pages:
            # SDK returns objects; be tolerant of dict-like access too.
            idx = getattr(p, "index", None)
            md = getattr(p, "markdown", None)
            imgs = getattr(p, "images", None)
            if md is None and isinstance(p, dict):
                idx, md, imgs = p.get("index"), p.get("markdown", ""), p.get("images")

            page_images: list[PageImage] = []
            for im in imgs or []:
                img_id = getattr(im, "id", None)
                b64 = getattr(im, "image_base64", None)
                if img_id is None and isinstance(im, dict):
                    img_id, b64 = im.get("id"), im.get("image_base64")
                if b64:
                    # Strip a data: prefix if the SDK includes one.
                    b64 = b64.split(",", 1)[-1] if b64.startswith("data:") else b64
                    page_images.append(PageImage(id=img_id or f"img-{len(page_images)}",
                                                  base64_data=b64))

            pages.append(PageText(index=idx if idx is not None else len(pages),
                                  markdown=md or "", images=page_images))
        return pages


def get_provider(name: str | None = None) -> OCRProvider:
    """Factory. Set OCR_PROVIDER in the environment to switch providers."""
    name = (name or os.getenv("OCR_PROVIDER", "mistral")).lower()
    if name == "mistral":
        return MistralOCRProvider()
    raise ValueError(f"Unknown OCR provider: {name!r}")
