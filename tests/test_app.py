"""Integration test: full app factory flow (Task 17).

Covers: register → login → POST /recipes → GET list → GET detail → /scaled →
bad scaled params → DELETE → 404 → /api/health → /vocab.
Also tests: DuplicateRecipeError produces the extra.existing_id envelope field
via a monkeypatched throwaway route.
"""

from __future__ import annotations

import logging

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
    assert "~120 g (approx.)" in flour_line["display"]

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
    titles = [r["title"] for r in resp.json()]
    assert "Test Cocktail Bread" in titles


def test_get_recipe_detail(app_client: TestClient) -> None:
    # First get the recipe id from the list
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(r["id"] for r in list_resp.json() if r["title"] == "Test Cocktail Bread")

    resp = app_client.get(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == recipe_id
    assert body["title"] == "Test Cocktail Bread"


def test_scaled_by_factor(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(r["id"] for r in list_resp.json() if r["title"] == "Test Cocktail Bread")

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
    recipe_id = next(r["id"] for r in list_resp.json() if r["title"] == "Test Cocktail Bread")

    resp = app_client.get(f"/api/recipes/{recipe_id}/scaled", params={"target_servings": 8})
    assert resp.status_code == 200
    body = resp.json()
    # factor = 8/4 = 2
    assert body["factor"] == pytest.approx(2.0, abs=0.01)


def test_scaled_both_params_422(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(r["id"] for r in list_resp.json() if r["title"] == "Test Cocktail Bread")

    resp = app_client.get(
        f"/api/recipes/{recipe_id}/scaled",
        params={"factor": 2, "target_servings": 6},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"


def test_scaled_no_params_422(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(r["id"] for r in list_resp.json() if r["title"] == "Test Cocktail Bread")

    resp = app_client.get(f"/api/recipes/{recipe_id}/scaled")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"


def test_delete_recipe_then_404(app_client: TestClient) -> None:
    list_resp = app_client.get("/api/recipes")
    recipe_id = next(r["id"] for r in list_resp.json() if r["title"] == "Test Cocktail Bread")

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
