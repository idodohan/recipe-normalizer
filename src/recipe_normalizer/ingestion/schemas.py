"""Pydantic output schemas for the ingestion API.

``JobOut`` is the standard job representation exposed at every endpoint.
``JobDetailOut`` extends it with the list of draft recipes for the
review-and-accept UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from recipe_normalizer.cookbook.schemas import RecipeSummary
from recipe_normalizer.ingestion.models import InputType, JobStatus

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class SubmitUrlIn(BaseModel):
    url: str


class SubmitTextIn(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Job output
# ---------------------------------------------------------------------------


class JobOut(BaseModel):
    """Serialisable representation of a Job row.

    ``source_fingerprint`` is intentionally EXCLUDED (internal implementation
    detail — do not expose to clients).

    ``url``, ``filename``, and ``text_preview`` are derived from *payload*
    at validation time (never the full raw payload).

    ``artifacts`` maps store keys to /api/files/{ref} URL paths.
    ``cost_usd`` is exposed as ``float`` for JSON friendliness.
    """

    id: uuid.UUID
    input_type: InputType
    status: JobStatus
    created_at: datetime
    attempts: int
    error: str | None = None
    reason: str | None = None
    extraction_meta: dict[str, Any] | None = None
    produced_recipe_ids: list[str] = []
    cost_usd: float = 0.0
    artifacts: dict[str, str] = {}

    # Payload summary fields — computed from raw payload by model_validator
    url: str | None = None
    filename: str | None = None
    text_preview: str | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _extract_payload_summary(cls, values: Any) -> Any:
        """Derive url/filename/text_preview from payload (ORM or dict)."""
        # Works both from ORM instances (via from_attributes) and plain dicts.
        if hasattr(values, "__dict__"):
            # ORM instance
            payload: dict[str, Any] = getattr(values, "payload", {}) or {}
            raw_artifacts: dict[str, Any] = getattr(values, "artifacts", {}) or {}
            input_type = getattr(values, "input_type", None)
            cost = getattr(values, "cost_usd", 0)
        elif isinstance(values, dict):
            payload = values.get("payload", {}) or {}
            raw_artifacts = values.get("artifacts", {}) or {}
            input_type = values.get("input_type")
            cost = values.get("cost_usd", 0)
        else:
            return values

        # URL
        if input_type == InputType.url or (isinstance(input_type, str) and input_type == "url"):
            values_out = _as_dict(values)
            values_out["url"] = payload.get("url")
            values_out["cost_usd"] = float(cost) if cost is not None else 0.0
            values_out["artifacts"] = _artifact_urls(raw_artifacts)
            return values_out

        # File
        if input_type in (InputType.pdf, InputType.image) or (
            isinstance(input_type, str) and input_type in ("pdf", "image")
        ):
            values_out = _as_dict(values)
            values_out["filename"] = payload.get("filename")
            values_out["cost_usd"] = float(cost) if cost is not None else 0.0
            values_out["artifacts"] = _artifact_urls(raw_artifacts)
            return values_out

        # Text
        if input_type == InputType.text or (isinstance(input_type, str) and input_type == "text"):
            raw_text: str = payload.get("text", "") or ""
            values_out = _as_dict(values)
            values_out["text_preview"] = raw_text[:120] if raw_text else None
            values_out["cost_usd"] = float(cost) if cost is not None else 0.0
            values_out["artifacts"] = _artifact_urls(raw_artifacts)
            return values_out

        # Fallback — coerce cost + artifacts anyway
        values_out = _as_dict(values)
        values_out["cost_usd"] = float(cost) if cost is not None else 0.0
        values_out["artifacts"] = _artifact_urls(raw_artifacts)
        return values_out


def _as_dict(obj: Any) -> dict[str, Any]:
    """Convert an ORM instance or plain dict to a plain mutable dict."""
    if isinstance(obj, dict):
        return dict(obj)
    # ORM instance — pull all mapped attributes
    from sqlalchemy.inspection import inspect as sa_inspect  # lazy import

    try:
        mapper = sa_inspect(type(obj))
        result: dict[str, Any] = {col.key: getattr(obj, col.key) for col in mapper.column_attrs}
    except Exception:
        result = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return result


def _artifact_urls(artifacts: dict[str, Any]) -> dict[str, str]:
    """Convert {key: file_ref} artifact dict to {key: /api/files/{ref}} URL paths."""
    return {k: f"/api/files/{v}" for k, v in artifacts.items() if isinstance(v, str)}


class JobDetailOut(JobOut):
    """Extended job output that also embeds the list of draft recipe summaries."""

    drafts: list[RecipeSummary] = []
