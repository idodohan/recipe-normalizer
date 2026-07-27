"""PDF extractor plugin (spec §6.2): text layer when present, vision otherwise.

pypdf reads the embedded text layer. When that yields too little meaningful text
(scanned/image-only PDFs, garbled encodings) the pages are rasterized to PNG via
pypdfium2 (144 dpi) and sent down the vision path of the shared normalize stage.
Multi-recipe PDFs need no special handling — normalize returns recipes[].

NO LLM in the acquire phase; the original upload (and rendered pages, for the
review screen) are retained as artifacts (spec §6.4).
"""

from __future__ import annotations

import io
import logging
import unicodedata
from typing import TYPE_CHECKING, Any, ClassVar

import pypdf
import pypdfium2 as pdfium

from recipe_normalizer.extraction.base import Acquired, TierFailed, register

if TYPE_CHECKING:
    from recipe_normalizer.filestore import FileStore
    from recipe_normalizer.llm.client import LLMClient

__all__ = ["PdfExtractor", "is_meaningful_text"]

logger = logging.getLogger(__name__)

# A PDF whose text layer yields fewer meaningful chars than this is treated as
# scanned and sent to the vision path instead.
_MIN_TEXT_CHARS = 200
# Above this fraction of replacement/control characters the "text" is garbage.
_MAX_GARBLED_RATIO = 0.1
_RENDER_DPI = 144
# Rasterizing pages to images (scanned-PDF vision path) is expensive, so it is
# capped low. Reading the text layer is cheap, so it gets a much higher ceiling
# — a normal multi-page cookbook PDF must not be rejected just because it has
# more than 10 pages; only a genuinely abusive page count is refused.
_MAX_PAGES = 10
_MAX_TEXT_PAGES = 2_000
# Text-layer bomb guard: a page can carry an unbounded amount of text even
# within the page cap, so accumulation stops here (a real recipe PDF is orders
# of magnitude smaller).
_MAX_TEXT_CHARS = 500_000
# Decompression-bomb guard: skip rasterizing any single page whose rendered
# area (at _RENDER_DPI) would exceed this many pixels.
_MAX_PAGE_PIXELS = 50_000_000


def is_meaningful_text(text: str) -> bool:
    """True when *text* is long enough AND not mostly garbled bytes."""
    stripped = text.strip()
    if len(stripped) < _MIN_TEXT_CHARS:
        return False
    bad = sum(
        1
        for char in stripped
        if char == "�" or (unicodedata.category(char).startswith("C") and char not in "\n\r\t")
    )
    return bad / len(stripped) <= _MAX_GARBLED_RATIO


def _page_cap_failure(page_count: int) -> TierFailed:
    return TierFailed(f"PDF has {page_count} pages; only the first {_MAX_PAGES} can be processed")


def _extract_text(data: bytes) -> str:
    """Read the text layer, refusing only genuinely abusive page counts.

    The page count is checked FIRST (it is cheap — pypdf reads the page tree,
    not the pages) so a 100k-page document fails immediately instead of walking
    every page's text layer. The threshold here is the high text-path ceiling,
    NOT the low render cap: a normal multi-page recipe PDF must extract fine.
    Accumulated text is capped too: one page can hold an unbounded amount.
    """
    reader = pypdf.PdfReader(io.BytesIO(data))
    page_count = len(reader.pages)
    if page_count > _MAX_TEXT_PAGES:
        raise TierFailed(
            f"PDF has {page_count} pages; only the first {_MAX_TEXT_PAGES} can be read"
        )
    chunks: list[str] = []
    remaining = _MAX_TEXT_CHARS
    for page in reader.pages:
        chunk = (page.extract_text() or "")[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            logger.warning("PDF text layer exceeds %d chars; truncating", _MAX_TEXT_CHARS)
            break
    return "\n".join(chunks)


def _render_pages(data: bytes) -> list[bytes]:
    """Rasterize each page to a PNG at 144 dpi; refuse PDFs past the page cap."""
    document = pdfium.PdfDocument(data)
    try:
        page_count = len(document)
        if page_count > _MAX_PAGES:
            raise _page_cap_failure(page_count)
        pngs: list[bytes] = []
        for index in range(page_count):
            page = document[index]
            mediabox = page.get_mediabox()
            if mediabox is None:
                # Unknown size: the pixel-cap check cannot run, so treat this
                # conservatively as over-cap rather than rendering an
                # unbounded unknown.
                logger.warning(
                    "Skipping page %d of %d: mediabox unavailable (unknown size)",
                    index,
                    page_count,
                )
                continue
            width_pt = mediabox[2] - mediabox[0]
            height_pt = mediabox[3] - mediabox[1]
            scale = _RENDER_DPI / 72
            pixel_area = (width_pt * scale) * (height_pt * scale)
            if pixel_area > _MAX_PAGE_PIXELS:
                logger.warning(
                    "Skipping page %d of %d: rendered area %.0f px exceeds cap %d",
                    index,
                    page_count,
                    pixel_area,
                    _MAX_PAGE_PIXELS,
                )
                continue
            pil_image = page.render(scale=_RENDER_DPI / 72).to_pil()
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            pngs.append(buffer.getvalue())
        if not pngs and page_count:
            raise TierFailed("PDF pages too large to render safely")
        return pngs
    finally:
        document.close()


class PdfExtractor:
    """payload {"file_ref": str, ...} → Acquired via text layer or page render."""

    input_type: ClassVar[str] = "pdf"

    def acquire(self, payload: dict[str, Any], *, llm: LLMClient, store: FileStore) -> Acquired:
        file_ref: str = payload["file_ref"]
        data = store.open(file_ref)
        artifacts = {"source_file_ref": file_ref}

        text = _extract_text(data)
        if is_meaningful_text(text):
            return Acquired(text=text, artifacts=artifacts, meta={"source": "pdf_text"})

        pages = _render_pages(data)
        # One artifact key per page (the API prefixes each value with /api/files/,
        # so a comma-joined value would not round-trip to usable URLs).
        page_artifacts = {
            f"page_image_ref_{index}": store.save(png, suffix="png")
            for index, png in enumerate(pages)
        }
        return Acquired(
            images=[(png, "image/png") for png in pages],
            artifacts={**artifacts, **page_artifacts},
            meta={"source": "pdf_vision", "page_count": len(pages)},
        )


register(PdfExtractor())
