"""Ingestion service: submit, deduplicate, and manage extraction jobs.

Transaction convention: service flushes; HTTP layer (or test) owns commit.
Import-linter: ingestion may import its own models + cookbook.service/schemas
+ filestore + netguard (dependency-free SSRF guard); NOT sibling
models nor the rest of extraction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.service import DuplicateRecipeError  # re-used
from recipe_normalizer.errors import ApiError
from recipe_normalizer.filestore import FileStore
from recipe_normalizer.ingestion.fingerprint import (
    fingerprint_bytes,
    fingerprint_text,
)
from recipe_normalizer.ingestion.fingerprint import (
    fingerprint_url as _fingerprint_url,
)
from recipe_normalizer.ingestion.models import InputType, Job, JobStatus
from recipe_normalizer.netguard import UnsafeUrlError, assert_public_url
from recipe_normalizer.sniff import detect_media_type

__all__ = [
    "accept_all_high_confidence",
    "accept_draft",
    "get_job",
    "list_jobs",
    "reject_draft",
    "retry_job",
    "submit_file",
    "submit_text",
    "submit_url",
]

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_MAX_URL_LEN = 2_000
_MAX_TEXT_LEN = 200_000
MAX_FILE_BYTES = 30 * 1024 * 1024  # 30 MB — public: the router bounds its read with this

_ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }
)

_MEDIA_TYPE_SUFFIX: dict[str, str] = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

_ACTIVE_STATUSES = frozenset(
    {
        JobStatus.queued,
        JobStatus.running,
        JobStatus.needs_review,
    }
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_active_job_dup(
    db: Session,
    user_id: uuid.UUID,
    fingerprint: str,
) -> None:
    """Raise ApiError 409 if there is already an active job for (user, fingerprint)."""
    existing = db.scalars(
        select(Job).where(
            Job.user_id == user_id,
            Job.source_fingerprint == fingerprint,
            Job.status.in_(list(_ACTIVE_STATUSES)),
        )
    ).first()
    if existing is not None:
        raise ApiError(
            409,
            "duplicate_job",
            "This source is already being processed.",
            extra={"job_id": str(existing.id)},
        )


def _maybe_complete(db: Session, job: Job) -> None:
    """Transition job to 'done' if all produced recipes are verified or removed.

    - Empty produced_recipe_ids list + needs_review → done.
    - All remaining ids verified → done.
    - Idempotent: already-done jobs are left alone.
    """
    if job.status == JobStatus.done:
        return
    ids_as_uuids = [uuid.UUID(rid) for rid in job.produced_recipe_ids]
    if not ids_as_uuids:
        job.status = JobStatus.done
        db.flush()
        return

    verification_map = cookbook_service.are_verified(db, job.user_id, ids_as_uuids)
    # All remaining (still-existing) recipes must be verified.
    # Deleted recipes (not in map) are treated as removed → skip.
    if all(verification_map.values()):
        job.status = JobStatus.done
        db.flush()


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def submit_url(
    db: Session,
    *,
    user_id: uuid.UUID,
    url: str,
) -> Job:
    """Submit a URL for extraction.

    Validates: http/https scheme, ≤2000 chars.
    Deduplicates against existing recipes and active jobs.
    Returns a new queued Job.
    """
    # Validate
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ApiError(422, "validation_error", "URL must use http or https scheme.")
    if len(url) > _MAX_URL_LEN:
        raise ApiError(422, "validation_error", f"URL must be ≤{_MAX_URL_LEN} characters.")
    try:
        assert_public_url(url)
    except UnsafeUrlError as exc:
        raise ApiError(
            422,
            "unsafe_url",
            "This URL points to a private or internal address and cannot be fetched.",
        ) from exc

    fingerprint = _fingerprint_url(url)

    # Dedupe against existing recipes
    existing_recipe_id = cookbook_service.find_recipe_id_by_fingerprint(db, user_id, fingerprint)
    if existing_recipe_id is not None:
        raise DuplicateRecipeError(existing_recipe_id)

    # Dedupe against active jobs
    _check_active_job_dup(db, user_id, fingerprint)

    job = Job(
        user_id=user_id,
        input_type=InputType.url,
        payload={"url": url},
        source_fingerprint=fingerprint,
        status=JobStatus.queued,
    )
    db.add(job)
    db.flush()
    return job


def submit_text(
    db: Session,
    *,
    user_id: uuid.UUID,
    text: str,
) -> Job:
    """Submit raw text for extraction.

    Validates: non-empty, ≤200_000 chars.
    Deduplicates against existing recipes and active jobs.
    Returns a new queued Job.
    """
    text = text.strip()
    if not text:
        raise ApiError(422, "validation_error", "Text must not be empty.")
    if len(text) > _MAX_TEXT_LEN:
        raise ApiError(422, "validation_error", f"Text must be ≤{_MAX_TEXT_LEN} characters.")

    fingerprint = fingerprint_text(text)

    existing_recipe_id = cookbook_service.find_recipe_id_by_fingerprint(db, user_id, fingerprint)
    if existing_recipe_id is not None:
        raise DuplicateRecipeError(existing_recipe_id)

    _check_active_job_dup(db, user_id, fingerprint)

    job = Job(
        user_id=user_id,
        input_type=InputType.text,
        payload={"text": text},
        source_fingerprint=fingerprint,
        status=JobStatus.queued,
    )
    db.add(job)
    db.flush()
    return job


def submit_file(
    db: Session,
    *,
    user_id: uuid.UUID,
    data: bytes,
    filename: str,
    media_type: str,
    store: FileStore,
) -> Job:
    """Submit an uploaded file (PDF or image) for extraction.

    Validates file size, then sniffs the actual content from magic bytes —
    the client-supplied media_type is only a hint and is never trusted for
    validation, storage suffix, or the stored media type (a mislabeled or
    spoofed Content-Type is caught here). Saves to the FileStore.
    Deduplicates against existing recipes and active jobs.
    Returns a new queued Job.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(422, "file_too_large", "File must be ≤ 30 MB.")

    sniffed_media_type = detect_media_type(data)
    if sniffed_media_type is None or sniffed_media_type not in _ALLOWED_MEDIA_TYPES:
        raise ApiError(
            422,
            "unsupported_file_type",
            f"File content is not a supported type (detected: {sniffed_media_type or 'unknown'}). "
            f"Allowed: {', '.join(sorted(_ALLOWED_MEDIA_TYPES))}",
        )
    media_type = sniffed_media_type  # sniffed content is authoritative, not the client header

    fingerprint = fingerprint_bytes(data)

    existing_recipe_id = cookbook_service.find_recipe_id_by_fingerprint(db, user_id, fingerprint)
    if existing_recipe_id is not None:
        raise DuplicateRecipeError(existing_recipe_id)

    _check_active_job_dup(db, user_id, fingerprint)

    suffix = _MEDIA_TYPE_SUFFIX[media_type]
    file_ref = store.save(data, suffix=suffix)

    input_type = InputType.pdf if media_type == "application/pdf" else InputType.image

    job = Job(
        user_id=user_id,
        input_type=input_type,
        payload={"file_ref": file_ref, "filename": filename, "media_type": media_type},
        source_fingerprint=fingerprint,
        status=JobStatus.queued,
    )
    db.add(job)
    db.flush()
    return job


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def list_jobs(
    db: Session,
    *,
    user_id: uuid.UUID,
    status: JobStatus | None = None,
    limit: int = 50,
) -> list[Job]:
    """Return jobs for *user_id*, newest-first, optionally filtered by status."""
    stmt = select(Job).where(Job.user_id == user_id).order_by(Job.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    return list(db.scalars(stmt).all())


def get_job(
    db: Session,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
) -> Job:
    """Fetch a job by id, owner-scoped.  Raises ApiError 404 if not found."""
    job = db.scalars(select(Job).where(Job.id == job_id, Job.user_id == user_id)).first()
    if job is None:
        raise ApiError(404, "not_found", f"Job {job_id} not found.")
    return job


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def retry_job(
    db: Session,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
) -> Job:
    """Reset a failed/not_a_recipe job back to queued for re-processing.

    Only allowed when status is ``failed`` or ``not_a_recipe``.
    Preserves artifacts (worker skips re-acquire when raw_text_ref present).
    """
    job = get_job(db, user_id=user_id, job_id=job_id)

    allowed = {JobStatus.failed, JobStatus.not_a_recipe}
    if job.status not in allowed:
        raise ApiError(
            422,
            "invalid_transition",
            f"Cannot retry a job with status '{job.status}'. "
            f"Only {' or '.join(s.value for s in allowed)} jobs may be retried.",
        )

    job.status = JobStatus.queued
    job.attempts = 0
    job.next_attempt_at = None
    job.error = None
    job.reason = None
    # artifacts are intentionally preserved
    db.flush()
    return job


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------


def accept_draft(
    db: Session,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> None:
    """Verify a draft recipe produced by *job_id*.

    - recipe_id must be in job.produced_recipe_ids (404 otherwise).
    - Calls cookbook.service.verify_recipe to set is_verified=True.
    - If all remaining produced recipes are now verified, transitions job → done.
    - Flushes; caller owns commit.
    """
    job = get_job(db, user_id=user_id, job_id=job_id)

    if str(recipe_id) not in job.produced_recipe_ids:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} is not a draft of job {job_id}.")

    cookbook_service.verify_recipe(db, user_id, recipe_id)
    _maybe_complete(db, job)


def reject_draft(
    db: Session,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> None:
    """Delete a draft recipe and remove it from job.produced_recipe_ids.

    - recipe_id must be in job.produced_recipe_ids (404 otherwise).
    - Deletes the recipe via cookbook.service.delete_recipe.
    - Assigns a new list to produced_recipe_ids (JSONB — never mutate in place).
    - If all remaining produced recipes are now verified (or list is empty),
      transitions job → done.
    - Flushes; caller owns commit.
    """
    job = get_job(db, user_id=user_id, job_id=job_id)

    recipe_id_str = str(recipe_id)
    if recipe_id_str not in job.produced_recipe_ids:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} is not a draft of job {job_id}.")

    cookbook_service.delete_recipe(db, owner_id=user_id, recipe_id=recipe_id)

    # JSONB: assign new list, never mutate in place
    job.produced_recipe_ids = [rid for rid in job.produced_recipe_ids if rid != recipe_id_str]
    db.flush()

    _maybe_complete(db, job)


# ---------------------------------------------------------------------------
# Bulk accept
# ---------------------------------------------------------------------------


def accept_all_high_confidence(
    db: Session,
    *,
    user_id: uuid.UUID,
    threshold: float = 0.9,
) -> int:
    """Accept all high-confidence draft recipes across needs_review jobs.

    Iterates all of the user's ``needs_review`` jobs where
    ``extraction_meta.confidence >= threshold``.  For each, verifies every
    produced recipe draft and marks the job done.

    Returns the count of *recipes* accepted (not jobs).
    """
    stmt = select(Job).where(
        Job.user_id == user_id,
        Job.status == JobStatus.needs_review,
    )
    jobs = list(db.scalars(stmt).all())

    accepted_count = 0
    for job in jobs:
        meta = job.extraction_meta or {}
        confidence = meta.get("confidence")
        try:
            is_confident = confidence is not None and float(confidence) >= threshold
        except (TypeError, ValueError):
            # Malformed extraction_meta (non-numeric confidence) — skip, never 500
            is_confident = False
        if not is_confident:
            continue

        for recipe_id_str in list(job.produced_recipe_ids):
            try:
                recipe_id = uuid.UUID(recipe_id_str)
                cookbook_service.verify_recipe(db, user_id, recipe_id)
                accepted_count += 1
            except (ApiError, ValueError):
                # Recipe already deleted or invalid UUID — skip silently
                pass

        job.status = JobStatus.done
        db.flush()

    return accepted_count
