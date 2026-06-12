"""Tests for ingestion Job model: defaults, field round-trips, and FK cascade."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.ingestion.models import InputType, Job, JobStatus
from recipe_normalizer.users.models import User


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        email="ingestion-test@example.com",
        password_hash="hashed",
        display_name="Ingestion Tester",
    )
    db_session.add(u)
    db_session.flush()
    return u


def test_job_defaults(db_session: Session, user: User) -> None:
    """A newly flushed Job should carry all expected defaults."""
    job = Job(
        user_id=user.id,
        input_type=InputType.url,
        payload={"url": "https://x.test/r"},
        source_fingerprint="abc123",
    )
    db_session.add(job)
    db_session.flush()
    db_session.refresh(job)

    assert job.status == JobStatus.queued
    assert job.attempts == 0
    assert job.artifacts == {}
    assert job.produced_recipe_ids == []
    assert job.cost_usd == Decimal("0")
    assert job.next_attempt_at is None
    assert job.locked_at is None
    assert job.locked_by is None
    assert job.error is None
    assert job.reason is None
    assert job.extraction_meta is None


def test_job_transition_fields_persist(db_session: Session, user: User) -> None:
    """Transition fields should survive expire+re-query with correct types."""
    recipe_id = str(uuid4())
    job = Job(
        user_id=user.id,
        input_type=InputType.url,
        payload={"url": "https://x.test/r"},
        source_fingerprint="abc123",
    )
    db_session.add(job)
    db_session.flush()

    # Transition to running state, filling in worker fields
    job.status = JobStatus.running
    job.locked_by = "w1"
    job.artifacts = {"raw_text_ref": "a/b.txt"}
    job.produced_recipe_ids = [recipe_id]
    job.extraction_meta = {"tier_used": 1, "confidence": 0.93}
    job.cost_usd = Decimal("0.012345")
    db_session.flush()

    job_id = job.id
    db_session.expire(job)

    reloaded = db_session.get(Job, job_id)
    assert reloaded is not None
    assert reloaded.status == JobStatus.running
    assert reloaded.locked_by == "w1"
    assert reloaded.artifacts == {"raw_text_ref": "a/b.txt"}
    assert reloaded.produced_recipe_ids == [recipe_id]
    assert reloaded.extraction_meta == {"tier_used": 1, "confidence": 0.93}
    assert reloaded.cost_usd == Decimal("0.012345")


def test_job_user_fk_cascade(db_session: Session, user: User) -> None:
    """Deleting the owning user should cascade-delete their jobs."""
    job = Job(
        user_id=user.id,
        input_type=InputType.url,
        payload={"url": "https://x.test/r"},
    )
    db_session.add(job)
    db_session.flush()
    job_id = job.id

    db_session.delete(user)
    db_session.flush()
    db_session.expire_all()

    assert db_session.get(Job, job_id) is None
