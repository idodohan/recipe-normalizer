"""Ingestion models: InputType, JobStatus, and Job.

Job lifecycle::

    queued → running → needs_review → done
                     ↘ failed
                     ↘ not_a_recipe

JSONB shapes
------------
payload:
  url inputs:   {"url": "<str>"}
  pdf/image:    {"file_ref": "<object-store-key>"}
  text:         {"text": "<raw text>"}

artifacts (populated during/after extraction):
  {"raw_text_ref": "<object-store-key>",   # OCR / fetched text
   "llm_response_ref": "<object-store-key>"}

extraction_meta (set after LLM extraction, nullable):
  {"tier_used": <int>, "confidence": <float>, ...}

produced_recipe_ids:
  List of UUID strings (non-FK by design — draft recipes may be deleted on reject).
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from recipe_normalizer.db import Base, TimestampMixin, new_uuid


class InputType(enum.StrEnum):
    url = "url"
    pdf = "pdf"
    image = "image"
    text = "text"


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    needs_review = "needs_review"
    done = "done"
    failed = "failed"
    not_a_recipe = "not_a_recipe"


class Job(TimestampMixin, Base):
    """An ingestion job: one user-submitted input moving through the extraction lifecycle."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    input_type: Mapped[InputType] = mapped_column(Enum(InputType))

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.queued, server_default="queued"
    )

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    source_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    attempts: Mapped[int] = mapped_column(default=0, server_default="0")

    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)

    error: Mapped[str | None] = mapped_column(String(2000), nullable=True, default=None)

    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True, default=None)

    artifacts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    extraction_meta: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    produced_recipe_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)

    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )

    __table_args__ = (
        # Composite index for queue-polling: workers scan by status + next_attempt_at
        Index("ix_jobs_queue_poll", "status", "next_attempt_at"),
    )
