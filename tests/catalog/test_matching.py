"""Tests for match_or_create: exact → fuzzy ≥90 → LLM-band → create_unreviewed.

Rapidfuzz scores verified against the seeded corpus (777 strings, lowercased):
  "all purpose flour"  → WRatio vs "all-purpose flour" = 94.1  (≥90 fuzzy path)
  "ap fluor"           → WRatio vs "ap flour"          = 87.5  (70–90 LLM band)
  "dragon fruit syrup" → best band match ≤85.5, none ≥90      (LLM band / unreviewed)
  "thick cream"        → WRatio vs "heavy whipping cream" = 85.5 (70–90 LLM band)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.catalog import service
from recipe_normalizer.catalog.models import CanonicalIngredient
from recipe_normalizer.catalog.seed_loader import load_seed

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(db_session: Session) -> Session:
    """Load seed data and return the session."""
    load_seed(db_session)
    return db_session


def _stub_llm(index: int | None) -> MagicMock:
    """Return a MagicMock whose .structured() method returns _MatchChoice(index=index)."""
    # We import here to avoid top-level import of the internal model.
    from recipe_normalizer.catalog.service import _MatchChoice  # type: ignore[attr-defined]

    llm = MagicMock()
    llm.structured.return_value = _MatchChoice(index=index)
    return llm


# ---------------------------------------------------------------------------
# Exact / alias path (no fuzzy work)
# ---------------------------------------------------------------------------


def test_exact_name_match_no_new_row(seeded: Session) -> None:
    """match_or_create with exact canonical name returns existing ingredient."""
    result = service.match_or_create(seeded, "all-purpose flour")
    assert result.name == "all-purpose flour"
    # No new row was created
    from sqlalchemy import func, select

    count = seeded.scalar(select(func.count()).select_from(CanonicalIngredient))
    assert count is not None
    # Seed has 243 entries — count should not have grown
    assert count == 243


def test_exact_alias_hebrew_resolves(seeded: Session) -> None:
    """Exact alias 'קמח' resolves to all-purpose flour without fuzzy work."""
    result = service.match_or_create(seeded, "קמח")
    assert result.name == "all-purpose flour"


# ---------------------------------------------------------------------------
# Fuzzy ≥90 path
# ---------------------------------------------------------------------------


def test_fuzzy_gte90_missing_hyphen_links(seeded: Session) -> None:
    """'all purpose flour' scores 94.1 vs 'all-purpose flour' → fuzzy-links, no new row.

    Verified score: rapidfuzz.fuzz.WRatio('all purpose flour', 'all-purpose flour') == 94.1
    """
    result = service.match_or_create(seeded, "all purpose flour")
    assert result.name == "all-purpose flour"
    # Confirm no unreviewed ingredient was created
    from sqlalchemy import func, select

    count = seeded.scalar(select(func.count()).select_from(CanonicalIngredient))
    assert count == 243


# ---------------------------------------------------------------------------
# LLM band (70–90) path: LLM says match
# ---------------------------------------------------------------------------


def test_llm_band_ap_fluor_with_llm_links(seeded: Session) -> None:
    """'ap fluor' (typo) → scores 87.5 vs 'ap flour' (in 70–90 band).

    With stub LLM returning index=0 (first candidate = 'ap flour' → all-purpose flour),
    the result should link to all-purpose flour — no new row.

    Verified score: rapidfuzz.fuzz.WRatio('ap fluor', 'ap flour') == 87.5
    """
    llm = _stub_llm(index=0)
    result = service.match_or_create(seeded, "AP fluor", llm=llm)
    assert result.name == "all-purpose flour"
    # LLM was called exactly once
    llm.structured.assert_called_once()

    # No new row
    from sqlalchemy import func, select

    count = seeded.scalar(select(func.count()).select_from(CanonicalIngredient))
    assert count == 243


def test_llm_band_thick_cream_with_llm_links(seeded: Session) -> None:
    """'thick cream' → 85.5 vs 'heavy whipping cream' (alias of heavy cream).

    LLM stub returns index=0 → links to 'heavy cream'.

    Verified score: rapidfuzz.fuzz.WRatio('thick cream', 'heavy whipping cream') == 85.5
    """
    llm = _stub_llm(index=0)
    result = service.match_or_create(seeded, "thick cream", llm=llm)
    assert result.name == "heavy cream"
    llm.structured.assert_called_once()


# ---------------------------------------------------------------------------
# LLM band: LLM says null → create unreviewed
# ---------------------------------------------------------------------------


def test_llm_band_null_response_creates_unreviewed(seeded: Session) -> None:
    """LLM returns index=null → falls through to create_unreviewed."""
    llm = _stub_llm(index=None)
    result = service.match_or_create(seeded, "thick cream", llm=llm)
    # A new unreviewed row should have been created
    assert result.name == "thick cream"
    from recipe_normalizer.catalog.models import IngredientStatus

    assert result.status == IngredientStatus.unreviewed
    llm.structured.assert_called_once()


# ---------------------------------------------------------------------------
# LLM band: llm=None → create unreviewed (no LLM call attempted)
# ---------------------------------------------------------------------------


def test_llm_band_no_llm_creates_unreviewed(seeded: Session) -> None:
    """In-band match with llm=None → create_unreviewed, no LLM call attempted.

    'thick cream' scores 85.5 vs 'heavy whipping cream' — in band but llm=None.
    """
    result = service.match_or_create(seeded, "thick cream")
    assert result.name == "thick cream"
    from recipe_normalizer.catalog.models import IngredientStatus

    assert result.status == IngredientStatus.unreviewed


# ---------------------------------------------------------------------------
# Clearly no match → create unreviewed
# ---------------------------------------------------------------------------


def test_no_match_creates_unreviewed(seeded: Session) -> None:
    """'dragon fruit syrup' has no ≥90 match → creates unreviewed."""
    result = service.match_or_create(seeded, "dragon fruit syrup")
    assert result.name == "dragon fruit syrup"
    from recipe_normalizer.catalog.models import IngredientStatus

    assert result.status == IngredientStatus.unreviewed


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_match_or_create_idempotent_existing(seeded: Session) -> None:
    """Same input twice returns the same canonical id."""
    r1 = service.match_or_create(seeded, "all-purpose flour")
    r2 = service.match_or_create(seeded, "all-purpose flour")
    assert r1.id == r2.id


def test_match_or_create_idempotent_new(seeded: Session) -> None:
    """Two calls for an unknown ingredient return the same unreviewed id."""
    r1 = service.match_or_create(seeded, "dragon fruit syrup")
    r2 = service.match_or_create(seeded, "dragon fruit syrup")
    assert r1.id == r2.id


# ---------------------------------------------------------------------------
# LLM prompt shape: content and system are properly formed
# ---------------------------------------------------------------------------


def test_llm_call_structured_kwargs(seeded: Session) -> None:
    """Verify that structured() is called with the expected keyword arguments."""
    from recipe_normalizer.catalog.service import _MatchChoice  # type: ignore[attr-defined]

    llm = _stub_llm(index=0)
    service.match_or_create(seeded, "thick cream", llm=llm)

    call_kwargs = llm.structured.call_args.kwargs
    assert call_kwargs["feature"] == "catalog.match"
    assert call_kwargs["fast"] is True
    assert call_kwargs["output_model"] is _MatchChoice
    assert "system" in call_kwargs
    assert "content" in call_kwargs
    # Content should mention the ingredient name and at least one candidate
    assert "thick cream" in call_kwargs["content"]
    assert "heavy whipping cream" in call_kwargs["content"]


# ---------------------------------------------------------------------------
# Out-of-range LLM index → falls through to create_unreviewed
# ---------------------------------------------------------------------------


def test_llm_out_of_range_index_falls_through(seeded: Session) -> None:
    """If LLM returns an out-of-range index, fall through to create_unreviewed."""
    llm = _stub_llm(index=999)
    result = service.match_or_create(seeded, "thick cream", llm=llm)
    from recipe_normalizer.catalog.models import IngredientStatus

    assert result.status == IngredientStatus.unreviewed
