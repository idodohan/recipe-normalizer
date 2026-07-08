"""Router-level tests for the public-links HTTP surface.

Covers the authenticated management endpoints (POST/GET/DELETE
/api/share/public) and the unauthenticated public endpoints (GET
/api/public/{token}[/scaled]) — including a structural check that the
public routes genuinely carry no ``get_current_user`` dependency, and a
scaled-endpoint parity check against the authenticated scaling route.
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.filestore import LocalFileStore
from recipe_normalizer.sharing.router import _public_limit
from recipe_normalizer.sharing.router import public_router as sharing_public_router
from recipe_normalizer.sharing.router import router as sharing_router
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake but sniffable png data"

_RECIPE_WITH_SERVINGS = {
    "title": "Public Cake",
    "servings": {"amount": 4, "unit_text": "servings"},
    "groups": [
        {
            "name": "Main",
            "lines": [{"original_text": "2 cups flour", "quantity": 2, "unit": "cup"}],
        }
    ],
}


@pytest.fixture()
def client(db_session, tmp_path):  # type: ignore[no-untyped-def]
    return make_client(
        db_session,
        users_router,
        cookbook_router,
        sharing_router,
        sharing_public_router,
        file_store=LocalFileStore(tmp_path),
    )


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


def _create_recipe(client: TestClient, body: dict | None = None) -> str:  # type: ignore[type-arg]
    resp = client.post("/api/recipes", json=body or _RECIPE_WITH_SERVINGS)
    assert resp.status_code == 201
    return resp.json()["id"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Structural: public routes carry no auth dependency
# ---------------------------------------------------------------------------


def _walk_dependencies(dependant):  # type: ignore[no-untyped-def]
    seen = set()

    def _walk(dep):  # type: ignore[no-untyped-def]
        if id(dep) in seen:
            return
        seen.add(id(dep))
        yield dep
        for sub in dep.dependencies:
            yield from _walk(sub)

    yield from _walk(dependant)


def test_public_routes_have_no_get_current_user_dependency() -> None:
    for route in sharing_public_router.routes:
        deps = list(_walk_dependencies(route.dependant))
        assert not any(d.call is get_current_user for d in deps), (
            f"{route.path} unexpectedly depends on get_current_user"
        )


# ---------------------------------------------------------------------------
# POST /api/share/public — create / idempotency / ownership
# ---------------------------------------------------------------------------


def test_create_public_link_happy_path(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    resp = owner_client.post("/api/share/public", json={"recipe_id": recipe_id})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body.keys()) == {
        "id",
        "token",
        "recipe_id",
        "recipe_title",
        "revoked_at",
        "created_at",
    }
    assert body["recipe_id"] == recipe_id
    assert body["revoked_at"] is None


def test_create_public_link_is_idempotent(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    first = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()
    second = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()
    assert first["id"] == second["id"]
    assert first["token"] == second["token"]


def test_create_public_link_unauthenticated_401(client: TestClient) -> None:
    resp = client.post("/api/share/public", json={"recipe_id": str(uuid.uuid4())})
    assert resp.status_code == 401


def test_create_public_link_non_owner_404(owner_client: TestClient, db_session: Session) -> None:
    other_client = make_client(db_session, users_router, cookbook_router)
    _register_and_login(other_client, "stranger@example.com")
    foreign_recipe_id = _create_recipe(other_client)

    resp = owner_client.post("/api/share/public", json={"recipe_id": foreign_recipe_id})
    assert resp.status_code == 404


def test_create_public_link_missing_recipe_404(owner_client: TestClient) -> None:
    resp = owner_client.post("/api/share/public", json={"recipe_id": str(uuid.uuid4())})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/share/public — mine only
# ---------------------------------------------------------------------------


def test_list_public_links_is_owner_scoped(owner_client: TestClient, db_session: Session) -> None:
    recipe_id = _create_recipe(owner_client)
    owner_client.post("/api/share/public", json={"recipe_id": recipe_id})

    other_client = make_client(db_session, users_router, cookbook_router, sharing_router)
    _register_and_login(other_client, "other@example.com")
    other_recipe_id = _create_recipe(other_client)
    other_client.post("/api/share/public", json={"recipe_id": other_recipe_id})

    resp = owner_client.get("/api/share/public")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["recipe_id"] == recipe_id


def test_list_public_links_unauthenticated_401(client: TestClient) -> None:
    resp = client.get("/api/share/public")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/share/public/{id} — revoke
# ---------------------------------------------------------------------------


def test_revoke_public_link_then_public_get_404s(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    resp = owner_client.delete(f"/api/share/public/{link['id']}")
    assert resp.status_code == 204

    resp = owner_client.get(f"/api/public/{link['token']}")
    assert resp.status_code == 404


def test_revoke_public_link_owner_scoped_404(owner_client: TestClient, db_session: Session) -> None:
    recipe_id = _create_recipe(owner_client)
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    other_client = make_client(db_session, users_router, cookbook_router, sharing_router)
    _register_and_login(other_client, "other2@example.com")

    resp = other_client.delete(f"/api/share/public/{link['id']}")
    assert resp.status_code == 404

    # Still live for the real owner — the failed attempt didn't revoke it.
    resp = owner_client.get(f"/api/public/{link['token']}")
    assert resp.status_code == 200


def test_revoke_public_link_missing_id_404(owner_client: TestClient) -> None:
    resp = owner_client.delete(f"/api/share/public/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/public/{token} — unauthenticated
# ---------------------------------------------------------------------------


def test_get_public_recipe_happy_path_no_cookie_needed(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    # A bare client with no session cookie at all — the whole point.
    anon = TestClient(owner_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/{link['token']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == recipe_id
    assert body["title"] == "Public Cake"
    assert "image_ref" not in body
    assert body["image_url"] is None  # no image uploaded


def test_get_public_recipe_image_url_points_at_public_image_route(
    owner_client: TestClient,
) -> None:
    recipe_id = _create_recipe(owner_client)
    owner_client.put(
        f"/api/recipes/{recipe_id}/image",
        files={"file": ("photo.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    anon = TestClient(owner_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/{link['token']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_url"] == f"/api/public/{link['token']}/image"


def test_get_public_recipe_unknown_token_404(client: TestClient) -> None:
    resp = client.get("/api/public/definitely-not-a-real-token")
    assert resp.status_code == 404


def test_get_public_recipe_unknown_and_revoked_return_identical_body(
    owner_client: TestClient,
) -> None:
    recipe_id = _create_recipe(owner_client)
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()
    owner_client.delete(f"/api/share/public/{link['id']}")

    revoked_resp = owner_client.get(f"/api/public/{link['token']}")
    unknown_resp = owner_client.get("/api/public/some-bogus-token-xyz")

    assert revoked_resp.status_code == unknown_resp.status_code == 404
    assert revoked_resp.json() == unknown_resp.json()


# ---------------------------------------------------------------------------
# GET /api/public/{token}/scaled — parity with the authenticated endpoint
# ---------------------------------------------------------------------------


def test_public_scaled_matches_authenticated_scaled_for_same_factor(
    owner_client: TestClient,
) -> None:
    recipe_id = _create_recipe(owner_client)
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    authed = owner_client.get(f"/api/recipes/{recipe_id}/scaled", params={"factor": 2})
    assert authed.status_code == 200

    anon = TestClient(owner_client.app, raise_server_exceptions=False)
    public = anon.get(f"/api/public/{link['token']}/scaled", params={"factor": 2})
    assert public.status_code == 200

    authed_body = authed.json()
    public_body = public.json()
    assert authed_body == public_body


def test_public_scaled_unknown_token_404(client: TestClient) -> None:
    resp = client.get("/api/public/nope/scaled", params={"factor": 2})
    assert resp.status_code == 404


def test_public_scaled_requires_exactly_one_param(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    resp = owner_client.get(f"/api/public/{link['token']}/scaled")
    assert resp.status_code == 422

    resp = owner_client.get(
        f"/api/public/{link['token']}/scaled", params={"factor": 2, "target_servings": 8}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /api/public/{token}/image — unauthenticated, token-scoped image bytes
# ---------------------------------------------------------------------------


def test_get_public_recipe_image_happy_path_no_cookie_needed(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    owner_client.put(
        f"/api/recipes/{recipe_id}/image",
        files={"file": ("photo.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    anon = TestClient(owner_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/{link['token']}/image")
    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES
    assert resp.headers["content-type"] == "image/png"


def test_get_public_recipe_image_no_image_returns_404(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)  # never gets an image
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    anon = TestClient(owner_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/{link['token']}/image")
    assert resp.status_code == 404


def test_get_public_recipe_image_unknown_token_404(client: TestClient) -> None:
    resp = client.get("/api/public/definitely-not-a-real-token/image")
    assert resp.status_code == 404


def test_get_public_recipe_image_revoked_token_404(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    owner_client.put(
        f"/api/recipes/{recipe_id}/image",
        files={"file": ("photo.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()
    owner_client.delete(f"/api/share/public/{link['id']}")

    anon = TestClient(owner_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/{link['token']}/image")
    assert resp.status_code == 404


def test_public_image_token_cannot_fetch_a_different_recipes_image(
    owner_client: TestClient,
) -> None:
    """A valid token only ever serves ITS OWN recipe's image, never another's.

    The route accepts no client-supplied ref — only a token — so this is
    really testing that two distinct (token, recipe, image) triples never
    cross-contaminate.
    """
    recipe_a = _create_recipe(owner_client, {**_RECIPE_WITH_SERVINGS, "title": "Recipe A"})
    owner_client.put(
        f"/api/recipes/{recipe_a}/image",
        files={"file": ("a.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    link_a = owner_client.post("/api/share/public", json={"recipe_id": recipe_a}).json()

    other_png = b"\x89PNG\r\n\x1a\n" + b"a totally different fake png payload"
    recipe_b = _create_recipe(owner_client, {**_RECIPE_WITH_SERVINGS, "title": "Recipe B"})
    owner_client.put(
        f"/api/recipes/{recipe_b}/image",
        files={"file": ("b.png", io.BytesIO(other_png), "image/png")},
    )
    link_b = owner_client.post("/api/share/public", json={"recipe_id": recipe_b}).json()

    anon = TestClient(owner_client.app, raise_server_exceptions=False)
    resp_a = anon.get(f"/api/public/{link_a['token']}/image")
    resp_b = anon.get(f"/api/public/{link_b['token']}/image")

    assert resp_a.status_code == resp_b.status_code == 200
    assert resp_a.content == _PNG_BYTES
    assert resp_b.content == other_png
    assert resp_a.content != resp_b.content


def test_public_image_is_rate_limited_per_ip(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    owner_client.put(
        f"/api/recipes/{recipe_id}/image",
        files={"file": ("photo.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    for _ in range(60):
        resp = owner_client.get(f"/api/public/{link['token']}/image")
        assert resp.status_code == 200

    resp = owner_client.get(f"/api/public/{link['token']}/image")
    assert resp.status_code == 429

    _public_limit.limiter.reset()  # type: ignore[attr-defined]


def test_public_get_is_rate_limited_per_ip(owner_client: TestClient) -> None:
    recipe_id = _create_recipe(owner_client)
    link = owner_client.post("/api/share/public", json={"recipe_id": recipe_id}).json()

    for _ in range(60):
        resp = owner_client.get(f"/api/public/{link['token']}")
        assert resp.status_code == 200

    resp = owner_client.get(f"/api/public/{link['token']}")
    assert resp.status_code == 429

    _public_limit.limiter.reset()  # type: ignore[attr-defined]
