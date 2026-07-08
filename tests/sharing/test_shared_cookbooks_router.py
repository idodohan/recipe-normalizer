"""Router-level tests for /api/shared-cookbooks and the widened cookbook
endpoints (GET/PATCH/DELETE /api/recipes/{id}) as seen through shared-cookbook
membership.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.sharing.router import router as sharing_router
from recipe_normalizer.sharing.router import shared_cookbooks_router
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

_SIMPLE_RECIPE_BODY = {
    "title": "Test Cake",
    "groups": [{"name": "Main", "lines": [{"original_text": "1 cup flour"}]}],
}


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    return make_client(
        db_session, users_router, cookbook_router, sharing_router, shared_cookbooks_router
    )


def _register_and_login(client: TestClient, email: str, display_name: str = "Tester") -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": "securepass1", "display_name": display_name},
    )
    assert resp.status_code == 201
    resp = client.post("/api/auth/login", json={"email": email, "password": "securepass1"})
    assert resp.status_code == 200


def _second_client(db_session: Session, email: str, display_name: str) -> TestClient:
    """A fresh TestClient bound to the same db_session, logged in as a second user."""
    c = make_client(
        db_session, users_router, cookbook_router, sharing_router, shared_cookbooks_router
    )
    _register_and_login(c, email, display_name)
    return c


@pytest.fixture()
def alice_client(client: TestClient) -> TestClient:
    _register_and_login(client, "alice@example.com", "Alice")
    return client


@pytest.fixture()
def bob_client(db_session: Session) -> TestClient:
    return _second_client(db_session, "bob@example.com", "Bob")


def _create_recipe(client: TestClient) -> str:
    resp = client.post("/api/recipes", json=_SIMPLE_RECIPE_BODY)
    assert resp.status_code == 201
    return resp.json()["id"]  # type: ignore[no-any-return]


def _create_cookbook(client: TestClient, name: str = "Sundays") -> dict:
    resp = client.post("/api/shared-cookbooks", json={"name": name})
    assert resp.status_code == 201
    return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# create / list
# ---------------------------------------------------------------------------


def test_create_shared_cookbook_happy_path(alice_client: TestClient) -> None:
    body = _create_cookbook(alice_client)
    assert body["name"] == "Sundays"
    assert body["member_count"] == 1
    assert body["recipe_count"] == 0


def test_create_shared_cookbook_unauthenticated_401(client: TestClient) -> None:
    resp = client.post("/api/shared-cookbooks", json={"name": "Sundays"})
    assert resp.status_code == 401


def test_list_shared_cookbooks_only_mine(alice_client: TestClient, bob_client: TestClient) -> None:
    _create_cookbook(alice_client, "Alice's")
    _create_cookbook(bob_client, "Bob's")

    resp = alice_client.get("/api/shared-cookbooks")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert names == ["Alice's"]


# ---------------------------------------------------------------------------
# get detail
# ---------------------------------------------------------------------------


def test_get_shared_cookbook_detail(alice_client: TestClient, bob_client: TestClient) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    recipe_id = _create_recipe(alice_client)
    alice_client.post(f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": recipe_id})

    resp = alice_client.get(f"/api/shared-cookbooks/{cb['id']}")
    assert resp.status_code == 200
    body = resp.json()
    member_names = {m["display_name"]: m["is_creator"] for m in body["members"]}
    assert member_names == {"Alice": True, "Bob": False}
    assert [r["id"] for r in body["recipes"]] == [recipe_id]
    assert "email" not in body["members"][0]


def test_get_shared_cookbook_non_member_returns_404(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    cb = _create_cookbook(alice_client)
    resp = bob_client.get(f"/api/shared-cookbooks/{cb['id']}")
    assert resp.status_code == 404


def test_get_shared_cookbook_unknown_id_returns_404(alice_client: TestClient) -> None:
    resp = alice_client.get(f"/api/shared-cookbooks/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


def test_invite_member_happy_path(alice_client: TestClient, bob_client: TestClient) -> None:
    cb = _create_cookbook(alice_client)
    resp = alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    assert resp.status_code == 201
    assert resp.json()["display_name"] == "Bob"


def test_invite_member_unknown_email_returns_404(alice_client: TestClient) -> None:
    cb = _create_cookbook(alice_client)
    resp = alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "nobody@example.com"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "recipient_not_found"


def test_invite_member_already_member_returns_409(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    resp = alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_member"


def test_remove_member_self_leave(alice_client: TestClient, bob_client: TestClient) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    bob_id = bob_client.get("/api/auth/me").json()["id"]

    resp = bob_client.delete(f"/api/shared-cookbooks/{cb['id']}/members/{bob_id}")
    assert resp.status_code == 204

    detail = alice_client.get(f"/api/shared-cookbooks/{cb['id']}").json()
    assert len(detail["members"]) == 1


def test_remove_member_creator_cannot_leave_while_others_present(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    alice_id = alice_client.get("/api/auth/me").json()["id"]

    resp = alice_client.delete(f"/api/shared-cookbooks/{cb['id']}/members/{alice_id}")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "creator_cannot_leave"


# ---------------------------------------------------------------------------
# recipes
# ---------------------------------------------------------------------------


def test_add_and_remove_recipe(alice_client: TestClient, bob_client: TestClient) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    recipe_id = _create_recipe(alice_client)

    resp = alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": recipe_id}
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == recipe_id

    resp = alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": recipe_id}
    )
    assert resp.status_code == 409

    # Bob (a mere member) can remove it.
    resp = bob_client.delete(f"/api/shared-cookbooks/{cb['id']}/recipes/{recipe_id}")
    assert resp.status_code == 204

    detail = alice_client.get(f"/api/shared-cookbooks/{cb['id']}").json()
    assert detail["recipes"] == []


def test_add_recipe_without_access_returns_404(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    # A recipe bob doesn't own and has no shared access to.
    foreign_recipe_id = _create_recipe(alice_client)
    # Remove bob's blanket access by not adding it — bob has none yet, so
    # adding it himself should 404 (no owner/member claim on this recipe).
    resp = bob_client.post(
        f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": foreign_recipe_id}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# the widened cookbook endpoints, via HTTP
# ---------------------------------------------------------------------------


def test_member_can_get_and_patch_shared_recipe_over_http(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    recipe_id = _create_recipe(alice_client)
    alice_client.post(f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": recipe_id})

    resp = bob_client.get(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 200

    patch_body = {**_SIMPLE_RECIPE_BODY, "title": "Bob's edit"}
    resp = bob_client.patch(f"/api/recipes/{recipe_id}", json=patch_body)
    assert resp.status_code == 200
    assert resp.json()["title"] == "Bob's edit"
    bob_id = bob_client.get("/api/auth/me").json()["id"]
    assert resp.json()["last_edited_by"] == bob_id


def test_member_cannot_delete_shared_recipe_over_http(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    recipe_id = _create_recipe(alice_client)
    alice_client.post(f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": recipe_id})

    resp = bob_client.delete(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 404

    resp = alice_client.get(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 200


def test_non_member_gets_404_over_http(alice_client: TestClient, bob_client: TestClient) -> None:
    cb = _create_cookbook(alice_client)
    recipe_id = _create_recipe(alice_client)
    alice_client.post(f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": recipe_id})

    resp = bob_client.get(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Owner-only gates: a shared-cookbook MEMBER (not the owner) must not be able
# to mint a public link or copy-on-share a recipe they don't own, and must
# never see the owner's personal fields on a plain GET.
# ---------------------------------------------------------------------------


def _share_recipe_with_bob(alice_client: TestClient) -> tuple[dict, str]:  # type: ignore[type-arg]
    cb = _create_cookbook(alice_client)
    alice_client.post(
        f"/api/shared-cookbooks/{cb['id']}/members", json={"email": "bob@example.com"}
    )
    recipe_id = _create_recipe(alice_client)
    alice_client.post(f"/api/shared-cookbooks/{cb['id']}/recipes", json={"recipe_id": recipe_id})
    return cb, recipe_id


def test_member_cannot_create_public_link_for_shared_recipe(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    _cb, recipe_id = _share_recipe_with_bob(alice_client)

    resp = bob_client.post("/api/share/public", json={"recipe_id": recipe_id})
    assert resp.status_code == 404

    # And alice (the real owner) never sees a link she didn't create.
    assert alice_client.get("/api/share/public").json() == []


def test_member_cannot_share_recipe_they_dont_own(
    alice_client: TestClient, bob_client: TestClient, db_session: Session
) -> None:
    _cb, recipe_id = _share_recipe_with_bob(alice_client)
    # carol must actually exist — otherwise a 404 here would just be
    # recipient_not_found, not the ownership gate this test targets.
    _second_client(db_session, "carol@example.com", "Carol")

    resp = bob_client.post(
        "/api/share/recipe", json={"recipe_id": recipe_id, "to_email": "carol@example.com"}
    )
    assert resp.status_code == 404


def test_member_get_recipe_scrubs_owner_personal_fields(
    alice_client: TestClient, bob_client: TestClient, db_session: Session
) -> None:
    from recipe_normalizer.cookbook.models import Recipe

    _cb, recipe_id = _share_recipe_with_bob(alice_client)

    # Alice (the owner) sets all of her personal/owner-only fields.
    resp = alice_client.patch(
        f"/api/recipes/{recipe_id}/personal",
        json={"is_favorite": True, "notes": "Family secret recipe notes"},
    )
    assert resp.status_code == 200
    collection = alice_client.post("/api/collections", json={"name": "Faves"}).json()
    resp = alice_client.put(
        f"/api/recipes/{recipe_id}/collections", json={"collection_ids": [collection["id"]]}
    )
    assert resp.status_code == 200

    # provenance isn't settable via a public endpoint — stamp it directly,
    # same trick the sharing leak-audit test uses, to cover the worst case.
    recipe_row = db_session.get(Recipe, uuid.UUID(recipe_id))
    assert recipe_row is not None
    recipe_row.provenance = {
        "shared_by": "someone@example.com",
        "shared_at": "2020-01-01T00:00:00+00:00",
        "origin_recipe_id": str(uuid.uuid4()),
    }
    db_session.flush()

    # Owner GET is unchanged — alice still sees all four fields.
    owner_resp = alice_client.get(f"/api/recipes/{recipe_id}")
    assert owner_resp.status_code == 200
    owner_body = owner_resp.json()
    assert owner_body["notes"] == "Family secret recipe notes"
    assert owner_body["is_favorite"] is True
    assert owner_body["collection_ids"] == [collection["id"]]
    assert owner_body["provenance"] is not None

    # Bob (a mere member) sees none of it.
    resp = bob_client.get(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["notes"] is None
    assert body["is_favorite"] is False
    assert body["collection_ids"] == []
    assert body["provenance"] is None
