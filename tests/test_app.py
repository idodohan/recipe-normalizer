"""Integration test: full app factory flow (Task 17).

Covers: register → login → POST /recipes → GET list → GET detail → /scaled →
bad scaled params → DELETE → 404 → /api/health → /vocab.
Also tests: DuplicateRecipeError produces the extra.existing_id envelope field
via a monkeypatched throwaway route.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.cookbook.service import DuplicateRecipeError
from recipe_normalizer.db import get_db
from recipe_normalizer.main import create_app

# ---------------------------------------------------------------------------
# Module-scoped test client wired to the shared db_session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_session(engine):  # type: ignore[no-untyped-def]
    """Single module-scoped session: seed catalog once, then leave open."""
    from sqlalchemy.orm import Session as SaSession

    conn = engine.connect()
    txn = conn.begin()
    session = SaSession(bind=conn, expire_on_commit=False)
    session.begin_nested()
    load_seed(session)
    yield session
    session.close()
    txn.rollback()
    conn.close()


@pytest.fixture(scope="module")
def app_client(seeded_session):  # type: ignore[no-untyped-def]
    """TestClient over create_app() with get_db overridden to seeded_session."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: seeded_session
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_PAYLOAD = {
    "email": "integration_test@example.com",
    "password": "securepass1",
    "display_name": "Tester",
}

LOGIN_PAYLOAD = {
    "email": "integration_test@example.com",
    "password": "securepass1",
}

# 1 fl oz gin (volume unit) → exact conversion: 1 fl_oz = 29.5735 ml
GIN_LINE = {
    "original_text": "1 fl oz gin",
    "quantity": "1",
    "unit": "fl oz",
    "name": "gin",
}

FLOUR_LINE = {
    "original_text": "1 cup all-purpose flour",
    "quantity": "1",
    "unit": "cup",
    "name": "all-purpose flour",
}

SALT_LINE = {
    "original_text": "salt to taste",
    # no quantity, no unit, no name — pure passthrough
}

RECIPE_PAYLOAD = {
    "title": "Test Cocktail Bread",
    "servings": {"amount": 4.0, "unit_text": "servings"},
    "groups": [
        {
            "name": "Main",
            "lines": [FLOUR_LINE, SALT_LINE, GIN_LINE],
        }
    ],
    "dish_types": ["cocktail"],
    "steps": [{"original_text": "Mix and serve."}],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health(app_client: TestClient) -> None:
    resp = app_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_register_and_login(app_client: TestClient) -> None:
    resp = app_client.post("/api/auth/register", json=REGISTER_PAYLOAD)
    assert resp.status_code == 201
    assert resp.json()["email"] == REGISTER_PAYLOAD["email"]

    resp = app_client.post("/api/auth/login", json=LOGIN_PAYLOAD)
    assert resp.status_code == 200


def test_create_recipe_201(app_client: TestClient) -> None:
    resp = app_client.post("/api/recipes", json=RECIPE_PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Test Cocktail Bread"
    assert body["source_type"] == "manual"

    # Check lines
    lines = body["groups"][0]["lines"]
    flour_line = next(ln for ln in lines if "flour" in ln["original_text"])
    salt_line = next(ln for ln in lines if "salt" in ln["original_text"])
    gin_line = next(ln for ln in lines if "gin" in ln["original_text"])

    # Flour: cup → g via gram_weight (120g), approx
    assert flour_line["is_approx"] is True
    assert "~120 g" in flour_line["display"]

    # Salt: passthrough (no quantity, no normalization)
    assert salt_line["normalized_amount"] is None
    assert salt_line["display"] == "salt to taste"

    # Gin: 1 fl oz → exact 29.57 ml (volume→volume, no density approx needed)
    # fl oz (fluid ounce volume) → ml: 1 fl_oz = 29.5735... ml → rounds to 29.57
    assert gin_line["is_approx"] is False
    assert gin_line["display"] == "1 fl oz gin → 29.57 ml"


def test_list_recipes_contains_summary(app_client: TestClient) -> None:
    resp = app_client.get("/api/recipes")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    titles = [r["title"] for r in body["items"]]
    assert "Test Cocktail Bread" in titles


def test_get_recipe_detail(app_client: TestClient) -> None:
    # First get the recipe id from the list
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(
        r["id"] for r in list_resp.json()["items"] if r["title"] == "Test Cocktail Bread"
    )

    resp = app_client.get(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == recipe_id
    assert body["title"] == "Test Cocktail Bread"


def test_scaled_by_factor(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(
        r["id"] for r in list_resp.json()["items"] if r["title"] == "Test Cocktail Bread"
    )

    resp = app_client.get(f"/api/recipes/{recipe_id}/scaled", params={"factor": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["factor"] == 2.0
    # step_text_disclaimer must be True when the recipe has steps
    assert body["step_text_disclaimer"] is True
    # Flour line: 120g × 2 = 240g
    flour_line = next(
        ln for g in body["groups"] for ln in g["lines"] if "flour" in ln["original_text"]
    )
    assert flour_line["normalized_amount"] == pytest.approx(240.0, abs=0.5)


def test_scaled_by_target_servings(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(
        r["id"] for r in list_resp.json()["items"] if r["title"] == "Test Cocktail Bread"
    )

    resp = app_client.get(f"/api/recipes/{recipe_id}/scaled", params={"target_servings": 8})
    assert resp.status_code == 200
    body = resp.json()
    # factor = 8/4 = 2
    assert body["factor"] == pytest.approx(2.0, abs=0.01)


def test_scaled_both_params_422(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(
        r["id"] for r in list_resp.json()["items"] if r["title"] == "Test Cocktail Bread"
    )

    resp = app_client.get(
        f"/api/recipes/{recipe_id}/scaled",
        params={"factor": 2, "target_servings": 6},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"


def test_scaled_no_params_422(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(
        r["id"] for r in list_resp.json()["items"] if r["title"] == "Test Cocktail Bread"
    )

    resp = app_client.get(f"/api/recipes/{recipe_id}/scaled")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"


def test_delete_recipe_then_404(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(
        r["id"] for r in list_resp.json()["items"] if r["title"] == "Test Cocktail Bread"
    )

    del_resp = app_client.delete(f"/api/recipes/{recipe_id}")
    assert del_resp.status_code == 204

    get_resp = app_client.get(f"/api/recipes/{recipe_id}")
    assert get_resp.status_code == 404
    body = get_resp.json()
    assert body["error"]["code"] == "not_found"


def test_vocab_includes_seeded_dish_types(app_client: TestClient) -> None:
    resp = app_client.get("/api/vocab")
    assert resp.status_code == 200
    body = resp.json()
    assert "cuisines" in body
    assert "dish_types" in body
    assert "tags" in body
    # "cocktail" was added via POST /recipes above
    assert "cocktail" in body["dish_types"]
    # Lists must be sorted
    assert body["dish_types"] == sorted(body["dish_types"])
    assert body["cuisines"] == sorted(body["cuisines"])
    assert body["tags"] == sorted(body["tags"])


def test_duplicate_recipe_error_envelope_has_existing_id(
    app_client: TestClient, seeded_session: Session
) -> None:
    """The ApiError extra mechanism puts existing_id in the error envelope."""
    import uuid

    # Build a tiny throwaway app with a route that raises DuplicateRecipeError
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from recipe_normalizer.errors import install_error_handlers

    throwaway = FastAPI()
    install_error_handlers(throwaway)
    fake_id = uuid.uuid4()

    @throwaway.get("/dup-test")
    def _dup_route() -> JSONResponse:
        raise DuplicateRecipeError(existing_id=fake_id)

    with TestClient(throwaway, raise_server_exceptions=False) as tc:
        resp = tc.get("/dup-test")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "duplicate_recipe"
    assert body["error"]["existing_id"] == str(fake_id)


# ---------------------------------------------------------------------------
# Scaled endpoint edge cases: factor bounds and missing servings
# ---------------------------------------------------------------------------


def test_scaled_factor_zero_returns_422(app_client: TestClient) -> None:
    """factor=0 violates the Query(gt=0) bound → 422 validation_error envelope."""
    import uuid

    resp = app_client.get(f"/api/recipes/{uuid.uuid4()}/scaled", params={"factor": 0})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"


def test_scaled_factor_too_large_returns_422(app_client: TestClient) -> None:
    """factor=200 violates the Query(le=100) bound → 422 validation_error envelope."""
    import uuid

    resp = app_client.get(f"/api/recipes/{uuid.uuid4()}/scaled", params={"factor": 200})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"


def test_scaled_target_servings_without_recipe_servings_returns_422(
    app_client: TestClient,
) -> None:
    """target_servings against a recipe with no servings_amount → 422 envelope."""
    payload = {
        "title": "No Servings Recipe",
        "groups": [{"name": "Main", "lines": [{"original_text": "water"}]}],
    }
    create_resp = app_client.post("/api/recipes", json=payload)
    assert create_resp.status_code == 201
    recipe_id = create_resp.json()["id"]

    resp = app_client.get(f"/api/recipes/{recipe_id}/scaled", params={"target_servings": 6})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "servings" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Observability: unhandled 500s are logged, requests get an access log line
# ---------------------------------------------------------------------------


def test_unhandled_error_returns_500_and_logs_traceback(caplog: pytest.LogCaptureFixture) -> None:
    """A route that raises an unhandled RuntimeError yields the 500 envelope

    AND the exception (with traceback) is logged at ERROR level, so an
    on-call engineer can find it in the logs.
    """
    from fastapi import FastAPI

    from recipe_normalizer.errors import install_error_handlers

    throwaway = FastAPI()
    install_error_handlers(throwaway)

    @throwaway.get("/boom")
    def _boom() -> None:
        raise RuntimeError("kaboom")

    with (
        caplog.at_level(logging.ERROR, logger="recipe_normalizer.errors"),
        TestClient(throwaway, raise_server_exceptions=False) as tc,
    ):
        resp = tc.get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal_error"

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected an ERROR-level log record for the unhandled exception"
    record = error_records[0]
    assert record.exc_info is not None
    assert "RuntimeError" in caplog.text
    assert "kaboom" in caplog.text


def test_crashing_request_still_appears_in_access_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crashing request on the REAL app stack must still produce an access-log
    line (with status 500), in addition to the error-log traceback record.

    ServerErrorMiddleware sits outside our user middleware: it renders the 500
    envelope and then re-raises, so `call_next` in `access_log` raises too. The
    access log line must still be emitted -- crashing requests are exactly what
    an access log needs to capture.
    """
    app = create_app()

    @app.get("/boom-real")
    def _boom_real() -> None:
        raise RuntimeError("kaboom-real")

    # A single root-level capture (rather than two nested per-logger
    # `at_level` calls) is required: pytest's caplog backs both calls with one
    # shared handler, so a second `at_level(..., logger=...)` call would
    # silently overwrite the level set by the first.
    with (
        caplog.at_level(logging.INFO),
        TestClient(app, raise_server_exceptions=False) as tc,
    ):
        resp = tc.get("/boom-real")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal_error"

    access_records = [
        r
        for r in caplog.records
        if r.name == "recipe_normalizer.access" and "/boom-real" in r.getMessage()
    ]
    assert access_records, "expected an access-log record for the crashing request"
    assert "500" in access_records[0].getMessage()

    error_records = [r for r in caplog.records if r.name == "recipe_normalizer.errors"]
    assert error_records, "expected an ERROR-level log record for the unhandled exception"
    assert error_records[0].exc_info is not None
    assert "RuntimeError" in caplog.text
    assert "kaboom-real" in caplog.text


def test_access_log_middleware_logs_request(caplog: pytest.LogCaptureFixture) -> None:
    """Every request produces an INFO access-log line with method, path, status."""
    app = create_app()
    with (
        caplog.at_level(logging.INFO, logger="recipe_normalizer.access"),
        TestClient(app, raise_server_exceptions=False) as tc,
    ):
        resp = tc.get("/api/health")

    assert resp.status_code == 200
    access_records = [r for r in caplog.records if r.name == "recipe_normalizer.access"]
    assert access_records, "expected an access-log record"
    message = access_records[0].getMessage()
    assert "GET" in message
    assert "/api/health" in message
    assert "200" in message


def test_access_log_redacts_public_link_tokens(caplog: pytest.LogCaptureFixture) -> None:
    """Requests to /api/public/{token} routes log <token> instead of the actual token."""
    app = create_app()
    token = "sometoken123"
    with (
        caplog.at_level(logging.INFO, logger="recipe_normalizer.access"),
        TestClient(app, raise_server_exceptions=False) as tc,
    ):
        # Will 404, but the access log still fires before the 404
        resp = tc.get(f"/api/public/{token}")

    # Verify it was a 404 (route doesn't exist yet or isn't authenticated)
    assert resp.status_code == 404

    access_records = [r for r in caplog.records if r.name == "recipe_normalizer.access"]
    assert access_records, "expected an access-log record"
    message = access_records[0].getMessage()

    # Log must contain the redacted form
    assert "/api/public/<token>" in message
    # Log must NOT contain the actual token
    assert token not in message


def test_access_log_redacts_public_link_tokens_scaled(caplog: pytest.LogCaptureFixture) -> None:
    """Requests to /api/public/{token}/scaled routes log <token> instead of the actual token."""
    app = create_app()
    token = "abc123def456"
    with (
        caplog.at_level(logging.INFO, logger="recipe_normalizer.access"),
        TestClient(app, raise_server_exceptions=False) as tc,
    ):
        # Will likely be 404 or 422 (e.g., missing query params), but the access log still fires
        resp = tc.get(f"/api/public/{token}/scaled")

    # Accept any non-2xx response; the key is the redaction in logs
    assert resp.status_code >= 400

    access_records = [r for r in caplog.records if r.name == "recipe_normalizer.access"]
    assert access_records, "expected an access-log record"
    message = access_records[0].getMessage()

    # Log must contain the redacted form
    assert "/api/public/<token>/scaled" in message
    # Log must NOT contain the actual token
    assert token not in message


# ---------------------------------------------------------------------------
# CORS configuration (Task 7)
# ---------------------------------------------------------------------------


def test_cors_origins_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """CORS origins should be configurable via settings.cors_origins."""
    from recipe_normalizer.config import settings

    monkeypatch.setattr(settings, "cors_origins", "https://a.example, https://b.example")
    app = create_app()

    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.options(
            "/api/health",
            headers={
                "Origin": "https://b.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://b.example"


# ---------------------------------------------------------------------------
# Path redaction for public-link token security
# ---------------------------------------------------------------------------


def test_redact_path_public_link_basic() -> None:
    """_redact_path redacts /api/public/{token} to /api/public/<token>."""
    from recipe_normalizer.main import _redact_path

    assert _redact_path("/api/public/abc") == "/api/public/<token>"


def test_redact_path_public_link_scaled() -> None:
    """_redact_path redacts /api/public/{token}/scaled to /api/public/<token>/scaled."""
    from recipe_normalizer.main import _redact_path

    assert _redact_path("/api/public/abc/scaled") == "/api/public/<token>/scaled"


def test_redact_path_no_match_if_not_public_api() -> None:
    """_redact_path leaves paths that don't start with /api/public/ unchanged."""
    from recipe_normalizer.main import _redact_path

    assert _redact_path("/api/publicnot/abc") == "/api/publicnot/abc"


def test_redact_path_cookbooks_token() -> None:
    """_redact_path redacts /api/public/cookbooks/{token} to .../cookbooks/<token>.

    Without the cookbooks-specific branch, the generic "redact the first
    segment" rule would treat the literal "cookbooks" as the token and leave
    the real token exposed right after it — this pins that it doesn't.
    """
    from recipe_normalizer.main import _redact_path

    assert _redact_path("/api/public/cookbooks/realtoken123") == "/api/public/cookbooks/<token>"


def test_redact_path_no_token_segment() -> None:
    """_redact_path leaves /api/public/ (no token) unchanged."""
    from recipe_normalizer.main import _redact_path

    assert _redact_path("/api/public/") == "/api/public/"


# ---------------------------------------------------------------------------
# Anonymous GET /api/public/cookbooks/{token} (Task 6)
# ---------------------------------------------------------------------------


def test_public_cookbook_happy_path(app_client: TestClient) -> None:
    """A public cookbook is readable anonymously and never leaks owner_id/email."""
    create_resp = app_client.post(
        "/api/cookbooks", json={"name": "Public Cookbook", "description": "For anyone"}
    )
    assert create_resp.status_code == 201
    cookbook = create_resp.json()

    recipe_resp = app_client.post(
        "/api/recipes", params={"cookbook_id": cookbook["id"]}, json=RECIPE_PAYLOAD
    )
    assert recipe_resp.status_code == 201

    patch_resp = app_client.patch(f"/api/cookbooks/{cookbook['id']}", json={"visibility": "public"})
    assert patch_resp.status_code == 200
    token = patch_resp.json()["public_token"]
    assert token

    # A fresh, cookie-less TestClient sharing the same app/db — anonymous.
    anon = TestClient(app_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/cookbooks/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Public Cookbook"
    assert body["description"] == "For anyone"
    assert len(body["recipes"]) == 1
    assert body["recipes"][0]["title"] == "Test Cocktail Bread"

    # Strict allowlist: no owner id, email, public_token, or ingestion internals.
    raw = resp.text
    assert "owner_id" not in raw
    assert "integration_test@example.com" not in raw
    assert "public_token" not in raw
    assert "extraction_meta" not in raw
    assert "notes" not in raw


def test_public_cookbook_hides_editor_member_last_edited_by(app_client: TestClient) -> None:
    """A recipe last-edited by an EDITOR MEMBER (not the owner) must not leak
    that member's user id via ``last_edited_by``.

    ``last_edited_by`` is set to the EDITING user's id, not necessarily the
    owner's (see ``cookbook.service.update_recipe``'s docstring) — a member
    who edits a recipe in a cookbook the owner later makes public never
    consented to their account id being exposed to anonymous visitors.
    """
    create_resp = app_client.post("/api/cookbooks", json={"name": "Leak Check Cookbook"})
    assert create_resp.status_code == 201
    cookbook = create_resp.json()

    recipe_resp = app_client.post(
        "/api/recipes", params={"cookbook_id": cookbook["id"]}, json=RECIPE_PAYLOAD
    )
    assert recipe_resp.status_code == 201
    recipe_id = recipe_resp.json()["id"]

    # A second client/cookie-jar over the SAME app+db, registered as a
    # separate user, invited as an editor.
    editor_email = "editor_leak_check@example.com"
    editor_client = TestClient(app_client.app, raise_server_exceptions=False)
    reg_resp = editor_client.post(
        "/api/auth/register",
        json={"email": editor_email, "password": "securepass1", "display_name": "Editor"},
    )
    assert reg_resp.status_code == 201
    editor_user_id = reg_resp.json()["id"]
    login_resp = editor_client.post(
        "/api/auth/login", json={"email": editor_email, "password": "securepass1"}
    )
    assert login_resp.status_code == 200

    invite_resp = app_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": editor_email, "role": "editor"},
    )
    assert invite_resp.status_code == 201

    # The editor does a full-replace edit, setting last_edited_by to THEIR id.
    edit_resp = editor_client.patch(
        f"/api/recipes/{recipe_id}", json={**RECIPE_PAYLOAD, "title": "Edited By Member"}
    )
    assert edit_resp.status_code == 200
    assert edit_resp.json()["last_edited_by"] == editor_user_id

    # Make the cookbook public AFTER the member edit.
    patch_resp = app_client.patch(f"/api/cookbooks/{cookbook['id']}", json={"visibility": "public"})
    assert patch_resp.status_code == 200
    token = patch_resp.json()["public_token"]
    assert token

    anon = TestClient(app_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/cookbooks/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["recipes"][0]["title"] == "Edited By Member"

    raw = resp.text
    assert editor_user_id not in raw
    assert "last_edited_by" not in raw


def test_public_cookbook_unlisted_also_readable(app_client: TestClient) -> None:
    create_resp = app_client.post("/api/cookbooks", json={"name": "Unlisted Cookbook"})
    cookbook = create_resp.json()
    patch_resp = app_client.patch(
        f"/api/cookbooks/{cookbook['id']}", json={"visibility": "unlisted"}
    )
    token = patch_resp.json()["public_token"]

    anon = TestClient(app_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/cookbooks/{token}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Unlisted Cookbook"


def test_public_cookbook_shows_a_placed_not_created_here_recipe(app_client: TestClient) -> None:
    """The public route's recipe source is the join, not `recipes.cookbook_id`.

    A recipe merely PLACED into a cookbook (via `POST /recipes/{id}/cookbooks`)
    — never created there — must still be visible to anonymous visitors once
    that cookbook is made public.
    """
    created_in = app_client.post("/api/cookbooks", json={"name": "Created In (Public Join Test)"})
    assert created_in.status_code == 201
    created_in_id = created_in.json()["id"]

    recipe_resp = app_client.post(
        "/api/recipes",
        params={"cookbook_id": created_in_id},
        json={**RECIPE_PAYLOAD, "title": "Placed Not Created Here"},
    )
    assert recipe_resp.status_code == 201
    recipe_id = recipe_resp.json()["id"]

    placed_into = app_client.post("/api/cookbooks", json={"name": "Placed Into (Public Join Test)"})
    assert placed_into.status_code == 201
    placed_into_id = placed_into.json()["id"]

    place_resp = app_client.post(
        f"/api/recipes/{recipe_id}/cookbooks", json={"cookbook_id": placed_into_id}
    )
    assert place_resp.status_code == 201

    patch_resp = app_client.patch(f"/api/cookbooks/{placed_into_id}", json={"visibility": "public"})
    assert patch_resp.status_code == 200
    token = patch_resp.json()["public_token"]
    assert token

    anon = TestClient(app_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/cookbooks/{token}")
    assert resp.status_code == 200
    assert [r["title"] for r in resp.json()["recipes"]] == ["Placed Not Created Here"]

    # ...and the cookbook it was CREATED in never gained a phantom copy.
    created_in_detail = app_client.get(f"/api/cookbooks/{created_in_id}")
    assert [r["title"] for r in created_in_detail.json()["recipes"]] == ["Placed Not Created Here"]


def test_public_cookbook_private_id_as_token_returns_404(app_client: TestClient) -> None:
    """A private cookbook's own id, passed as if it were a token, 404s.

    Private cookbooks never have a public_token set, so the id can't
    coincidentally resolve to a real token — this pins that a caller can't
    probe cookbook ids this way.
    """
    create_resp = app_client.post("/api/cookbooks", json={"name": "Private Cookbook"})
    cookbook = create_resp.json()
    assert cookbook["visibility"] == "private"

    anon = TestClient(app_client.app, raise_server_exceptions=False)
    resp = anon.get(f"/api/public/cookbooks/{cookbook['id']}")
    assert resp.status_code == 404


def test_public_cookbook_unknown_token_returns_404(app_client: TestClient) -> None:
    anon = TestClient(app_client.app, raise_server_exceptions=False)
    resp = anon.get("/api/public/cookbooks/totally-bogus-token-xyz")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The flat compat shim: GET /api/recipes spans every readable cookbook, and
# the retired /api/shared-cookbooks* surface is gone (Task 9)
# ---------------------------------------------------------------------------


def test_flat_recipe_list_includes_a_cookbook_shared_to_me(app_client: TestClient) -> None:
    """GET /api/recipes is "my stuff + shared-with-me", so the current
    (pre-rebuild) frontend's flat cookbook page still shows everything the
    user can reach — including recipes in someone else's cookbook they were
    invited into as a mere VIEWER.
    """
    create_resp = app_client.post("/api/cookbooks", json={"name": "Compat Shim Cookbook"})
    assert create_resp.status_code == 201
    cookbook = create_resp.json()

    recipe_resp = app_client.post(
        "/api/recipes",
        params={"cookbook_id": cookbook["id"]},
        json={**RECIPE_PAYLOAD, "title": "Shared Into My Flat List"},
    )
    assert recipe_resp.status_code == 201

    viewer_email = "flat_list_viewer@example.com"
    viewer_client = TestClient(app_client.app, raise_server_exceptions=False)
    assert (
        viewer_client.post(
            "/api/auth/register",
            json={"email": viewer_email, "password": "securepass1", "display_name": "Viewer"},
        ).status_code
        == 201
    )
    assert (
        viewer_client.post(
            "/api/auth/login", json={"email": viewer_email, "password": "securepass1"}
        ).status_code
        == 200
    )

    # Before the invite the viewer's flat list can't see it.
    before = viewer_client.get("/api/recipes")
    assert before.status_code == 200
    assert "Shared Into My Flat List" not in [r["title"] for r in before.json()["items"]]

    invite_resp = app_client.post(
        f"/api/cookbooks/{cookbook['id']}/members",
        json={"email": viewer_email, "role": "viewer"},
    )
    assert invite_resp.status_code == 201

    after = viewer_client.get("/api/recipes")
    assert after.status_code == 200
    assert "Shared Into My Flat List" in [r["title"] for r in after.json()["items"]]


def test_retired_shared_cookbook_routes_are_gone(app_client: TestClient) -> None:
    """The /api/shared-cookbooks* surface 404s at the ROUTER level.

    Not a 500 from a dropped table, and not a stale route: the paths are
    simply not registered any more, so an old client hitting them gets a
    clean 404 (co-owned cookbooks live at /api/cookbooks now).
    """
    routes = {getattr(route, "path", "") for route in app_client.app.routes}  # type: ignore[attr-defined]
    assert not any(path.startswith("/api/shared-cookbooks") for path in routes)

    for method, path in (
        ("GET", "/api/shared-cookbooks"),
        ("POST", "/api/shared-cookbooks"),
        ("GET", f"/api/shared-cookbooks/{uuid.uuid4()}"),
        ("POST", f"/api/shared-cookbooks/{uuid.uuid4()}/recipes"),
        ("DELETE", f"/api/shared-cookbooks/{uuid.uuid4()}/members/{uuid.uuid4()}"),
    ):
        resp = app_client.request(method, path, json={"name": "x"})
        assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"
