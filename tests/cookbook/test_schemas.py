"""Tests for cookbook schemas: DTOs + dual-quantity display rule."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

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


def test_format_amount_tiny_amount() -> None:
    # Tiny nonzero amounts should not be rendered as "0"
    assert format_amount(0.004) == "0.004"


def test_format_amount_zero_unchanged() -> None:
    # Zero remains "0"
    assert format_amount(0.0) == "0"


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
    assert result == "1 cup flour → ~120 g"


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
    assert lines[0].display == "1 cup flour → ~120 g"
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
# RecipeOut from a real persisted ORM Recipe (round-trip through the DB)
# ---------------------------------------------------------------------------


def test_recipe_out_from_persisted_orm_recipe(db_session: Session) -> None:
    """Persist a full aggregate, re-query it, and validate into RecipeOut.

    This guards against silent ORM-mapping regressions (e.g. relationship
    attributes not being picked up, producing groups=[] on a 200 response).
    """
    from sqlalchemy import select

    from recipe_normalizer.cookbook.models import (
        Cuisine,
        DishType,
        IngredientGroup,
        IngredientLine,
        Recipe,
        SourceType,
        Step,
        Tag,
    )
    from recipe_normalizer.users.models import User

    owner = User(
        email=f"schema-test-{uuid.uuid4()}@example.com",
        password_hash="hash",
        display_name="Schema Tester",
    )
    db_session.add(owner)
    db_session.flush()

    cuisine = Cuisine(name=f"Cuisine-{uuid.uuid4()}")
    dish_type = DishType(name=f"Dish-{uuid.uuid4()}")
    tag = Tag(name=f"Tag-{uuid.uuid4()}")
    db_session.add_all([cuisine, dish_type, tag])
    db_session.flush()

    recipe = Recipe(
        owner_id=owner.id,
        # No cookbook needed on the row: containment is the `cookbook_recipes`
        # join, and `RecipeOut` no longer reports a cookbook at all.
        title="Persisted Recipe",
        source_type=SourceType.manual,
        servings_amount=4.0,
        servings_unit_text="servings",
        total_min=45,
    )
    db_session.add(recipe)
    db_session.flush()

    group0 = IngredientGroup(recipe_id=recipe.id, name="Dough", order_index=0)
    group1 = IngredientGroup(recipe_id=recipe.id, name="Topping", order_index=1)
    db_session.add_all([group0, group1])
    db_session.flush()

    db_session.add_all(
        [
            IngredientLine(
                group_id=group0.id,
                order_index=0,
                original_text="1 cup flour",
                quantity=Decimal("1"),
                unit="cup",
                normalized_amount=120.0,
                normalized_unit="g",
                is_approx=True,
            ),
            IngredientLine(
                group_id=group0.id,
                order_index=1,
                original_text="salt to taste",
            ),
            IngredientLine(
                group_id=group1.id,
                order_index=0,
                original_text="1 oz gin",
                quantity=Decimal("1"),
                unit="oz",
                normalized_amount=29.57,
                normalized_unit="ml",
                is_approx=False,
            ),
        ]
    )
    db_session.add_all(
        [
            Step(recipe_id=recipe.id, order_index=0, original_text="Mix."),
            Step(recipe_id=recipe.id, order_index=1, original_text="Bake."),
        ]
    )
    recipe.cuisines.append(cuisine)
    recipe.dish_types.append(dish_type)
    recipe.tags.append(tag)
    db_session.flush()

    recipe_id = recipe.id
    cuisine_name = cuisine.name
    dish_type_name = dish_type.name
    tag_name = tag.name

    db_session.expire_all()
    loaded = db_session.execute(select(Recipe).where(Recipe.id == recipe_id)).scalar_one()

    out = RecipeOut.model_validate(loaded)

    # groups + lines populated in order
    assert [g.name for g in out.groups] == ["Dough", "Topping"]
    assert [ln.original_text for ln in out.groups[0].lines] == ["1 cup flour", "salt to taste"]
    assert out.groups[0].lines[0].quantity == 1.0  # Decimal -> float

    # display rule applied per-line
    assert out.groups[0].lines[0].display == "1 cup flour → ~120 g"
    assert out.groups[0].lines[1].display == "salt to taste"
    assert out.groups[1].lines[0].display == "1 oz gin → 29.57 ml"

    # steps in order
    assert [s.original_text for s in out.steps] == ["Mix.", "Bake."]

    # servings built from flat columns
    assert out.servings is not None
    assert out.servings.amount == 4.0
    assert out.servings.unit_text == "servings"

    # vocab as name lists
    assert out.cuisines == [cuisine_name]
    assert out.dish_types == [dish_type_name]
    assert out.tags == [tag_name]

    # raw servings columns excluded from serialized output; computed servings present
    dumped = out.model_dump()
    assert "servings_amount" not in dumped
    assert "servings_unit_text" not in dumped
    assert dumped["servings"] == {"amount": 4.0, "unit_text": "servings"}


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
    # Absolute URLs pass through untouched.
    assert summary.image_ref == "https://example.com/img.jpg"


def test_recipe_summary_bare_image_ref_becomes_files_url() -> None:
    from types import SimpleNamespace

    orm = SimpleNamespace(
        id=uuid.uuid4(),
        title="Cornbread",
        image_ref="2ffc7e/abc.png",  # bare store ref
        dish_types=[],
        total_min=None,
        is_verified=False,
        created_at=datetime.now(UTC),
    )
    summary = RecipeSummary.model_validate(orm)
    assert summary.image_ref == "/api/files/2ffc7e/abc.png"
