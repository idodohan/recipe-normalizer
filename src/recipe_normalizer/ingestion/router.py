"""Ingestion API routes: submit, job management, and review actions."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response, UploadFile
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user, limit_by_user
from recipe_normalizer.config import settings
from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import RecipeSummary
from recipe_normalizer.db import get_db
from recipe_normalizer.errors import ApiError
from recipe_normalizer.filestore import FileStore, get_file_store
from recipe_normalizer.ingestion import service as ingestion_service
from recipe_normalizer.ingestion.models import Job, JobStatus
from recipe_normalizer.ingestion.schemas import JobDetailOut, JobOut, SubmitTextIn, SubmitUrlIn

router = APIRouter(prefix="/api", tags=["ingestion"])

_ingest_limit = limit_by_user("ingest", settings.rate_limit_ingest_per_hour, 3600.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_out(job: Job) -> JobOut:
    return JobOut.model_validate(job)


def _job_detail_out(job: Job, db: Session, user_id: uuid.UUID) -> JobDetailOut:
    """Build a JobDetailOut directly from the ORM Job, then attach draft summaries.

    Validating straight from the ORM object ensures the payload-summary
    validator sees the real payload (url/filename/text_preview populated,
    artifact paths prefixed exactly once).
    """
    detail = JobDetailOut.model_validate(job)

    drafts: list[RecipeSummary] = []
    for rid in job.produced_recipe_ids:
        try:
            recipe = cookbook_service.get_recipe(db, owner_id=user_id, recipe_id=uuid.UUID(rid))
        except ApiError:
            continue  # Deleted or inaccessible — skip
        drafts.append(
            RecipeSummary(
                id=recipe.id,
                title=recipe.title,
                image_ref=recipe.image_ref,
                dish_types=recipe.dish_types,
                total_min=recipe.total_min,
                is_verified=recipe.is_verified,
                is_favorite=recipe.is_favorite,
                created_at=recipe.created_at,
            )
        )

    detail.drafts = drafts
    return detail


# ---------------------------------------------------------------------------
# Submission endpoints
# ---------------------------------------------------------------------------


@router.post("/ingest/url", status_code=202, response_model=JobOut)
def ingest_url(
    body: SubmitUrlIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
    _: None = Depends(_ingest_limit),  # noqa: B008
) -> JobOut:
    job = ingestion_service.submit_url(db, user_id=current_user.id, url=body.url)
    return _job_out(job)


@router.post("/ingest/text", status_code=202, response_model=JobOut)
def ingest_text(
    body: SubmitTextIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
    _: None = Depends(_ingest_limit),  # noqa: B008
) -> JobOut:
    job = ingestion_service.submit_text(db, user_id=current_user.id, text=body.text)
    return _job_out(job)


@router.post("/ingest/file", status_code=202, response_model=JobOut)
async def ingest_file(
    file: UploadFile,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
    store: FileStore = Depends(get_file_store),  # noqa: B008
    _: None = Depends(_ingest_limit),  # noqa: B008
) -> JobOut:
    # Bounded read: never buffer more than the limit + 1 byte. If we got the
    # extra byte the body exceeds the limit and the service rejects it.
    data = await file.read(ingestion_service.MAX_FILE_BYTES + 1)
    media_type = file.content_type or "application/octet-stream"
    filename = file.filename or "upload"
    job = ingestion_service.submit_file(
        db,
        user_id=current_user.id,
        data=data,
        filename=filename,
        media_type=media_type,
        store=store,
    )
    return _job_out(job)


# ---------------------------------------------------------------------------
# Job query endpoints
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    status: JobStatus | None = Query(default=None),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[JobOut]:
    jobs = ingestion_service.list_jobs(db, user_id=current_user.id, status=status)
    return [_job_out(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def get_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> JobDetailOut:
    job = ingestion_service.get_job(db, user_id=current_user.id, job_id=job_id)
    return _job_detail_out(job, db, current_user.id)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> JobOut:
    job = ingestion_service.retry_job(db, user_id=current_user.id, job_id=job_id)
    return _job_out(job)


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/recipes/{recipe_id}/accept", status_code=204)
def accept_draft(
    job_id: uuid.UUID,
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    ingestion_service.accept_draft(db, user_id=current_user.id, job_id=job_id, recipe_id=recipe_id)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/recipes/{recipe_id}/reject", status_code=204)
def reject_draft(
    job_id: uuid.UUID,
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    ingestion_service.reject_draft(db, user_id=current_user.id, job_id=job_id, recipe_id=recipe_id)
    return Response(status_code=204)


@router.post("/jobs/accept-high-confidence")
def accept_high_confidence(
    threshold: float = Query(default=0.9, ge=0.0, le=1.0),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> dict[str, int]:
    count = ingestion_service.accept_all_high_confidence(
        db, user_id=current_user.id, threshold=threshold
    )
    return {"accepted": count}
