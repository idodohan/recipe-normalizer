"""Router-level tests for /api/ai/conversations."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.ai.router import router as ai_router
from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

_SIMPLE_RECIPE_BODY = {
    "title": "Test Cake",
    "groups": [{"name": "Main", "lines": [{"original_text": "1 cup flour"}]}],
}


@pytest.fixture()
def client(db_session):  # type: ignore[no-untyped-def]
    return make_client(db_session, users_router, cookbook_router, ai_router)


def _register_and_login(client: TestClient, email: str) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": "securepass1", "display_name": "Tester"},
    )
    assert resp.status_code == 201
    resp = client.post("/api/auth/login", json={"email": email, "password": "securepass1"})
    assert resp.status_code == 200


@pytest.fixture()
def owner_client(client: TestClient) -> TestClient:
    _register_and_login(client, "owner@example.com")
    return client


def _create_recipe(client: TestClient) -> str:
    resp = client.post("/api/recipes", json=_SIMPLE_RECIPE_BODY)
    assert resp.status_code == 201
    return resp.json()["id"]  # type: ignore[no-any-return]


def _register_second_client(db_session: Session, email: str = "other@example.com") -> TestClient:
    """A second TestClient bound to the SAME db_session, registered + logged in.

    Mirrors tests/sharing/test_router.py's `_register_recipient` pattern.
    """
    second = make_client(db_session, users_router, cookbook_router, ai_router)
    resp = second.post(
        "/api/auth/register",
        json={"email": email, "password": "securepass1", "display_name": "Other"},
    )
    assert resp.status_code == 201
    resp = second.post("/api/auth/login", json={"email": email, "password": "securepass1"})
    assert resp.status_code == 200
    return second


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_and_get_conversation(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)

    resp = owner_client.post(
        "/api/ai/conversations",
        json={"recipe_id": recipe_id, "kind": "recipe_chat", "title": "Ask about this cake"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["recipe_id"] == recipe_id
    assert body["kind"] == "recipe_chat"
    assert body["title"] == "Ask about this cake"
    conversation_id = body["id"]

    resp = owner_client.get(f"/api/ai/conversations/{conversation_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == conversation_id
    assert detail["messages"] == []


def test_create_cookbook_qa_conversation_without_recipe(owner_client: TestClient) -> None:
    resp = owner_client.post("/api/ai/conversations", json={"kind": "cookbook_qa"})
    assert resp.status_code == 201
    assert resp.json()["recipe_id"] is None


def test_list_conversations_filters_by_recipe_id(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    resp = owner_client.post(
        "/api/ai/conversations", json={"recipe_id": recipe_id, "kind": "recipe_chat"}
    )
    assert resp.status_code == 201
    owner_client.post("/api/ai/conversations", json={"kind": "cookbook_qa"})

    resp = owner_client.get("/api/ai/conversations", params={"recipe_id": recipe_id})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["recipe_id"] == recipe_id


# ---------------------------------------------------------------------------
# Auth / ownership
# ---------------------------------------------------------------------------


def test_conversations_require_authentication(client: TestClient) -> None:
    assert client.get("/api/ai/conversations").status_code == 401
    assert client.post("/api/ai/conversations", json={"kind": "cookbook_qa"}).status_code == 401
    assert client.get(f"/api/ai/conversations/{uuid.uuid4()}").status_code == 401


def test_get_conversation_owned_by_someone_else_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    resp = owner_client.post("/api/ai/conversations", json={"kind": "cookbook_qa"})
    conversation_id = resp.json()["id"]

    other = _register_second_client(db_session)
    resp = other.get(f"/api/ai/conversations/{conversation_id}")
    assert resp.status_code == 404


def test_create_conversation_for_foreign_recipe_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    other = _register_second_client(db_session)
    foreign_recipe_id = _create_recipe(other)

    resp = owner_client.post(
        "/api/ai/conversations", json={"recipe_id": foreign_recipe_id, "kind": "recipe_chat"}
    )
    assert resp.status_code == 404


def test_get_missing_conversation_returns_404(owner_client: TestClient) -> None:
    resp = owner_client.get(f"/api/ai/conversations/{uuid.uuid4()}")
    assert resp.status_code == 404
