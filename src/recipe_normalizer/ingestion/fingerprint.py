"""Content fingerprinting for ingestion deduplication.

All functions are pure (no I/O, no database).  The fingerprints are used as
stable identity keys in the ``jobs.source_fingerprint`` and
``recipes.source_fingerprint`` columns.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse

# ---------------------------------------------------------------------------
# Tracking / noise query-parameter keys to strip before fingerprinting.
# Prefix-matched keys (e.g. utm_*) are listed as plain prefixes without "*".
# ---------------------------------------------------------------------------

_STRIP_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)
_STRIP_PARAM_EXACT: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
    }
)

# Default ports per scheme — strip them if present, they're noise.
_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
}


def _is_tracking_param(key: str) -> bool:
    """Return True if *key* is a tracking / noise query parameter."""
    lower = key.lower()
    if lower in _STRIP_PARAM_EXACT:
        return True
    return any(lower.startswith(prefix) for prefix in _STRIP_PARAM_PREFIXES)


def fingerprint_url(url: str) -> str:
    """Compute a stable SHA-256 fingerprint for a URL.

    Normalisation steps applied before hashing:

    1. Parse the URL.
    2. Lowercase scheme + host.
    3. Strip default ports (80 for http, 443 for https).
    4. Drop tracking query parameters (utm_*, fbclid, gclid, mc_cid,
       mc_eid, igshid, ref).
    5. Sort remaining query parameters by key (stable ordering).
    6. Strip the fragment (#…).
    7. Strip a trailing slash from the path (but keep the path ``/`` if it
       is the only path component — we only strip *trailing* slashes from
       longer paths).

    Returns the lowercase hex SHA-256 digest of the normalised URL string.
    """
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""  # hostname is already lowercased by urlparse
    port = parsed.port

    # Build netloc: host only if port is default or absent.
    netloc = f"{host}:{port}" if port is not None and _DEFAULT_PORTS.get(scheme) != port else host

    # Normalise path: strip single trailing slash (but keep root "/")
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Filter + sort query params
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    kept.sort(key=lambda kv: kv[0])
    query = urlencode(kept)

    # Reconstruct without fragment
    normalised = f"{scheme}://{netloc}{path}"
    if query:
        normalised = f"{normalised}?{query}"

    return hashlib.sha256(normalised.encode()).hexdigest()


def fingerprint_bytes(data: bytes) -> str:
    """Return the SHA-256 hexdigest of *data* (raw bytes, no normalisation)."""
    return hashlib.sha256(data).hexdigest()


def fingerprint_text(text: str) -> str:
    """Return a stable SHA-256 fingerprint for a text string.

    Normalisation: strip leading/trailing whitespace, collapse internal
    whitespace runs to a single space, casefold.  Then SHA-256 the UTF-8
    encoding of the result.
    """
    # collapse all internal whitespace (spaces, tabs, newlines, etc.)
    normalised = re.sub(r"\s+", " ", text.strip()).casefold()
    return hashlib.sha256(normalised.encode()).hexdigest()
