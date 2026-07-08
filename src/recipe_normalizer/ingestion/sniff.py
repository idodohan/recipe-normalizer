"""Magic-byte media-type detection for uploads. Client Content-Type is a hint,
never trusted — the sniffed type decides validation and storage suffix."""

from __future__ import annotations

__all__ = ["detect_media_type"]

_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def detect_media_type(data: bytes) -> str | None:
    for sig, media_type in _SIGNATURES:
        if data.startswith(sig):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
