"""Object-storage seam for Recipe Normalizer.

Design: local-disk implementation now; S3 (or any other backend) is a
deployment-time swap — just provide a different FileStore implementation
and wire it into get_file_store().

Layout on disk:  <root>/<sha256[:16]>/<sha256>.<suffix>

The 16-char prefix shard keeps directory listing fast even with millions
of files (same strategy as Git's object store and many CDNs).

All public methods validate refs before touching the filesystem, so no
path-traversal is possible.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path
from typing import Protocol, runtime_checkable

from recipe_normalizer.config import settings

# ---------------------------------------------------------------------------
# Protocol (structural interface — swap for S3FileStore, GCSFileStore, etc.)
# ---------------------------------------------------------------------------

_MAX_SUFFIX_LEN = 8


@runtime_checkable
class FileStore(Protocol):
    def save(self, data: bytes, *, suffix: str) -> str:
        """Persist *data* and return an opaque content-addressed *ref*."""
        ...

    def open(self, ref: str) -> bytes:
        """Return the bytes stored at *ref*.

        Raises:
            ValueError: ref resolves outside the store root (traversal).
            FileNotFoundError: ref does not exist in the store.
        """
        ...

    def exists(self, ref: str) -> bool:
        """Return True iff *ref* is present in the store."""
        ...

    def delete(self, ref: str) -> None:
        """Remove *ref* from the store (idempotent — no error if missing).

        Raises:
            ValueError: ref resolves outside the store root (traversal).
        """
        ...

    def url_path(self, ref: str) -> str:
        """Return the HTTP path for serving *ref* via the files route."""
        ...


# ---------------------------------------------------------------------------
# Local-disk implementation
# ---------------------------------------------------------------------------


def _validate_suffix(suffix: str) -> None:
    """Raise ValueError if *suffix* is not safe (alnum, ≤8 chars)."""
    if not suffix.isalnum() or len(suffix) > _MAX_SUFFIX_LEN:
        raise ValueError(
            f"Invalid suffix {suffix!r}: must be alphanumeric and at most "
            f"{_MAX_SUFFIX_LEN} characters."
        )


class LocalFileStore:
    """Content-addressed local disk store.

    Content addressing means:
    - Identical bytes always produce the same ref (idempotent saves).
    - Refs are derived purely from content; no separate metadata store needed.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_path(self, ref: str) -> Path:
        """Resolve *ref* relative to the store root, rejecting traversal.

        Raises ValueError if the resolved path escapes the root.
        """
        # Reject obviously bad refs immediately (absolute paths, leading
        # slashes, null bytes) before we even try to resolve.
        if not ref or Path(ref).is_absolute() or ref.startswith("/"):
            raise ValueError(f"Invalid ref {ref!r}: must be a relative path.")

        candidate = (self._root / ref).resolve()

        # The resolved path MUST be equal to or nested under the root.
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(
                f"Ref {ref!r} resolves outside the store root (path traversal)."
            ) from exc

        return candidate

    # ------------------------------------------------------------------
    # FileStore protocol
    # ------------------------------------------------------------------

    def save(self, data: bytes, *, suffix: str) -> str:
        """Save *data* and return its content-addressed ref.

        Layout: ``<sha256[:16]>/<sha256>.<suffix>``
        Saving identical bytes again returns the same ref without rewriting
        the file (idempotent).
        """
        _validate_suffix(suffix)
        sha = hashlib.sha256(data).hexdigest()
        shard = sha[:16]
        ref = f"{shard}/{sha}.{suffix}"
        dest = self._root / shard / f"{sha}.{suffix}"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return ref

    def open(self, ref: str) -> bytes:
        """Return the raw bytes stored at *ref*.

        Raises:
            ValueError: ref escapes the root (path traversal).
            FileNotFoundError: ref is not present in the store.
        """
        path = self._safe_path(ref)
        if not path.exists():
            raise FileNotFoundError(f"Ref {ref!r} not found in store.")
        return path.read_bytes()

    def exists(self, ref: str) -> bool:
        """Return True iff *ref* exists in the store."""
        path = self._safe_path(ref)
        return path.exists()

    def delete(self, ref: str) -> None:
        """Remove *ref* from the store.  No-op if already absent."""
        path = self._safe_path(ref)
        path.unlink(missing_ok=True)

    def url_path(self, ref: str) -> str:
        """Return the HTTP path for serving this file."""
        return f"/api/files/{ref}"


# ---------------------------------------------------------------------------
# Module-level factory (memoized so the same instance is reused per process)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _make_store() -> LocalFileStore:
    return LocalFileStore(settings.file_store_root)


def get_file_store() -> FileStore:
    """Return the process-level FileStore (LocalFileStore by default).

    Override in tests via FastAPI dependency_overrides:
        app.dependency_overrides[get_file_store] = lambda: LocalFileStore(tmp_path)
    """
    return _make_store()
