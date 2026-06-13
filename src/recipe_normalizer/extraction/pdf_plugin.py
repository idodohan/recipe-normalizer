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
_MAX_PAGES = 10


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


def _extract_text(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _render_pages(data: bytes) -> list[bytes]:
    """Rasterize each page to a PNG at 144 dpi; refuse PDFs past the page cap."""
    document = pdfium.PdfDocument(data)
    try:
        page_count = len(document)
        if page_count > _MAX_PAGES:
            raise TierFailed(
                f"PDF has {page_count} pages; only the first {_MAX_PAGES} can be processed"
            )
        pngs: list[bytes] = []
        for index in range(page_count):
            page = document[index]
            pil_image = page.render(scale=_RENDER_DPI / 72).to_pil()
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            pngs.append(buffer.getvalue())
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
