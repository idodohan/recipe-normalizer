"""Tests for the PDF extractor (extraction/pdf_plugin.py), spec §6.2.

Real fixture PDFs (committed; regenerate via fixtures/make_media_fixtures.py):
text_layer_recipe.pdf has a selectable text layer → text path; scanned_recipe.pdf
is image-only → vision (page-render) path. NO LLM here — the plugin only
acquires; normalize runs later against the stub seam.
"""

from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium
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


def _pdf_with_page_size(width_pt: float, height_pt: float) -> bytes:
    """A blank single-page PDF with an explicit mediabox — cheap to build
    (no rasterized content) regardless of how large the page claims to be."""
    document = pdfium.PdfDocument.new()
    document.new_page(width_pt, height_pt)
    buffer = io.BytesIO()
    document.save(buffer)
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
# Per-page decompression-bomb guard
# ---------------------------------------------------------------------------


def test_page_over_pixel_cap_is_skipped_with_others_rendered(store: LocalFileStore) -> None:
    """A huge page (mediabox implies >50M px at 144dpi) is skipped; a normal
    page in the same document still gets rendered."""
    huge = pdfium.PdfDocument(_pdf_with_page_size(5000, 5000))  # 10000x10000px @144dpi
    normal = pdfium.PdfDocument(_multipage_scanned_pdf(1))
    huge.import_pages(normal, pages=[0])
    buffer = io.BytesIO()
    huge.save(buffer)
    ref = store.save(buffer.getvalue(), suffix="pdf")
    payload = {"file_ref": ref, "filename": "mixed.pdf", "media_type": "application/pdf"}

    acquired = PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    # Only the normal-sized page was rendered; the oversized one was skipped.
    assert len(acquired.images) == 1
    assert acquired.meta["page_count"] == 1


def test_all_pages_over_pixel_cap_raises_tier_failed(store: LocalFileStore) -> None:
    ref = store.save(_pdf_with_page_size(5000, 5000), suffix="pdf")
    payload = {"file_ref": ref, "filename": "huge.pdf", "media_type": "application/pdf"}

    with pytest.raises(TierFailed) as exc_info:
        PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert "too large" in exc_info.value.reason.lower()


def test_page_with_unknown_mediabox_is_skipped_with_others_rendered(
    store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When get_mediabox() returns None the pixel-cap check cannot run — an
    unknown-size page must be treated conservatively and skipped, not rendered
    unbounded. Other pages in the same document are unaffected."""
    original_get_mediabox = pdfium.PdfPage.get_mediabox
    calls = {"count": 0}

    def fake_get_mediabox(self: pdfium.PdfPage, *args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original_get_mediabox(self, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfPage, "get_mediabox", fake_get_mediabox)

    ref = store.save(_multipage_scanned_pdf(2), suffix="pdf")
    payload = {"file_ref": ref, "filename": "unknown_box.pdf", "media_type": "application/pdf"}

    acquired = PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    # Only the page with a known mediabox was rendered; the unknown one was skipped.
    assert len(acquired.images) == 1
    assert acquired.meta["page_count"] == 1


def test_all_pages_with_unknown_mediabox_raises_tier_failed(
    store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdfium.PdfPage, "get_mediabox", lambda self, *a, **kw: None)  # noqa: ARG005

    ref = store.save(_multipage_scanned_pdf(1), suffix="pdf")
    payload = {"file_ref": ref, "filename": "unknown.pdf", "media_type": "application/pdf"}

    with pytest.raises(TierFailed) as exc_info:
        PdfExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert "too large" in exc_info.value.reason.lower()


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
