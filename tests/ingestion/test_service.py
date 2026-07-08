"""Service-level tests for ingestion: submit, dedupe, jobs CRUD, review actions."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.models import Recipe
from recipe_normalizer.cookbook.schemas import IngredientGroupIn, IngredientLineIn, RecipeIn
from recipe_normalizer.cookbook.service import DuplicateRecipeError
from recipe_normalizer.errors import ApiError
from recipe_normalizer.ingestion import service as svc
from recipe_normalizer.ingestion.models import InputType, Job, JobStatus
from recipe_normalizer.ingestion.schemas import JobOut
from recipe_normalizer.users.models import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user(db: Session, suffix: str = "") -> User:
    u = User(
        email=f"ingest{suffix}@example.com",
        password_hash="hash",
        display_name=f"Tester{suffix}",
    )
    db.add(u)
    db.flush()
    return u


def simple_recipe_in(title: str = "Spaghetti") -> RecipeIn:
    return RecipeIn(
        title=title,
        groups=[
            IngredientGroupIn(
                name="Main",
                lines=[IngredientLineIn(original_text="100g pasta")],
            )
        ],
    )


def make_draft_recipe(
    db: Session,
    owner_id: uuid.UUID,
    *,
    fingerprint: str | None = None,
    title: str = "Draft Recipe",
    is_verified: bool = False,
) -> Recipe:
    """Create a minimal cookbook Recipe directly (bypassing service for speed)."""
    from recipe_normalizer.cookbook.models import IngredientGroup, IngredientLine, SourceType

    recipe = Recipe(
        owner_id=owner_id,
        title=title,
        source_type=SourceType.text,
        source_fingerprint=fingerprint,
        is_verified=is_verified,
    )
    db.add(recipe)
    db.flush()

    group = IngredientGroup(recipe_id=recipe.id, name="Main", order_index=0)
    db.add(group)
    db.flush()

    line = IngredientLine(group_id=group.id, order_index=0, original_text="100g pasta")
    db.add(line)
    db.flush()
    return recipe


def make_filestore(tmp_path: Path) -> MagicMock:
    """A mock FileStore that saves files to tmp_path and tracks calls."""
    from recipe_normalizer.filestore import LocalFileStore

    real_store = LocalFileStore(tmp_path)
    return real_store


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
def user2(db: Session) -> User:
    return make_user(db, suffix="b" + str(uuid.uuid4())[:7])


@pytest.fixture()
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    from recipe_normalizer.filestore import LocalFileStore

    return LocalFileStore(tmp_path)


# ---------------------------------------------------------------------------
# submit_url
# ---------------------------------------------------------------------------


class TestSubmitUrl:
    @pytest.fixture(autouse=True)
    def _public_dns(self) -> Iterator[None]:
        """submit_url now resolves the host via netguard; pin every lookup in
        this class to a public address so tests stay deterministic and don't
        depend on real DNS for example.com."""

        def fake(host: str, port: object, *args: object, **kwargs: object) -> object:
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        with patch("socket.getaddrinfo", fake):
            yield

    def test_happy_path_queued(self, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/recipe")
        assert job.status == JobStatus.queued
        assert job.input_type == InputType.url
        assert job.payload == {"url": "https://example.com/recipe"}
        assert job.source_fingerprint is not None
        assert len(job.source_fingerprint) == 64

    def test_fingerprint_set(self, db: Session, user: User) -> None:
        from recipe_normalizer.ingestion.fingerprint import fingerprint_url

        url = "https://example.com/recipe?utm_source=x"
        job = svc.submit_url(db, user_id=user.id, url=url)
        # fingerprint should be the cleaned version
        assert job.source_fingerprint == fingerprint_url(url)

    def test_invalid_scheme_raises_422(self, db: Session, user: User) -> None:
        with pytest.raises(ApiError) as exc:
            svc.submit_url(db, user_id=user.id, url="ftp://example.com/r")
        assert exc.value.status_code == 422

    def test_too_long_url_raises_422(self, db: Session, user: User) -> None:
        long_url = "https://example.com/" + "a" * 2000
        with pytest.raises(ApiError) as exc:
            svc.submit_url(db, user_id=user.id, url=long_url)
        assert exc.value.status_code == 422

    def test_recipe_dup_raises_409(self, db: Session, user: User) -> None:
        from recipe_normalizer.ingestion.fingerprint import fingerprint_url

        url = "https://example.com/pasta"
        fp = fingerprint_url(url)
        make_draft_recipe(db, user.id, fingerprint=fp)

        with pytest.raises(DuplicateRecipeError) as exc:
            svc.submit_url(db, user_id=user.id, url=url)
        assert exc.value.status_code == 409
        assert exc.value.code == "duplicate_recipe"
        assert "existing_id" in exc.value.extra

    def test_active_job_dup_raises_409(self, db: Session, user: User) -> None:
        url = "https://example.com/pasta2"
        svc.submit_url(db, user_id=user.id, url=url)
        with pytest.raises(ApiError) as exc:
            svc.submit_url(db, user_id=user.id, url=url)
        assert exc.value.status_code == 409
        assert exc.value.code == "duplicate_job"
        assert "job_id" in exc.value.extra

    def test_different_users_same_url_ok(self, db: Session, user: User, user2: User) -> None:
        url = "https://example.com/shared-recipe"
        j1 = svc.submit_url(db, user_id=user.id, url=url)
        j2 = svc.submit_url(db, user_id=user2.id, url=url)
        assert j1.id != j2.id

    def test_tracking_params_dedupe(self, db: Session, user: User) -> None:
        """Two URLs that differ only in tracking params should be deduped."""
        url_clean = "https://example.com/recipe"
        url_with_utm = "https://example.com/recipe?utm_source=newsletter"
        svc.submit_url(db, user_id=user.id, url=url_clean)
        with pytest.raises(ApiError) as exc:
            svc.submit_url(db, user_id=user.id, url=url_with_utm)
        assert exc.value.code == "duplicate_job"

    def test_submit_url_rejects_private_address(self, db: Session, user: User) -> None:
        def fake(host: str, port: object, *args: object, **kwargs: object) -> object:
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        with patch("socket.getaddrinfo", fake), pytest.raises(ApiError) as exc_info:
            svc.submit_url(db, user_id=user.id, url="http://localhost:8000/admin")
        assert exc_info.value.code == "unsafe_url"


# ---------------------------------------------------------------------------
# submit_text
# ---------------------------------------------------------------------------


class TestSubmitText:
    def test_happy_path(self, db: Session, user: User) -> None:
        job = svc.submit_text(db, user_id=user.id, text="Boil pasta for 8 minutes.")
        assert job.status == JobStatus.queued
        assert job.input_type == InputType.text
        assert job.source_fingerprint is not None

    def test_empty_text_raises_422(self, db: Session, user: User) -> None:
        with pytest.raises(ApiError) as exc:
            svc.submit_text(db, user_id=user.id, text="   ")
        assert exc.value.status_code == 422

    def test_too_long_text_raises_422(self, db: Session, user: User) -> None:
        with pytest.raises(ApiError) as exc:
            svc.submit_text(db, user_id=user.id, text="x" * 200_001)
        assert exc.value.status_code == 422

    def test_recipe_dup_409(self, db: Session, user: User) -> None:
        from recipe_normalizer.ingestion.fingerprint import fingerprint_text

        text = "Boil water and cook pasta for 8 minutes."
        fp = fingerprint_text(text)
        make_draft_recipe(db, user.id, fingerprint=fp)

        with pytest.raises(DuplicateRecipeError) as exc:
            svc.submit_text(db, user_id=user.id, text=text)
        assert exc.value.status_code == 409

    def test_active_job_dup_409(self, db: Session, user: User) -> None:
        text = "Unique pasta recipe text here."
        svc.submit_text(db, user_id=user.id, text=text)
        with pytest.raises(ApiError) as exc:
            svc.submit_text(db, user_id=user.id, text=text)
        assert exc.value.code == "duplicate_job"
        assert "job_id" in exc.value.extra


# ---------------------------------------------------------------------------
# submit_file
# ---------------------------------------------------------------------------


class TestSubmitFile:
    def test_pdf_happy_path(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        data = b"%PDF-1.4 fake pdf content"
        job = svc.submit_file(
            db,
            user_id=user.id,
            data=data,
            filename="recipe.pdf",
            media_type="application/pdf",
            store=store,
        )
        assert job.status == JobStatus.queued
        assert job.input_type == InputType.pdf
        assert job.payload["filename"] == "recipe.pdf"
        assert job.payload["media_type"] == "application/pdf"
        assert "file_ref" in job.payload
        assert job.source_fingerprint is not None

    def test_image_happy_path(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        data = b"\x89PNG\r\n\x1a\n fake png"
        job = svc.submit_file(
            db,
            user_id=user.id,
            data=data,
            filename="photo.png",
            media_type="image/png",
            store=store,
        )
        assert job.input_type == InputType.image

    def test_bad_media_type_422(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ApiError) as exc:
            svc.submit_file(
                db,
                user_id=user.id,
                data=b"hello",
                filename="doc.txt",
                media_type="text/plain",
                store=store,
            )
        assert exc.value.status_code == 422
        assert exc.value.code == "unsupported_file_type"

    def test_sniffed_type_wins_over_client_header_unsupported(
        self, db: Session, user: User, store
    ) -> None:  # type: ignore[no-untyped-def]
        """Client claims image/png but the bytes are HTML — sniffing rejects it
        regardless of the declared Content-Type."""
        with pytest.raises(ApiError) as exc:
            svc.submit_file(
                db,
                user_id=user.id,
                data=b"<html>not a file</html>",
                filename="fake.png",
                media_type="image/png",
                store=store,
            )
        assert exc.value.status_code == 422
        assert exc.value.code == "unsupported_file_type"

    def test_sniffed_pdf_with_png_header_stored_as_pdf(
        self, db: Session, user: User, store
    ) -> None:  # type: ignore[no-untyped-def]
        """A real PDF payload mislabeled as image/png by the client must be
        sniffed and stored as a PDF — the sniffed type is authoritative."""
        data = b"%PDF-1.7 actually a pdf"
        job = svc.submit_file(
            db,
            user_id=user.id,
            data=data,
            filename="sneaky.png",
            media_type="image/png",
            store=store,
        )
        assert job.input_type == InputType.pdf
        assert job.payload["media_type"] == "application/pdf"
        assert job.payload["file_ref"].endswith(".pdf")

    def test_oversize_file_422(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        big = b"x" * (30 * 1024 * 1024 + 1)
        with pytest.raises(ApiError) as exc:
            svc.submit_file(
                db,
                user_id=user.id,
                data=big,
                filename="big.pdf",
                media_type="application/pdf",
                store=store,
            )
        assert exc.value.status_code == 422
        assert exc.value.code == "file_too_large"

    def test_file_saved_to_store(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        data = b"\xff\xd8\xff\xe0 JPEG fake data"
        job = svc.submit_file(
            db,
            user_id=user.id,
            data=data,
            filename="photo.jpg",
            media_type="image/jpeg",
            store=store,
        )
        assert store.exists(job.payload["file_ref"])

    def test_recipe_dup_409(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        from recipe_normalizer.ingestion.fingerprint import fingerprint_bytes

        data = b"%PDF-1.4 PDF content here"
        fp = fingerprint_bytes(data)
        make_draft_recipe(db, user.id, fingerprint=fp)

        with pytest.raises(DuplicateRecipeError):
            svc.submit_file(
                db,
                user_id=user.id,
                data=data,
                filename="r.pdf",
                media_type="application/pdf",
                store=store,
            )

    def test_active_job_dup_409(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        data = b"%PDF-1.4 unique pdf bytes"
        svc.submit_file(
            db,
            user_id=user.id,
            data=data,
            filename="r.pdf",
            media_type="application/pdf",
            store=store,
        )
        with pytest.raises(ApiError) as exc:
            svc.submit_file(
                db,
                user_id=user.id,
                data=data,
                filename="r2.pdf",
                media_type="application/pdf",
                store=store,
            )
        assert exc.value.code == "duplicate_job"
        assert "job_id" in exc.value.extra


# ---------------------------------------------------------------------------
# list_jobs / get_job
# ---------------------------------------------------------------------------


class TestListGetJobs:
    def test_list_owner_scoped(self, db: Session, user: User, user2: User) -> None:
        svc.submit_url(db, user_id=user.id, url="https://example.com/a")
        svc.submit_url(db, user_id=user2.id, url="https://example.com/b")
        jobs = svc.list_jobs(db, user_id=user.id)
        assert all(j.user_id == user.id for j in jobs)
        assert len(jobs) == 1

    def test_list_status_filter(self, db: Session, user: User) -> None:
        j1 = svc.submit_url(db, user_id=user.id, url="https://example.com/x1")
        j2 = svc.submit_url(db, user_id=user.id, url="https://example.com/x2")
        # Mark one as failed
        j1.status = JobStatus.failed
        db.flush()

        failed_jobs = svc.list_jobs(db, user_id=user.id, status=JobStatus.failed)
        assert len(failed_jobs) == 1
        assert failed_jobs[0].id == j1.id

        queued_jobs = svc.list_jobs(db, user_id=user.id, status=JobStatus.queued)
        assert len(queued_jobs) == 1
        assert queued_jobs[0].id == j2.id

    def test_get_job_owner_scoped(self, db: Session, user: User, user2: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/mine")
        with pytest.raises(ApiError) as exc:
            svc.get_job(db, user_id=user2.id, job_id=job.id)
        assert exc.value.status_code == 404

    def test_get_job_not_found(self, db: Session, user: User) -> None:
        with pytest.raises(ApiError) as exc:
            svc.get_job(db, user_id=user.id, job_id=uuid.uuid4())
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# retry_job
# ---------------------------------------------------------------------------


class TestRetryJob:
    def test_retry_failed_job(self, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/retry1")
        job.status = JobStatus.failed
        job.attempts = 3
        job.error = "network error"
        job.reason = "timeout"
        db.flush()

        retried = svc.retry_job(db, user_id=user.id, job_id=job.id)
        assert retried.status == JobStatus.queued
        assert retried.attempts == 0
        assert retried.error is None
        assert retried.reason is None
        assert retried.next_attempt_at is None

    def test_retry_not_a_recipe(self, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/retry2")
        job.status = JobStatus.not_a_recipe
        db.flush()
        retried = svc.retry_job(db, user_id=user.id, job_id=job.id)
        assert retried.status == JobStatus.queued

    def test_retry_queued_raises_422(self, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/retry3")
        with pytest.raises(ApiError) as exc:
            svc.retry_job(db, user_id=user.id, job_id=job.id)
        assert exc.value.status_code == 422

    def test_retry_preserves_artifacts(self, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/retry4")
        job.status = JobStatus.failed
        job.artifacts = {"raw_text_ref": "shard/abc.txt"}
        db.flush()
        retried = svc.retry_job(db, user_id=user.id, job_id=job.id)
        assert retried.artifacts == {"raw_text_ref": "shard/abc.txt"}

    def test_retry_preserves_cost_usd_so_cap_counts_prior_spend(
        self, db: Session, user: User
    ) -> None:
        """attempts resets to 0 on retry, but cost_usd must survive — the per-job
        cost cap is seeded from job.cost_usd (worker.make_llm_for_job), so a
        retry that wiped it would let a user re-spend the cap forever."""
        from decimal import Decimal

        job = svc.submit_url(db, user_id=user.id, url="https://example.com/retry5")
        job.status = JobStatus.failed
        job.attempts = 3
        job.cost_usd = Decimal("1.50")
        db.flush()

        retried = svc.retry_job(db, user_id=user.id, job_id=job.id)

        assert retried.attempts == 0
        assert retried.cost_usd == Decimal("1.50")


# ---------------------------------------------------------------------------
# accept_draft / reject_draft
# ---------------------------------------------------------------------------


class TestReviewActions:
    def _make_job_with_drafts(
        self,
        db: Session,
        user: User,
        n: int = 2,
        is_verified: bool = False,
    ) -> tuple[Job, list[Recipe]]:
        job = svc.submit_url(
            db, user_id=user.id, url=f"https://example.com/r{uuid.uuid4().hex[:8]}"
        )
        job.status = JobStatus.needs_review
        db.flush()

        drafts = []
        for i in range(n):
            r = make_draft_recipe(db, user.id, title=f"Draft {i}", is_verified=is_verified)
            drafts.append(r)

        job.produced_recipe_ids = [str(r.id) for r in drafts]
        db.flush()
        return job, drafts

    def test_accept_single_draft_marks_verified(self, db: Session, user: User) -> None:
        job, drafts = self._make_job_with_drafts(db, user, n=1)
        svc.accept_draft(db, user_id=user.id, job_id=job.id, recipe_id=drafts[0].id)
        db.refresh(drafts[0])
        assert drafts[0].is_verified is True

    def test_accept_last_draft_completes_job(self, db: Session, user: User) -> None:
        job, drafts = self._make_job_with_drafts(db, user, n=1)
        svc.accept_draft(db, user_id=user.id, job_id=job.id, recipe_id=drafts[0].id)
        db.refresh(job)
        assert job.status == JobStatus.done

    def test_accept_partial_does_not_complete_job(self, db: Session, user: User) -> None:
        job, drafts = self._make_job_with_drafts(db, user, n=2)
        svc.accept_draft(db, user_id=user.id, job_id=job.id, recipe_id=drafts[0].id)
        db.refresh(job)
        assert job.status == JobStatus.needs_review  # still waiting on second

    def test_accept_both_completes_job(self, db: Session, user: User) -> None:
        job, drafts = self._make_job_with_drafts(db, user, n=2)
        svc.accept_draft(db, user_id=user.id, job_id=job.id, recipe_id=drafts[0].id)
        svc.accept_draft(db, user_id=user.id, job_id=job.id, recipe_id=drafts[1].id)
        db.refresh(job)
        assert job.status == JobStatus.done

    def test_accept_unknown_recipe_id_raises_404(self, db: Session, user: User) -> None:
        job, _ = self._make_job_with_drafts(db, user, n=1)
        with pytest.raises(ApiError) as exc:
            svc.accept_draft(db, user_id=user.id, job_id=job.id, recipe_id=uuid.uuid4())
        assert exc.value.status_code == 404

    def test_reject_removes_from_produced_ids(self, db: Session, user: User) -> None:
        job, drafts = self._make_job_with_drafts(db, user, n=2)
        svc.reject_draft(db, user_id=user.id, job_id=job.id, recipe_id=drafts[0].id)
        db.refresh(job)
        assert str(drafts[0].id) not in job.produced_recipe_ids
        assert str(drafts[1].id) in job.produced_recipe_ids

    def test_reject_deletes_recipe(self, db: Session, user: User) -> None:
        job, drafts = self._make_job_with_drafts(db, user, n=1)
        recipe_id = drafts[0].id
        svc.reject_draft(db, user_id=user.id, job_id=job.id, recipe_id=recipe_id)
        assert db.get(Recipe, recipe_id) is None

    def test_reject_last_draft_completes_job(self, db: Session, user: User) -> None:
        job, drafts = self._make_job_with_drafts(db, user, n=1)
        svc.reject_draft(db, user_id=user.id, job_id=job.id, recipe_id=drafts[0].id)
        db.refresh(job)
        assert job.status == JobStatus.done

    def test_reject_unknown_id_raises_404(self, db: Session, user: User) -> None:
        job, _ = self._make_job_with_drafts(db, user, n=1)
        with pytest.raises(ApiError) as exc:
            svc.reject_draft(db, user_id=user.id, job_id=job.id, recipe_id=uuid.uuid4())
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# accept_all_high_confidence
# ---------------------------------------------------------------------------


class TestAcceptAllHighConfidence:
    def _make_needs_review_job(
        self,
        db: Session,
        user: User,
        confidence: float,
        n_drafts: int = 2,
    ) -> Job:
        job = svc.submit_url(
            db,
            user_id=user.id,
            url=f"https://example.com/r{uuid.uuid4().hex[:8]}",
        )
        job.status = JobStatus.needs_review
        job.extraction_meta = {"confidence": confidence, "tier_used": 1}
        db.flush()

        draft_ids = []
        for i in range(n_drafts):
            r = make_draft_recipe(db, user.id, title=f"Auto {i}")
            draft_ids.append(str(r.id))
        job.produced_recipe_ids = draft_ids
        db.flush()
        return job

    def test_accepts_high_confidence_recipes(self, db: Session, user: User) -> None:
        job = self._make_needs_review_job(db, user, confidence=0.95, n_drafts=3)
        count = svc.accept_all_high_confidence(db, user_id=user.id, threshold=0.9)
        assert count == 3
        db.refresh(job)
        assert job.status == JobStatus.done

    def test_skips_low_confidence(self, db: Session, user: User) -> None:
        self._make_needs_review_job(db, user, confidence=0.7, n_drafts=2)
        count = svc.accept_all_high_confidence(db, user_id=user.id, threshold=0.9)
        assert count == 0

    def test_threshold_boundary_exact(self, db: Session, user: User) -> None:
        """confidence == threshold should be accepted (>= check)."""
        job = self._make_needs_review_job(db, user, confidence=0.9, n_drafts=1)
        count = svc.accept_all_high_confidence(db, user_id=user.id, threshold=0.9)
        assert count == 1
        db.refresh(job)
        assert job.status == JobStatus.done

    def test_only_affects_needs_review(self, db: Session, user: User) -> None:
        """Done / queued jobs are not touched."""
        self._make_needs_review_job(db, user, confidence=0.95)
        j2 = self._make_needs_review_job(db, user, confidence=0.95)
        j2.status = JobStatus.done
        db.flush()

        count = svc.accept_all_high_confidence(db, user_id=user.id, threshold=0.9)
        # Only j1's drafts (2) should be accepted
        assert count == 2

    def test_owner_scoped(self, db: Session, user: User, user2: User) -> None:
        """Only the calling user's jobs are processed."""
        self._make_needs_review_job(db, user2, confidence=0.95, n_drafts=3)
        count = svc.accept_all_high_confidence(db, user_id=user.id, threshold=0.9)
        assert count == 0

    def test_malformed_confidence_skipped_not_500(self, db: Session, user: User) -> None:
        """A non-numeric confidence in extraction_meta must skip the job, not raise."""
        job = self._make_needs_review_job(db, user, confidence=0.95, n_drafts=1)
        job.extraction_meta = {"confidence": "high", "tier_used": 1}
        db.flush()

        count = svc.accept_all_high_confidence(db, user_id=user.id, threshold=0.9)
        assert count == 0
        db.refresh(job)
        assert job.status == JobStatus.needs_review  # untouched


# ---------------------------------------------------------------------------
# JobOut schema
# ---------------------------------------------------------------------------


class TestJobOutSchema:
    def test_source_fingerprint_not_in_output(self, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/fp-test")
        out = JobOut.model_validate(job)
        data = out.model_dump()
        assert "source_fingerprint" not in data

    def test_text_preview_truncated(self, db: Session, user: User) -> None:
        long_text = "A" * 200 + " tail"
        job = svc.submit_text(db, user_id=user.id, text=long_text)
        out = JobOut.model_validate(job)
        assert out.text_preview is not None
        assert len(out.text_preview) <= 120

    def test_url_job_out_has_url_field(self, db: Session, user: User) -> None:
        job = svc.submit_url(db, user_id=user.id, url="https://example.com/out-test")
        out = JobOut.model_validate(job)
        assert out.url == "https://example.com/out-test"
        assert out.filename is None
        assert out.text_preview is None

    def test_file_job_out_has_filename(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        job = svc.submit_file(
            db,
            user_id=user.id,
            data=b"%PDF-1.4",
            filename="recipe.pdf",
            media_type="application/pdf",
            store=store,
        )
        out = JobOut.model_validate(job)
        assert out.filename == "recipe.pdf"

    def test_artifacts_converted_to_url_paths(self, db: Session, user: User, store) -> None:  # type: ignore[no-untyped-def]
        job = svc.submit_file(
            db,
            user_id=user.id,
            data=b"\x89PNG\r\n\x1a\n img",
            filename="photo.png",
            media_type="image/png",
            store=store,
        )
        job.artifacts = {"raw_text_ref": "abc/def.txt"}
        db.flush()
        out = JobOut.model_validate(job)
        assert out.artifacts.get("raw_text_ref") == "/api/files/abc/def.txt"

    def test_cost_usd_as_float(self, db: Session, user: User) -> None:
        from decimal import Decimal

        job = svc.submit_url(db, user_id=user.id, url="https://example.com/cost-test")
        job.cost_usd = Decimal("0.012345")
        db.flush()
        out = JobOut.model_validate(job)
        assert isinstance(out.cost_usd, float)
        assert abs(out.cost_usd - 0.012345) < 1e-9


# ---------------------------------------------------------------------------
# cookbook service helpers (verify_recipe, are_verified, find_by_fingerprint)
# ---------------------------------------------------------------------------


class TestCookbookHelpers:
    def test_verify_recipe(self, db: Session, user: User) -> None:
        r = make_draft_recipe(db, user.id)
        assert r.is_verified is False
        cookbook_service.verify_recipe(db, user.id, r.id)
        db.refresh(r)
        assert r.is_verified is True

    def test_verify_recipe_wrong_owner_404(self, db: Session, user: User, user2: User) -> None:
        r = make_draft_recipe(db, user.id)
        with pytest.raises(ApiError) as exc:
            cookbook_service.verify_recipe(db, user2.id, r.id)
        assert exc.value.status_code == 404

    def test_are_verified(self, db: Session, user: User) -> None:
        r1 = make_draft_recipe(db, user.id, is_verified=False)
        r2 = make_draft_recipe(db, user.id, is_verified=False)
        cookbook_service.verify_recipe(db, user.id, r1.id)
        db.flush()

        result = cookbook_service.are_verified(db, user.id, [r1.id, r2.id])
        assert result[r1.id] is True
        assert result[r2.id] is False

    def test_are_verified_empty_list(self, db: Session, user: User) -> None:
        assert cookbook_service.are_verified(db, user.id, []) == {}

    def test_find_recipe_id_by_fingerprint(self, db: Session, user: User) -> None:
        fp = "deadbeef" * 8  # 64 chars
        r = make_draft_recipe(db, user.id, fingerprint=fp)
        found = cookbook_service.find_recipe_id_by_fingerprint(db, user.id, fp)
        assert found == r.id

    def test_find_recipe_id_not_found(self, db: Session, user: User) -> None:
        assert cookbook_service.find_recipe_id_by_fingerprint(db, user.id, "nope") is None

    def test_find_recipe_id_owner_scoped(self, db: Session, user: User, user2: User) -> None:
        fp = "cafebabe" * 8
        make_draft_recipe(db, user2.id, fingerprint=fp)
        assert cookbook_service.find_recipe_id_by_fingerprint(db, user.id, fp) is None
