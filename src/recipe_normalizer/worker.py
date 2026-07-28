"""Extraction worker: claims queued jobs and runs acquire → normalize → persist.

Run: ``python -m recipe_normalizer.worker [--once]``

Transaction model — the worker is NOT behind get_db; it owns sessions
explicitly, one phase per session:

1. housekeeping/claim session: ``release_stale`` + ``purge_expired_sessions``
   (one commit), ``claim_next`` (commit immediately so the FOR UPDATE row
   lock is released quickly);
2. processing session: re-fetch the job, ``process_job`` (flush-only),
   commit on success — and ROLL BACK when ``process_job`` returns False,
   i.e. the job was stolen (stale-released + re-claimed) mid-attempt;
3. failure session: if processing raised, the broken session is rolled back
   and closed, and ``queue.fail`` runs in a NEW session so the failure
   bookkeeping is isolated from whatever poisoned the first one.

The poll loop is crash-tolerant: a transient error in claim/processing is
logged and the loop continues; only SIGTERM/SIGINT (graceful stop: the current
job finishes, then the loop exits), KeyboardInterrupt and SystemExit end it.
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

# The worker is a standalone entrypoint (not behind the API's router imports), so
# it must register every model module itself or cross-table FKs (jobs.user_id →
# users.id, …) fail to resolve at mapper-configuration time.
import recipe_normalizer.catalog.models  # noqa: E402, F401
import recipe_normalizer.cookbook.models  # noqa: E402, F401
import recipe_normalizer.ingestion.models  # noqa: E402, F401
import recipe_normalizer.llm.models  # noqa: E402, F401
import recipe_normalizer.users.models  # noqa: E402, F401
from recipe_normalizer import db as db_module
from recipe_normalizer.config import settings  # noqa: E402
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
from recipe_normalizer.users.service import purge_expired_sessions

__all__ = ["JobProcessingError", "main", "make_llm_for_job", "process_job", "run_worker"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Max length of the worker fencing token — must fit jobs.locked_by (String(100)).
_WORKER_ID_MAX_LEN = 100

# Consecutive failed poll iterations before the worker gives up and exits for the
# orchestrator to restart (distinguishes a transient blip from a permanent fault).
_MAX_CONSECUTIVE_FAILURES = 10


class JobProcessingError(Exception):
    """Wraps a processing exception with the LLM cost spent this attempt.

    ``process_job`` seeds its LLM client with the job's prior spend (so the
    cost cap is per-job across attempts/retries, not per-attempt). When an
    attempt crashes, the processing session is rolled back before the crash
    is recorded — so the spend the attempt made this time would otherwise
    never reach ``job.cost_usd``. This wraps the original exception together
    with just this attempt's cost so ``_fail_in_fresh_session`` can account
    it via ``queue.fail(cost_usd=...)``.
    """

    def __init__(self, original: BaseException, cost_usd: Decimal) -> None:
        super().__init__(str(original))
        self.original = original
        self.cost_usd = cost_usd


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
    smoke tests) can exercise the full pipeline without an LLM API key.

    Satisfies the LLMClient surface used by the pipeline:
    structured / classify_bool / tool_loop / spent_usd / model_for.
    """

    @property
    def spent_usd(self) -> float:
        return 0.0

    def model_for(self, *, fast: bool = False) -> str:
        return "stub"

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
        if "lines" in output_model.model_fields:  # extract.tier1_enrich (EnrichedLines)
            # Blank parse, one entry per numbered input line — structure from the
            # JSON-LD stays authoritative; the stub adds no parsing.
            count = sum(1 for line in str(content).splitlines() if line.strip())
            return output_model(lines=[{} for _ in range(count)])
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
        already_spent_usd=float(job.cost_usd or 0),
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
    extractor again, and the retained ``acquired_meta`` (tier_used,
    actions_log, ...) is rehydrated so extraction_meta stays complete.
    """
    artifacts = job.artifacts or {}
    raw_text_ref = artifacts.get("raw_text_ref")
    if raw_text_ref:
        text = store.open(raw_text_ref).decode("utf-8")
        meta = cast(dict[str, Any], artifacts.get("acquired_meta") or {})
        return Acquired(text=text, artifacts=cast(dict[str, str], dict(artifacts)), meta=dict(meta))
    extractor = EXTRACTORS[job.input_type.value]
    return extractor.acquire(job.payload, llm=llm, store=store)


def _save_source_image(acquired: Acquired, store: FileStore) -> str | None:
    if acquired.source_image is None:
        return None
    data, media_type = acquired.source_image
    return store.save(data, suffix=_IMAGE_SUFFIX.get(media_type, "bin"))


def process_job(db: Session, job: Job, *, worker_id: str | None = None) -> bool:
    """Run the full extraction pipeline for one claimed job.

    Returns True when the job's terminal transition was applied and the caller
    should COMMIT; False when the compare-and-set guard rejected it (the job
    was stale-released and re-claimed by another worker while we were working)
    — the caller must then ROLL BACK, or the drafts this attempt persisted
    would be committed with no Job referencing them (invisible/unreviewable)
    and would burn the (owner_id, source_fingerprint) unique index for the new
    owner's attempt.

    Flush-only — the CALLER commits. Known pipeline failures (TierFailed,
    CostCapExceeded, DuplicateRecipeError, missing extractor) are recorded via
    queue.fail here; unexpected exceptions propagate so run_worker can fail
    the job from a fresh session.

    Draft persistence runs inside a SAVEPOINT: if persist_drafts raises after
    flushing some drafts (e.g. the cost cap trips during catalog matching),
    the partial drafts — including the fingerprinted first one — are rolled
    back, so the failure never burns the source_fingerprint with a half-built
    recipe. Acquire/normalize LlmUsage rows are flushed before the savepoint
    and survive.

    ``worker_id`` (when given) is passed as the compare-and-set guard for
    every queue transition, so a job stolen via stale-release is left alone.
    """
    started = time.monotonic()
    input_type = job.input_type.value
    logger.info(
        "job=%s input_type=%s attempt=%d status=running", job.id, input_type, job.attempts + 1
    )

    if not (job.artifacts or {}).get("raw_text_ref") and input_type not in EXTRACTORS:
        return _finish(
            job,
            started,
            queue.fail(
                db,
                job,
                error=f"no extractor for input type {input_type!r}",
                retryable=False,
                expected_locked_by=worker_id,
            ),
        )

    llm = make_llm_for_job(db, job)
    store = get_file_store()
    # llm is seeded with already_spent_usd=job.cost_usd (per-job cap across
    # attempts/retries), so llm.spent_usd INCLUDES this prior spend. Every
    # handled outcome below must record only THIS attempt's delta —
    # queue._add_cost is additive, so passing the full seeded total would
    # double-count prior spend. Captured once, before any spend this attempt;
    # nothing mutates job.cost_usd until the outcome clauses call into
    # queue.complete_needs_review / complete_not_a_recipe / fail.
    prior_spend = job.cost_usd or Decimal("0")

    def _attempt_cost() -> Decimal:
        return max(_spent(llm) - prior_spend, Decimal("0"))

    try:
        acquired = _acquire(job, llm=llm, store=store)
        # Retain the acquire meta for future retries (rehydrated by _acquire);
        # flushed BEFORE the persist savepoint so a failed persist keeps it.
        if acquired.meta and "acquired_meta" not in (job.artifacts or {}):
            job.artifacts = {**(job.artifacts or {}), "acquired_meta": dict(acquired.meta)}
            db.flush()
        image_ref = _save_source_image(acquired, store)
        # Deterministic tiers (URL tier 1) prebuild the NormalizeResult —
        # use it directly and skip the full normalize() LLM pass.
        if acquired.prebuilt is not None:
            result = acquired.prebuilt
        else:
            result = normalize(acquired, llm=llm)

        if not result.is_recipe:
            return _finish(
                job,
                started,
                queue.complete_not_a_recipe(
                    db,
                    job,
                    reason=result.reason or "not a recipe",
                    artifacts=dict(acquired.artifacts),
                    cost_usd=_attempt_cost(),
                    expected_locked_by=worker_id,
                ),
            )

        extraction_meta: dict[str, Any] = {
            **acquired.meta,  # tier_used, actions_log, ...
            "confidence": result.confidence,
            # settings.llm_model is "" when provider defaults are in play —
            # ask the client which model it actually used.
            "model": llm.model_for(),
            "extracted_at": datetime.now(UTC).isoformat(),
        }
        with db.begin_nested():  # savepoint: all drafts or none
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
        applied = queue.complete_needs_review(
            db,
            job,
            extraction_meta=extraction_meta,
            produced_ids=[str(recipe_id) for recipe_id in produced],
            artifacts=dict(acquired.artifacts),
            cost_usd=_attempt_cost(),
            expected_locked_by=worker_id,
        )
    except TierFailed as exc:
        # Retain whatever the tier left behind (tier-3 screenshot + action log,
        # the raw page) and keep the legacy screenshot_ref key the UI links to.
        artifacts: dict[str, Any] = dict(exc.artifacts or {})
        if exc.screenshot_ref:
            artifacts.setdefault("screenshot_ref", exc.screenshot_ref)
        applied = queue.fail(
            db,
            job,
            error=exc.reason,
            artifacts=artifacts,
            cost_usd=_attempt_cost(),
            retryable=True,
            expected_locked_by=worker_id,
        )
    except CostCapExceeded as exc:
        applied = queue.fail(
            db,
            job,
            error=str(exc),
            cost_usd=_attempt_cost(),
            retryable=False,
            expected_locked_by=worker_id,
        )
    except DuplicateRecipeError as exc:
        applied = queue.fail(
            db,
            job,
            error=f"duplicate of existing recipe {exc.existing_id}",
            cost_usd=_attempt_cost(),
            retryable=False,
            expected_locked_by=worker_id,
        )
    except Exception as exc:
        # Unexpected crash — propagate to _process_one, but carry THIS attempt's
        # spend (spent_usd includes already_spent_usd, seeded from job.cost_usd)
        # so it isn't lost when the processing session rolls back. Same formula
        # as every handled outcome above — _attempt_cost() closes over prior_spend.
        raise JobProcessingError(exc, _attempt_cost()) from exc
    return _finish(job, started, applied)


def _finish(job: Job, started: float, applied: bool) -> bool:
    """Log the outcome and hand the commit/rollback decision back to the caller."""
    if applied:
        _log_transition(job, started)
    else:
        # queue._cas_guard already logged the mismatch in detail.
        logger.warning("job=%s claim lost — this attempt's work must be rolled back", job.id)
    return applied


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
        purged = purge_expired_sessions(db)
        db.commit()
        if released:
            logger.info("worker=%s released %d stale job(s)", worker_id, released)
        if purged:
            logger.info("worker=%s purged %d expired session(s)", worker_id, purged)
        job = queue.claim_next(db, worker_id=worker_id)
        job_id = job.id if job is not None else None
        db.commit()  # release the FOR UPDATE row lock immediately
    return job_id


def _fail_in_fresh_session(
    job_id: uuid.UUID,
    exc: BaseException,
    worker_id: str,
    *,
    cost_usd: Decimal | None = None,
) -> None:
    """Record an unexpected crash on a NEW session, isolated from the broken one."""
    try:
        with db_module.SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            if not queue.fail(
                db,
                job,
                error=f"{type(exc).__name__}: {exc}",
                cost_usd=cost_usd,
                expected_locked_by=worker_id,
            ):
                db.rollback()  # another worker owns the job now — leave it alone
                return
            db.commit()
            logger.info("job=%s status=%s after crash", job_id, job.status.value)
    except Exception:
        logger.exception("job=%s could not record crash failure", job_id)


def _process_one(job_id: uuid.UUID, worker_id: str) -> None:
    """Process one claimed job in its own session; crash handling is isolated."""
    session = db_module.SessionLocal()
    try:
        job = session.get(Job, job_id)
        if job is None:
            logger.warning("job=%s vanished between claim and processing", job_id)
            return
        if process_job(session, job, worker_id=worker_id):
            session.commit()
        else:
            # CAS guard rejected the transition: the job belongs to another
            # worker now, so everything this attempt staged (drafts included)
            # must go — committing would leave orphan, unreviewable recipes.
            session.rollback()
    except Exception as exc:
        session.rollback()
        logger.exception("job=%s crashed during processing", job_id)
        session.close()
        if isinstance(exc, JobProcessingError):
            _fail_in_fresh_session(job_id, exc.original, worker_id, cost_usd=exc.cost_usd)
        else:
            _fail_in_fresh_session(job_id, exc, worker_id)
    finally:
        session.close()


def _make_worker_id() -> str:
    """Fencing token identifying this worker PROCESS.

    ``host:pid`` alone is not unique in a container: the hostname is fixed and
    the worker is PID 1, so a restarted worker would reuse the crashed one's
    token and ``queue._cas_guard`` could not tell them apart. The uuid4 suffix
    makes every process distinct; the hostname is truncated so the id always
    fits ``jobs.locked_by`` (String(100)).
    """
    suffix = f":{os.getpid()}:{uuid.uuid4().hex[:8]}"
    hostname = socket.gethostname()[: _WORKER_ID_MAX_LEN - len(suffix)]
    return f"{hostname}{suffix}"


def run_worker(*, poll_interval: float = 2.0, once: bool = False) -> None:
    """Poll the queue and process jobs until SIGTERM/SIGINT.

    ``once=True`` polls a single time, processes at most one job, and returns
    (for tests and e2e smoke runs). On SIGTERM/SIGINT the current job is
    finished before exiting.

    Each iteration is exception-guarded: a transient failure (dropped DB
    connection, failover) is logged and polling continues, so one bad poll
    never takes the worker process down.
    """
    stop = threading.Event()
    worker_id = _make_worker_id()

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        logger.info("worker=%s received signal %d — finishing current job", worker_id, signum)
        stop.set()

    previous_handlers = {
        sig: signal.signal(sig, _request_stop) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    logger.info("worker=%s started (poll_interval=%.1fs, once=%s)", worker_id, poll_interval, once)
    consecutive_failures = 0
    try:
        while not stop.is_set():
            try:
                job_id = _claim_one(worker_id)
                if job_id is not None:
                    _process_one(job_id, worker_id)
                consecutive_failures = 0
            except Exception:
                # A transient DB hiccup (dropped connection, failover) must not
                # kill the worker — log it and poll again. KeyboardInterrupt/
                # SystemExit still propagate. But a PERSISTENT failure (DB gone,
                # misconfigured provider) should not spin silently forever: after
                # _MAX_CONSECUTIVE_FAILURES in a row, re-raise so the orchestrator
                # restarts the process with its own backoff.
                consecutive_failures += 1
                logger.exception(
                    "worker=%s poll iteration failed (%d in a row)",
                    worker_id,
                    consecutive_failures,
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "worker=%s exiting after %d consecutive failures",
                        worker_id,
                        consecutive_failures,
                    )
                    raise
                job_id = None
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
