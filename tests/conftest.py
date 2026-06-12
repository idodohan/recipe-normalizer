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
    import recipe_normalizer.users.models  # noqa: F401

    Base.metadata.create_all(eng)
    # Throwaway table used by tests asserting db_session transaction isolation.
    with eng.begin() as conn:
        conn.execute(text("create table if not exists _scratch (id int)"))
    yield eng
    eng.dispose()


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
