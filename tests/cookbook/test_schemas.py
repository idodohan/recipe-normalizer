"""Tests for cookbook schemas: DTOs + dual-quantity display rule."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from recipe_normalizer.cookbook.schemas import (
    RecipeIn,
    RecipeOut,
    RecipeSummary,
    build_display,
    format_amount,
)

# ---------------------------------------------------------------------------
# format_amount
# ---------------------------------------------------------------------------


def test_format_amount_integer_trailing_zero() -> None:
    assert format_amount(120.0) == "120"


def test_format_amount_decimal_retained() -> None:
    assert format_amount(29.57) == "29.57"


def test_format_amount_fractional() -> None:
    assert format_amount(0.5) == "0.5"


def test_format_amount_rounds_to_2dp() -> None:
    # 1.005 floated is actually 1.005, round to 2 dp => "1.01" or "1.0" depending on IEEE;
    # main contract: no more than 2 decimal places, trailing zeros stripped.
    result = format_amount(1.125)
    assert "." not in result or len(result.split(".")[1]) <= 2


# ---------------------------------------------------------------------------
# build_display
# ---------------------------------------------------------------------------


def test_build_display_approx() -> None:
    result = build_display(
        original_text="1 cup flour",
        normalized_amount=120.0,
        normalized_unit="g",
        is_approx=True,
    )
    assert result == "1 cup flour → ~120 g (approx.)"


def test_build_display_exact() -> None:
    result = build_display(
        original_text="1 oz gin",
        normalized_amount=29.57,
        normalized_unit="ml",
        is_approx=False,
    )
    assert result == "1 oz gin → 29.57 ml"


def test_build_display_unconvertible() -> None:
    result = build_display(
        original_text="salt to taste",
        normalized_amount=None,
        normalized_unit=None,
        is_approx=False,
    )
    assert result == "salt to taste"


# ---------------------------------------------------------------------------
# RecipeIn validation
# ---------------------------------------------------------------------------


VALID_PAYLOAD: dict = {
    "title": "Pasta Carbonara",
    "language": "en",
    "servings": {"amount": 4.0, "unit_text": "servings"},
    "prep_min": 10,
    "cook_min": 20,
    "total_min": 30,
    "cuisines": ["Italian"],
    "dish_types": ["main"],
    "tags": ["quick"],
    "groups": [
        {
            "name": "Pasta",
            "lines": [
                {"original_text": "200 g spaghetti"},
                {
                    "original_text": "2 eggs",
                    "quantity": "2",
                    "unit": None,
                    "note": "large",
                    "is_optional": False,
                },
            ],
        },
        {
            "name": "Sauce",
            "lines": [
                {"original_text": "100 g pancetta", "is_optional": False},
            ],
        },
    ],
    "steps": [
        {"original_text": "Boil the pasta."},
        {"original_text": "Fry the pancetta."},
    ],
}


def test_recipe_in_valid() -> None:
    recipe = RecipeIn.model_validate(VALID_PAYLOAD)
    assert recipe.title == "Pasta Carbonara"
    assert len(recipe.groups) == 2
    assert len(recipe.groups[0].lines) == 2
    assert recipe.servings is not None
    assert recipe.servings.amount == 4.0


def test_recipe_in_rejects_empty_title() -> None:
    bad = {**VALID_PAYLOAD, "title": ""}
    with pytest.raises(ValidationError):
        RecipeIn.model_validate(bad)


def test_recipe_in_rejects_empty_groups() -> None:
    bad = {**VALID_PAYLOAD, "groups": []}
    with pytest.raises(ValidationError):
        RecipeIn.model_validate(bad)


def test_recipe_in_rejects_group_with_no_lines() -> None:
    bad = {
        **VALID_PAYLOAD,
        "groups": [{"name": "Empty", "lines": []}],
    }
    with pytest.raises(ValidationError):
        RecipeIn.model_validate(bad)


def test_recipe_in_rejects_negative_quantity() -> None:
    bad_groups = [
        {
            "name": "Test",
            "lines": [{"original_text": "flour", "quantity": "-1"}],
        }
    ]
    bad = {**VALID_PAYLOAD, "groups": bad_groups}
    with pytest.raises(ValidationError):
        RecipeIn.model_validate(bad)


def test_recipe_in_rejects_negative_servings_amount() -> None:
    bad = {**VALID_PAYLOAD, "servings": {"amount": -1.0, "unit_text": "servings"}}
    with pytest.raises(ValidationError):
        RecipeIn.model_validate(bad)


def test_recipe_in_rejects_empty_original_text() -> None:
    bad_groups = [
        {
            "name": "Test",
            "lines": [{"original_text": ""}],
        }
    ]
    bad = {**VALID_PAYLOAD, "groups": bad_groups}
    with pytest.raises(ValidationError):
        RecipeIn.model_validate(bad)


# ---------------------------------------------------------------------------
# RecipeOut from ORM objects
# ---------------------------------------------------------------------------


def _make_orm_recipe() -> object:
    """Build an in-memory ORM-like namespace (no DB) for RecipeOut testing."""
    from types import SimpleNamespace

    line1 = SimpleNamespace(
        id=uuid.uuid4(),
        original_text="1 cup flour",
        quantity=Decimal("1"),
        unit="cup",
        canonical_ingredient_id=None,
        normalized_amount=120.0,
        normalized_unit="g",
        is_approx=True,
        note=None,
        is_optional=False,
        order_index=0,
    )
    line2 = SimpleNamespace(
        id=uuid.uuid4(),
        original_text="1 oz gin",
        quantity=Decimal("1"),
        unit="oz",
        canonical_ingredient_id=None,
        normalized_amount=29.57,
        normalized_unit="ml",
        is_approx=False,
        note=None,
        is_optional=False,
        order_index=1,
    )
    group = SimpleNamespace(
        id=uuid.uuid4(),
        name="Main",
        order_index=0,
        ingredient_lines=[line1, line2],
    )

    cuisine = SimpleNamespace(name="French")
    dish_type = SimpleNamespace(name="cocktail")
    tag = SimpleNamespace(name="classic")

    step = SimpleNamespace(
        id=uuid.uuid4(),
        order_index=0,
        original_text="Shake well.",
        ingredient_line_refs=[],
    )

    from recipe_normalizer.cookbook.models import SourceType

    return SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        schema_version=1,
        title="Test Recipe",
        description="A test",
        image_ref=None,
        source=None,
        source_type=SourceType.manual,
        language="en",
        servings_amount=2.0,
        servings_unit_text="glasses",
        prep_min=5,
        cook_min=None,
        total_min=5,
        extraction_meta=None,
        is_verified=False,
        provenance=None,
        derived_from=None,
        last_edited_by=None,
        last_edited_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        ingredient_groups=[group],
        steps=[step],
        cuisines=[cuisine],
        dish_types=[dish_type],
        tags=[tag],
    )


def test_recipe_out_display_strings() -> None:
    orm_recipe = _make_orm_recipe()
    out = RecipeOut.model_validate(orm_recipe)

    assert len(out.groups) == 1
    lines = out.groups[0].lines
    assert len(lines) == 2

    # line1: approx conversion
    assert lines[0].display == "1 cup flour → ~120 g (approx.)"
    # line2: exact conversion
    assert lines[1].display == "1 oz gin → 29.57 ml"


def test_recipe_out_vocab_as_name_lists() -> None:
    orm_recipe = _make_orm_recipe()
    out = RecipeOut.model_validate(orm_recipe)
    assert out.cuisines == ["French"]
    assert out.dish_types == ["cocktail"]
    assert out.tags == ["classic"]


def test_recipe_out_servings_built() -> None:
    orm_recipe = _make_orm_recipe()
    out = RecipeOut.model_validate(orm_recipe)
    assert out.servings is not None
    assert out.servings.amount == 2.0
    assert out.servings.unit_text == "glasses"


# ---------------------------------------------------------------------------
# RecipeSummary from ORM objects
# ---------------------------------------------------------------------------


def test_recipe_summary_fields() -> None:
    from types import SimpleNamespace

    dish_type = SimpleNamespace(name="main")
    now = datetime.now(UTC)
    orm = SimpleNamespace(
        id=uuid.uuid4(),
        title="Quick Pasta",
        image_ref="https://example.com/img.jpg",
        dish_types=[dish_type],
        total_min=30,
        is_verified=True,
        created_at=now,
    )
    summary = RecipeSummary.model_validate(orm)
    assert summary.title == "Quick Pasta"
    assert summary.dish_types == ["main"]
    assert summary.total_min == 30
    assert summary.is_verified is True
    assert summary.created_at == now
