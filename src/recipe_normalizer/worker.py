"""Extraction worker: claims queued jobs and runs acquire → normalize → persist.

Run: ``python -m recipe_normalizer.worker [--once]``

Transaction model — the worker is NOT behind get_db; it owns sessions
explicitly, one phase per session:

1. housekeeping/claim session: ``release_stale`` (commit), ``claim_next``
   (commit immediately so the FOR UPDATE row lock is released quickly);
2. processing session: re-fetch the job, ``process_job`` (flush-only),
   commit on success;
3. failure session: if processing raised, the broken session is rolled back
   and closed, and ``queue.fail`` runs in a NEW session so the failure
   bookkeeping is isolated from whatever poisoned the first one.

SIGTERM/SIGINT request a graceful stop: the current job finishes, then the
loop exits.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from types import FrameType
from typing import Any, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy.orm import Session

from recipe_normalizer import db as db_module
from recipe_normalizer.config import settings
from recipe_normalizer.cookbook.service import DuplicateRecipeError, SourceType
from recipe_normalizer.extraction import EXTRACTORS, Acquired, TierFailed
from recipe_normalizer.extraction.normalize import (
    NormalizedGroup,
    NormalizedLine,
    NormalizedRecipe,
    NormalizedStep,
    NormalizeResult,
    normalize,
)
from recipe_normalizer.extraction.persist import persist_drafts
from recipe_normalizer.filestore import FileStore, get_file_store
from recipe_normalizer.ingestion import queue
from recipe_normalizer.ingestion.models import Job
from recipe_normalizer.llm.client import CostCapExceeded, DbUsageRecorder, LLMClient

__all__ = ["main", "make_llm_for_job", "process_job", "run_worker"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# InputType value → cookbook SourceType for persisted drafts.
_SOURCE_TYPE_BY_INPUT: dict[str, SourceType] = {
    "url": SourceType.web,
    "pdf": SourceType.pdf,
    "image": SourceType.image,
    "text": SourceType.text,
}

_IMAGE_SUFFIX: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


# ---------------------------------------------------------------------------
# Stub LLM (RN_LLM_STUB=1) — TEST / E2E ONLY
# ---------------------------------------------------------------------------


def _stub_normalize_result() -> NormalizeResult:
    return NormalizeResult(
        is_recipe=True,
        confidence=0.95,
        recipes=[
            NormalizedRecipe(
                title="Stub Recipe",
                groups=[
                    NormalizedGroup(
                        name=None,
                        lines=[
                            NormalizedLine(
                                original_text="1 cup all-purpose flour",
                                name="all-purpose flour",
                                quantity=1.0,
                                unit="cup",
                            ),
                            NormalizedLine(
                                original_text="2 eggs",
                                name="egg",
                                quantity=2.0,
                            ),
                        ],
                    )
                ],
                steps=[NormalizedStep(original_text="Mix everything and bake.")],
            )
        ],
    )


class _StubLLMClient:
    """Deterministic fake LLM, enabled ONLY by ``RN_LLM_STUB=1``.

    *** TEST / E2E ONLY — NEVER ENABLE IN PRODUCTION. ***

    It fabricates a fixed "Stub Recipe" NormalizeResult for ANY input, answers
    every yes/no classification with True, makes zero API calls, spends $0,
    and records no usage. It exists so end-to-end worker runs (CI, compose
    smoke tests) can exercise the full pipeline without an ANTHROPIC_API_KEY.

    Satisfies the LLMClient surface used by the pipeline:
    structured / classify_bool / tool_loop / spent_usd.
    """

    @property
    def spent_usd(self) -> float:
        return 0.0

    def structured(
        self,
        *,
        feature: str,
        output_model: type[T],
        content: str | list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        fast: bool = False,
        max_tokens: int = 8000,
    ) -> T:
        if output_model is NormalizeResult:
            return cast(T, _stub_normalize_result())
        if "index" in output_model.model_fields:  # catalog.match band choice
            return output_model(index=None)
        raise NotImplementedError(f"stub LLM cannot answer feature {feature!r}")

    def classify_bool(
        self, *, feature: str, question: str, content: str, max_chars: int = 30000
    ) -> bool:
        return True

    def tool_loop(
        self,
        *,
        feature: str,
        system: str,
        tools: list[dict[str, Any]],
        initial_content: list[dict[str, Any]],
        execute: Callable[[str, dict[str, Any]], str | list[dict[str, Any]]],
        max_iterations: int = 15,
        max_tokens: int = 8000,
    ) -> Any:
        raise NotImplementedError("stub LLM does not run tool loops")


# ---------------------------------------------------------------------------
# LLM factory (module-level so tests can monkeypatch it)
# ---------------------------------------------------------------------------


def make_llm_for_job(db: Session, job: Job) -> LLMClient:
    """Build the per-job LLM client: usage recorded to this session, capped spend.

    ``RN_LLM_STUB=1`` (env) swaps in :class:`_StubLLMClient` — TEST/E2E ONLY,
    see its docstring.
    """
    if os.environ.get("RN_LLM_STUB") == "1":
        logger.warning("RN_LLM_STUB=1 — using the stub LLM client (test/e2e only)")
        return cast(LLMClient, _StubLLMClient())
    return LLMClient(
        recorder=DbUsageRecorder(db, user_id=job.user_id, job_id=job.id),
        cost_cap_usd=settings.job_cost_cap_usd,
    )


# ---------------------------------------------------------------------------
# Pipeline (one job)
# ---------------------------------------------------------------------------


def _spent(llm: LLMClient) -> Decimal:
    return Decimal(str(llm.spent_usd))


def _source_of(job: Job) -> str | None:
    """Human-facing source string for the draft: url or filename, else None."""
    return cast(str | None, job.payload.get("url") or job.payload.get("filename"))


def _acquire(job: Job, *, llm: LLMClient, store: FileStore) -> Acquired:
    """Acquire content for the job; a retry NEVER re-scrapes (spec §6.4).

    When ``job.artifacts.raw_text_ref`` exists (retained from a previous
    attempt), the text is reloaded from the filestore instead of running the
    extractor again.
    """
    raw_text_ref = (job.artifacts or {}).get("raw_text_ref")
    if raw_text_ref:
        text = store.open(raw_text_ref).decode("utf-8")
        return Acquired(text=text, artifacts=cast(dict[str, str], dict(job.artifacts)))
    extractor = EXTRACTORS[job.input_type.value]
    return extractor.acquire(job.payload, llm=llm, store=store)


def _save_source_image(acquired: Acquired, store: FileStore) -> str | None:
    if acquired.source_image is None:
        return None
    data, media_type = acquired.source_image
    return store.save(data, suffix=_IMAGE_SUFFIX.get(media_type, "bin"))


def process_job(db: Session, job: Job) -> None:
    """Run the full extraction pipeline for one claimed job.

    Flush-only — the CALLER commits. Known pipeline failures (TierFailed,
    CostCapExceeded, DuplicateRecipeError, missing extractor) are recorded via
    queue.fail here; unexpected exceptions propagate so run_worker can fail
    the job from a fresh session.
    """
    started = time.monotonic()
    input_type = job.input_type.value
    logger.info(
        "job=%s input_type=%s attempt=%d status=running", job.id, input_type, job.attempts + 1
    )

    if not (job.artifacts or {}).get("raw_text_ref") and input_type not in EXTRACTORS:
        queue.fail(db, job, error=f"no extractor for input type {input_type!r}", retryable=False)
        _log_transition(job, started)
        return

    llm = make_llm_for_job(db, job)
    store = get_file_store()

    try:
        acquired = _acquire(job, llm=llm, store=store)
        image_ref = _save_source_image(acquired, store)
        result = normalize(acquired, llm=llm)

        if not result.is_recipe:
            queue.complete_not_a_recipe(
                db,
                job,
                reason=result.reason or "not a recipe",
                artifacts=dict(acquired.artifacts),
                cost_usd=_spent(llm),
            )
            _log_transition(job, started)
            return

        extraction_meta: dict[str, Any] = {
            **acquired.meta,  # tier_used, actions_log, ...
            "confidence": result.confidence,
            "model": settings.llm_model,
            "extracted_at": datetime.now(UTC).isoformat(),
        }
        produced = persist_drafts(
            db,
            owner_id=job.user_id,
            result=result,
            llm=llm,
            source=_source_of(job),
            source_type=_SOURCE_TYPE_BY_INPUT[input_type],
            source_fingerprint=job.source_fingerprint,
            extraction_meta=extraction_meta,
            image_ref=image_ref,
        )
        queue.complete_needs_review(
            db,
            job,
            extraction_meta=extraction_meta,
            produced_ids=[str(recipe_id) for recipe_id in produced],
            artifacts=dict(acquired.artifacts),
            cost_usd=_spent(llm),
        )
    except TierFailed as exc:
        artifacts: dict[str, Any] = (
            {"screenshot_ref": exc.screenshot_ref} if exc.screenshot_ref else {}
        )
        queue.fail(
            db, job, error=exc.reason, artifacts=artifacts, cost_usd=_spent(llm), retryable=True
        )
    except CostCapExceeded as exc:
        queue.fail(db, job, error=str(exc), cost_usd=_spent(llm), retryable=False)
    except DuplicateRecipeError as exc:
        queue.fail(
            db,
            job,
            error=f"duplicate of existing recipe {exc.existing_id}",
            cost_usd=_spent(llm),
            retryable=False,
        )
    _log_transition(job, started)


def _log_transition(job: Job, started: float) -> None:
    logger.info(
        "job=%s status=%s attempts=%d duration=%.2fs cost_usd=%s",
        job.id,
        job.status.value,
        job.attempts,
        time.monotonic() - started,
        job.cost_usd,
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def _claim_one(worker_id: str) -> uuid.UUID | None:
    """Housekeeping + claim in one short-lived session; commits release locks fast."""
    with db_module.SessionLocal() as db:
        released = queue.release_stale(db)
        db.commit()
        if released:
            logger.info("worker=%s released %d stale job(s)", worker_id, released)
        job = queue.claim_next(db, worker_id=worker_id)
        job_id = job.id if job is not None else None
        db.commit()  # release the FOR UPDATE row lock immediately
    return job_id


def _fail_in_fresh_session(job_id: uuid.UUID, exc: Exception) -> None:
    """Record an unexpected crash on a NEW session, isolated from the broken one."""
    try:
        with db_module.SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            queue.fail(db, job, error=f"{type(exc).__name__}: {exc}")
            db.commit()
            logger.info("job=%s status=%s after crash", job_id, job.status.value)
    except Exception:
        logger.exception("job=%s could not record crash failure", job_id)


def _process_one(job_id: uuid.UUID) -> None:
    """Process one claimed job in its own session; crash handling is isolated."""
    session = db_module.SessionLocal()
    try:
        job = session.get(Job, job_id)
        if job is None:
            logger.warning("job=%s vanished between claim and processing", job_id)
            return
        process_job(session, job)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("job=%s crashed during processing", job_id)
        session.close()
        _fail_in_fresh_session(job_id, exc)
    finally:
        session.close()


def run_worker(*, poll_interval: float = 2.0, once: bool = False) -> None:
    """Poll the queue and process jobs until SIGTERM/SIGINT.

    ``once=True`` polls a single time, processes at most one job, and returns
    (for tests and e2e smoke runs). On SIGTERM/SIGINT the current job is
    finished before exiting.
    """
    stop = threading.Event()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        logger.info("worker=%s received signal %d — finishing current job", worker_id, signum)
        stop.set()

    previous_handlers = {
        sig: signal.signal(sig, _request_stop) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    logger.info("worker=%s started (poll_interval=%.1fs, once=%s)", worker_id, poll_interval, once)
    try:
        while not stop.is_set():
            job_id = _claim_one(worker_id)
            if job_id is not None:
                _process_one(job_id)
            if once:
                return
            if job_id is None:
                stop.wait(poll_interval)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        logger.info("worker=%s stopped", worker_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m recipe_normalizer.worker",
        description="Recipe-normalizer extraction worker (Postgres job queue).",
    )
    parser.add_argument("--once", action="store_true", help="process at most one job, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_worker(once=args.once)


if __name__ == "__main__":
    main()
