"""Golden corpus regression suite (spec §9).

Committed cases under golden/cases/<name>/ run offline end-to-end through the
REAL worker pipeline (acquire → tier select → normalize/prebuilt → persist into
seeded Postgres). The LLM seam is stubbed per case; everything around it is the
production code path, so these tests pin the DETERMINISTIC envelope: tier
selection, JSON-LD mapping + duration parsing (no stub — true expected JSON),
verbatim text (incl. Hebrew), multi-recipe fan-out + fingerprint placement,
not-a-recipe handling, artifact retention, and extraction_meta.

Each case dir holds:
- input.<html|txt|pdf>  — the source (extension picks the input type)
- expected.json         — the asserted envelope
- llm_stub.json         — optional canned normalize/enrich/judge for LLM stages
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer import worker
from recipe_normalizer.cookbook.models import Recipe
from recipe_normalizer.extraction import EXTRACTORS
from recipe_normalizer.extraction.normalize import NormalizeResult
from recipe_normalizer.extraction.url_plugin import (
    EnrichedLine,
    EnrichedLines,
    FetchResult,
    UrlExtractor,
)
from recipe_normalizer.filestore import LocalFileStore
from recipe_normalizer.ingestion.models import InputType, Job, JobStatus
from recipe_normalizer.users.models import User

GOLDEN_CASES = Path(__file__).parent / "golden" / "cases"
_EXT_TO_INPUT_TYPE = {".html": InputType.url, ".txt": InputType.text, ".pdf": InputType.pdf}


class GoldenStubLLM:
    """Per-case LLM seam stub driven by the case's llm_stub.json (or blanks)."""

    spent_usd = 0.0

    def __init__(self, stub: dict[str, Any]) -> None:
        self._stub = stub

    def structured(
        self,
        *,
        feature: str,
        output_model: type[Any],
        content: str | list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        fast: bool = False,
        max_tokens: int = 8000,
    ) -> Any:
        if feature == "extract.tier1_enrich":
            if "enrich" in self._stub:
                return EnrichedLines(lines=[EnrichedLine(**line) for line in self._stub["enrich"]])
            # Default: blank parse, one entry per numbered input line (no mismatch).
            count = sum(1 for line in str(content).splitlines() if line.strip())
            return EnrichedLines(lines=[EnrichedLine() for _ in range(count)])
        if feature == "extract.normalize":
            assert "normalize" in self._stub, "case needs a normalize stub for this tier"
            return NormalizeResult.model_validate(self._stub["normalize"])
        if feature == "catalog.match":
            return output_model(index=None)
        raise AssertionError(f"unexpected LLM feature in golden run: {feature!r}")

    def classify_bool(
        self, *, feature: str, question: str, content: str, max_chars: int = 30000
    ) -> bool:
        assert feature == "extract.tier2_judge"
        return bool(self._stub.get("judge", True))


def _build_job(case_dir: Path, input_file: Path, store: LocalFileStore, owner: User) -> Job:
    input_type = _EXT_TO_INPUT_TYPE[input_file.suffix]
    fingerprint = f"golden-{case_dir.name}"
    if input_type is InputType.url:
        payload: dict[str, Any] = {"url": "https://golden.test/recipe"}
    elif input_type is InputType.text:
        payload = {"text": input_file.read_text(encoding="utf-8")}
    else:
        ref = store.save(input_file.read_bytes(), suffix="pdf")
        payload = {"file_ref": ref, "filename": input_file.name, "media_type": "application/pdf"}
    return Job(
        user_id=owner.id,
        input_type=input_type,
        payload=payload,
        source_fingerprint=fingerprint,
        status=JobStatus.running,
    )


def _patch_url_extractor(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    def fetch(url: str) -> FetchResult:
        return FetchResult(url=url, status=200, html=html, content_type="text/html")

    monkeypatch.setitem(
        EXTRACTORS,
        "url",
        UrlExtractor(fetch=fetch, fetch_bytes=lambda _url: (b"hero-bytes", "image/jpeg")),
    )


def _case_dirs() -> list[Path]:
    if not GOLDEN_CASES.exists():
        return []
    return sorted(path for path in GOLDEN_CASES.iterdir() if path.is_dir())


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda path: path.name)
def test_golden_case(
    case_dir: Path,
    db_session: Session,
    owner: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    stub_path = case_dir / "llm_stub.json"
    stub = json.loads(stub_path.read_text(encoding="utf-8")) if stub_path.exists() else {}
    (input_file,) = (p for p in case_dir.iterdir() if p.name.startswith("input."))

    store = LocalFileStore(tmp_path)
    monkeypatch.setattr(worker, "get_file_store", lambda: store)
    monkeypatch.setattr(worker, "make_llm_for_job", lambda db, job: GoldenStubLLM(stub))
    if input_file.suffix == ".html":
        _patch_url_extractor(monkeypatch, input_file.read_text(encoding="utf-8"))

    job = _build_job(case_dir, input_file, store, owner)
    db_session.add(job)
    db_session.flush()

    worker.process_job(db_session, job)
    db_session.refresh(job)

    assert job.status.value == expected["status"], job.error
    _assert_artifacts(job, expected)
    if job.status is JobStatus.needs_review:
        _assert_recipes(db_session, job, expected)


def _assert_artifacts(job: Job, expected: dict[str, Any]) -> None:
    for key in expected.get("artifact_keys", []):
        assert key in (job.artifacts or {}), f"missing artifact {key!r} in {job.artifacts}"


def _assert_recipes(db: Session, job: Job, expected: dict[str, Any]) -> None:
    ids = list(job.produced_recipe_ids or [])
    assert len(ids) == expected["recipe_count"]
    recipes = {str(r.id): r for r in db.query(Recipe).filter(Recipe.id.in_(ids)).all()}
    ordered = [recipes[i] for i in ids]

    meta = job.extraction_meta or {}
    if "tier_used" in expected:
        assert meta.get("tier_used") == expected["tier_used"]
    if "source" in expected:
        assert meta.get("source") == expected["source"]

    for recipe, want in zip(ordered, expected["recipes"], strict=True):
        assert recipe.is_verified is False  # drafts await the review gate
        assert recipe.title == want["title"]
        if "language" in want:
            assert recipe.language == want["language"]
        for field in ("servings_amount", "prep_min", "cook_min", "total_min"):
            if field in want:
                assert getattr(recipe, field) == want[field]
        lines = [line for group in recipe.ingredient_groups for line in group.ingredient_lines]
        if "line_count" in want:
            assert len(lines) == want["line_count"]
        if "step_count" in want:
            assert len(recipe.steps) == want["step_count"]
        if "first_line" in want:
            assert lines[0].original_text == want["first_line"]

    if expected.get("fingerprint_on_first_only"):
        assert ordered[0].source_fingerprint == job.source_fingerprint
        assert all(r.source_fingerprint is None for r in ordered[1:])
