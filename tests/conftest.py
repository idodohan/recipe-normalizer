from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from recipe_normalizer.db import Base


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def engine(pg_url: str) -> Iterator[Engine]:
    eng = create_engine(pg_url)
    import recipe_normalizer.catalog.models  # noqa: F401
    import recipe_normalizer.cookbook.models  # noqa: F401
    import recipe_normalizer.users.models  # noqa: F401

    Base.metadata.create_all(eng)
    yield eng


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    txn = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    yield session
    session.close()
    txn.rollback()
    connection.close()
