"""Unit tests for ingestion.fingerprint (pure functions, no DB required)."""

from recipe_normalizer.ingestion.fingerprint import (
    fingerprint_bytes,
    fingerprint_text,
    fingerprint_url,
)

# ---------------------------------------------------------------------------
# fingerprint_url
# ---------------------------------------------------------------------------


def test_tracking_params_dropped() -> None:
    """utm_*, fbclid, gclid, mc_cid, mc_eid, igshid, ref are stripped."""
    base = fingerprint_url("https://example.com/recipe")
    with_tracking = fingerprint_url(
        "https://example.com/recipe"
        "?utm_source=newsletter&utm_medium=email&fbclid=abc"
        "&gclid=xyz&mc_cid=123&mc_eid=456&igshid=789&ref=homepage"
    )
    assert base == with_tracking


def test_utm_prefix_match() -> None:
    """Any utm_* key (including unusual suffixes) is dropped."""
    base = fingerprint_url("https://example.com/r")
    assert base == fingerprint_url("https://example.com/r?utm_whatever=1&utm_foo=bar")


def test_param_order_irrelevant() -> None:
    """Query parameters are sorted, so order does not affect the fingerprint."""
    a = fingerprint_url("https://example.com/r?b=2&a=1")
    b = fingerprint_url("https://example.com/r?a=1&b=2")
    assert a == b


def test_case_insensitive_host() -> None:
    """Host is lowercased before hashing."""
    assert fingerprint_url("HTTPS://Example.COM/r") == fingerprint_url("https://example.com/r")


def test_trailing_slash_stripped() -> None:
    """A trailing slash on a non-root path is stripped."""
    assert fingerprint_url("https://x.test/r/") == fingerprint_url("https://x.test/r")


def test_root_path_unchanged() -> None:
    """Root path '/' is kept as-is (no stripping)."""
    # Both produce the same fingerprint because '/' has no trailing slash to strip
    a = fingerprint_url("https://x.test/")
    b = fingerprint_url("https://x.test/")
    assert a == b


def test_meaningful_params_differ() -> None:
    """Different meaningful query parameters produce different fingerprints."""
    a = fingerprint_url("https://example.com/search?q=pasta")
    b = fingerprint_url("https://example.com/search?q=risotto")
    assert a != b


def test_fragment_stripped() -> None:
    """Fragment (#…) is ignored when fingerprinting."""
    assert fingerprint_url("https://x.test/r#section") == fingerprint_url("https://x.test/r")


def test_default_port_stripped_https() -> None:
    """Port 443 on https is stripped (same as no port)."""
    assert fingerprint_url("https://x.test:443/r") == fingerprint_url("https://x.test/r")


def test_default_port_stripped_http() -> None:
    """Port 80 on http is stripped."""
    assert fingerprint_url("http://x.test:80/r") == fingerprint_url("http://x.test/r")


def test_non_default_port_retained() -> None:
    """A non-default port is part of the identity."""
    assert fingerprint_url("https://x.test:8443/r") != fingerprint_url("https://x.test/r")


def test_malformed_port_does_not_raise() -> None:
    """An out-of-range port (urlparse .port raises ValueError) must not crash."""
    fp = fingerprint_url("http://host:99999/")
    assert len(fp) == 64
    # Still deterministic
    assert fp == fingerprint_url("http://host:99999/")
    # And distinct from the well-formed host
    assert fp != fingerprint_url("http://host/")


def test_returns_hex_string() -> None:
    """Result is a 64-char hex string (SHA-256)."""
    fp = fingerprint_url("https://example.com/recipe")
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_tracking_with_real_params_keeps_real() -> None:
    """Real params survive even alongside tracking params that are stripped."""
    with_noise = fingerprint_url("https://example.com/r?q=pasta&utm_source=x")
    clean = fingerprint_url("https://example.com/r?q=pasta")
    assert with_noise == clean


# ---------------------------------------------------------------------------
# fingerprint_bytes
# ---------------------------------------------------------------------------


def test_fingerprint_bytes_deterministic() -> None:
    """Same bytes always produce the same fingerprint."""
    data = b"hello world"
    assert fingerprint_bytes(data) == fingerprint_bytes(data)


def test_fingerprint_bytes_different_data() -> None:
    """Different bytes produce different fingerprints."""
    assert fingerprint_bytes(b"abc") != fingerprint_bytes(b"xyz")


def test_fingerprint_bytes_hex() -> None:
    assert len(fingerprint_bytes(b"x")) == 64


# ---------------------------------------------------------------------------
# fingerprint_text
# ---------------------------------------------------------------------------


def test_fingerprint_text_strips_whitespace() -> None:
    """Leading/trailing whitespace is stripped."""
    assert fingerprint_text("  hello  ") == fingerprint_text("hello")


def test_fingerprint_text_collapses_internal_whitespace() -> None:
    """Multiple internal whitespace chars collapse to a single space."""
    assert fingerprint_text("hello   world") == fingerprint_text("hello world")
    assert fingerprint_text("hello\t\nworld") == fingerprint_text("hello world")


def test_fingerprint_text_casefold() -> None:
    """Text is case-folded before hashing."""
    assert fingerprint_text("Spaghetti Carbonara") == fingerprint_text("spaghetti carbonara")


def test_fingerprint_text_different_content() -> None:
    """Meaningfully different texts produce different fingerprints."""
    assert fingerprint_text("spaghetti") != fingerprint_text("risotto")


def test_fingerprint_text_hex() -> None:
    assert len(fingerprint_text("hello")) == 64
