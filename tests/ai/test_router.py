"""Router-level tests for /api/ai/conversations."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.ai.router import _chat_limit, _cookbook_qa_limit, _transform_limit
from recipe_normalizer.ai.router import router as ai_router
from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.ingestion.router import router as ingestion_router
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

_SIMPLE_RECIPE_BODY = {
    "title": "Test Cake",
    "groups": [{"name": "Main", "lines": [{"original_text": "1 cup flour"}]}],
}


@pytest.fixture()
def client(db_session):  # type: ignore[no-untyped-def]
    # ingestion_router is included so transform tests can follow a returned
    # job_id straight into the EXISTING review-gate endpoints
    # (GET /api/jobs, GET /api/jobs/{id}, POST .../accept) — the whole point
    # of Task 6's integration choice is that those need zero changes.
    return make_client(db_session, users_router, cookbook_router, ai_router, ingestion_router)


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
    second = make_client(db_session, users_router, cookbook_router, ai_router, ingestion_router)
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


# ---------------------------------------------------------------------------
# POST /conversations/{id}/messages — recipe chat
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every chat-endpoint test runs against the RN_LLM_STUB=1 canned LLM — no real API calls."""
    monkeypatch.setenv("RN_LLM_STUB", "1")


def _create_chat_conversation(client: TestClient, recipe_id: str) -> str:
    resp = client.post(
        "/api/ai/conversations", json={"recipe_id": recipe_id, "kind": "recipe_chat"}
    )
    assert resp.status_code == 201
    return resp.json()["id"]  # type: ignore[no-any-return]


def test_post_chat_message_returns_assistant_reply_and_persists_both_turns(
    owner_client: TestClient,
) -> None:
    recipe_id = _create_recipe(owner_client)
    conversation_id = _create_chat_conversation(owner_client, recipe_id)

    resp = owner_client.post(
        f"/api/ai/conversations/{conversation_id}/messages",
        json={"content": "How long do I bake this?"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "assistant"
    assert body["content"]  # the stub's canned reply

    detail = owner_client.get(f"/api/ai/conversations/{conversation_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "How long do I bake this?"


def test_post_chat_message_requires_authentication(client: TestClient) -> None:
    resp = client.post(f"/api/ai/conversations/{uuid.uuid4()}/messages", json={"content": "hi"})
    assert resp.status_code == 401


def test_post_chat_message_rejects_empty_content(
    owner_client: TestClient,
) -> None:
    recipe_id = _create_recipe(owner_client)
    conversation_id = _create_chat_conversation(owner_client, recipe_id)

    resp = owner_client.post(
        f"/api/ai/conversations/{conversation_id}/messages", json={"content": ""}
    )
    assert resp.status_code == 422


def test_post_chat_message_on_cookbook_qa_conversation_returns_422(
    owner_client: TestClient,
) -> None:
    resp = owner_client.post("/api/ai/conversations", json={"kind": "cookbook_qa"})
    conversation_id = resp.json()["id"]

    resp = owner_client.post(
        f"/api/ai/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert resp.status_code == 422


def test_post_chat_message_on_missing_conversation_returns_404(owner_client: TestClient) -> None:
    resp = owner_client.post(
        f"/api/ai/conversations/{uuid.uuid4()}/messages", json={"content": "hi"}
    )
    assert resp.status_code == 404


def test_post_chat_message_owned_by_someone_else_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    recipe_id = _create_recipe(owner_client)
    conversation_id = _create_chat_conversation(owner_client, recipe_id)

    other = _register_second_client(db_session)
    resp = other.post(f"/api/ai/conversations/{conversation_id}/messages", json={"content": "hi"})
    assert resp.status_code == 404


def test_post_chat_message_rate_limited_after_60_per_hour(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)

    # The limit is per-user, not per-conversation — use a fresh conversation
    # per call so the per-conversation 50-message cap never interferes.
    for _ in range(60):
        conversation_id = _create_chat_conversation(owner_client, recipe_id)
        resp = owner_client.post(
            f"/api/ai/conversations/{conversation_id}/messages", json={"content": "hi"}
        )
        assert resp.status_code == 201

    conversation_id = _create_chat_conversation(owner_client, recipe_id)
    resp = owner_client.post(
        f"/api/ai/conversations/{conversation_id}/messages", json={"content": "one too many"}
    )
    assert resp.status_code == 429

    _chat_limit.limiter.reset()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# POST /cookbook-qa
# ---------------------------------------------------------------------------


def test_post_cookbook_qa_returns_message_and_referenced_recipe_ids(
    owner_client: TestClient,
) -> None:
    _create_recipe(owner_client)

    resp = owner_client.post("/api/ai/cookbook-qa", json={"content": "What can I make tonight?"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"]  # the stub's canned reply
    assert "referenced_recipe_ids" in body
    assert isinstance(body["referenced_recipe_ids"], list)
    assert body["conversation_id"]

    detail = owner_client.get(f"/api/ai/conversations/{body['conversation_id']}").json()
    assert detail["kind"] == "cookbook_qa"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_post_cookbook_qa_continues_an_existing_conversation(owner_client: TestClient) -> None:
    resp = owner_client.post("/api/ai/cookbook-qa", json={"content": "first question"})
    conversation_id = resp.json()["conversation_id"]

    resp = owner_client.post(
        "/api/ai/cookbook-qa",
        json={"content": "second question", "conversation_id": conversation_id},
    )
    assert resp.status_code == 201
    assert resp.json()["conversation_id"] == conversation_id

    detail = owner_client.get(f"/api/ai/conversations/{conversation_id}").json()
    assert len(detail["messages"]) == 4


def test_post_cookbook_qa_requires_authentication(client: TestClient) -> None:
    resp = client.post("/api/ai/cookbook-qa", json={"content": "hi"})
    assert resp.status_code == 401


def test_post_cookbook_qa_rejects_empty_content(owner_client: TestClient) -> None:
    resp = owner_client.post("/api/ai/cookbook-qa", json={"content": ""})
    assert resp.status_code == 422


def test_post_cookbook_qa_on_recipe_chat_conversation_returns_422(
    owner_client: TestClient,
) -> None:
    recipe_id = _create_recipe(owner_client)
    conversation_id = _create_chat_conversation(owner_client, recipe_id)

    resp = owner_client.post(
        "/api/ai/cookbook-qa",
        json={"content": "hi", "conversation_id": conversation_id},
    )
    assert resp.status_code == 422


def test_post_cookbook_qa_on_someone_elses_conversation_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    resp = owner_client.post("/api/ai/cookbook-qa", json={"content": "hi"})
    conversation_id = resp.json()["conversation_id"]

    other = _register_second_client(db_session)
    resp = other.post(
        "/api/ai/cookbook-qa",
        json={"content": "hi", "conversation_id": conversation_id},
    )
    assert resp.status_code == 404


def test_post_cookbook_qa_rate_limited_after_60_per_hour(owner_client: TestClient) -> None:
    for _ in range(60):
        resp = owner_client.post("/api/ai/cookbook-qa", json={"content": "hi"})
        assert resp.status_code == 201

    resp = owner_client.post("/api/ai/cookbook-qa", json={"content": "one too many"})
    assert resp.status_code == 429

    _cookbook_qa_limit.limiter.reset()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# POST /recipes/{recipe_id}/transform
# ---------------------------------------------------------------------------


def test_post_transform_returns_job_and_recipe_ids_routed_through_the_review_gate(
    owner_client: TestClient,
) -> None:
    recipe_id = _create_recipe(owner_client)

    resp = owner_client.post(
        f"/api/ai/recipes/{recipe_id}/transform", json={"instruction": "make it vegan"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["job_id"]
    assert body["recipe_id"]

    # Reachable via the EXISTING job/review endpoints — zero new review UI.
    job_resp = owner_client.get(f"/api/jobs/{body['job_id']}")
    assert job_resp.status_code == 200
    job = job_resp.json()
    assert job["status"] == "needs_review"
    assert job["input_type"] == "transform"
    assert job["produced_recipe_ids"] == [body["recipe_id"]]

    jobs_list = owner_client.get("/api/jobs").json()
    assert any(j["id"] == body["job_id"] for j in jobs_list)

    # The produced draft carries derived_from — fetchable via the normal recipe GET.
    draft = owner_client.get(f"/api/recipes/{body['recipe_id']}").json()
    assert draft["derived_from"] == recipe_id
    assert draft["provenance"]["transformed_from"] == recipe_id
    assert draft["provenance"]["instruction"] == "make it vegan"
    assert draft["is_verified"] is False

    # The existing accept endpoint works on it unmodified.
    accept_resp = owner_client.post(
        f"/api/jobs/{body['job_id']}/recipes/{body['recipe_id']}/accept"
    )
    assert accept_resp.status_code == 204


def test_post_transform_pure_scaling_instruction_refused_without_llm_call(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unset the stub for this one call to prove NO LLM client method is ever
    # invoked for a pure-scaling instruction — if the scaling boundary didn't
    # short-circuit before the LLM call, this would blow up trying to reach
    # the real Anthropic API (no ANTHROPIC_API_KEY in tests).
    monkeypatch.delenv("RN_LLM_STUB", raising=False)
    recipe_id = _create_recipe(owner_client)

    resp = owner_client.post(
        f"/api/ai/recipes/{recipe_id}/transform", json={"instruction": "halve it"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "use_scale_feature"
    assert resp.json()["error"]["scale_endpoint"] == f"/api/recipes/{recipe_id}/scaled"


def test_post_transform_requires_authentication(client: TestClient) -> None:
    resp = client.post(
        f"/api/ai/recipes/{uuid.uuid4()}/transform", json={"instruction": "make it vegan"}
    )
    assert resp.status_code == 401


def test_post_transform_rejects_empty_instruction(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    resp = owner_client.post(f"/api/ai/recipes/{recipe_id}/transform", json={"instruction": ""})
    assert resp.status_code == 422


def test_post_transform_missing_recipe_returns_404(owner_client: TestClient) -> None:
    resp = owner_client.post(
        f"/api/ai/recipes/{uuid.uuid4()}/transform", json={"instruction": "make it vegan"}
    )
    assert resp.status_code == 404


def test_post_transform_foreign_recipe_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    other = _register_second_client(db_session)
    foreign_recipe_id = _create_recipe(other)

    resp = owner_client.post(
        f"/api/ai/recipes/{foreign_recipe_id}/transform", json={"instruction": "make it vegan"}
    )
    assert resp.status_code == 404


def test_post_transform_rate_limited_after_20_per_hour(owner_client: TestClient) -> None:
    for _ in range(20):
        recipe_id = _create_recipe(owner_client)
        resp = owner_client.post(
            f"/api/ai/recipes/{recipe_id}/transform", json={"instruction": "make it vegan"}
        )
        assert resp.status_code == 201

    recipe_id = _create_recipe(owner_client)
    resp = owner_client.post(
        f"/api/ai/recipes/{recipe_id}/transform", json={"instruction": "one too many"}
    )
    assert resp.status_code == 429

    _transform_limit.limiter.reset()  # type: ignore[attr-defined]
