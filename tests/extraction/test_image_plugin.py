"""Tests for the image extractor (extraction/image_plugin.py), spec §6.2.

An image input is a single vision block. Oversized images are downscaled +
re-encoded to JPEG so the LLM call stays bounded. The original upload is
retained as an artifact; source_image stays None (an uploaded card/photo is the
SOURCE, not necessarily a dish hero image — spec §5).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from recipe_normalizer.extraction import EXTRACTORS
from recipe_normalizer.extraction.base import TierFailed
from recipe_normalizer.extraction.image_plugin import ImageExtractor
from recipe_normalizer.filestore import LocalFileStore

IMAGE_FIXTURES = Path(__file__).parent / "fixtures" / "images"
_THREE_MB = 3 * 1024 * 1024


@pytest.fixture()
def store(tmp_path: Path) -> LocalFileStore:
    return LocalFileStore(tmp_path)


class _UnusedLLM:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"image acquire must not touch the LLM (accessed {name!r})")


def _payload(store: LocalFileStore, data: bytes, *, suffix: str, media_type: str) -> dict[str, str]:
    ref = store.save(data, suffix=suffix)
    return {"file_ref": ref, "filename": f"x.{suffix}", "media_type": media_type}


def _big_image_bytes() -> bytes:
    """A noisy image that exceeds 3 MB as PNG (incompressible) and is large."""
    import os

    image = Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Passthrough
# ---------------------------------------------------------------------------


def test_jpeg_passes_through_as_single_block(store: LocalFileStore) -> None:
    data = (IMAGE_FIXTURES / "recipe_photo.jpg").read_bytes()
    payload = _payload(store, data, suffix="jpg", media_type="image/jpeg")

    acquired = ImageExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert len(acquired.images) == 1
    out_data, media_type = acquired.images[0]
    assert media_type == "image/jpeg"
    assert out_data == data  # small enough → untouched
    assert acquired.source_image is None
    assert acquired.artifacts["source_file_ref"] == payload["file_ref"]
    assert acquired.meta["source"] == "image"


def test_png_card_passes_through(store: LocalFileStore) -> None:
    data = (IMAGE_FIXTURES / "handwritten_card.png").read_bytes()
    payload = _payload(store, data, suffix="png", media_type="image/png")

    acquired = ImageExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    assert acquired.images[0][1] == "image/png"
    assert acquired.images[0][0] == data
    assert acquired.source_image is None


# ---------------------------------------------------------------------------
# Oversized → downscale + re-encode
# ---------------------------------------------------------------------------


def test_oversized_image_is_downscaled_and_reencoded(store: LocalFileStore) -> None:
    big = _big_image_bytes()
    assert len(big) > _THREE_MB  # precondition
    payload = _payload(store, big, suffix="png", media_type="image/png")

    acquired = ImageExtractor().acquire(payload, llm=_UnusedLLM(), store=store)  # type: ignore[arg-type]

    out_data, media_type = acquired.images[0]
    assert media_type == "image/jpeg"  # re-encoded
    assert len(out_data) < _THREE_MB  # smaller than the cap now
    sent = Image.open(io.BytesIO(out_data))
    assert max(sent.size) <= 2000  # long edge clamped
    # The original (full-size) upload is still retained untouched.
    assert store.open(payload["file_ref"]) == big


# ---------------------------------------------------------------------------
# Decompression-bomb guard
# ---------------------------------------------------------------------------


def test_downscale_raises_tier_failed_on_decompression_bomb() -> None:
    """A tiny-on-disk image that decodes to an enormous pixel count must be
    rejected as TierFailed, not allowed to blow up memory/CPU."""
    from recipe_normalizer.extraction.image_plugin import _downscale_to_jpeg

    # 12000x10000 = 120M pixels, well past 2x the 50M cap. Solid fill keeps
    # the encoded PNG tiny — this is exactly the small-file/huge-pixel shape
    # a decompression bomb exploits.
    huge = Image.new("1", (12000, 10000))
    buffer = io.BytesIO()
    huge.save(buffer, format="PNG")
    data = buffer.getvalue()

    with pytest.raises(TierFailed, match="too large"):
        _downscale_to_jpeg(data)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_image_extractor_is_registered() -> None:
    assert isinstance(EXTRACTORS["image"], ImageExtractor)
