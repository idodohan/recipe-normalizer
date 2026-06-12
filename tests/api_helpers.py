from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.db import get_db
from recipe_normalizer.errors import install_error_handlers


def make_client(db_session: Session, *routers) -> TestClient:  # type: ignore[no-untyped-def]
    app = FastAPI()
    install_error_handlers(app)
    for r in routers:
        app.include_router(r)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app, raise_server_exceptions=False)
