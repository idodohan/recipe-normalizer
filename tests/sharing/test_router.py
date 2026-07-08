"""Router-level tests for POST /api/share/recipe."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.sharing.router import _share_limit
from recipe_normalizer.sharing.router import router as sharing_router
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

_SIMPLE_RECIPE_BODY = {
    "title": "Test Cake",
    "groups": [{"name": "Main", "lines": [{"original_text": "1 cup flour"}]}],
}


@pytest.fixture()
def client(db_session):  # type: ignore[no-untyped-def]
    return make_client(db_session, users_router, cookbook_router, sharing_router)


def _register_and_login(client: TestClient, email: str) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": "securepass1", "display_name": "Tester"},
    )
    assert resp.status_code == 201
    resp = client.post("/api/auth/login", json={"email": email, "password": "securepass1"})
    assert resp.status_code == 200


@pytest.fixture()
def sharer_client(client: TestClient) -> TestClient:
    _register_and_login(client, "sharer@example.com")
    return client


def _create_recipe(client: TestClient) -> str:
    resp = client.post("/api/recipes", json=_SIMPLE_RECIPE_BODY)
    assert resp.status_code == 201
    return resp.json()["id"]  # type: ignore[no-any-return]


def _register_recipient(db_session: Session, email: str = "recipient@example.com") -> None:
    """Register a second user directly against the same db_session.

    A throwaway TestClient bound to the same db_session is used purely to
    hit /api/auth/register — sharing.service looks the recipient up by
    email, not by which TestClient instance created them, so any client
    sharing the same `db_session` works.
    """
    app_client = make_client(db_session, users_router)
    resp = app_client.post(
        "/api/auth/register",
        json={"email": email, "password": "securepass1", "display_name": "Recipient"},
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_share_recipe_happy_path(sharer_client: TestClient, db_session: Session) -> None:
    _register_recipient(db_session)
    recipe_id = _create_recipe(sharer_client)

    resp = sharer_client.post(
        "/api/share/recipe", json={"recipe_id": recipe_id, "to_email": "recipient@example.com"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert set(body.keys()) == {"id", "copied_recipe_id", "to_email", "created_at"}
    assert body["copied_recipe_id"] != recipe_id
    assert body["to_email"] == "recipient@example.com"


# ---------------------------------------------------------------------------
# Auth / validation errors
# ---------------------------------------------------------------------------


def test_share_recipe_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/api/share/recipe",
        json={"recipe_id": str(uuid.uuid4()), "to_email": "someone@example.com"},
    )
    assert resp.status_code == 401


def test_share_recipe_unknown_recipient_returns_404(sharer_client: TestClient) -> None:
    recipe_id = _create_recipe(sharer_client)
    resp = sharer_client.post(
        "/api/share/recipe", json={"recipe_id": recipe_id, "to_email": "nobody@example.com"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "recipient_not_found"


def test_share_recipe_self_share_returns_422(sharer_client: TestClient) -> None:
    recipe_id = _create_recipe(sharer_client)
    resp = sharer_client.post(
        "/api/share/recipe", json={"recipe_id": recipe_id, "to_email": "sharer@example.com"}
    )
    assert resp.status_code == 422


def test_share_recipe_non_owner_returns_404(sharer_client: TestClient, db_session: Session) -> None:
    _register_recipient(db_session)
    # A recipe owned by "recipient" (not the sharer) — sharer tries to share it.
    # A fresh client bound to the same db_session, logged in as the recipient,
    # creates the recipe that belongs to them instead of the sharer.
    recipient_client = make_client(db_session, users_router, cookbook_router)
    login_resp = recipient_client.post(
        "/api/auth/login", json={"email": "recipient@example.com", "password": "securepass1"}
    )
    assert login_resp.status_code == 200
    foreign_recipe_id = _create_recipe(recipient_client)

    resp = sharer_client.post(
        "/api/share/recipe",
        json={"recipe_id": foreign_recipe_id, "to_email": "recipient@example.com"},
    )
    assert resp.status_code == 404


def test_share_recipe_missing_recipe_returns_404(
    sharer_client: TestClient, db_session: Session
) -> None:
    _register_recipient(db_session)
    resp = sharer_client.post(
        "/api/share/recipe",
        json={"recipe_id": str(uuid.uuid4()), "to_email": "recipient@example.com"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Rate limit smoke test
# ---------------------------------------------------------------------------


def test_share_recipe_rate_limited_after_30_per_hour(
    sharer_client: TestClient, db_session: Session
) -> None:
    _register_recipient(db_session)
    recipe_id = _create_recipe(sharer_client)

    for _ in range(30):
        resp = sharer_client.post(
            "/api/share/recipe",
            json={"recipe_id": recipe_id, "to_email": "recipient@example.com"},
        )
        assert resp.status_code == 201

    resp = sharer_client.post(
        "/api/share/recipe", json={"recipe_id": recipe_id, "to_email": "recipient@example.com"}
    )
    assert resp.status_code == 429

    _share_limit.limiter.reset()  # type: ignore[attr-defined]
