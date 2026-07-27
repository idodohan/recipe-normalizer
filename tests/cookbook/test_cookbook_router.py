"""Router-level tests for cookbook CRUD, visibility, and membership endpoints
(Task 6) — GET/POST/PATCH/DELETE /api/cookbooks + members.

Anonymous GET /api/public/cookbooks/{token} is tested in tests/test_app.py
instead: that route is defined inline in main.py's create_app(), not on any
standalone router this file's `client` fixture can mount.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.cookbook_router import router as cookbooks_router
from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

_SIMPLE_RECIPE_BODY = {
    "title": "Test Cake",
    "groups": [{"name": "Main", "lines": [{"original_text": "1 cup flour"}]}],
}


@pytest.fixture()
def client(db_session):  # type: ignore[no-untyped-def]
    return make_client(db_session, users_router, cookbook_router, cookbooks_router)


def _register_and_login(client: TestClient, email: str, display_name: str = "Tester") -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": "securepass1", "display_name": display_name},
    )
    assert resp.status_code == 201
    resp = client.post("/api/auth/login", json={"email": email, "password": "securepass1"})
    assert resp.status_code == 200


@pytest.fixture()
def owner_client(client: TestClient) -> TestClient:
    _register_and_login(client, "owner@example.com", "Owner")
    return client


def _create_cookbook(
    client: TestClient, name: str = "Weeknight", description: str | None = None
) -> dict:  # type: ignore[type-arg]
    resp = client.post("/api/cookbooks", json={"name": name, "description": description})
    assert resp.status_code == 201
    return resp.json()  # type: ignore[no-any-return]


def _create_recipe(client: TestClient, cookbook_id: str | None = None) -> str:
    params = {"cookbook_id": cookbook_id} if cookbook_id else {}
    resp = client.post("/api/recipes", params=params, json=_SIMPLE_RECIPE_BODY)
    assert resp.status_code == 201
    return resp.json()["id"]  # type: ignore[no-any-return]


def _second_client(db_session: Session, email: str = "member@example.com") -> TestClient:
    """A fresh TestClient bound to the same db_session, logged in as a new user."""
    second = make_client(db_session, users_router, cookbook_router, cookbooks_router)
    _register_and_login(second, email, "Member")
    return second


# ---------------------------------------------------------------------------
# GET /api/cookbooks
# ---------------------------------------------------------------------------


def test_list_cookbooks_includes_default(owner_client: TestClient) -> None:
    """A brand-new user with no recipes yet still sees their default cookbook."""
    resp = owner_client.get("/api/cookbooks")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "My Cookbook"
    assert body[0]["is_default"] is True
    assert body[0]["role"] == "owner"
    assert body[0]["recipe_count"] == 0


def test_list_cookbooks_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.get("/api/cookbooks")
    assert resp.status_code == 401


def test_list_cookbooks_ordered_owned_before_shared_then_name(
    owner_client: TestClient, db_session: Session
) -> None:
    _create_cookbook(owner_client, "Zebra")
    _create_cookbook(owner_client, "Apple")
    # "My Cookbook" (default) + "Apple" + "Zebra" — owned, alphabetical.
    resp = owner_client.get("/api/cookbooks")
    names = [c["name"] for c in resp.json()]
    assert names == ["Apple", "My Cookbook", "Zebra"]

    # A second user, invited as a member of "Apple", sees their own default
    # cookbook (owned) BEFORE the shared "Apple" cookbook.
    apple_id = next(c for c in resp.json() if c["name"] == "Apple")["id"]
    member = _second_client(db_session)
    invite = owner_client.post(
        f"/api/cookbooks/{apple_id}/members",
        json={"email": "member@example.com", "role": "viewer"},
    )
    assert invite.status_code == 201

    member_resp = member.get("/api/cookbooks")
    member_names = [c["name"] for c in member_resp.json()]
    assert member_names == ["My Cookbook", "Apple"]
    assert member_resp.json()[1]["role"] == "viewer"


# ---------------------------------------------------------------------------
# POST /api/cookbooks
# ---------------------------------------------------------------------------


def test_create_cookbook_happy_path(owner_client: TestClient) -> None:
    body = _create_cookbook(owner_client, "Weeknight Meals", "Quick dinners")
    assert body["name"] == "Weeknight Meals"
    assert body["description"] == "Quick dinners"
    assert body["visibility"] == "private"
    assert body["role"] == "owner"
    assert body["recipe_count"] == 0
    assert body["is_default"] is False
    assert body["public_token"] is None


# ---------------------------------------------------------------------------
# GET /api/cookbooks/{id} — detail + access sweep
# ---------------------------------------------------------------------------


def test_get_cookbook_detail_includes_recipes(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    _create_recipe(owner_client, cookbook["id"])

    resp = owner_client.get(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Weeknight"
    assert len(body["recipes"]) == 1
    assert body["recipes"][0]["title"] == "Test Cake"
    assert body["recipe_count"] == 1


def test_get_cookbook_editor_member_sees_it(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "editor"},
    )
    resp = member.get(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 200
    assert resp.json()["role"] == "editor"


def test_get_cookbook_viewer_member_sees_it(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "viewer"},
    )
    resp = member.get(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 200
    assert resp.json()["role"] == "viewer"


def test_get_cookbook_non_member_returns_404(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    stranger = _second_client(db_session, "stranger@example.com")
    resp = stranger.get(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 404


def test_get_cookbook_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.get(f"/api/cookbooks/{uuid.uuid4()}")
    assert resp.status_code == 401


def test_get_cookbook_missing_returns_404(owner_client: TestClient) -> None:
    resp = owner_client.get(f"/api/cookbooks/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/cookbooks/{id} — owner-only + visibility mints public_token
# ---------------------------------------------------------------------------


def test_update_cookbook_owner_can_rename_and_describe(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    resp = owner_client.patch(
        f"/api/cookbooks/{cookbook['id']}",
        json={"name": "Renamed", "description": "New desc"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "New desc"


def test_update_cookbook_visibility_mints_public_token(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    resp = owner_client.patch(f"/api/cookbooks/{cookbook['id']}", json={"visibility": "public"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["visibility"] == "public"
    assert body["public_token"] is not None

    # Moving back to private clears it.
    resp2 = owner_client.patch(f"/api/cookbooks/{cookbook['id']}", json={"visibility": "private"})
    assert resp2.json()["public_token"] is None


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_update_cookbook_member_forbidden_returns_404(
    owner_client: TestClient, db_session: Session, role: str
) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": role},
    )
    resp = member.patch(f"/api/cookbooks/{cookbook['id']}", json={"name": "Hijacked"})
    assert resp.status_code == 404


def test_update_cookbook_non_member_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    cookbook = _create_cookbook(owner_client)
    stranger = _second_client(db_session, "stranger@example.com")
    resp = stranger.patch(f"/api/cookbooks/{cookbook['id']}", json={"name": "Hijacked"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/cookbooks/{id} — owner-only, 409 on default
# ---------------------------------------------------------------------------


def test_delete_cookbook_happy_path(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    resp = owner_client.delete(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 204
    assert owner_client.get(f"/api/cookbooks/{cookbook['id']}").status_code == 404


def test_delete_default_cookbook_returns_409(owner_client: TestClient) -> None:
    default_id = owner_client.get("/api/cookbooks").json()[0]["id"]
    resp = owner_client.delete(f"/api/cookbooks/{default_id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "cannot_delete_default"


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_delete_cookbook_member_forbidden_returns_404(
    owner_client: TestClient, db_session: Session, role: str
) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": role},
    )
    resp = member.delete(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 404


def test_delete_cookbook_non_member_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    cookbook = _create_cookbook(owner_client)
    stranger = _second_client(db_session, "stranger@example.com")
    resp = stranger.delete(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def test_invite_member_happy_path(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    _second_client(db_session)
    resp = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "editor"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "editor"
    assert body["cookbook_id"] == cookbook["id"]


def test_invite_member_reinvite_upserts_role(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "viewer"},
    )
    resp = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "editor"},
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "editor"


def test_invite_owner_own_email_returns_422(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    resp = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "owner@example.com", "role": "editor"},
    )
    assert resp.status_code == 422


def test_invite_unknown_email_returns_404(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    resp = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "nobody@example.com", "role": "editor"},
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_invite_member_forbidden_for_non_owner(
    owner_client: TestClient, db_session: Session, role: str
) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": role},
    )
    stranger = _second_client(db_session, "stranger@example.com")
    resp = member.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "stranger@example.com", "role": "viewer"},
    )
    assert resp.status_code == 404
    # Also confirm the stranger got registered (sanity — used only to prove
    # the invite really was rejected, not that the recipient didn't exist).
    assert stranger.get("/api/cookbooks").status_code == 200


def test_update_member_role(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    _second_client(db_session)
    invite = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "viewer"},
    )
    user_id = invite.json()["user_id"]
    resp = owner_client.patch(
        f"/api/cookbooks/{cookbook['id']}/members/{user_id}", json={"role": "editor"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "editor"


def test_update_member_role_missing_member_returns_404(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    resp = owner_client.patch(
        f"/api/cookbooks/{cookbook['id']}/members/{uuid.uuid4()}", json={"role": "editor"}
    )
    assert resp.status_code == 404


def test_remove_member(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    invite = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "viewer"},
    )
    user_id = invite.json()["user_id"]
    resp = owner_client.delete(f"/api/cookbooks/{cookbook['id']}/members/{user_id}")
    assert resp.status_code == 204
    # The former member has lost access.
    assert member.get(f"/api/cookbooks/{cookbook['id']}").status_code == 404


def test_remove_member_idempotent_for_non_member(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client)
    resp = owner_client.delete(f"/api/cookbooks/{cookbook['id']}/members/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_leave_cookbook(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "viewer"},
    )
    resp = member.post(f"/api/cookbooks/{cookbook['id']}/leave")
    assert resp.status_code == 204
    assert member.get(f"/api/cookbooks/{cookbook['id']}").status_code == 404
