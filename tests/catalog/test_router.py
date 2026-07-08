"""Router-level tests for /api/catalog endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from recipe_normalizer.catalog import service
from recipe_normalizer.catalog.router import router as catalog_router
from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_session):  # type: ignore[no-untyped-def]
    return make_client(db_session, users_router, catalog_router)


@pytest.fixture()
def auth_client(client: TestClient, db_session):  # type: ignore[no-untyped-def]
    """A TestClient already authenticated with a registered + logged-in user."""
    client.post(
        "/api/auth/register",
        json={"email": "tester@example.com", "password": "securepass1", "display_name": "Tester"},
    )
    resp = client.post(
        "/api/auth/login",
        json={"email": "tester@example.com", "password": "securepass1"},
    )
    assert resp.status_code == 200
    return client


@pytest.fixture()
def seeded_auth_client(auth_client: TestClient, db_session):  # type: ignore[no-untyped-def]
    load_seed(db_session)
    return auth_client


@pytest.fixture()
def admin_client(client: TestClient, db_session):  # type: ignore[no-untyped-def]
    """A TestClient already authenticated with a registered + logged-in admin user."""
    client.post(
        "/api/auth/register",
        json={"email": "admin@example.com", "password": "securepass1", "display_name": "Admin"},
    )
    from recipe_normalizer.users.models import User

    user = db_session.scalars(select(User).where(User.email == "admin@example.com")).one()
    user.is_admin = True
    db_session.flush()
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "securepass1"},
    )
    assert resp.status_code == 200
    return client


# ---------------------------------------------------------------------------
# Unauthenticated → 401 envelope
# ---------------------------------------------------------------------------


def test_list_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.get("/api/catalog/ingredients")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


def test_get_unauthenticated_returns_401(client: TestClient) -> None:
    import uuid

    resp = client.get(f"/api/catalog/ingredients/{uuid.uuid4()}")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


def test_patch_unauthenticated_returns_401(client: TestClient) -> None:
    import uuid

    resp = client.patch(f"/api/catalog/ingredients/{uuid.uuid4()}", json={})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


def test_merge_unauthenticated_returns_401(client: TestClient) -> None:
    import uuid

    resp = client.post(
        f"/api/catalog/ingredients/{uuid.uuid4()}/merge",
        json={"target_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# GET /api/catalog/ingredients
# ---------------------------------------------------------------------------


def test_list_ingredients_returns_empty_list(auth_client: TestClient) -> None:
    resp = auth_client.get("/api/catalog/ingredients")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_ingredients_returns_seeded(seeded_auth_client: TestClient) -> None:
    resp = seeded_auth_client.get("/api/catalog/ingredients")
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert "all-purpose flour" in names


def test_list_ingredients_q_filter(seeded_auth_client: TestClient) -> None:
    resp = seeded_auth_client.get("/api/catalog/ingredients", params={"q": "flou"})
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert "all-purpose flour" in names


def test_list_ingredients_status_filter(auth_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    service.create_unreviewed(db_session, name="mystery herb")
    resp = auth_client.get("/api/catalog/ingredients", params={"status": "unreviewed"})
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert "mystery herb" in names


# ---------------------------------------------------------------------------
# GET /api/catalog/ingredients/{id}
# ---------------------------------------------------------------------------


def test_get_ingredient_by_id(auth_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    ing = service.create_unreviewed(db_session, name="test herb")
    resp = auth_client.get(f"/api/catalog/ingredients/{ing.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "test herb"


def test_get_ingredient_not_found_returns_404(auth_client: TestClient) -> None:
    import uuid

    resp = auth_client.get(f"/api/catalog/ingredients/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# PATCH /api/catalog/ingredients/{id}
# ---------------------------------------------------------------------------


def test_patch_ingredient_updates_status(admin_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    ing = service.create_unreviewed(db_session, name="patchable herb")
    resp = admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"status": "reviewed"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "reviewed"


def test_patch_ingredient_updates_dietary_flags(
    admin_client: TestClient,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    ing = service.create_unreviewed(db_session, name="patchable herb2")
    resp = admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"dietary_flags": ["dairy"]},
    )
    assert resp.status_code == 200
    assert resp.json()["dietary_flags"] == ["dairy"]


def test_patch_ingredient_invalid_gram_weights_returns_422(
    admin_client: TestClient,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    ing = service.create_unreviewed(db_session, name="patchable herb3")
    resp = admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"gram_weights": {"badunit": 100.0}},
    )
    assert resp.status_code == 422


def test_patch_ingredient_non_admin_returns_403(
    auth_client: TestClient,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    ing = service.create_unreviewed(db_session, name="non-admin patch herb")
    resp = auth_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"status": "reviewed"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_patch_ingredient_admin_returns_200(
    admin_client: TestClient,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    ing = service.create_unreviewed(db_session, name="admin patch herb")
    resp = admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"status": "reviewed"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/catalog/ingredients/{id}/merge
# ---------------------------------------------------------------------------


def test_merge_happy_path(admin_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    source = service.create_unreviewed(db_session, name="source herb")
    target = service.create_unreviewed(db_session, name="target herb")
    resp = admin_client.post(
        f"/api/catalog/ingredients/{source.id}/merge",
        json={"target_id": str(target.id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "target herb"
    alias_texts = [a["alias"] for a in body["aliases"]]
    assert "source herb" in alias_texts


def test_merge_into_itself_returns_422(admin_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    source = service.create_unreviewed(db_session, name="self herb")
    resp = admin_client.post(
        f"/api/catalog/ingredients/{source.id}/merge",
        json={"target_id": str(source.id)},
    )
    assert resp.status_code == 422


def test_merge_already_merged_returns_409(admin_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    source = service.create_unreviewed(db_session, name="merged source herb")
    target = service.create_unreviewed(db_session, name="merged target herb")
    third = service.create_unreviewed(db_session, name="third herb")
    service.merge(db_session, source_id=source.id, target_id=target.id)
    resp = admin_client.post(
        f"/api/catalog/ingredients/{source.id}/merge",
        json={"target_id": str(third.id)},
    )
    assert resp.status_code == 409


def test_merge_non_admin_returns_403(auth_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    source = service.create_unreviewed(db_session, name="non-admin merge source")
    target = service.create_unreviewed(db_session, name="non-admin merge target")
    resp = auth_client.post(
        f"/api/catalog/ingredients/{source.id}/merge",
        json={"target_id": str(target.id)},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_merge_admin_returns_200(admin_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    source = service.create_unreviewed(db_session, name="admin merge source")
    target = service.create_unreviewed(db_session, name="admin merge target")
    resp = admin_client.post(
        f"/api/catalog/ingredients/{source.id}/merge",
        json={"target_id": str(target.id)},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Review fixes: PATCH null density semantics, bounded limit param
# ---------------------------------------------------------------------------


def test_patch_density_null_clears_it(admin_client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
    ing = service.create_unreviewed(db_session, name="density herb")
    set_resp = admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"density_g_per_ml": 0.5},
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["density_g_per_ml"] == 0.5

    clear_resp = admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"density_g_per_ml": None},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["density_g_per_ml"] is None


def test_patch_without_density_leaves_it_unchanged(
    admin_client: TestClient,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    ing = service.create_unreviewed(db_session, name="density herb2")
    admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"density_g_per_ml": 0.5},
    )
    resp = admin_client.patch(
        f"/api/catalog/ingredients/{ing.id}",
        json={"category": "spices"},
    )
    assert resp.status_code == 200
    assert resp.json()["density_g_per_ml"] == 0.5
    assert resp.json()["category"] == "spices"


def test_list_limit_caps_results(seeded_auth_client: TestClient) -> None:
    resp = seeded_auth_client.get("/api/catalog/ingredients", params={"limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_limit_out_of_bounds_returns_422(auth_client: TestClient) -> None:
    resp = auth_client.get("/api/catalog/ingredients", params={"limit": 0})
    assert resp.status_code == 422
    resp = auth_client.get("/api/catalog/ingredients", params={"limit": 201})
    assert resp.status_code == 422
