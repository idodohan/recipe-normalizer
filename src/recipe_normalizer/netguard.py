"""Guards outbound fetches of user-supplied URLs against SSRF.

Every URL the pipeline fetches — the submitted page, hero images discovered in
page content, and each redirect hop — must pass ``assert_public_url`` first.
The check resolves the hostname and rejects any address that is not globally
routable (loopback, RFC1918, link-local incl. 169.254.169.254 cloud metadata,
CGNAT, ULA, unspecified). Resolution happens at check time, so a TOCTOU/DNS-
rebinding window remains; acceptable at friends-and-family scale, documented.

``settings.netguard_allow_hosts`` is a test/e2e escape hatch: a comma-separated
list of exact hostnames (case-insensitive) that skip address resolution
entirely, so local fixture servers (e.g. Playwright's ``localhost:8099``) can
be exercised without being rejected as private addresses. The scheme check
still applies to allowlisted hosts. This setting MUST stay empty in
production — it is read from config, not hardcoded, specifically so tests can
opt in via env/monkeypatch without weakening the default guard.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from recipe_normalizer.config import settings

__all__ = ["MAX_REDIRECTS", "UnsafeUrlError", "assert_public_url"]

MAX_REDIRECTS = 5

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(ValueError):
    """The URL is not safe to fetch server-side."""


def _allowlisted_hosts() -> frozenset[str]:
    return frozenset(
        h.strip().lower() for h in settings.netguard_allow_hosts.split(",") if h.strip()
    )


def assert_public_url(url: str) -> None:
    """Raise UnsafeUrlError unless *url* is http(s) to a globally-routable host."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"scheme '{parsed.scheme or '(none)'}' is not allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")
    if host.lower() in _allowlisted_hosts():
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve host '{host}'") from exc
    if not infos:
        raise UnsafeUrlError(f"could not resolve host '{host}'")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise UnsafeUrlError(f"host '{host}' resolved to an invalid address") from exc
        if not ip.is_global:  # is_global handles IPv4-mapped IPv6 correctly on Python >= 3.12.4
            raise UnsafeUrlError(f"host '{host}' resolves to a non-public address")
