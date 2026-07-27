"""Router-level tests for /api/auth endpoints."""

import pytest
from fastapi.testclient import TestClient

from recipe_normalizer.users.router import router
from tests.api_helpers import make_client


@pytest.fixture()
def client(db_session):  # type: ignore[no-untyped-def]
    return make_client(db_session, router)


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------


def test_register_returns_201_with_user_fields(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": "alice@example.com", "password": "securepass1", "display_name": "Alice"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert "id" in body
    assert "password" not in body
    assert "password_hash" not in body


def test_register_duplicate_returns_401_envelope(client: TestClient) -> None:
    payload = {"email": "bob@example.com", "password": "securepass1", "display_name": "Bob"}
    client.post("/api/auth/register", json=payload)
    resp = client.post("/api/auth/register", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "auth_error"
    assert "message" in body["error"]


def test_register_bad_email_returns_422_envelope(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": "not-an-email", "password": "securepass1", "display_name": "C"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "message" in body["error"]


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


def test_login_returns_200_and_sets_session_cookie(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={"email": "dave@example.com", "password": "securepass1", "display_name": "Dave"},
    )
    resp = client.post(
        "/api/auth/login",
        json={"email": "dave@example.com", "password": "securepass1"},
    )
    assert resp.status_code == 200
    # Check set-cookie header contains HttpOnly, SameSite=Lax and a Max-Age
    set_cookie = resp.headers.get("set-cookie", "")
    assert "session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "max-age=" in set_cookie.lower()


def test_login_oversized_password_rejected_before_hashing(client: TestClient) -> None:
    """A huge password must 422 at validation, never reach argon2.

    Both login branches (known and unknown email) run an argon2 verify with a
    64 MiB memory cost, so an unbounded password field is a cheap CPU/memory
    amplifier. Same bound applies to register.
    """
    huge = "a" * 5000
    resp = client.post("/api/auth/login", json={"email": "mallory@example.com", "password": huge})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"

    resp = client.post(
        "/api/auth/register",
        json={"email": "mallory@example.com", "password": huge, "display_name": "M"},
    )
    assert resp.status_code == 422


def test_login_wrong_password_returns_401_envelope(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={"email": "eve@example.com", "password": "securepass1", "display_name": "Eve"},
    )
    resp = client.post(
        "/api/auth/login",
        json={"email": "eve@example.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "auth_error"


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------


def test_me_returns_user_after_login(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={"email": "frank@example.com", "password": "securepass1", "display_name": "Frank"},
    )
    client.post(
        "/api/auth/login",
        json={"email": "frank@example.com", "password": "securepass1"},
    )
    # TestClient carries cookies from login automatically
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "frank@example.com"


def test_me_without_cookie_returns_401_envelope(client: TestClient) -> None:
    resp = client.get("/api/auth/me", cookies={})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_login_rate_limited_after_threshold_returns_429(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={"email": "harry@example.com", "password": "securepass1", "display_name": "Harry"},
    )
    payload = {"email": "harry@example.com", "password": "wrongpassword"}
    statuses = [client.post("/api/auth/login", json=payload).status_code for _ in range(11)]
    assert 429 in statuses
    limited_resp = client.post("/api/auth/login", json=payload)
    assert limited_resp.status_code == 429
    assert limited_resp.json()["error"]["code"] == "rate_limited"


def test_register_rate_limited_after_threshold_returns_429(client: TestClient) -> None:
    responses = [
        client.post(
            "/api/auth/register",
            json={
                "email": f"reguser{i}@example.com",
                "password": "securepass1",
                "display_name": f"Reg{i}",
            },
        )
        for i in range(11)
    ]
    statuses = [r.status_code for r in responses]
    assert 429 in statuses


def test_logout_returns_204_and_subsequent_me_returns_401(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={"email": "gina@example.com", "password": "securepass1", "display_name": "Gina"},
    )
    client.post(
        "/api/auth/login",
        json={"email": "gina@example.com", "password": "securepass1"},
    )
    logout_resp = client.post("/api/auth/logout")
    assert logout_resp.status_code == 204

    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 401
    body = me_resp.json()
    assert body["error"]["code"] == "unauthorized"
