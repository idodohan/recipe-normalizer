"""Image extractor plugin (spec §6.2): an uploaded photo/card is one vision block.

Small images pass through untouched. Oversized ones are downscaled (long edge
≤ 2000 px) and re-encoded to JPEG so the vision request stays bounded. The
original upload is retained as an artifact for the review screen. source_image
stays None: an uploaded recipe card or photo is the SOURCE, not necessarily a
dish hero image (spec §5).
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from PIL import Image

from recipe_normalizer.extraction.base import Acquired, TierFailed, register

if TYPE_CHECKING:
    from recipe_normalizer.filestore import FileStore
    from recipe_normalizer.llm.client import LLMClient

__all__ = ["ImageExtractor"]

logger = logging.getLogger(__name__)

# Above this size an image is downscaled + re-encoded before going to vision.
_MAX_BYTES = 3 * 1024 * 1024
_MAX_EDGE = 2000
_JPEG_QUALITY = 85

# Decompression-bomb guard: Pillow raises DecompressionBombError once a
# decoded image would exceed roughly 2x this many pixels. A small file can
# still claim a huge width/height, so this must be set before any Image.open.
Image.MAX_IMAGE_PIXELS = 50_000_000
_MAX_PIXELS = Image.MAX_IMAGE_PIXELS


def _check_pixel_cap(data: bytes) -> None:
    """Reject any image whose dimensions exceed the pixel cap, before it
    reaches the LLM. Image.open() on a BytesIO only reads the header to
    determine .size, so this is a cheap probe regardless of file size — it
    must run on every image, not just the ones big enough to hit the
    downscale path. A corrupt/undecodable image is reported the same way the
    downscale path reports Pillow errors."""
    try:
        image: Image.Image = Image.open(io.BytesIO(data))
        width, height = image.size
    except Image.DecompressionBombError as exc:
        raise TierFailed("image too large to process safely") from exc
    except Exception as exc:  # noqa: BLE001 - any Pillow decode failure is bad input
        raise TierFailed("image could not be read") from exc
    if width * height > _MAX_PIXELS:
        raise TierFailed("image too large to process safely")


def _downscale_to_jpeg(data: bytes) -> bytes:
    try:
        image: Image.Image = Image.open(io.BytesIO(data))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((_MAX_EDGE, _MAX_EDGE))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
        return buffer.getvalue()
    except Image.DecompressionBombError as exc:
        raise TierFailed("image too large to process safely") from exc


class ImageExtractor:
    """payload {"file_ref": str, "media_type": str, ...} → single vision block."""

    input_type: ClassVar[str] = "image"

    def acquire(self, payload: dict[str, Any], *, llm: LLMClient, store: FileStore) -> Acquired:
        file_ref: str = payload["file_ref"]
        media_type: str = payload["media_type"]
        data = store.open(file_ref)

        _check_pixel_cap(data)
        if len(data) > _MAX_BYTES:
            data = _downscale_to_jpeg(data)
            media_type = "image/jpeg"

        return Acquired(
            images=[(data, media_type)],
            source_image=None,
            artifacts={"source_file_ref": file_ref},
            meta={"source": "image"},
        )


register(ImageExtractor())
