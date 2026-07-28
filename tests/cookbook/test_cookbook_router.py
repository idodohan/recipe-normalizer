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


def test_get_cookbook_masks_the_owners_favorite_flag_for_a_member(
    owner_client: TestClient, db_session: Session
) -> None:
    """The cookbook's recipe list answers `is_favorite` exactly like the flat list.

    Same recipe, two endpoints: `GET /api/cookbooks/{id}` and
    `GET /api/recipes` must never disagree about whose favorite it is.
    `is_favorite` is the OWNER's personal flag and `set_personal` is
    owner-only, so a member seeing it as theirs would render a filled heart
    whose toggle 404s.
    """
    cookbook = _create_cookbook(owner_client)
    recipe_id = _create_recipe(owner_client, cookbook["id"])
    assert (
        owner_client.patch(f"/api/recipes/{recipe_id}/personal", json={"is_favorite": True})
    ).status_code == 200

    member = _second_client(db_session)
    owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": "editor"},
    )

    owner_detail = owner_client.get(f"/api/cookbooks/{cookbook['id']}").json()
    assert owner_detail["recipes"][0]["is_favorite"] is True
    owner_flat = owner_client.get("/api/recipes").json()
    assert next(r for r in owner_flat["items"] if r["id"] == recipe_id)["is_favorite"] is True

    member_detail = member.get(f"/api/cookbooks/{cookbook['id']}").json()
    assert member_detail["recipes"][0]["is_favorite"] is False
    member_flat = member.get("/api/recipes").json()
    assert next(r for r in member_flat["items"] if r["id"] == recipe_id)["is_favorite"] is False


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
def test_get_cookbook_hides_public_token_from_members(
    owner_client: TestClient, db_session: Session, role: str
) -> None:
    """``public_token`` is the shareable secret — only the owner ever sees it.

    For an unlisted cookbook the token grants anonymous, non-expiring read
    access to anyone holding it, so a member (editor or viewer) must not be
    handed it: only the owner decides who it goes to.
    """
    cookbook = _create_cookbook(owner_client, "Secret Book")
    patch_resp = owner_client.patch(
        f"/api/cookbooks/{cookbook['id']}", json={"visibility": "unlisted"}
    )
    token = patch_resp.json()["public_token"]
    assert token is not None

    member = _second_client(db_session)
    assert (
        owner_client.post(
            f"/api/cookbooks/{cookbook['id']}/members",
            json={"email": "member@example.com", "role": role},
        )
    ).status_code == 201

    member_body = member.get(f"/api/cookbooks/{cookbook['id']}").json()
    assert member_body["role"] == role
    assert member_body["public_token"] is None

    # The owner still gets the real token back from the same route.
    owner_body = owner_client.get(f"/api/cookbooks/{cookbook['id']}").json()
    assert owner_body["public_token"] == token


def test_get_cookbook_hides_public_token_from_a_non_member_viewer(
    owner_client: TestClient, db_session: Session
) -> None:
    """A stranger who resolves to viewer via `public` visibility gets no token."""
    cookbook = _create_cookbook(owner_client, "Open Book")
    token = owner_client.patch(
        f"/api/cookbooks/{cookbook['id']}", json={"visibility": "public"}
    ).json()["public_token"]
    assert token is not None

    stranger = _second_client(db_session, "stranger@example.com")
    body = stranger.get(f"/api/cookbooks/{cookbook['id']}").json()
    assert body["role"] == "viewer"
    assert body["public_token"] is None


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


def test_delete_cookbook_with_recipes_returns_204_and_keeps_them(
    owner_client: TestClient,
) -> None:
    """Deleting a NON-EMPTY cookbook is a 204 and the recipes SURVIVE.

    The boards inversion of the Phase-1 behavior: ``recipes.cookbook_id``'s ON
    DELETE CASCADE used to destroy every recipe the cookbook held, and this test
    asserted exactly that. With the column gone (migration a3f7c2d8e015) a
    recipe placed only here is re-filed into the owner's default cookbook, so it
    is still readable and still listed.
    """
    cookbook = _create_cookbook(owner_client, "Full")
    recipe_id = _create_recipe(owner_client, cookbook["id"])
    assert owner_client.get(f"/api/recipes/{recipe_id}").status_code == 200

    resp = owner_client.delete(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 204

    assert owner_client.get(f"/api/cookbooks/{cookbook['id']}").status_code == 404
    # The recipe is untouched — readable, listed, and now on the default board.
    assert owner_client.get(f"/api/recipes/{recipe_id}").status_code == 200
    listed = owner_client.get("/api/recipes").json()
    assert any(r["id"] == recipe_id for r in listed["items"])
    default_id = next(c["id"] for c in owner_client.get("/api/cookbooks").json() if c["is_default"])
    placements = owner_client.get(f"/api/recipes/{recipe_id}/cookbooks").json()
    assert [c["id"] for c in placements] == [default_id]


def test_delete_cookbook_with_a_member_returns_204(
    owner_client: TestClient, db_session: Session
) -> None:
    """Deleting a cookbook that has a member is a 204, and the member loses it."""
    cookbook = _create_cookbook(owner_client, "Shared")
    member = _second_client(db_session)
    assert (
        owner_client.post(
            f"/api/cookbooks/{cookbook['id']}/members",
            json={"email": "member@example.com", "role": "editor"},
        )
    ).status_code == 201
    assert any(c["id"] == cookbook["id"] for c in member.get("/api/cookbooks").json())

    resp = owner_client.delete(f"/api/cookbooks/{cookbook['id']}")
    assert resp.status_code == 204

    # Membership row went with the cookbook — it's no longer in the member's list.
    assert all(c["id"] != cookbook["id"] for c in member.get("/api/cookbooks").json())
    assert member.get(f"/api/cookbooks/{cookbook['id']}").status_code == 404


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


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_update_member_role_forbidden_for_non_owner(
    owner_client: TestClient, db_session: Session, role: str
) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    invite = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": role},
    )
    member_user_id = invite.json()["user_id"]
    stranger = _second_client(db_session, "stranger@example.com")
    resp = member.patch(
        f"/api/cookbooks/{cookbook['id']}/members/{member_user_id}", json={"role": "editor"}
    )
    assert resp.status_code == 404
    resp2 = stranger.patch(
        f"/api/cookbooks/{cookbook['id']}/members/{member_user_id}", json={"role": "editor"}
    )
    assert resp2.status_code == 404


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


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_remove_member_forbidden_for_non_owner(
    owner_client: TestClient, db_session: Session, role: str
) -> None:
    cookbook = _create_cookbook(owner_client)
    member = _second_client(db_session)
    invite = owner_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": "member@example.com", "role": role},
    )
    member_user_id = invite.json()["user_id"]
    stranger = _second_client(db_session, "stranger@example.com")

    # A member can't remove themselves (or anyone) via this owner-only route.
    resp = member.delete(f"/api/cookbooks/{cookbook['id']}/members/{member_user_id}")
    assert resp.status_code == 404
    resp2 = stranger.delete(f"/api/cookbooks/{cookbook['id']}/members/{member_user_id}")
    assert resp2.status_code == 404

    # The member is still a member — neither forbidden attempt actually removed them.
    assert member.get(f"/api/cookbooks/{cookbook['id']}").status_code == 200


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


# ---------------------------------------------------------------------------
# Recipe placements — POST/DELETE/GET /api/recipes/{id}/cookbooks (Task 2)
# ---------------------------------------------------------------------------


def test_save_own_recipe_to_another_cookbook_is_a_reference(owner_client: TestClient) -> None:
    """Own recipe -> another of my cookbooks: a plain reference, no copy."""
    cookbook = _create_cookbook(owner_client, "First")
    second = _create_cookbook(owner_client, "Second")
    recipe_id = _create_recipe(owner_client, cookbook["id"])

    resp = owner_client.post(
        f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": second["id"]}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body == {"cookbook_id": second["id"], "recipe_id": recipe_id, "copied": False}

    # ...and it now shows up in BOTH cookbooks' detail views.
    first_detail = owner_client.get(f"/api/cookbooks/{cookbook['id']}").json()
    second_detail = owner_client.get(f"/api/cookbooks/{second['id']}").json()
    assert [r["id"] for r in first_detail["recipes"]] == [recipe_id]
    assert [r["id"] for r in second_detail["recipes"]] == [recipe_id]
    assert second_detail["recipe_count"] == 1


def test_save_own_recipe_requires_editor_on_the_target_cookbook(
    owner_client: TestClient, db_session: Session
) -> None:
    cookbook = _create_cookbook(owner_client, "Mine")
    recipe_id = _create_recipe(owner_client, cookbook["id"])
    stranger = _second_client(db_session, "stranger_save1@example.com")
    strangers_cookbook = stranger.post("/api/cookbooks", json={"name": "Not Yours"}).json()

    resp = owner_client.post(
        f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": strangers_cookbook["id"]}
    )
    assert resp.status_code == 404


def test_save_someone_elses_readable_recipe_makes_an_owned_copy(
    owner_client: TestClient, db_session: Session
) -> None:
    """A public recipe, saved by someone else, is COPIED into their cookbook."""
    cookbook = _create_cookbook(owner_client, "Public Book")
    recipe_id = _create_recipe(owner_client, cookbook["id"])
    visibility_resp = owner_client.patch(
        f"/api/cookbooks/{cookbook['id']}", json={"visibility": "public"}
    )
    assert visibility_resp.status_code == 200

    saver = _second_client(db_session, "saver@example.com")
    mine = saver.post("/api/cookbooks", json={"name": "Mine"}).json()

    resp = saver.post(f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": mine["id"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["copied"] is True
    assert body["cookbook_id"] == mine["id"]
    assert body["recipe_id"] != recipe_id

    # The copy lands in the CHOSEN cookbook, and only there.
    mine_detail = saver.get(f"/api/cookbooks/{mine['id']}").json()
    assert [r["id"] for r in mine_detail["recipes"]] == [body["recipe_id"]]
    # The original is untouched.
    original_detail = owner_client.get(f"/api/cookbooks/{cookbook['id']}").json()
    assert [r["id"] for r in original_detail["recipes"]] == [recipe_id]
    # The saver can read (and owns) their new copy; the original owner cannot
    # be impersonated by it — a fresh row with a fresh id.
    copy_resp = saver.get(f"/api/recipes/{body['recipe_id']}")
    assert copy_resp.status_code == 200


def test_save_unreadable_recipe_returns_404(owner_client: TestClient, db_session: Session) -> None:
    cookbook = _create_cookbook(owner_client, "Private Book")
    recipe_id = _create_recipe(owner_client, cookbook["id"])

    stranger = _second_client(db_session, "stranger_save2@example.com")
    mine = stranger.post("/api/cookbooks", json={"name": "Mine"}).json()

    resp = stranger.post(f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": mine["id"]})
    assert resp.status_code == 404


def test_remove_recipe_placement(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client, "First")
    second = _create_cookbook(owner_client, "Second")
    recipe_id = _create_recipe(owner_client, cookbook["id"])
    owner_client.post(f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": second["id"]})

    resp = owner_client.delete(f"/api/recipes/{recipe_id}/cookbooks/{cookbook['id']}")
    assert resp.status_code == 204

    remaining = owner_client.get(f"/api/recipes/{recipe_id}/cookbooks").json()
    assert [c["id"] for c in remaining] == [second["id"]]


def test_remove_last_placement_returns_409(owner_client: TestClient) -> None:
    cookbook = _create_cookbook(owner_client, "Only One")
    recipe_id = _create_recipe(owner_client, cookbook["id"])

    resp = owner_client.delete(f"/api/recipes/{recipe_id}/cookbooks/{cookbook['id']}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "last_placement"


def test_list_recipe_cookbooks_scoped_to_readable(
    owner_client: TestClient, db_session: Session
) -> None:
    """A recipe in my cookbook + a cookbook I've lost access to: only mine shows."""
    mine = _create_cookbook(owner_client, "Mine")
    recipe_id = _create_recipe(owner_client, mine["id"])

    stranger = _second_client(db_session, "stranger_scope@example.com")
    shared = stranger.post("/api/cookbooks", json={"name": "Stranger's Shared"}).json()
    invite = stranger.post(
        f"/api/cookbooks/{shared['id']}/members",
        json={"email": "owner@example.com", "role": "editor"},
    )
    assert invite.status_code == 201
    owner_user_id = invite.json()["user_id"]

    place_resp = owner_client.post(
        f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": shared["id"]}
    )
    assert place_resp.status_code == 201

    before = owner_client.get(f"/api/recipes/{recipe_id}/cookbooks")
    assert before.status_code == 200
    assert {c["id"] for c in before.json()} == {mine["id"], shared["id"]}

    # Revoke the owner's membership — the placement in `shared` survives, but
    # the owner can no longer read that cookbook.
    remove_resp = stranger.delete(f"/api/cookbooks/{shared['id']}/members/{owner_user_id}")
    assert remove_resp.status_code == 204

    after = owner_client.get(f"/api/recipes/{recipe_id}/cookbooks")
    assert after.status_code == 200
    assert [c["id"] for c in after.json()] == [mine["id"]]
    # The recipe itself is still readable (they're its owner) — the router
    # never 404s the whole GET just because one placement fell out of reach.
    assert owner_client.get(f"/api/recipes/{recipe_id}").status_code == 200


def test_list_recipe_cookbooks_unreadable_recipe_returns_404(
    owner_client: TestClient, db_session: Session
) -> None:
    cookbook = _create_cookbook(owner_client, "Private Book")
    recipe_id = _create_recipe(owner_client, cookbook["id"])
    stranger = _second_client(db_session, "stranger_scope2@example.com")

    resp = stranger.get(f"/api/recipes/{recipe_id}/cookbooks")
    assert resp.status_code == 404


def test_list_recipe_cookbooks_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.get(f"/api/recipes/{uuid.uuid4()}/cookbooks")
    assert resp.status_code == 401


def test_cookbook_detail_shows_a_placed_not_created_here_recipe(
    owner_client: TestClient,
) -> None:
    """GET /api/cookbooks/{id} recipes come from the join, not just cookbook_id."""
    created_in = _create_cookbook(owner_client, "Created In")
    placed_into = _create_cookbook(owner_client, "Placed Into")
    recipe_id = _create_recipe(owner_client, created_in["id"])

    resp = owner_client.post(
        f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": placed_into["id"]}
    )
    assert resp.status_code == 201

    detail = owner_client.get(f"/api/cookbooks/{placed_into['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert [r["id"] for r in body["recipes"]] == [recipe_id]
    assert body["recipe_count"] == 1
