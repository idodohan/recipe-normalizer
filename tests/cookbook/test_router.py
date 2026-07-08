"""Router-level tests for cookbook recipe endpoints: personal metadata + images."""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook.router import router as cookbook_router
from recipe_normalizer.filestore import LocalFileStore
from recipe_normalizer.users.router import router as users_router
from tests.api_helpers import make_client

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A 1x1 PNG's real magic bytes — enough for detect_media_type to sniff it as
# image/png without needing a fully valid PNG payload.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake but sniffable png data"


@pytest.fixture()
def client(db_session, tmp_path):  # type: ignore[no-untyped-def]
    return make_client(
        db_session, users_router, cookbook_router, file_store=LocalFileStore(tmp_path)
    )


def _register_and_login(client: TestClient, email: str) -> None:
    client.post(
        "/api/auth/register",
        json={"email": email, "password": "securepass1", "display_name": "Tester"},
    )
    resp = client.post("/api/auth/login", json={"email": email, "password": "securepass1"})
    assert resp.status_code == 200


@pytest.fixture()
def auth_client(client: TestClient, db_session):  # type: ignore[no-untyped-def]
    """A TestClient already authenticated with a registered + logged-in user."""
    _register_and_login(client, "tester@example.com")
    return client


_SIMPLE_RECIPE_BODY = {
    "title": "Test Cake",
    "groups": [{"name": "Main", "lines": [{"original_text": "1 cup flour"}]}],
}


def _create_recipe(client: TestClient) -> str:
    resp = client.post("/api/recipes", json=_SIMPLE_RECIPE_BODY)
    assert resp.status_code == 201
    return resp.json()["id"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Unauthenticated → 401 envelope
# ---------------------------------------------------------------------------


def test_patch_personal_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.patch(f"/api/recipes/{uuid.uuid4()}/personal", json={"is_favorite": True})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# PATCH /api/recipes/{id}/personal
# ---------------------------------------------------------------------------


def test_patch_personal_favorite_toggle_persists(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)

    resp = auth_client.patch(f"/api/recipes/{recipe_id}/personal", json={"is_favorite": True})
    assert resp.status_code == 200
    assert resp.json()["is_favorite"] is True

    # Persisted — a fresh GET reflects it too.
    get_resp = auth_client.get(f"/api/recipes/{recipe_id}")
    assert get_resp.json()["is_favorite"] is True

    resp2 = auth_client.patch(f"/api/recipes/{recipe_id}/personal", json={"is_favorite": False})
    assert resp2.status_code == 200
    assert resp2.json()["is_favorite"] is False


def test_patch_personal_notes_set_and_explicit_null_clears(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)

    set_resp = auth_client.patch(f"/api/recipes/{recipe_id}/personal", json={"notes": "so good"})
    assert set_resp.status_code == 200
    assert set_resp.json()["notes"] == "so good"

    clear_resp = auth_client.patch(f"/api/recipes/{recipe_id}/personal", json={"notes": None})
    assert clear_resp.status_code == 200
    assert clear_resp.json()["notes"] is None


def test_patch_personal_absent_field_leaves_value_unchanged(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)
    auth_client.patch(
        f"/api/recipes/{recipe_id}/personal",
        json={"is_favorite": True, "notes": "keep me"},
    )

    resp = auth_client.patch(f"/api/recipes/{recipe_id}/personal", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_favorite"] is True
    assert body["notes"] == "keep me"


def test_patch_personal_other_owner_returns_404(client: TestClient, db_session: Session) -> None:
    _register_and_login(client, "owner@example.com")
    recipe_id = _create_recipe(client)

    # Log out (new client instance shares db_session but not cookies) — use a
    # second registered user and log in as them instead.
    _register_and_login(client, "other@example.com")
    resp = client.patch(f"/api/recipes/{recipe_id}/personal", json={"is_favorite": True})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_full_patch_replace_preserves_favorite_and_notes(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)
    auth_client.patch(
        f"/api/recipes/{recipe_id}/personal",
        json={"is_favorite": True, "notes": "keep me"},
    )

    replace_resp = auth_client.patch(
        f"/api/recipes/{recipe_id}",
        json={
            "title": "Renamed Cake",
            "groups": [{"name": "Main", "lines": [{"original_text": "1 cup sugar"}]}],
        },
    )
    assert replace_resp.status_code == 200
    body = replace_resp.json()
    assert body["title"] == "Renamed Cake"
    assert body["is_favorite"] is True
    assert body["notes"] == "keep me"


# ---------------------------------------------------------------------------
# PUT/DELETE /api/recipes/{id}/image
# ---------------------------------------------------------------------------


def _upload_image(
    client: TestClient,
    recipe_id: str,
    *,
    data: bytes = _PNG_BYTES,
    filename: str = "photo.png",
    content_type: str = "image/png",
):
    return client.put(
        f"/api/recipes/{recipe_id}/image",
        files={"file": (filename, io.BytesIO(data), content_type)},
    )


def test_upload_image_sets_image_ref(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)

    resp = _upload_image(auth_client, recipe_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_ref"] is not None
    assert body["image_ref"].startswith("/api/files/")
    assert body["image_ref"].endswith(".png")

    # Persisted — a fresh GET reflects it too.
    get_resp = auth_client.get(f"/api/recipes/{recipe_id}")
    assert get_resp.json()["image_ref"] == body["image_ref"]


def test_upload_image_wrong_content_rejected(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)

    resp = _upload_image(
        auth_client,
        recipe_id,
        data=b"not actually an image",
        filename="photo.png",
        content_type="image/png",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_file_type"

    # Recipe is unaffected.
    get_resp = auth_client.get(f"/api/recipes/{recipe_id}")
    assert get_resp.json()["image_ref"] is None


def test_upload_image_too_large_rejected(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)

    oversized = _PNG_BYTES + (b"\x00" * (10 * 1024 * 1024))
    resp = _upload_image(auth_client, recipe_id, data=oversized)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "file_too_large"


def test_upload_image_other_owner_returns_404(client: TestClient, db_session: Session) -> None:
    _register_and_login(client, "owner@example.com")
    recipe_id = _create_recipe(client)

    _register_and_login(client, "other@example.com")
    resp = _upload_image(client, recipe_id)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_delete_image_clears_it(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)
    upload_resp = _upload_image(auth_client, recipe_id)
    assert upload_resp.json()["image_ref"] is not None

    del_resp = auth_client.delete(f"/api/recipes/{recipe_id}/image")
    assert del_resp.status_code == 200
    assert del_resp.json()["image_ref"] is None

    get_resp = auth_client.get(f"/api/recipes/{recipe_id}")
    assert get_resp.json()["image_ref"] is None


def test_delete_image_other_owner_returns_404(client: TestClient, db_session: Session) -> None:
    _register_and_login(client, "owner@example.com")
    recipe_id = _create_recipe(client)
    _upload_image(client, recipe_id)

    _register_and_login(client, "other@example.com")
    resp = client.delete(f"/api/recipes/{recipe_id}/image")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_upload_image_replace_overwrites(auth_client: TestClient) -> None:
    recipe_id = _create_recipe(auth_client)

    first = _upload_image(auth_client, recipe_id)
    first_ref = first.json()["image_ref"]

    second_bytes = b"\xff\xd8\xff\xe0" + b"a different jpeg's worth of fake bytes"
    second = _upload_image(
        auth_client,
        recipe_id,
        data=second_bytes,
        filename="photo.jpg",
        content_type="image/jpeg",
    )
    assert second.status_code == 200
    second_ref = second.json()["image_ref"]
    assert second_ref != first_ref
    assert second_ref.endswith(".jpg")

    get_resp = auth_client.get(f"/api/recipes/{recipe_id}")
    assert get_resp.json()["image_ref"] == second_ref
