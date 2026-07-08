import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from recipe_normalizer.db import Base

# Disable the Ryuk reaper container — it can fail to bind its port in some
# Docker-in-Docker / macOS environments, causing spurious test suite errors.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def engine(pg_url: str) -> Iterator[Engine]:
    eng = create_engine(pg_url)
    import recipe_normalizer.catalog.models  # noqa: F401
    import recipe_normalizer.cookbook.models  # noqa: F401
    import recipe_normalizer.ingestion.models  # noqa: F401
    import recipe_normalizer.llm.models  # noqa: F401
    import recipe_normalizer.sharing.models  # noqa: F401
    import recipe_normalizer.users.models  # noqa: F401

    # Tests build schema via Base.metadata.create_all (not alembic), so the
    # pg_trgm extension the migration enables (for trigram similarity search
    # and the trigram/tsvector indexes) must be created here too — indexes
    # themselves are performance-only and are skipped (create_all doesn't
    # know about them), but queries must still work without them.
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    Base.metadata.create_all(eng)
    # Throwaway table used by tests asserting db_session transaction isolation.
    with eng.begin() as conn:
        conn.execute(text("create table if not exists _scratch (id int)"))
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> Iterator[None]:
    """Rate limiters are process-global — reset between every test.

    Without this, unrelated tests that hammer /api/auth/* or /api/ingest/*
    endpoints from the same TestClient "IP"/user would trip each other's
    limits and flake, since the limiter instances live for the whole test
    session (created once at router import time).
    """
    from recipe_normalizer.ingestion.router import _ingest_limit
    from recipe_normalizer.sharing.router import _public_limit, _share_limit
    from recipe_normalizer.users.router import _login_limit, _register_limit

    _login_limit.limiter.reset()  # type: ignore[attr-defined]
    _register_limit.limiter.reset()  # type: ignore[attr-defined]
    _ingest_limit.limiter.reset()  # type: ignore[attr-defined]
    _share_limit.limiter.reset()  # type: ignore[attr-defined]
    _public_limit.limiter.reset()  # type: ignore[attr-defined]
    yield
    _login_limit.limiter.reset()  # type: ignore[attr-defined]
    _register_limit.limiter.reset()  # type: ignore[attr-defined]
    _ingest_limit.limiter.reset()  # type: ignore[attr-defined]
    _share_limit.limiter.reset()  # type: ignore[attr-defined]
    _public_limit.limiter.reset()  # type: ignore[attr-defined]


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    txn = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    session.begin_nested()
    yield session
    session.close()
    txn.rollback()
    connection.close()
