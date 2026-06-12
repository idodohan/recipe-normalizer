"""TDD tests for FileStore seam (Plan 2, Task 1).

Tests cover:
- LocalFileStore content-addressed save (sha256 layout)
- Idempotent save (same bytes → same ref, no duplicate file)
- open() round-trip
- exists() true/false
- delete() removes + is idempotent
- url_path()
- Path-traversal security (ValueError)
- Missing ref → FileNotFoundError
- Serving route: authenticated fetch returns bytes + content-type
- Unauthenticated fetch → 401 envelope
- Traversal ref → 404
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.filestore import FileStore, LocalFileStore

# ---------------------------------------------------------------------------
# LocalFileStore unit tests
# ---------------------------------------------------------------------------


def test_save_returns_content_addressed_ref(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    data = b"hello"
    ref = store.save(data, suffix="txt")
    sha = hashlib.sha256(data).hexdigest()
    expected = f"{sha[:16]}/{sha}.txt"
    assert ref == expected


def test_save_idempotent_same_ref_no_duplicate(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    data = b"hello"
    ref1 = store.save(data, suffix="txt")
    ref2 = store.save(data, suffix="txt")
    assert ref1 == ref2
    # Only one file on disk
    parent = tmp_path / ref1.split("/")[0]
    files = list(parent.iterdir())
    assert len(files) == 1


def test_open_round_trips_bytes(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    data = b"\x00\x01\x02hello world\xff"
    ref = store.save(data, suffix="bin")
    assert store.open(ref) == data


def test_exists_true_after_save(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    ref = store.save(b"data", suffix="txt")
    assert store.exists(ref) is True


def test_exists_false_for_missing(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    assert store.exists("abcd1234abcd1234/abcdef.txt") is False


def test_delete_removes_file(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    ref = store.save(b"byebye", suffix="txt")
    assert store.exists(ref) is True
    store.delete(ref)
    assert store.exists(ref) is False


def test_delete_idempotent(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    ref = store.save(b"byebye", suffix="txt")
    store.delete(ref)
    # Second delete must not raise
    store.delete(ref)


def test_url_path(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    ref = store.save(b"hi", suffix="png")
    assert store.url_path(ref) == f"/api/files/{ref}"


def test_open_traversal_raises_value_error(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    with pytest.raises(ValueError):
        store.open("../../etc/passwd")


def test_open_absolute_raises_value_error(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    with pytest.raises(ValueError):
        store.open("/etc/passwd")


def test_open_missing_raises_file_not_found(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.open("abcd1234abcd1234/missingfile.txt")


def test_delete_traversal_raises_value_error(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    with pytest.raises(ValueError):
        store.delete("../../etc/passwd")


def test_exists_traversal_raises_value_error(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    with pytest.raises(ValueError):
        store.exists("../../etc/passwd")


def test_filestore_protocol_satisfied(tmp_path: Path) -> None:
    """LocalFileStore satisfies the FileStore protocol (structural type check)."""
    store: FileStore = LocalFileStore(tmp_path)
    ref = store.save(b"proto", suffix="dat")
    assert store.exists(ref)
    assert store.open(ref) == b"proto"
    assert store.url_path(ref).startswith("/api/files/")
    store.delete(ref)
    assert not store.exists(ref)


# ---------------------------------------------------------------------------
# Serving route tests (Step 3)
# ---------------------------------------------------------------------------


def _make_app_with_store(store: LocalFileStore, db_session: Session) -> FastAPI:
    """Build a minimal app with the files router and overridden store."""
    from recipe_normalizer.db import get_db
    from recipe_normalizer.filestore import get_file_store
    from recipe_normalizer.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_file_store] = lambda: store
    return app


def _register_and_login(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={
            "email": "filetest@example.com",
            "password": "securepass1",
            "display_name": "FileTester",
        },
    )
    client.post(
        "/api/auth/login",
        json={"email": "filetest@example.com", "password": "securepass1"},
    )


# Minimal 1×1 PNG bytes (valid PNG header)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_file_serve_authenticated_returns_bytes_and_content_type(
    tmp_path: Path, db_session: Session
) -> None:
    store = LocalFileStore(tmp_path)
    ref = store.save(_TINY_PNG, suffix="png")

    app = _make_app_with_store(store, db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        _register_and_login(client)
        resp = client.get(f"/api/files/{ref}")

    assert resp.status_code == 200
    assert resp.content == _TINY_PNG
    assert resp.headers["content-type"].startswith("image/png")


def test_file_serve_unauthenticated_returns_401_envelope(
    tmp_path: Path, db_session: Session
) -> None:
    store = LocalFileStore(tmp_path)
    ref = store.save(_TINY_PNG, suffix="png")

    app = _make_app_with_store(store, db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/api/files/{ref}")

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


def test_file_serve_traversal_ref_returns_404(tmp_path: Path, db_session: Session) -> None:
    store = LocalFileStore(tmp_path)

    app = _make_app_with_store(store, db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        _register_and_login(client)
        # URL-encoded traversal
        resp = client.get("/api/files/../../etc/passwd")

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"


def test_file_serve_missing_ref_returns_404(tmp_path: Path, db_session: Session) -> None:
    store = LocalFileStore(tmp_path)

    app = _make_app_with_store(store, db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        _register_and_login(client)
        resp = client.get("/api/files/abcd1234abcd1234/nosuchfile.txt")

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
