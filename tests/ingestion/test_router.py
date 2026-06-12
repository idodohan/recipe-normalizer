"""Router-level tests for ingestion: HTTP transport, auth guards, and response shapes."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.cookbook.models import IngredientGroup, IngredientLine, Recipe, SourceType
from recipe_normalizer.db import get_db
from recipe_normalizer.errors import install_error_handlers
from recipe_normalizer.filestore import get_file_store
from recipe_normalizer.ingestion import service as svc
from recipe_normalizer.ingestion.models import Job, JobStatus
from recipe_normalizer.ingestion.router import router as ingestion_router
from recipe_normalizer.users.models import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user(db: Session, suffix: str = "") -> User:
    u = User(
        email=f"routertest{suffix}@example.com",
        password_hash="hash",
        display_name=f"RT{suffix}",
    )
    db.add(u)
    db.flush()
    return u


def make_draft_recipe(
    db: Session,
    owner_id: uuid.UUID,
    *,
    fingerprint: str | None = None,
    title: str = "Draft",
    is_verified: bool = False,
) -> Recipe:
    recipe = Recipe(
        owner_id=owner_id,
        title=title,
        source_type=SourceType.text,
        source_fingerprint=fingerprint,
        is_verified=is_verified,
    )
    db.add(recipe)
    db.flush()
    grp = IngredientGroup(recipe_id=recipe.id, name=None, order_index=0)
    db.add(grp)
    db.flush()
    line = IngredientLine(group_id=grp.id, order_index=0, original_text="100g pasta")
    db.add(line)
    db.flush()
    return recipe


def _make_client(db_session: Session, user: User, tmp_path: Path) -> TestClient:
    from recipe_normalizer.filestore import LocalFileStore

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ingestion_router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_file_store] = lambda: LocalFileStore(tmp_path)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(db_session: Session) -> Session:
    load_seed(db_session)
    return db_session


@pytest.fixture()
def user(db: Session) -> User:
    return make_user(db, suffix=str(uuid.uuid4())[:8])


@pytest.fixture()
def client(db: Session, user: User, tmp_path: Path) -> TestClient:
    return _make_client(db, user, tmp_path)


# ---------------------------------------------------------------------------
# POST /api/ingest/url
# ---------------------------------------------------------------------------


class TestIngestUrl:
    def test_202_with_job_out(self, client: TestClient) -> None:
        resp = client.post("/api/ingest/url", json={"url": "https://example.com/pasta"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert data["input_type"] == "url"
        assert data["url"] == "https://example.com/pasta"
        assert "source_fingerprint" not in data
        assert "id" in data

    def test_invalid_url_scheme_422(self, client: TestClient) -> None:
        resp = client.post("/api/ingest/url", json={"url": "ftp://example.com/r"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_duplicate_url_409(self, client: TestClient, db: Session, user: User) -> None:
        client.post("/api/ingest/url", json={"url": "https://example.com/dedup"})
        resp = client.post("/api/ingest/url", json={"url": "https://example.com/dedup"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "duplicate_job"
        assert "job_id" in resp.json()["error"]

    def test_recipe_dup_409_has_existing_id(
        self, client: TestClient, db: Session, user: User
    ) -> None:
        from recipe_normalizer.ingestion.fingerprint import fingerprint_url

        url = "https://example.com/existing-recipe"
        fp = fingerprint_url(url)
        r = make_draft_recipe(db, user.id, fingerprint=fp)

        resp = client.post("/api/ingest/url", json={"url": url})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "duplicate_recipe"
        assert resp.json()["error"]["existing_id"] == str(r.id)


# ---------------------------------------------------------------------------
# POST /api/ingest/text
# ---------------------------------------------------------------------------


class TestIngestText:
    def test_202_queued(self, client: TestClient) -> None:
        resp = client.post("/api/ingest/text", json={"text": "Boil pasta for 8 minutes."})
        assert resp.status_code == 202
        data = resp.json()
        assert data["input_type"] == "text"
        assert data["text_preview"] == "Boil pasta for 8 minutes."

    def test_text_preview_truncated_to_120(self, client: TestClient) -> None:
        text = "X" * 200
        resp = client.post("/api/ingest/text", json={"text": text})
        assert resp.status_code == 202
        assert len(resp.json()["text_preview"]) <= 120

    def test_empty_text_422(self, client: TestClient) -> None:
        resp = client.post("/api/ingest/text", json={"text": "   "})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/ingest/file
# ---------------------------------------------------------------------------


class TestIngestFile:
    def test_pdf_202(self, client: TestClient) -> None:
        resp = client.post(
            "/api/ingest/file",
            files={"file": ("recipe.pdf", io.BytesIO(b"fake pdf"), "application/pdf")},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["input_type"] == "pdf"
        assert data["filename"] == "recipe.pdf"

    def test_png_202(self, client: TestClient) -> None:
        resp = client.post(
            "/api/ingest/file",
            files={"file": ("photo.png", io.BytesIO(b"fake png"), "image/png")},
        )
        assert resp.status_code == 202
        assert resp.json()["input_type"] == "image"

    def test_unsupported_media_type_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/ingest/file",
            files={"file": ("doc.txt", io.BytesIO(b"text"), "text/plain")},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unsupported_file_type"

    def test_duplicate_file_409(self, client: TestClient) -> None:
        data = b"identical pdf content"
        client.post(
            "/api/ingest/file",
            files={"file": ("r.pdf", io.BytesIO(data), "application/pdf")},
        )
        resp = client.post(
            "/api/ingest/file",
            files={"file": ("r2.pdf", io.BytesIO(data), "application/pdf")},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "duplicate_job"


# ---------------------------------------------------------------------------
# GET /api/jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    def test_returns_jobs_list(self, client: TestClient) -> None:
        client.post("/api/ingest/url", json={"url": "https://example.com/j1"})
        client.post("/api/ingest/text", json={"text": "Some recipe text."})
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_status_filter(self, client: TestClient, db: Session, user: User) -> None:
        j1 = svc.submit_url(db, user_id=user.id, url="https://example.com/s1")
        j1.status = JobStatus.failed
        db.flush()
        svc.submit_url(db, user_id=user.id, url="https://example.com/s2")

        resp = client.get("/api/jobs?status=failed")
        assert resp.status_code == 200
        ids = [j["id"] for j in resp.json()]
        assert str(j1.id) in ids
        assert len(ids) == 1

    def test_source_fingerprint_not_in_list_response(self, client: TestClient) -> None:
        client.post("/api/ingest/url", json={"url": "https://example.com/fp-list"})
        resp = client.get("/api/jobs")
        for item in resp.json():
            assert "source_fingerprint" not in item


# ---------------------------------------------------------------------------
# GET /api/jobs/{id}
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_returns_job_detail(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/detail")
        resp = client.get(f"/api/jobs/{job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(job.id)
        assert "drafts" in data

    def test_detail_includes_draft_summaries(
        self, client: TestClient, db: Session, user: User
    ) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/draft-detail")
        job.status = JobStatus.needs_review
        db.flush()
        r = make_draft_recipe(db, user.id, title="My Draft")
        job.produced_recipe_ids = [str(r.id)]
        db.flush()

        resp = client.get(f"/api/jobs/{job.id}")
        assert resp.status_code == 200
        drafts = resp.json()["drafts"]
        assert len(drafts) == 1
        assert drafts[0]["title"] == "My Draft"

    def test_404_for_unknown_id(self, client: TestClient) -> None:
        resp = client.get(f"/api/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/jobs/{id}/retry
# ---------------------------------------------------------------------------


class TestRetryJob:
    def test_retry_failed(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/retry-r")
        job.status = JobStatus.failed
        db.flush()
        resp = client.post(f"/api/jobs/{job.id}/retry")
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    def test_retry_active_422(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/retry-a")
        resp = client.post(f"/api/jobs/{job.id}/retry")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/jobs/{id}/recipes/{recipe_id}/accept  (204)
# ---------------------------------------------------------------------------


class TestAcceptDraft:
    def test_accept_returns_204(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/acc1")
        job.status = JobStatus.needs_review
        db.flush()
        r = make_draft_recipe(db, user.id, title="Accept Me")
        job.produced_recipe_ids = [str(r.id)]
        db.flush()

        resp = client.post(f"/api/jobs/{job.id}/recipes/{r.id}/accept")
        assert resp.status_code == 204

    def test_accept_verifies_recipe(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/acc2")
        job.status = JobStatus.needs_review
        db.flush()
        r = make_draft_recipe(db, user.id)
        job.produced_recipe_ids = [str(r.id)]
        db.flush()

        client.post(f"/api/jobs/{job.id}/recipes/{r.id}/accept")
        db.refresh(r)
        assert r.is_verified is True

    def test_accept_unknown_recipe_404(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/acc3")
        job.status = JobStatus.needs_review
        db.flush()
        job.produced_recipe_ids = []
        db.flush()

        resp = client.post(f"/api/jobs/{job.id}/recipes/{uuid.uuid4()}/accept")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/jobs/{id}/recipes/{recipe_id}/reject  (204)
# ---------------------------------------------------------------------------


class TestRejectDraft:
    def test_reject_returns_204(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/rej1")
        job.status = JobStatus.needs_review
        db.flush()
        r = make_draft_recipe(db, user.id)
        job.produced_recipe_ids = [str(r.id)]
        db.flush()

        resp = client.post(f"/api/jobs/{job.id}/recipes/{r.id}/reject")
        assert resp.status_code == 204

    def test_reject_deletes_recipe(self, client: TestClient, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/rej2")
        job.status = JobStatus.needs_review
        db.flush()
        r = make_draft_recipe(db, user.id)
        recipe_id = r.id
        job.produced_recipe_ids = [str(recipe_id)]
        db.flush()

        client.post(f"/api/jobs/{job.id}/recipes/{recipe_id}/reject")
        assert db.get(Recipe, recipe_id) is None


# ---------------------------------------------------------------------------
# POST /api/jobs/accept-high-confidence
# ---------------------------------------------------------------------------


class TestAcceptHighConfidence:
    def _make_needs_review_job(
        self,
        db: Session,
        user: User,
        confidence: float,
        n: int = 2,
    ) -> Job:
        job = svc.submit_url(
            db,
            user_id=user.id,
            url=f"https://example.com/hc{uuid.uuid4().hex[:8]}",
        )
        job.status = JobStatus.needs_review
        job.extraction_meta = {"confidence": confidence, "tier_used": 1}
        db.flush()
        ids = []
        for i in range(n):
            r = make_draft_recipe(db, user.id, title=f"HC {i}")
            ids.append(str(r.id))
        job.produced_recipe_ids = ids
        db.flush()
        return job

    def test_returns_count(self, client: TestClient, db: Session, user: User) -> None:
        self._make_needs_review_job(db, user, confidence=0.95, n=3)
        resp = client.post("/api/jobs/accept-high-confidence")
        assert resp.status_code == 200
        assert resp.json() == {"accepted": 3}

    def test_low_confidence_skipped(self, client: TestClient, db: Session, user: User) -> None:
        self._make_needs_review_job(db, user, confidence=0.5, n=2)
        resp = client.post("/api/jobs/accept-high-confidence")
        assert resp.json() == {"accepted": 0}

    def test_custom_threshold(self, client: TestClient, db: Session, user: User) -> None:
        self._make_needs_review_job(db, user, confidence=0.8, n=1)
        resp = client.post("/api/jobs/accept-high-confidence?threshold=0.75")
        assert resp.json()["accepted"] == 1
