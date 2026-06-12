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
    # Check set-cookie header contains HttpOnly and SameSite=Lax
    set_cookie = resp.headers.get("set-cookie", "")
    assert "session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()


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
