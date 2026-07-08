"""SSRF guard: user-supplied URLs must never reach private/internal addresses."""

from unittest.mock import patch

import httpx
import pytest

from recipe_normalizer.extraction.base import TierFailed
from recipe_normalizer.extraction.netguard import UnsafeUrlError, assert_public_url
from recipe_normalizer.extraction.url_plugin import default_fetch


def _fake_getaddrinfo(ip: str):
    def fake(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (ip, 0))]

    return fake


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # RFC1918
        "172.16.0.1",  # RFC1918
        "192.168.1.1",  # RFC1918
        "169.254.169.254",  # cloud metadata (link-local)
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 ULA
        "fe80::1",  # IPv6 link-local
    ],
)
def test_private_addresses_rejected(ip: str) -> None:
    with patch("socket.getaddrinfo", _fake_getaddrinfo(ip)), pytest.raises(UnsafeUrlError):
        assert_public_url("http://example.com/recipe")


def test_public_address_allowed() -> None:
    with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
        assert_public_url("https://example.com/recipe")


def test_bad_scheme_rejected() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url("file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        assert_public_url("ftp://example.com/x")


def test_no_host_rejected() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url("http:///nohost")


def test_unresolvable_host_rejected() -> None:
    import socket as socket_mod

    def boom(host, port, *args, **kwargs):
        raise socket_mod.gaierror("no such host")

    with patch("socket.getaddrinfo", boom), pytest.raises(UnsafeUrlError):
        assert_public_url("http://definitely-not-a-real-host.example/")


def test_mixed_resolution_rejected() -> None:
    """If ANY resolved address is private, reject (DNS round-robin trickery)."""

    def fake(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("10.0.0.1", 0))]

    with patch("socket.getaddrinfo", fake), pytest.raises(UnsafeUrlError):
        assert_public_url("http://example.com/")


def test_default_fetch_rejects_unsafe_initial_url() -> None:
    with (
        patch("socket.getaddrinfo", _fake_getaddrinfo("127.0.0.1")),
        pytest.raises(TierFailed, match="non-public|not allowed|blocked"),
    ):
        default_fetch("http://internal.example/")


def test_default_fetch_rejects_unsafe_redirect_hop() -> None:
    """A public URL that redirects to a private address must be blocked."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, text="<html>secret</html>")

    def fake(host, port, *args, **kwargs):
        ip = "93.184.216.34" if host == "public.example" else "10.0.0.1"
        return [(2, 1, 6, "", (ip, 0))]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch("socket.getaddrinfo", fake), pytest.raises(TierFailed):
        default_fetch("http://public.example/recipe", client=client)
    assert calls == ["http://public.example/recipe"]  # never followed the redirect


def test_default_fetch_follows_safe_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://public.example/new"})
        return httpx.Response(
            200, text="<html>recipe here</html>", headers={"content-type": "text/html"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
        result = default_fetch("https://public.example/old", client=client)
    assert result.status == 200
    assert result.url == "https://public.example/new"
