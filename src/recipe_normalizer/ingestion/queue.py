"""Postgres-backed job queue: claim, complete, fail (with backoff), release stale.

Concurrency model: ``SELECT ... FOR UPDATE SKIP LOCKED`` — many workers can
poll the same table; a claimed row is invisible to other claimers while the
claiming transaction holds the lock, and becomes visible as ``running``
(hence unclaimable) once committed.

Transaction convention: every function flushes only — the CALLER owns commit.
In particular the worker commits immediately after ``claim_next`` so the row
lock is released quickly.

JSONB discipline: artifacts / extraction_meta / produced_recipe_ids are always
ASSIGNED a new value, never mutated in place (SQLAlchemy does not track
in-place JSONB mutation).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.orm import Session

from recipe_normalizer.ingestion.models import Job, JobStatus

__all__ = [
    "MAX_ATTEMPTS",
    "claim_next",
    "complete_needs_review",
    "complete_not_a_recipe",
    "fail",
    "release_stale",
]

MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 30
_ERROR_MAX_LEN = 2000
_DEFAULT_FALLBACK_REASON = "extraction failed"


def _now() -> datetime:
    return datetime.now(UTC)


def _merge_artifacts(job: Job, artifacts: dict[str, Any] | None) -> None:
    if artifacts:
        job.artifacts = {**(job.artifacts or {}), **artifacts}


def _add_cost(job: Job, cost_usd: Decimal | None) -> None:
    if cost_usd is not None:
        job.cost_usd = (job.cost_usd or Decimal("0")) + cost_usd


def _clear_lock(job: Job) -> None:
    job.locked_at = None
    job.locked_by = None


def claim_next(db: Session, *, worker_id: str) -> Job | None:
    """Claim the oldest due queued job, mark it running, and return it.

    Uses FOR UPDATE SKIP LOCKED so concurrent claimers never block on (or
    double-claim) the same row. The caller must commit promptly to release
    the row lock.
    """
    job = db.scalars(
        select(Job)
        .where(
            Job.status == JobStatus.queued,
            or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= _now()),
        )
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        return None

    job.status = JobStatus.running
    job.locked_at = _now()
    job.locked_by = worker_id
    db.flush()
    return job


def complete_needs_review(
    db: Session,
    job: Job,
    *,
    extraction_meta: dict[str, Any],
    produced_ids: list[str],
    artifacts: dict[str, Any],
    cost_usd: Decimal,
) -> None:
    """Extraction succeeded: drafts created, job awaits user review."""
    job.status = JobStatus.needs_review
    job.extraction_meta = dict(extraction_meta)
    job.produced_recipe_ids = list(produced_ids)
    _merge_artifacts(job, artifacts)
    _add_cost(job, cost_usd)
    _clear_lock(job)
    db.flush()


def complete_not_a_recipe(
    db: Session,
    job: Job,
    *,
    reason: str,
    artifacts: dict[str, Any],
    cost_usd: Decimal,
) -> None:
    """Extraction ran but the content is not a recipe (terminal, retryable by user)."""
    job.status = JobStatus.not_a_recipe
    job.reason = reason
    _merge_artifacts(job, artifacts)
    _add_cost(job, cost_usd)
    _clear_lock(job)
    db.flush()


def fail(
    db: Session,
    job: Job,
    *,
    error: str,
    artifacts: dict[str, Any] | None = None,
    cost_usd: Decimal | None = None,
    retryable: bool = True,
) -> None:
    """Record a failed attempt: requeue with exponential backoff or fail terminally.

    Backoff: 30s * 2**(attempts-1) → 30s after the first failure, 60s after the
    second; the third failure is terminal. ``retryable=False`` (e.g.
    CostCapExceeded, duplicate recipe) fails immediately regardless of attempts.
    """
    job.attempts += 1
    _merge_artifacts(job, artifacts)
    _add_cost(job, cost_usd)
    _clear_lock(job)

    if retryable and job.attempts < MAX_ATTEMPTS:
        job.status = JobStatus.queued
        backoff = timedelta(seconds=_BACKOFF_BASE_SECONDS * 2 ** (job.attempts - 1))
        job.next_attempt_at = _now() + backoff
    else:
        job.status = JobStatus.failed
        job.error = error[:_ERROR_MAX_LEN]
        if not job.reason:
            job.reason = _DEFAULT_FALLBACK_REASON
        job.next_attempt_at = None
    db.flush()


def release_stale(db: Session, *, older_than_minutes: int = 10) -> int:
    """Requeue running jobs whose lock is older than the threshold (dead workers).

    Attempts are left unchanged — a crash is not an extraction failure.
    Returns the number of jobs released.
    """
    cutoff = _now() - timedelta(minutes=older_than_minutes)
    result = cast(
        CursorResult[Any],
        db.execute(
            update(Job)
            .where(Job.status == JobStatus.running, Job.locked_at < cutoff)
            .values(status=JobStatus.queued, locked_at=None, locked_by=None)
            .execution_options(synchronize_session=False)
        ),
    )
    return int(result.rowcount or 0)
