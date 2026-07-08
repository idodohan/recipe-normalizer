"""Magic-byte media-type detection tests (ingestion/sniff.py)."""

from __future__ import annotations

import pytest

from recipe_normalizer.ingestion.sniff import detect_media_type


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7 rest", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n rest", "image/png"),
        (b"\xff\xd8\xff\xe0 rest", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBP rest", "image/webp"),
        (b"GIF89a rest", "image/gif"),
        (b"GIF87a rest", "image/gif"),
        (b"<html>not a file</html>", None),
        (b"", None),
    ],
)
def test_detect_media_type(data: bytes, expected: str | None) -> None:
    assert detect_media_type(data) == expected
