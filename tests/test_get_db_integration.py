"""Integration tests exercising the REAL get_db dependency.

All other tests override get_db with the rollback-wrapped db_session fixture,
so the commit-on-success / rollback-on-exception logic in
``recipe_normalizer.db.get_db`` had zero CI coverage. These tests monkeypatch
``SessionLocal`` to the test container engine and run requests through
``create_app()`` WITHOUT any dependency overrides, then verify persistence
(or its absence) from a separate session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import recipe_normalizer.db as db_module
from recipe_normalizer.cookbook.models import Cuisine
from recipe_normalizer.errors import ApiError
from recipe_normalizer.main import create_app
from recipe_normalizer.users.models import Session as DbSession
from recipe_normalizer.users.models import User

# Unique-per-run identifiers so committed rows never clash with other tests.
EMAIL = f"real-getdb-{uuid.uuid4().hex[:8]}@example.com"
CUISINE_NAME = f"RollbackCuisine-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def real_db_client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient over create_app() using the REAL get_db (no overrides).

    SessionLocal is monkeypatched to bind to the test container engine;
    pytest's monkeypatch fixture restores the original automatically.
    """
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )
    app = create_app()

    @app.post("/test/flush-then-fail")
    def flush_then_fail(db: Session = Depends(db_module.get_db)) -> None:  # noqa: B008
        db.add(Cuisine(name=CUISINE_NAME))
        db.flush()
        raise ApiError(422, "validation_error", "boom after flush")

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    # Cleanup: remove committed rows so the shared engine stays pristine.
    with sessionmaker(bind=engine)() as s:
        user = s.scalars(select(User).where(User.email == EMAIL)).first()
        if user is not None:
            s.execute(delete(DbSession).where(DbSession.user_id == user.id))
            s.delete(user)
        s.execute(delete(Cuisine).where(Cuisine.name == CUISINE_NAME))
        s.commit()


def test_register_commits_via_real_get_db(real_db_client: TestClient, engine: Engine) -> None:
    """A successful request commits: the user row is visible from a separate session."""
    resp = real_db_client.post(
        "/api/auth/register",
        json={"email": EMAIL, "password": "securepass1", "display_name": "RealDb"},
    )
    assert resp.status_code == 201

    # Verify from a completely separate connection/session — only a real
    # commit makes the row visible here.
    with sessionmaker(bind=engine)() as separate:
        user = separate.scalars(select(User).where(User.email == EMAIL)).first()
        assert user is not None
        assert user.email == EMAIL


def test_apierror_rolls_back_flushed_row(real_db_client: TestClient, engine: Engine) -> None:
    """An ApiError raised after a flush rolls back: no row persists, 422 envelope returned."""
    resp = real_db_client.post("/test/flush-then-fail")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "boom after flush"

    with sessionmaker(bind=engine)() as separate:
        row = separate.scalars(select(Cuisine).where(Cuisine.name == CUISINE_NAME)).first()
        assert row is None
