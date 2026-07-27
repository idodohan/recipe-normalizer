"""Guards on the deployment wiring in docker-compose.yml.

These are cheap regression tests for two settings that are invisible in the
Python code but security-relevant in the composed deployment:

- uvicorn must trust the nginx proxy's forwarded headers, otherwise every
  request arrives with the proxy's IP and the per-IP rate limiters
  (``api_deps.limit_by_ip``) share one bucket across all users.
- ``RN_COOKIE_SECURE`` must be forwarded into the api container so a prod
  deploy can flip the session cookie to Secure without editing the compose file.
"""

from pathlib import Path

COMPOSE = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()


def _api_service_block() -> str:
    start = COMPOSE.index("\n  api:")
    end = COMPOSE.index("\n  worker:")
    return COMPOSE[start:end]


def test_api_uvicorn_trusts_proxy_headers() -> None:
    block = _api_service_block()
    assert "--proxy-headers" in block
    assert "--forwarded-allow-ips" in block


def test_api_forwards_cookie_secure_setting() -> None:
    assert "RN_COOKIE_SECURE: ${RN_COOKIE_SECURE:-false}" in _api_service_block()


def test_api_has_a_healthcheck_and_restart_policy() -> None:
    """An orchestrator must be able to tell a wedged API from a healthy one,
    and a crashed API must come back."""
    block = _api_service_block()
    assert "healthcheck:" in block
    assert "/api/health" in block
    assert "restart: unless-stopped" in block


def test_web_waits_for_api_to_be_healthy() -> None:
    """nginx must not start proxying /api/ before the API is actually up,
    or a fresh `up` briefly serves 502s."""
    web_block = COMPOSE[COMPOSE.index("\n  web:") :]
    assert "condition: service_healthy" in web_block
