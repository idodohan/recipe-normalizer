"""Tests for the PDF extractor (extraction/pdf_plugin.py), spec §6.2.

Real fixture PDFs (committed; regenerate via fixtures/make_media_fixtures.py):
text_layer_recipe.pdf has a selectable text layer → text path; scanned_recipe.pdf
is image-only → vision (page-render) path. NO LLM here — the plugin only
acquires; normalize runs later against the stub seam.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from recipe_normalizer.extraction import EXTRACTORS
from recipe_normalizer.extraction.base import TierFailed
from recipe_normalizer.extraction.pdf_plugin import PdfExtractor, is_meaningful_text
from recipe_normalizer.filestore import LocalFileStore

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


@pytest.fixture()
def store(tmp_path: Path) -> LocalFileStore:
    return LocalFileStore(tmp_path)


class _UnusedLLM:
    """The PDF acquire phase never calls the LLM; any access fails the test."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"PDF acquire must not touch the LLM (accessed {name!r})")


def _payload(store: LocalFileStore, fixture: str) -> dict[str, object]:
    data = (PDF_FIXTURES / fixture).read_bytes()
    ref = store.save(data, suffix="pdf")
    return {"file_ref": ref, "filename": fixture, "media_type": "application/pdf"}


def _multipage_scanned_pdf(pages: int) -> bytes:
    """An image-only PDF with *pages* pages (no text layer)."""
    images = [Image.new("RGB", (400, 560), "white") for _ in range(pages)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Text path
# ---------------------------------------------------------------------------


def test_text_layer_pdf_uses_text_path(store: LocalFileStore) -> None:
    payload = _payload(store, "text_layer_recipe.pdf")
    acquired = PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert acquired.text is not None
    assert "all-purpose flour" in acquired.text
    assert acquired.images == []
    assert acquired.meta["source"] == "pdf_text"
    # The original PDF is retained for the review screen.
    assert acquired.artifacts["source_file_ref"] == payload["file_ref"]


# ---------------------------------------------------------------------------
# Vision (page-render) path
# ---------------------------------------------------------------------------


def test_scanned_pdf_renders_pages_to_images(store: LocalFileStore) -> None:
    payload = _payload(store, "scanned_recipe.pdf")
    acquired = PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert acquired.text is None
    assert len(acquired.images) == 1
    data, media_type = acquired.images[0]
    assert media_type == "image/png"
    assert Image.open(io.BytesIO(data)).format == "PNG"  # really a rendered page
    assert acquired.meta["source"] == "pdf_vision"
    assert acquired.meta["page_count"] == 1
    assert acquired.artifacts["source_file_ref"] == payload["file_ref"]


def test_multipage_scanned_pdf_renders_every_page(store: LocalFileStore) -> None:
    ref = store.save(_multipage_scanned_pdf(3), suffix="pdf")
    payload = {"file_ref": ref, "filename": "x.pdf", "media_type": "application/pdf"}
    acquired = PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert len(acquired.images) == 3
    assert acquired.meta["page_count"] == 3


def test_pdf_over_page_cap_fails_with_reason(store: LocalFileStore) -> None:
    ref = store.save(_multipage_scanned_pdf(11), suffix="pdf")
    payload = {"file_ref": ref, "filename": "long.pdf", "media_type": "application/pdf"}

    with pytest.raises(TierFailed) as exc_info:
        PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert "11" in exc_info.value.reason
    assert "10" in exc_info.value.reason


# ---------------------------------------------------------------------------
# is_meaningful_text heuristic
# ---------------------------------------------------------------------------


def test_is_meaningful_text_true_for_long_clean_text() -> None:
    assert is_meaningful_text("a clean recipe paragraph. " * 20) is True


def test_is_meaningful_text_false_for_short_text() -> None:
    assert is_meaningful_text("2 eggs") is False


def test_is_meaningful_text_false_for_garbled_text() -> None:
    # Long enough by length, but mostly replacement/control chars → not real text.
    assert is_meaningful_text("�" * 300) is False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_pdf_extractor_is_registered() -> None:
    assert isinstance(EXTRACTORS["pdf"], PdfExtractor)
