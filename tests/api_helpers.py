from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.db import get_db
from recipe_normalizer.errors import install_error_handlers
from recipe_normalizer.filestore import FileStore, get_file_store


def make_client(  # type: ignore[no-untyped-def]
    db_session: Session, *routers, file_store: FileStore | None = None
) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    for r in routers:
        app.include_router(r)
    app.dependency_overrides[get_db] = lambda: db_session
    if file_store is not None:
        app.dependency_overrides[get_file_store] = lambda: file_store
    return TestClient(app, raise_server_exceptions=False)
