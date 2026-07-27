"""Tests for persist_drafts: NormalizeResult → cookbook draft recipes."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.models import Recipe
from recipe_normalizer.cookbook.service import DuplicateRecipeError, SourceType
from recipe_normalizer.extraction.normalize import (
    NormalizedGroup,
    NormalizedLine,
    NormalizedRecipe,
    NormalizedStep,
    NormalizeResult,
)
from recipe_normalizer.extraction.persist import persist_drafts
from recipe_normalizer.users.models import User
from tests.extraction.conftest import StubLLM


def _flour_recipe(title: str = "Flour Bread") -> NormalizedRecipe:
    return NormalizedRecipe(
        title=title,
        groups=[
            NormalizedGroup(
                lines=[
                    NormalizedLine(
                        original_text="2 cups all-purpose flour",
                        name="all-purpose flour",
                        quantity=2.0,
                        unit="cup",
                    )
                ]
            )
        ],
        steps=[NormalizedStep(original_text="Mix and bake.")],
        dish_types=["bread"],
    )


def _result(*recipes: NormalizedRecipe) -> NormalizeResult:
    return NormalizeResult(is_recipe=True, confidence=0.9, recipes=list(recipes))


def test_persist_single_recipe_draft(seeded: Session, owner: User) -> None:
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(_flour_recipe()),
        llm=StubLLM(),  # type: ignore[arg-type]
        source="pasted text",
        source_type=SourceType.text,
        source_fingerprint="fp-single",
        extraction_meta={"tier_used": 1, "actions_log": []},
        image_ref="ab/cd.png",
    )

    assert len(ids) == 1
    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])

    # Draft, not verified — review gate (spec §6.4)
    assert out.is_verified is False
    assert out.source == "pasted text"
    assert out.source_type == "text"
    assert out.extraction_meta == {"tier_used": 1, "actions_log": []}
    assert out.image_ref == "/api/files/ab/cd.png"  # exposed as a servable URL
    assert out.dish_types == ["bread"]

    # Line is catalog-linked and normalized: 2 cups flour → 240 g approx
    line = out.groups[0].lines[0]
    assert line.canonical_ingredient_id is not None
    assert line.display == "2 cups all-purpose flour → ~240 g"

    # Fingerprint persisted on the draft
    row = seeded.get(Recipe, ids[0])
    assert row is not None
    assert row.source_fingerprint == "fp-single"


def test_persist_multi_recipe_fingerprint_only_on_first(seeded: Session, owner: User) -> None:
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(_flour_recipe("First"), _flour_recipe("Second")),
        llm=StubLLM(),  # type: ignore[arg-type]
        source="https://example.com/roundup",
        source_type=SourceType.web,
        source_fingerprint="fp-roundup",
        extraction_meta=None,
        image_ref=None,
    )

    assert len(ids) == 2
    first, second = (seeded.get(Recipe, rid) for rid in ids)
    assert first is not None and second is not None
    assert first.title == "First"
    assert second.title == "Second"
    # Partial unique index is per-recipe: only the FIRST draft carries the fingerprint.
    assert first.source_fingerprint == "fp-roundup"
    assert second.source_fingerprint is None


def test_persist_skips_recipe_with_no_valid_lines(seeded: Session, owner: User) -> None:
    empty = NormalizedRecipe(
        title="Ghost Recipe",
        groups=[NormalizedGroup(lines=[NormalizedLine(original_text="   ")])],
        steps=[NormalizedStep(original_text="Stir.")],
    )
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(empty, _flour_recipe("Survivor")),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint="fp-skip",
        extraction_meta=None,
        image_ref=None,
    )

    assert len(ids) == 1
    row = seeded.get(Recipe, ids[0])
    assert row is not None
    assert row.title == "Survivor"
    # Fingerprint goes to the first draft actually created.
    assert row.source_fingerprint == "fp-skip"


def test_persist_drops_empty_groups_and_steps(seeded: Session, owner: User) -> None:
    recipe = NormalizedRecipe(
        title="Sparse",
        groups=[
            NormalizedGroup(name="Empty", lines=[NormalizedLine(original_text="")]),
            NormalizedGroup(
                name="Real",
                lines=[
                    NormalizedLine(original_text="1 pinch of magic"),
                    NormalizedLine(original_text="  "),
                ],
            ),
        ],
        steps=[NormalizedStep(original_text=""), NormalizedStep(original_text="Serve.")],
    )
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(recipe),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint=None,
        extraction_meta=None,
        image_ref=None,
    )

    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])
    assert [g.name for g in out.groups] == ["Real"]
    assert [line.original_text for line in out.groups[0].lines] == ["1 pinch of magic"]
    assert [s.original_text for s in out.steps] == ["Serve."]


def test_persist_preserves_hebrew_original_text(seeded: Session, owner: User) -> None:
    hebrew_line = "2 כוסות קמח לכל מטרה"
    hebrew_step = "לערבב את כל החומרים יחד."
    recipe = NormalizedRecipe(
        title="חלה ביתית",
        language="he",
        groups=[
            NormalizedGroup(
                name="לבצק",
                lines=[
                    NormalizedLine(
                        original_text=hebrew_line,
                        name="all-purpose flour",
                        quantity=2.0,
                        unit="כוס",
                    )
                ],
            )
        ],
        steps=[NormalizedStep(original_text=hebrew_step)],
    )
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(recipe),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.image,
        source_fingerprint=None,
        extraction_meta=None,
        image_ref=None,
    )

    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])
    assert out.title == "חלה ביתית"
    assert out.language == "he"
    assert out.groups[0].name == "לבצק"
    assert out.groups[0].lines[0].original_text == hebrew_line
    assert out.steps[0].original_text == hebrew_step
    # English name still drives catalog matching.
    assert out.groups[0].lines[0].canonical_ingredient_id is not None


def test_persist_maps_servings_and_times(seeded: Session, owner: User) -> None:
    recipe = _flour_recipe("Timed")
    recipe.servings_amount = 4.0
    recipe.servings_unit_text = "servings"
    recipe.prep_min = 10
    recipe.cook_min = 35
    recipe.total_min = 45
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(recipe),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint=None,
        extraction_meta=None,
        image_ref=None,
    )

    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])
    assert out.servings is not None
    assert out.servings.amount == 4.0
    assert out.servings.unit_text == "servings"
    assert (out.prep_min, out.cook_min, out.total_min) == (10, 35, 45)


def test_persist_coerces_invalid_quantity_to_none(seeded: Session, owner: User) -> None:
    """Negative/zero LLM quantities must not fail the draft — they become None."""
    recipe = NormalizedRecipe(
        title="Bad Quantities",
        groups=[
            NormalizedGroup(
                lines=[
                    NormalizedLine(original_text="-2 cups flour", quantity=-2.0, unit="cup"),
                    NormalizedLine(original_text="0 pinches salt", quantity=0.0, unit="pinch"),
                ]
            )
        ],
    )
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(recipe),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint=None,
        extraction_meta=None,
        image_ref=None,
    )

    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])
    assert [line.quantity for line in out.groups[0].lines] == [None, None]
    # original_text untouched by the coercion
    assert out.groups[0].lines[0].original_text == "-2 cups flour"


def test_persist_coerces_negative_times_to_none(seeded: Session, owner: User) -> None:
    """One bad LLM time field must not fail a whole multi-draft persist."""
    recipe = _flour_recipe("Bad Times")
    recipe.prep_min = -5
    recipe.cook_min = -1
    recipe.total_min = 45
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(recipe),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint=None,
        extraction_meta=None,
        image_ref=None,
    )

    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])
    assert (out.prep_min, out.cook_min, out.total_min) == (None, None, 45)


def test_persist_truncates_overlong_title(seeded: Session, owner: User) -> None:
    recipe = _flour_recipe("T" * 400)
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(recipe),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint=None,
        extraction_meta=None,
        image_ref=None,
    )

    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])
    assert out.title == "T" * 300


def test_persist_empty_title_falls_back_to_untitled(seeded: Session, owner: User) -> None:
    recipe = _flour_recipe("   ")
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(recipe),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint=None,
        extraction_meta=None,
        image_ref=None,
    )

    out = cookbook_service.get_recipe(seeded, owner_id=owner.id, recipe_id=ids[0])
    assert out.title == "Untitled recipe"


def test_persist_duplicate_fingerprint_raises(seeded: Session, owner: User) -> None:
    kwargs = dict(
        owner_id=owner.id,
        llm=StubLLM(),
        source=None,
        source_type=SourceType.text,
        source_fingerprint="fp-dup",
        extraction_meta=None,
        image_ref=None,
    )
    persist_drafts(seeded, result=_result(_flour_recipe()), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(DuplicateRecipeError):
        persist_drafts(seeded, result=_result(_flour_recipe()), **kwargs)  # type: ignore[arg-type]


def test_persist_empty_result_returns_no_ids(seeded: Session, owner: User) -> None:
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=NormalizeResult(is_recipe=False, reason="not a recipe"),
        llm=StubLLM(),  # type: ignore[arg-type]
        source=None,
        source_type=SourceType.text,
        source_fingerprint="fp-none",
        extraction_meta=None,
        image_ref=None,
    )
    assert ids == []


def test_persist_lands_drafts_in_the_ingesting_users_default_cookbook(
    seeded: Session, owner: User
) -> None:
    """No cookbook-less recipe escapes the worker's persist path.

    ``persist_drafts`` never passes a ``cookbook_id``, so it relies on
    ``create_recipe``'s ``ensure_default_cookbook`` fallback — this pins that
    the fallback really is the path taken (the alternative, a NULL
    ``cookbook_id``, is what the finalize migration's NOT NULL forbids).
    """
    ids = persist_drafts(
        seeded,
        owner_id=owner.id,
        result=_result(_flour_recipe("Ingested Loaf")),
        llm=StubLLM(),  # type: ignore[arg-type]
        source="pasted text",
        source_type=SourceType.text,
        source_fingerprint="fp-default-cookbook",
        extraction_meta=None,
        image_ref=None,
    )

    default = cookbook_service.ensure_default_cookbook(seeded, owner.id)
    row = seeded.get(Recipe, ids[0])
    assert row is not None
    assert row.cookbook_id == default.id
