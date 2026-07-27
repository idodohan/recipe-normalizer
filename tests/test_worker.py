"""Tests for the extraction worker: process_job pipeline + run_worker loop.

process_job tests run on the savepoint-isolated ``db_session``; the
run_worker end-to-end tests need REAL commits, so they monkeypatch
``db_module.SessionLocal`` onto the test container engine and clean up
committed rows afterwards (pattern from tests/test_get_db_integration.py).
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import recipe_normalizer.db as db_module
from recipe_normalizer import worker
from recipe_normalizer.catalog.models import CanonicalIngredient
from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.config import settings
from recipe_normalizer.cookbook.models import Recipe
from recipe_normalizer.cookbook.service import SourceType
from recipe_normalizer.extraction import EXTRACTORS, Acquired, TierFailed
from recipe_normalizer.extraction.normalize import (
    NormalizedGroup,
    NormalizedLine,
    NormalizedRecipe,
    NormalizedStep,
    NormalizeResult,
)
from recipe_normalizer.extraction.persist import persist_drafts
from recipe_normalizer.filestore import LocalFileStore
from recipe_normalizer.ingestion.models import InputType, Job, JobStatus
from recipe_normalizer.llm.client import CostCapExceeded, LLMClient
from recipe_normalizer.users.models import User
from tests.extraction.conftest import StubLLM

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class FakeLLM(StubLLM):
    """StubLLM (extract.normalize + catalog.match) plus spent_usd / model_for."""

    def __init__(self, result: Any = None, spent: float = 0.0) -> None:
        super().__init__(result)
        self.spent_usd = spent

    def model_for(self, *, fast: bool = False) -> str:
        return "fake-fast-model" if fast else "fake-model"


class CapBlownLLM:
    """LLM whose first call raises CostCapExceeded (cap already spent)."""

    spent_usd = 1.5

    def structured(self, **kwargs: Any) -> Any:
        raise CostCapExceeded("spent $1.500000 >= cost cap $1.500000")


def _recipe(
    title: str = "Flour Bread",
    name: str = "all-purpose flour",
    original_text: str = "2 cups all-purpose flour",
) -> NormalizedRecipe:
    return NormalizedRecipe(
        title=title,
        groups=[
            NormalizedGroup(
                lines=[
                    NormalizedLine(
                        original_text=original_text,
                        name=name,
                        quantity=2.0,
                        unit="cup",
                    )
                ]
            )
        ],
        steps=[NormalizedStep(original_text="Mix and bake.")],
    )


def _flour_result(confidence: float = 0.9) -> NormalizeResult:
    return NormalizeResult(is_recipe=True, confidence=confidence, recipes=[_recipe()])


@pytest.fixture()
def store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> LocalFileStore:
    """Isolated filestore; worker.get_file_store is patched to return it."""
    s = LocalFileStore(tmp_path)
    monkeypatch.setattr(worker, "get_file_store", lambda: s)
    return s


@pytest.fixture()
def owner(db_session: Session) -> User:
    load_seed(db_session)
    user = User(
        email=f"worker-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="hash",
        display_name="Worker Tester",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _text_job(db: Session, owner: User, text: str = "2 cups flour. Mix and bake.") -> Job:
    job = Job(
        user_id=owner.id,
        input_type=InputType.text,
        payload={"text": text},
        source_fingerprint=f"fp-{uuid.uuid4().hex[:12]}",
        status=JobStatus.running,
    )
    db.add(job)
    db.flush()
    return job


def _patch_llm(monkeypatch: pytest.MonkeyPatch, llm: Any) -> None:
    monkeypatch.setattr(worker, "make_llm_for_job", lambda db, job: llm)


# ---------------------------------------------------------------------------
# process_job
# ---------------------------------------------------------------------------


def test_process_job_happy_path(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _text_job(db_session, owner)
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result(), spent=0.0123))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    assert len(job.produced_recipe_ids) == 1
    recipe = db_session.get(Recipe, uuid.UUID(job.produced_recipe_ids[0]))
    assert recipe is not None
    assert recipe.is_verified is False
    assert recipe.source_type == SourceType.text
    # extraction_meta carries confidence/model/extracted_at
    assert job.extraction_meta is not None
    assert job.extraction_meta["confidence"] == 0.9
    assert job.extraction_meta["model"] == "fake-model"
    assert job.extraction_meta["extracted_at"]
    # cost from llm.spent_usd, lock cleared, raw text retained
    assert job.cost_usd == Decimal("0.0123")
    assert job.locked_at is None and job.locked_by is None
    assert store.exists(job.artifacts["raw_text_ref"])


def test_extraction_meta_records_the_model_the_client_resolved(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``settings.llm_model`` defaults to "" (provider-default models are
    resolved inside LLMClient), so extraction provenance must come from
    ``llm.model_for()`` — recording the setting would stamp every job model=""."""
    monkeypatch.setattr(settings, "llm_model", "")
    job = _text_job(db_session, owner)
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result()))

    worker.process_job(db_session, job)

    assert job.extraction_meta is not None
    assert job.extraction_meta["model"] == "fake-model"


def test_process_job_not_a_recipe(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _text_job(db_session, owner, text="Welcome to our restaurant menu")
    _patch_llm(
        monkeypatch,
        FakeLLM(result=NormalizeResult(is_recipe=False, reason="This is a menu"), spent=0.002),
    )

    worker.process_job(db_session, job)

    assert job.status == JobStatus.not_a_recipe
    assert job.reason == "This is a menu"
    assert job.produced_recipe_ids == []
    assert job.cost_usd == Decimal("0.002")


def test_process_job_cost_cap_exceeded_fails_non_retryable(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _text_job(db_session, owner)
    _patch_llm(monkeypatch, CapBlownLLM())

    worker.process_job(db_session, job)

    assert job.status == JobStatus.failed  # first attempt, yet terminal
    assert job.attempts == 1
    assert job.next_attempt_at is None
    assert job.error is not None and "cost cap" in job.error
    assert job.cost_usd == Decimal("1.5")


def test_process_job_tier_failed_requeues_with_screenshot(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingExtractor:
        input_type = "text"

        def acquire(self, payload: dict[str, Any], **kwargs: Any) -> Acquired:
            raise TierFailed("fetch blocked by robots", screenshot_ref="ss/shot.png")

    monkeypatch.setitem(EXTRACTORS, "text", FailingExtractor())  # type: ignore[misc]
    job = _text_job(db_session, owner)
    _patch_llm(monkeypatch, FakeLLM(spent=0.05))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.queued  # retryable
    assert job.attempts == 1
    assert job.next_attempt_at is not None
    assert job.artifacts["screenshot_ref"] == "ss/shot.png"
    assert job.cost_usd == Decimal("0.05")


def test_process_job_duplicate_recipe_fails_non_retryable(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_drafts(
        db_session,
        owner_id=owner.id,
        result=_flour_result(),
        llm=FakeLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint="fp-already-there",
        extraction_meta=None,
        image_ref=None,
    )
    job = _text_job(db_session, owner)
    job.source_fingerprint = "fp-already-there"
    db_session.flush()
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result()))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.failed
    assert job.attempts == 1
    assert job.error is not None and "duplicate of existing recipe" in job.error


def test_process_job_missing_extractor_fails_non_retryable(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every input type now has a plugin; simulate a missing one by unregistering.
    monkeypatch.delitem(EXTRACTORS, "pdf")  # type: ignore[arg-type]
    job = Job(
        user_id=owner.id,
        input_type=InputType.pdf,
        payload={"filename": "recipe.pdf"},
        status=JobStatus.running,
    )
    db_session.add(job)
    db_session.flush()
    _patch_llm(monkeypatch, FakeLLM())

    worker.process_job(db_session, job)

    assert job.status == JobStatus.failed
    assert job.error is not None and "no extractor for input type" in job.error


def test_process_job_retry_skips_acquire_with_retained_raw_text(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §6.4: a retry must never re-scrape — retained raw text is reused."""
    ref = store.save(b"2 cups flour. Mix and bake.", suffix="txt")
    acquire_calls: list[int] = []

    class CountingExtractor:
        input_type = "text"

        def acquire(self, payload: dict[str, Any], **kwargs: Any) -> Acquired:
            acquire_calls.append(1)
            return Acquired(text="MUST NOT BE USED")

    monkeypatch.setitem(EXTRACTORS, "text", CountingExtractor())  # type: ignore[misc]
    job = _text_job(db_session, owner)
    job.artifacts = {"raw_text_ref": ref}
    db_session.flush()
    fake = FakeLLM(result=_flour_result())
    _patch_llm(monkeypatch, fake)

    worker.process_job(db_session, job)

    assert acquire_calls == []  # acquire was skipped
    assert job.status == JobStatus.needs_review
    assert job.artifacts["raw_text_ref"] == ref
    # The retained text (not the extractor's) reached normalize.
    normalize_call = next(c for c in fake.calls if c["feature"] == "extract.normalize")
    assert normalize_call["content"][0]["text"] == "2 cups flour. Mix and bake."


def test_process_job_retained_retry_rehydrates_acquired_meta(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tier_used/actions_log retained from the first attempt survive a retry."""
    ref = store.save(b"2 cups flour. Mix and bake.", suffix="txt")
    job = _text_job(db_session, owner)
    job.artifacts = {
        "raw_text_ref": ref,
        "acquired_meta": {"tier_used": 3, "actions_log": ["scrolled", "clicked"]},
    }
    db_session.flush()
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result()))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    assert job.extraction_meta is not None
    assert job.extraction_meta["tier_used"] == 3
    assert job.extraction_meta["actions_log"] == ["scrolled", "clicked"]


def test_process_job_persists_acquired_meta_into_artifacts(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh acquire's meta is retained in job.artifacts for later retries."""
    ref = store.save(b"2 cups flour. Mix and bake.", suffix="txt")

    class MetaExtractor:
        input_type = "text"

        def acquire(self, payload: dict[str, Any], **kwargs: Any) -> Acquired:
            return Acquired(
                text="2 cups flour. Mix and bake.",
                artifacts={"raw_text_ref": ref},
                meta={"tier_used": 2, "actions_log": ["fetched"]},
            )

    monkeypatch.setitem(EXTRACTORS, "text", MetaExtractor())  # type: ignore[misc]
    job = _text_job(db_session, owner)
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result()))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    assert job.artifacts["acquired_meta"] == {"tier_used": 2, "actions_log": ["fetched"]}
    assert job.extraction_meta is not None
    assert job.extraction_meta["tier_used"] == 2


def test_process_job_saves_source_image(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"

    class ImageExtractor:
        input_type = "text"

        def acquire(self, payload: dict[str, Any], **kwargs: Any) -> Acquired:
            return Acquired(
                text="2 cups flour. Mix and bake.",
                source_image=(png_bytes, "image/png"),
            )

    monkeypatch.setitem(EXTRACTORS, "text", ImageExtractor())  # type: ignore[misc]
    job = _text_job(db_session, owner)
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result()))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    recipe = db_session.get(Recipe, uuid.UUID(job.produced_recipe_ids[0]))
    assert recipe is not None
    assert recipe.image_ref is not None
    assert recipe.image_ref.endswith(".png")
    assert store.open(recipe.image_ref) == png_bytes


def test_process_job_prebuilt_result_skips_normalize(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier-1 URL extraction returns a prebuilt NormalizeResult — the worker
    must use it directly and never run the full normalize() LLM pass."""

    class PrebuiltExtractor:
        input_type = "text"

        def acquire(self, payload: dict[str, Any], **kwargs: Any) -> Acquired:
            return Acquired(prebuilt=_flour_result(confidence=0.95), meta={"tier_used": 1})

    monkeypatch.setitem(EXTRACTORS, "text", PrebuiltExtractor())  # type: ignore[misc]
    job = _text_job(db_session, owner)
    # FakeLLM with NO canned result: any extract.normalize call would fail loudly.
    fake = FakeLLM(result=None)
    _patch_llm(monkeypatch, fake)

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    assert "extract.normalize" not in [c["feature"] for c in fake.calls]
    assert job.extraction_meta is not None
    assert job.extraction_meta["confidence"] == 0.95  # from the prebuilt result
    assert job.extraction_meta["tier_used"] == 1
    recipe = db_session.get(Recipe, uuid.UUID(job.produced_recipe_ids[0]))
    assert recipe is not None
    assert recipe.title == "Flour Bread"


# ---------------------------------------------------------------------------
# process_job — handled outcomes record the attempt DELTA, not the seeded total
# ---------------------------------------------------------------------------


def test_second_attempt_records_delta_not_total(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried job's llm client is seeded with already_spent_usd=job.cost_usd,
    so llm.spent_usd INCLUDES prior spend. The handled needs_review outcome
    must record only this attempt's delta, not the full seeded total."""
    job = _text_job(db_session, owner)
    job.cost_usd = Decimal("0.42")  # recorded by a prior (failed) attempt
    db_session.flush()
    # Simulates make_llm_for_job seeding already_spent_usd=0.42, then this
    # attempt spending 0.10 more: llm.spent_usd (seeded + this attempt) == 0.52.
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result(), spent=0.52))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    assert job.cost_usd == Decimal("0.52")  # 0.42 + delta(0.10), not 0.42 + 0.52 = 0.94


def test_retry_then_success_records_true_total(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same accounting bug, larger prior spend: a job that already carries
    cost_usd from an earlier retry must end up with prior + this attempt's
    delta, never prior + the full seeded total."""
    job = _text_job(db_session, owner)
    job.cost_usd = Decimal("1.50")  # preserved across a prior retry
    db_session.flush()
    _patch_llm(monkeypatch, FakeLLM(result=_flour_result(), spent=1.80))

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    assert job.cost_usd == Decimal("1.80")  # 1.50 + delta(0.30), not 1.50 + 1.80 = 3.30


# ---------------------------------------------------------------------------
# make_llm_for_job / RN_LLM_STUB
# ---------------------------------------------------------------------------


def test_make_llm_for_job_returns_real_client(
    db_session: Session, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RN_LLM_STUB", raising=False)
    job = _text_job(db_session, owner)
    llm = worker.make_llm_for_job(db_session, job)
    assert isinstance(llm, LLMClient)


def test_make_llm_seeds_already_spent(
    db_session: Session, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap is per-job across attempts/retries: a client seeded with prior spend
    must already be over a cap lower than that prior spend."""
    monkeypatch.delenv("RN_LLM_STUB", raising=False)
    monkeypatch.setattr(settings, "job_cost_cap_usd", 1.50)
    job = _text_job(db_session, owner)
    job.cost_usd = Decimal("2.00")
    db_session.flush()

    llm = worker.make_llm_for_job(db_session, job)

    assert llm.spent_usd == 2.0
    with pytest.raises(CostCapExceeded):
        llm._check_cost_cap()


def test_make_llm_for_job_stub_env(
    db_session: Session, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RN_LLM_STUB", "1")
    job = _text_job(db_session, owner)
    llm = worker.make_llm_for_job(db_session, job)

    assert isinstance(llm, worker._StubLLMClient)
    result = llm.structured(
        feature="extract.normalize", output_model=NormalizeResult, content="anything at all"
    )
    assert result.is_recipe is True
    assert result.recipes[0].title == "Stub Recipe"
    texts = [line.original_text for line in result.recipes[0].groups[0].lines]
    assert "1 cup all-purpose flour" in texts
    assert llm.classify_bool(feature="x", question="recipe?", content="y") is True
    assert llm.spent_usd == 0.0
    assert llm.model_for() == "stub"  # provenance surface the pipeline records


def test_process_job_with_stub_llm_end_to_end(
    db_session: Session, owner: User, store: LocalFileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RN_LLM_STUB", "1")
    job = _text_job(db_session, owner)

    worker.process_job(db_session, job)

    assert job.status == JobStatus.needs_review
    recipe = db_session.get(Recipe, uuid.UUID(job.produced_recipe_ids[0]))
    assert recipe is not None
    assert recipe.title == "Stub Recipe"


# ---------------------------------------------------------------------------
# run_worker (real commits via monkeypatched SessionLocal)
# ---------------------------------------------------------------------------


@pytest.fixture()
def committed_env(engine: Engine, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """SessionLocal → test engine, stub LLM, tmp filestore; cleans up commits."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setenv("RN_LLM_STUB", "1")
    s = LocalFileStore(tmp_path)
    monkeypatch.setattr(worker, "get_file_store", lambda: s)

    with factory() as session:
        preexisting_ingredients = set(session.scalars(select(CanonicalIngredient.id)).all())
        user = User(
            email=f"worker-e2e-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="hash",
            display_name="Worker E2E",
        )
        session.add(user)
        session.commit()
        user_id = user.id

    yield factory, user_id

    with factory() as session:
        # User delete cascades recipes + jobs; then drop ingredients we created.
        session.execute(delete(User).where(User.id == user_id))
        session.flush()
        new_ingredients = (
            set(session.scalars(select(CanonicalIngredient.id)).all()) - preexisting_ingredients
        )
        if new_ingredients:
            session.execute(
                delete(CanonicalIngredient).where(CanonicalIngredient.id.in_(new_ingredients))
            )
        session.commit()


def test_run_worker_once_end_to_end(committed_env: Any) -> None:
    factory, user_id = committed_env
    with factory() as s:
        job = Job(
            user_id=user_id,
            input_type=InputType.text,
            payload={"text": "stub recipe text"},
            source_fingerprint=f"fp-{uuid.uuid4().hex[:12]}",
        )
        s.add(job)
        s.commit()
        job_id = job.id

    worker.run_worker(once=True)

    # Everything must be COMMITTED — visible from a brand-new session.
    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.needs_review
        assert fresh.locked_at is None and fresh.locked_by is None
        assert fresh.extraction_meta is not None
        assert fresh.extraction_meta["confidence"] > 0
        recipe = s.get(Recipe, uuid.UUID(fresh.produced_recipe_ids[0]))
        assert recipe is not None
        assert recipe.title == "Stub Recipe"
        assert recipe.is_verified is False


def test_run_worker_once_with_empty_queue_returns(committed_env: Any) -> None:
    worker.run_worker(once=True)  # no queued jobs — must return, not spin


class CapOnCatalogMatchLLM:
    """Answers extract.normalize fine, then blows the cost cap on catalog.match.

    Reproduces the orphan-draft bug: persist_drafts creates draft #1 (carrying
    the source_fingerprint), then the cap trips while matching draft #2's
    ingredients — without a savepoint the half-built draft would be committed
    alongside the failed job, permanently 409-ing future resubmits.
    """

    spent_usd = 1.5

    def __init__(self) -> None:
        self.features: list[str] = []

    def model_for(self, *, fast: bool = False) -> str:
        return "fake-model"

    def structured(self, *, feature: str, **kwargs: Any) -> Any:
        self.features.append(feature)
        if feature == "extract.normalize":
            return NormalizeResult(
                is_recipe=True,
                confidence=0.9,
                recipes=[
                    # Draft 1: creates ingredient "all-purpose flour" (no LLM call).
                    _recipe("First Draft", "all-purpose flour", "2 cups all-purpose flour"),
                    # Draft 2: "bread flour" fuzzes into the 70-90 band vs
                    # "all-purpose flour" → catalog.match LLM call → cap trips.
                    _recipe("Second Draft", "bread flour", "2 cups bread flour"),
                ],
            )
        raise CostCapExceeded("spent $1.500000 >= cost cap $1.500000")


def test_cost_cap_mid_persist_commits_no_orphan_drafts(
    committed_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL regression: a failure inside persist_drafts must not commit
    partial drafts — especially not the fingerprinted first draft."""
    factory, user_id = committed_env
    fingerprint = f"fp-{uuid.uuid4().hex[:12]}"
    with factory() as s:
        job = Job(
            user_id=user_id,
            input_type=InputType.text,
            payload={"text": "two recipes"},
            source_fingerprint=fingerprint,
            status=JobStatus.running,
        )
        s.add(job)
        s.commit()
        job_id = job.id

    llm = CapOnCatalogMatchLLM()
    _patch_llm(monkeypatch, llm)

    session = factory()
    try:
        job_attached = session.get(Job, job_id)
        assert job_attached is not None
        worker.process_job(session, job_attached)
        session.commit()
    finally:
        session.close()

    # The cap must have tripped INSIDE persist (during catalog matching).
    assert "catalog.match" in llm.features

    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.failed
        assert fresh.error is not None and "cost cap" in fresh.error
        # No orphan drafts survive the commit...
        assert s.scalars(select(Recipe).where(Recipe.owner_id == user_id)).all() == []
        # ...and the fingerprint is NOT burned for future resubmits.
        assert (
            s.scalars(select(Recipe).where(Recipe.source_fingerprint == fingerprint)).first()
            is None
        )


def test_stolen_claim_rolls_back_instead_of_committing_orphan_drafts(
    committed_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL regression: when the CAS guard rejects the completion (the job
    was stale-released and re-claimed by another worker), the drafts this
    attempt persisted must be ROLLED BACK. Committing them would leave recipes
    referenced by no Job (invisible/unreviewable) and burn the
    (owner_id, source_fingerprint) unique index for the new owner's retry."""
    factory, user_id = committed_env
    fingerprint = f"fp-{uuid.uuid4().hex[:12]}"
    with factory() as s:
        job = Job(
            user_id=user_id,
            input_type=InputType.text,
            payload={"text": "stub recipe text"},
            source_fingerprint=fingerprint,
            status=JobStatus.running,
            locked_at=datetime.now(UTC),
            locked_by="w2",  # stolen: another worker re-claimed after stale release
        )
        s.add(job)
        s.commit()
        job_id = job.id

    worker._process_one(job_id, "w1")  # we still think we own it

    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.running  # untouched — w2 owns it now
        assert fresh.locked_by == "w2"
        assert fresh.produced_recipe_ids == []
        # No orphan drafts committed...
        assert s.scalars(select(Recipe).where(Recipe.owner_id == user_id)).all() == []
        # ...and the fingerprint is not burned for w2's attempt.
        assert (
            s.scalars(select(Recipe).where(Recipe.source_fingerprint == fingerprint)).first()
            is None
        )


def test_process_job_reports_lost_claim_to_the_caller(
    committed_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """process_job returns False when the claim was lost (caller must roll back)
    and True on a normal completion (caller commits)."""
    factory, user_id = committed_env
    with factory() as s:
        stolen = Job(
            user_id=user_id,
            input_type=InputType.text,
            payload={"text": "stub recipe text"},
            source_fingerprint=f"fp-{uuid.uuid4().hex[:12]}",
            status=JobStatus.running,
            locked_at=datetime.now(UTC),
            locked_by="w2",
        )
        s.add(stolen)
        s.commit()
        stolen_id = stolen.id

    with factory() as s:
        job = s.get(Job, stolen_id)
        assert job is not None
        assert worker.process_job(s, job, worker_id="w1") is False
        s.rollback()

    with factory() as s:
        mine = Job(
            user_id=user_id,
            input_type=InputType.text,
            payload={"text": "stub recipe text"},
            source_fingerprint=f"fp-{uuid.uuid4().hex[:12]}",
            status=JobStatus.running,
            locked_at=datetime.now(UTC),
            locked_by="w1",
        )
        s.add(mine)
        s.commit()
        assert worker.process_job(s, mine, worker_id="w1") is True
        s.commit()


def test_run_worker_crash_fails_job_in_fresh_session(
    committed_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected crash mid-processing rolls back, then fail() runs isolated."""
    factory, user_id = committed_env
    with factory() as s:
        job = Job(
            user_id=user_id,
            input_type=InputType.text,
            payload={"text": "will crash"},
            source_fingerprint=f"fp-{uuid.uuid4().hex[:12]}",
        )
        s.add(job)
        s.commit()
        job_id = job.id

    def explode(db: Session, job: Job, **kwargs: Any) -> None:
        raise RuntimeError("kaboom mid-pipeline")

    monkeypatch.setattr(worker, "process_job", explode)

    worker.run_worker(once=True)

    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.queued  # retryable failure → backoff requeue
        assert fresh.attempts == 1
        assert fresh.next_attempt_at is not None
        assert fresh.next_attempt_at > datetime.now(UTC)
        assert fresh.locked_at is None and fresh.locked_by is None


class CrashAfterSpendLLM:
    """Fake LLM: records spend on the extract.normalize call, then crashes."""

    def __init__(self, spend: float) -> None:
        self.spent_usd = 0.0
        self._spend = spend

    def structured(self, **kwargs: Any) -> Any:
        self.spent_usd += self._spend
        raise RuntimeError("kaboom after spend")


def test_crash_attempt_cost_reaches_job_row(
    committed_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker crash mid-attempt must not lose the LLM spend that attempt made:
    process_job wraps the propagating exception with the attempt's cost, and
    _fail_in_fresh_session records it on the requeued job."""
    factory, user_id = committed_env
    with factory() as s:
        job = Job(
            user_id=user_id,
            input_type=InputType.text,
            payload={"text": "will crash after spending"},
            source_fingerprint=f"fp-{uuid.uuid4().hex[:12]}",
        )
        s.add(job)
        s.commit()
        job_id = job.id

    _patch_llm(monkeypatch, CrashAfterSpendLLM(spend=0.42))

    worker.run_worker(once=True)

    with factory() as s:
        fresh = s.get(Job, job_id)
        assert fresh is not None
        assert fresh.status == JobStatus.queued  # retryable failure → backoff requeue
        assert fresh.attempts == 1
        assert fresh.cost_usd == Decimal("0.42")


# ---------------------------------------------------------------------------
# Poll-loop resilience / worker identity
# ---------------------------------------------------------------------------


def test_poll_loop_survives_transient_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """One transient OperationalError must NOT kill the worker process: the
    iteration is logged and the loop polls again."""
    calls: list[str] = []

    def flaky_claim(worker_id: str) -> uuid.UUID | None:
        calls.append(worker_id)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        raise SystemExit(0)  # stop the loop from the second iteration

    monkeypatch.setattr(worker, "_claim_one", flaky_claim)

    with pytest.raises(SystemExit):  # SystemExit still propagates
        worker.run_worker(poll_interval=0.01)

    assert len(calls) == 2  # survived the first failure and polled again


def test_poll_loop_survives_processing_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash escaping _process_one is contained by the loop, not fatal."""
    calls: list[str] = []

    def claim(worker_id: str) -> uuid.UUID | None:
        calls.append(worker_id)
        if len(calls) > 2:
            raise SystemExit(0)
        return uuid.uuid4()

    def boom(job_id: uuid.UUID, worker_id: str) -> None:
        raise RuntimeError("session factory exploded")

    monkeypatch.setattr(worker, "_claim_one", claim)
    monkeypatch.setattr(worker, "_process_one", boom)

    with pytest.raises(SystemExit):
        worker.run_worker(poll_interval=0.01)

    assert len(calls) == 3


def test_poll_loop_propagates_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(worker_id: str) -> uuid.UUID | None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "_claim_one", interrupt)

    with pytest.raises(KeyboardInterrupt):
        worker.run_worker(poll_interval=0.01)


def test_poll_loop_gives_up_after_persistent_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """A permanent fault (never recovers) must exit for orchestrator restart
    rather than spin forever — after _MAX_CONSECUTIVE_FAILURES in a row."""
    calls: list[str] = []

    def always_broken(worker_id: str) -> uuid.UUID | None:
        calls.append(worker_id)
        raise OperationalError("SELECT 1", {}, Exception("db is gone"))

    monkeypatch.setattr(worker, "_claim_one", always_broken)

    with pytest.raises(OperationalError):
        worker.run_worker(poll_interval=0.001)

    assert len(calls) == worker._MAX_CONSECUTIVE_FAILURES


def test_poll_loop_failure_counter_resets_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Intermittent failures interleaved with successes never trip the breaker."""
    outcomes: list[bool] = []

    def flaky(worker_id: str) -> uuid.UUID | None:
        outcomes.append(True)
        n = len(outcomes)
        if n > worker._MAX_CONSECUTIVE_FAILURES * 2:
            raise SystemExit(0)
        if n % 2 == 1:  # fail on odd, succeed on even — never N in a row
            raise OperationalError("SELECT 1", {}, Exception("blip"))
        return None  # a clean empty poll resets the counter

    monkeypatch.setattr(worker, "_claim_one", flaky)

    with pytest.raises(SystemExit):  # reached the SystemExit, breaker never tripped
        worker.run_worker(poll_interval=0.001)

    assert len(outcomes) == worker._MAX_CONSECUTIVE_FAILURES * 2 + 1


def test_worker_id_is_unique_per_process_and_fits_locked_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In containers hostname is fixed and the worker is PID 1, so
    ``host:pid`` alone would let a restarted worker reuse a crashed worker's
    fencing token — the CAS guard could not tell them apart."""
    first = worker._make_worker_id()
    second = worker._make_worker_id()

    assert first != second
    assert first.split(":")[:2] == [socket.gethostname(), str(os.getpid())]
    locked_by_len = Job.__table__.c.locked_by.type.length
    assert len(first) <= locked_by_len


def test_worker_id_truncates_a_long_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.socket, "gethostname", lambda: "h" * 300)
    worker_id = worker._make_worker_id()
    assert len(worker_id) == Job.__table__.c.locked_by.type.length
    assert worker_id.endswith(f":{os.getpid()}:{worker_id.rsplit(':', 1)[1]}")
