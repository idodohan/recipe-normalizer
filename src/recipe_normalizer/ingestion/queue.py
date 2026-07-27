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

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from recipe_normalizer.ingestion.models import Job, JobStatus

logger = logging.getLogger(__name__)

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
# jobs.reason is String(1000) — an LLM-authored "why not a recipe" can be
# longer, and an over-long value fails the flush (StringDataRightTruncation),
# poisoning the session mid-transition.
_REASON_MAX_LEN = 1000
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


def _cas_guard(db: Session, job: Job, expected_locked_by: str | None) -> bool:
    """Compare-and-set guard for transitions out of ``running``.

    With ``expected_locked_by`` set, the row is re-read under FOR UPDATE and
    the transition only proceeds if it is still running AND locked by that
    worker — guarding against the stale-release double-claim race (job
    released by release_stale and re-claimed by another worker while we were
    still processing it). ``None`` skips the check.
    """
    if expected_locked_by is None:
        return True
    db.refresh(job, with_for_update=True)
    if job.status != JobStatus.running or job.locked_by != expected_locked_by:
        logger.warning(
            "job=%s transition skipped: status=%s locked_by=%s (expected running/%s)",
            job.id,
            job.status.value,
            job.locked_by,
            expected_locked_by,
        )
        return False
    return True


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
    expected_locked_by: str | None = None,
) -> bool:
    """Extraction succeeded: drafts created, job awaits user review.

    Returns False (having changed nothing) when the CAS guard rejects the
    transition — the caller MUST roll back whatever it staged for this job,
    see :func:`_cas_guard`.
    """
    if not _cas_guard(db, job, expected_locked_by):
        return False
    job.status = JobStatus.needs_review
    job.extraction_meta = dict(extraction_meta)
    job.produced_recipe_ids = list(produced_ids)
    _merge_artifacts(job, artifacts)
    _add_cost(job, cost_usd)
    _clear_lock(job)
    db.flush()
    return True


def complete_not_a_recipe(
    db: Session,
    job: Job,
    *,
    reason: str,
    artifacts: dict[str, Any],
    cost_usd: Decimal,
    expected_locked_by: str | None = None,
) -> bool:
    """Extraction ran but the content is not a recipe (terminal, retryable by user).

    Returns False (having changed nothing) when the CAS guard rejects the
    transition — see :func:`complete_needs_review`.
    """
    if not _cas_guard(db, job, expected_locked_by):
        return False
    job.status = JobStatus.not_a_recipe
    job.reason = reason[:_REASON_MAX_LEN]
    _merge_artifacts(job, artifacts)
    _add_cost(job, cost_usd)
    _clear_lock(job)
    db.flush()
    return True


def fail(
    db: Session,
    job: Job,
    *,
    error: str,
    artifacts: dict[str, Any] | None = None,
    cost_usd: Decimal | None = None,
    retryable: bool = True,
    expected_locked_by: str | None = None,
) -> bool:
    """Record a failed attempt: requeue with exponential backoff or fail terminally.

    Backoff: 30s * 2**(attempts-1) → 30s after the first failure, 60s after the
    second; the third failure is terminal. ``retryable=False`` (e.g.
    CostCapExceeded, duplicate recipe) fails immediately regardless of attempts.

    Returns False (having changed nothing) when the CAS guard rejects the
    transition — see :func:`complete_needs_review`.
    """
    if not _cas_guard(db, job, expected_locked_by):
        return False
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
    return True


def release_stale(db: Session, *, older_than_minutes: int = 10) -> int:
    """Requeue running jobs whose lock is older than the threshold (dead workers).

    Each release counts as an attempt so a job that repeatedly kills its worker
    (OOM, segfault) cannot loop forever: past MAX_ATTEMPTS it fails terminally.
    Returns the number of jobs released or failed.
    """
    cutoff = _now() - timedelta(minutes=older_than_minutes)
    jobs = db.scalars(
        select(Job)
        .where(Job.status == JobStatus.running, Job.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in jobs:
        job.attempts += 1
        _clear_lock(job)
        if job.attempts < MAX_ATTEMPTS:
            job.status = JobStatus.queued
        else:
            job.status = JobStatus.failed
            job.error = "worker died repeatedly while processing this job"
            if not job.reason:
                job.reason = _DEFAULT_FALLBACK_REASON
    db.flush()
    return len(jobs)
