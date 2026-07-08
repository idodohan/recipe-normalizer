"""Tests for the Postgres job queue (claim/complete/fail/release_stale).

These tests exercise REAL row locking (SELECT ... FOR UPDATE SKIP LOCKED), so
they use independent committed sessions over the ``engine`` fixture — NOT the
savepoint-isolated ``db_session``. Everything committed here is cleaned up in
the ``queue_env`` fixture teardown (same pattern as test_get_db_integration).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session, sessionmaker

from recipe_normalizer.ingestion.models import InputType, Job, JobStatus
from recipe_normalizer.ingestion.queue import (
    claim_next,
    complete_needs_review,
    complete_not_a_recipe,
    fail,
    release_stale,
)
from recipe_normalizer.users.models import User


@pytest.fixture()
def queue_env(engine: Engine) -> Iterator[tuple[sessionmaker[Session], uuid.UUID]]:
    """(session_factory, user_id) over committed sessions; cleans up on teardown."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        user = User(
            email=f"queue-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="hash",
            display_name="Queue Tester",
        )
        s.add(user)
        s.commit()
        user_id = user.id

    yield factory, user_id

    with factory() as s:
        s.execute(delete(Job).where(Job.user_id == user_id))
        s.execute(delete(User).where(User.id == user_id))
        s.commit()


def _add_job(s: Session, user_id: uuid.UUID, **overrides: object) -> Job:
    job = Job(
        user_id=user_id,
        input_type=InputType.text,
        payload={"text": "some recipe text"},
        **overrides,  # type: ignore[arg-type]
    )
    s.add(job)
    s.commit()
    return job


# ---------------------------------------------------------------------------
# claim_next
# ---------------------------------------------------------------------------


def test_claim_next_returns_none_when_empty(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, _user_id = queue_env
    with factory() as s:
        assert claim_next(s, worker_id="w1") is None


def test_claim_marks_running_and_locked(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        job = _add_job(s, user_id)
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == JobStatus.running
        assert claimed.locked_by == "w1"
        assert claimed.locked_at is not None
        s.commit()


def test_two_sessions_claim_different_jobs(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    """SKIP LOCKED: concurrent claimers never grab the same row."""
    factory, user_id = queue_env
    with factory() as setup:
        j1 = _add_job(setup, user_id)
        j2 = _add_job(setup, user_id)

    s1, s2 = factory(), factory()
    try:
        c1 = claim_next(s1, worker_id="w1")  # row lock held until s1 commits
        c2 = claim_next(s2, worker_id="w2")
        assert c1 is not None and c2 is not None
        assert {c1.id, c2.id} == {j1.id, j2.id}
        s1.commit()
        s2.commit()
    finally:
        s1.close()
        s2.close()


def test_claimed_job_invisible_to_second_claimer(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as setup:
        _add_job(setup, user_id)

    s1, s2 = factory(), factory()
    try:
        claimed = claim_next(s1, worker_id="w1")
        assert claimed is not None
        # Before s1 commits: the row is lock-held → skipped, not blocked on.
        assert claim_next(s2, worker_id="w2") is None
        s2.rollback()
        s1.commit()
        # After s1 commits: the job is running (locked), still unclaimable.
        assert claim_next(s2, worker_id="w2") is None
        s2.rollback()
    finally:
        s1.close()
        s2.close()


def test_future_next_attempt_at_not_claimed(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        job = _add_job(s, user_id, next_attempt_at=datetime.now(UTC) + timedelta(minutes=5))
        assert claim_next(s, worker_id="w1") is None
        s.rollback()

        # Backdate it → claimable.
        job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        assert claimed.id == job.id
        s.commit()


def test_claim_oldest_first(queue_env: tuple[sessionmaker[Session], uuid.UUID]) -> None:
    factory, user_id = queue_env
    with factory() as s:
        old = _add_job(s, user_id, created_at=datetime.now(UTC) - timedelta(minutes=2))
        _new = _add_job(s, user_id)
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        assert claimed.id == old.id
        s.commit()


# ---------------------------------------------------------------------------
# fail: retry/backoff schedule
# ---------------------------------------------------------------------------


def _assert_seconds_close(actual: float, expected: float, tolerance: float = 5.0) -> None:
    assert abs(actual - expected) <= tolerance, f"expected ~{expected}s, got {actual}s"


def test_fail_backoff_schedule_30_60_then_failed(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        job = _add_job(s, user_id)

        # Attempt 1 → requeued with ~30s backoff.
        fail(s, job, error="boom 1")
        s.commit()
        assert job.status == JobStatus.queued
        assert job.attempts == 1
        assert job.locked_at is None and job.locked_by is None
        assert job.next_attempt_at is not None
        _assert_seconds_close((job.next_attempt_at - datetime.now(UTC)).total_seconds(), 30)

        # Attempt 2 → requeued with ~60s backoff.
        fail(s, job, error="boom 2")
        s.commit()
        assert job.status == JobStatus.queued
        assert job.attempts == 2
        assert job.next_attempt_at is not None
        _assert_seconds_close((job.next_attempt_at - datetime.now(UTC)).total_seconds(), 60)

        # Attempt 3 → terminal failure.
        fail(s, job, error="boom 3")
        s.commit()
        assert job.status == JobStatus.failed
        assert job.attempts == 3
        assert job.error == "boom 3"
        assert job.reason == "extraction failed"


def test_fail_non_retryable_goes_straight_to_failed(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        job = _add_job(s, user_id)
        fail(s, job, error="cost cap exceeded", retryable=False)
        s.commit()
        assert job.status == JobStatus.failed
        assert job.attempts == 1
        assert job.error == "cost cap exceeded"
        assert job.reason == "extraction failed"


def test_fail_truncates_error_and_keeps_existing_reason(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        job = _add_job(s, user_id, reason="tier 1 gave up")
        fail(s, job, error="x" * 5000, retryable=False)
        s.commit()
        assert job.error == "x" * 2000
        assert job.reason == "tier 1 gave up"


def test_fail_merges_artifacts_and_adds_cost(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        job = _add_job(s, user_id, artifacts={"raw_text_ref": "aa/bb.txt"})
        fail(
            s,
            job,
            error="tier failed",
            artifacts={"screenshot_ref": "cc/dd.png"},
            cost_usd=Decimal("0.10"),
        )
        s.commit()
        assert job.artifacts == {"raw_text_ref": "aa/bb.txt", "screenshot_ref": "cc/dd.png"}
        assert job.cost_usd == Decimal("0.10")

        fail(s, job, error="tier failed again", cost_usd=Decimal("0.05"))
        s.commit()
        assert job.cost_usd == Decimal("0.15")


# ---------------------------------------------------------------------------
# complete_*
# ---------------------------------------------------------------------------


def test_complete_needs_review_sets_fields_and_clears_locks(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        _add_job(s, user_id, artifacts={"raw_text_ref": "aa/bb.txt"})
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        recipe_id = str(uuid.uuid4())
        complete_needs_review(
            s,
            claimed,
            extraction_meta={"confidence": 0.92, "tier_used": 1},
            produced_ids=[recipe_id],
            artifacts={"llm_response_ref": "ee/ff.json"},
            cost_usd=Decimal("0.25"),
        )
        s.commit()
        assert claimed.status == JobStatus.needs_review
        assert claimed.extraction_meta == {"confidence": 0.92, "tier_used": 1}
        assert claimed.produced_recipe_ids == [recipe_id]
        assert claimed.artifacts == {
            "raw_text_ref": "aa/bb.txt",
            "llm_response_ref": "ee/ff.json",
        }
        assert claimed.cost_usd == Decimal("0.25")
        assert claimed.locked_at is None and claimed.locked_by is None


def test_complete_not_a_recipe_sets_reason(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        job = _add_job(s, user_id)
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        assert claimed.id == job.id
        complete_not_a_recipe(
            s,
            claimed,
            reason="This is a restaurant menu, not a recipe",
            artifacts={"raw_text_ref": "aa/bb.txt"},
            cost_usd=Decimal("0.01"),
        )
        s.commit()
        assert claimed.status == JobStatus.not_a_recipe
        assert claimed.reason == "This is a restaurant menu, not a recipe"
        assert claimed.artifacts == {"raw_text_ref": "aa/bb.txt"}
        assert claimed.cost_usd == Decimal("0.01")
        assert claimed.locked_at is None and claimed.locked_by is None


# ---------------------------------------------------------------------------
# release_stale
# ---------------------------------------------------------------------------


def test_release_stale_flips_only_stale_running_jobs(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        stale = _add_job(
            s,
            user_id,
            status=JobStatus.running,
            locked_at=datetime.now(UTC) - timedelta(minutes=30),
            locked_by="dead-worker",
            attempts=0,
        )
        fresh = _add_job(
            s,
            user_id,
            status=JobStatus.running,
            locked_at=datetime.now(UTC) - timedelta(minutes=1),
            locked_by="live-worker",
        )
        queued = _add_job(s, user_id)

        count = release_stale(s, older_than_minutes=10)
        s.commit()
        assert count == 1

        s.expire_all()
        assert stale.status == JobStatus.queued
        assert stale.locked_at is None and stale.locked_by is None
        assert stale.attempts == 1  # incremented by release
        assert fresh.status == JobStatus.running
        assert fresh.locked_by == "live-worker"
        assert queued.status == JobStatus.queued


def test_release_stale_increments_attempts(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    """Stale release counts as an attempt; requeued with attempts incremented."""
    factory, user_id = queue_env
    with factory() as s:
        stale = _add_job(
            s,
            user_id,
            status=JobStatus.running,
            locked_at=datetime.now(UTC) - timedelta(minutes=11),
            locked_by="dead-worker",
            attempts=0,
        )
        count = release_stale(s, older_than_minutes=10)
        s.commit()
        assert count == 1

        s.expire_all()
        assert stale.status == JobStatus.queued
        assert stale.attempts == 1
        assert stale.locked_at is None and stale.locked_by is None


def test_release_stale_fails_poison_job(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    """Job released at attempts >= MAX_ATTEMPTS - 1 fails terminally."""
    factory, user_id = queue_env
    from recipe_normalizer.ingestion.queue import MAX_ATTEMPTS

    with factory() as s:
        stale = _add_job(
            s,
            user_id,
            status=JobStatus.running,
            locked_at=datetime.now(UTC) - timedelta(minutes=11),
            locked_by="dead-worker",
            attempts=MAX_ATTEMPTS - 1,
        )
        count = release_stale(s, older_than_minutes=10)
        s.commit()
        assert count == 1

        s.expire_all()
        assert stale.status == JobStatus.failed
        assert stale.attempts == MAX_ATTEMPTS
        assert "worker died repeatedly" in stale.error
        assert stale.locked_at is None and stale.locked_by is None


# ---------------------------------------------------------------------------
# Compare-and-set on terminal transitions (stale-release double-claim race)
# ---------------------------------------------------------------------------


def _claim_then_steal(factory: sessionmaker[Session], user_id: uuid.UUID) -> uuid.UUID:
    """Claim a job as w1, then simulate stale-release + reclaim by w2."""
    with factory() as s:
        _add_job(s, user_id)
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        s.commit()
        job_id = claimed.id

    with factory() as s:
        stolen = s.get(Job, job_id)
        assert stolen is not None
        stolen.locked_by = "w2"  # another worker reclaimed after stale release
        s.commit()
    return job_id


def test_complete_needs_review_skipped_when_lock_stolen(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    job_id = _claim_then_steal(factory, user_id)

    with factory() as s:
        job = s.get(Job, job_id)
        assert job is not None
        complete_needs_review(
            s,
            job,
            extraction_meta={"confidence": 0.9},
            produced_ids=[str(uuid.uuid4())],
            artifacts={},
            cost_usd=Decimal("0.10"),
            expected_locked_by="w1",
        )
        s.commit()

    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.running  # untouched — w2 owns it now
        assert fresh.locked_by == "w2"
        assert fresh.produced_recipe_ids == []
        assert fresh.extraction_meta is None
        assert fresh.cost_usd == Decimal("0")


def test_fail_skipped_when_lock_stolen(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    job_id = _claim_then_steal(factory, user_id)

    with factory() as s:
        job = s.get(Job, job_id)
        assert job is not None
        fail(s, job, error="boom", expected_locked_by="w1")
        s.commit()

    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.running
        assert fresh.locked_by == "w2"
        assert fresh.attempts == 0
        assert fresh.error is None


def test_complete_not_a_recipe_skipped_when_no_longer_running(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    """Status changed under us (e.g. stale-released back to queued) → skip."""
    factory, user_id = queue_env
    with factory() as s:
        _add_job(s, user_id)
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        s.commit()
        job_id = claimed.id

    with factory() as s:
        released = s.get(Job, job_id)
        assert released is not None
        released.status = JobStatus.queued  # stale-released behind our back
        released.locked_at = None
        released.locked_by = None
        s.commit()

    with factory() as s:
        job = s.get(Job, job_id)
        assert job is not None
        complete_not_a_recipe(
            s,
            job,
            reason="not a recipe",
            artifacts={},
            cost_usd=Decimal("0.01"),
            expected_locked_by="w1",
        )
        s.commit()

    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.queued
        assert fresh.reason is None


def test_complete_needs_review_applies_when_lock_matches(
    queue_env: tuple[sessionmaker[Session], uuid.UUID],
) -> None:
    factory, user_id = queue_env
    with factory() as s:
        _add_job(s, user_id)
        claimed = claim_next(s, worker_id="w1")
        assert claimed is not None
        s.commit()
        complete_needs_review(
            s,
            claimed,
            extraction_meta={"confidence": 0.9},
            produced_ids=[],
            artifacts={},
            cost_usd=Decimal("0.10"),
            expected_locked_by="w1",
        )
        s.commit()
        assert claimed.status == JobStatus.needs_review
        assert claimed.locked_by is None
