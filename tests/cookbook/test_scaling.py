"""Tests for deterministic scaling — TDD Step 1.

All tests in this file MUST fail before `cookbook/scaling.py` exists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from recipe_normalizer.cookbook.scaling import (
    format_quantity,
    scale_factor_for,
    scale_recipe,
)
from recipe_normalizer.cookbook.schemas import (
    IngredientGroupOut,
    IngredientLineOut,
    RecipeOut,
    StepOut,
)

# ---------------------------------------------------------------------------
# Helpers to build in-memory RecipeOut fixtures
# ---------------------------------------------------------------------------


def _make_line(
    *,
    original_text: str,
    quantity: float | None = None,
    unit: str | None = None,
    name: str | None = None,
    normalized_amount: float | None = None,
    normalized_unit: str | None = None,
    is_approx: bool = False,
    note: str | None = None,
    is_optional: bool = False,
) -> IngredientLineOut:
    return IngredientLineOut(
        id=uuid.uuid4(),
        original_text=original_text,
        quantity=quantity,
        unit=unit,
        canonical_name=name,
        canonical_ingredient_id=None,
        normalized_amount=normalized_amount,
        normalized_unit=normalized_unit,
        is_approx=is_approx,
        note=note,
        is_optional=is_optional,
    )


def _make_recipe(
    groups: list[IngredientGroupOut],
    *,
    servings_amount: float | None = 4.0,
    servings_unit_text: str | None = "servings",
    steps: list[StepOut] | None = None,
) -> RecipeOut:
    """Build a minimal in-memory RecipeOut (no DB) for scaling tests."""
    from types import SimpleNamespace

    from recipe_normalizer.cookbook.models import SourceType

    group_namespaces = []
    for g in groups:
        # Build ORM-like namespace so RecipeOut.model_validate works
        lines_ns = []
        for ln in g.lines:
            from decimal import Decimal

            lines_ns.append(
                SimpleNamespace(
                    id=ln.id,
                    original_text=ln.original_text,
                    quantity=Decimal(str(ln.quantity)) if ln.quantity is not None else None,
                    unit=ln.unit,
                    canonical_name=ln.name,
                    canonical_ingredient_id=ln.canonical_ingredient_id,
                    normalized_amount=ln.normalized_amount,
                    normalized_unit=ln.normalized_unit,
                    is_approx=ln.is_approx,
                    note=ln.note,
                    is_optional=ln.is_optional,
                )
            )
        group_namespaces.append(
            SimpleNamespace(
                id=g.id,
                name=g.name,
                ingredient_lines=lines_ns,
            )
        )

    steps_ns = []
    for s in steps or []:
        steps_ns.append(
            SimpleNamespace(
                id=s.id,
                original_text=s.original_text,
                ingredient_line_refs=s.ingredient_line_refs,
            )
        )

    orm = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        schema_version=1,
        title="Test Recipe",
        description=None,
        image_ref=None,
        source=None,
        source_type=SourceType.manual,
        language="en",
        servings_amount=servings_amount,
        servings_unit_text=servings_unit_text,
        prep_min=None,
        cook_min=None,
        total_min=None,
        extraction_meta=None,
        is_verified=False,
        provenance=None,
        derived_from=None,
        last_edited_by=None,
        last_edited_at=None,
        created_at=datetime.now(UTC),
        ingredient_groups=group_namespaces,
        steps=steps_ns,
        cuisines=[],
        dish_types=[],
        tags=[],
    )
    return RecipeOut.model_validate(orm)


def _make_group(lines: list[IngredientLineOut], name: str | None = None) -> IngredientGroupOut:
    return IngredientGroupOut(
        id=uuid.uuid4(),
        name=name,
        lines=lines,
    )


def _make_step(text: str) -> StepOut:
    return StepOut(id=uuid.uuid4(), original_text=text, ingredient_line_refs=[])


# ---------------------------------------------------------------------------
# scale_factor_for
# ---------------------------------------------------------------------------


def test_scale_factor_by_target_servings() -> None:
    assert scale_factor_for(base_servings=4, target_servings=6) == 1.5


def test_scale_factor_requires_positive_inputs() -> None:
    with pytest.raises(ValueError):
        scale_factor_for(base_servings=0, target_servings=4)
    with pytest.raises(ValueError):
        scale_factor_for(base_servings=4, target_servings=0)


# ---------------------------------------------------------------------------
# format_quantity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2.25, "2¼"),
        (0.5, "½"),
        (0.333, "⅓"),
        (1.0, "1"),
        (2.6666, "2⅔"),
        (3.875, "3⅞"),
        (0.125, "⅛"),
        (0.75, "¾"),
        (1.5, "1½"),
        (2.0, "2"),
        (0.66, "⅔"),
        (5.33, "5⅓"),
        (2.4, "2.4"),  # not near a friendly fraction → decimal
        (0.1, "0.1"),
        # positive near-zero must never render as "0" (e.g. ⅛ tsp × 0.25)
        (0.04, "0.04"),
        (0.031, "0.03"),
        (0.004, "0.004"),
        (0.0, "0"),  # true zero stays "0"
    ],
)
def test_format_quantity(value: float, expected: str) -> None:
    assert format_quantity(value) == expected


# ---------------------------------------------------------------------------
# scale_recipe — individual line scenarios
# ---------------------------------------------------------------------------


def test_scale_recipe_quantity_and_normalized() -> None:
    """factor 2: quantity 1.125 cup + normalized 135g → '2¼ cup all-purpose flour → ~270 g'.

    The scaled row carries the canonical ingredient name so it is self-sufficient,
    and the ~ marks the approximation without a redundant "(approx.)" suffix.
    """
    line = _make_line(
        original_text="1⅛ cups flour",
        quantity=1.125,
        unit="cup",
        name="all-purpose flour",
        normalized_amount=135.0,
        normalized_unit="g",
        is_approx=True,
    )
    recipe = _make_recipe([_make_group([line])])
    result = scale_recipe(recipe, factor=2.0)

    scaled_line = result.groups[0].lines[0]
    assert scaled_line.quantity_display == "2¼"
    assert scaled_line.unit == "cup"
    assert scaled_line.normalized_amount == 270.0
    assert scaled_line.display == "2¼ cup all-purpose flour → ~270 g"
    # original_text preserved unchanged
    assert scaled_line.original_text == "1⅛ cups flour"
    assert scaled_line.passes_through is False


def test_scale_recipe_names_the_ingredient_even_without_a_unit() -> None:
    """P0 regression: a doubled unitless line reads '4 egg', never a bare '4'."""
    line = _make_line(original_text="2 eggs", quantity=2.0, unit=None, name="egg")
    recipe = _make_recipe([_make_group([line])])
    result = scale_recipe(recipe, factor=2.0)

    scaled_line = result.groups[0].lines[0]
    assert scaled_line.display == "4 egg"


def test_scale_recipe_unmatched_line_falls_back_to_quantity_only() -> None:
    """No canonical name (unmatched line): scaled display is quantity+unit as before."""
    line = _make_line(original_text="2 knobs butter", quantity=2.0, unit="knob", name=None)
    recipe = _make_recipe([_make_group([line])])
    result = scale_recipe(recipe, factor=1.5)

    assert result.groups[0].lines[0].display == "3 knob"


def test_scale_recipe_unconvertible_passthrough() -> None:
    """'salt to taste' — no quantity/unit/normalized → passes through unchanged."""
    line = _make_line(original_text="salt to taste")
    recipe = _make_recipe([_make_group([line])])
    result = scale_recipe(recipe, factor=2.0)

    scaled_line = result.groups[0].lines[0]
    assert scaled_line.passes_through is True
    assert scaled_line.display == "salt to taste"
    assert scaled_line.original_text == "salt to taste"
    # no quantity mutation
    assert scaled_line.quantity_display is None


def test_scale_recipe_parts_line() -> None:
    """'parts' unit scales natively: quantity 2 × 1.5 → display '3'."""
    line = _make_line(
        original_text="2 parts vodka",
        quantity=2.0,
        unit="part",
        normalized_amount=None,
    )
    recipe = _make_recipe([_make_group([line])])
    result = scale_recipe(recipe, factor=1.5)

    scaled_line = result.groups[0].lines[0]
    assert scaled_line.passes_through is False
    assert scaled_line.quantity_display == "3"
    assert scaled_line.display == "3 part"


def test_scale_recipe_quantity_only_no_unit() -> None:
    """Quantity present but no unit (e.g. '2 eggs'): scale qty, no arrow."""
    line = _make_line(
        original_text="2 eggs",
        quantity=2.0,
        unit=None,
        normalized_amount=None,
    )
    recipe = _make_recipe([_make_group([line])])
    result = scale_recipe(recipe, factor=2.0)

    scaled_line = result.groups[0].lines[0]
    assert scaled_line.passes_through is False
    assert scaled_line.quantity_display == "4"
    assert scaled_line.display == "4"


def test_scale_recipe_servings_scaled() -> None:
    """ServingsOut(amount=4) at factor 1.5 → amount 6."""
    recipe = _make_recipe(
        [_make_group([_make_line(original_text="1 cup sugar", quantity=1.0, unit="cup")])],
        servings_amount=4.0,
        servings_unit_text="servings",
    )
    result = scale_recipe(recipe, factor=1.5)

    assert result.servings is not None
    assert result.servings.amount == 6.0
    assert result.servings.unit_text == "servings"


def test_scale_recipe_step_text_disclaimer() -> None:
    """Scaled result always has step_text_disclaimer=True."""
    step = _make_step("Boil the pasta.")
    recipe = _make_recipe(
        [_make_group([_make_line(original_text="1 cup pasta", quantity=1.0, unit="cup")])],
        steps=[step],
    )
    result = scale_recipe(recipe, factor=2.0)

    assert result.step_text_disclaimer is True
    # steps are verbatim — original text untouched
    assert result.steps[0].original_text == "Boil the pasta."


def test_scale_recipe_does_not_mutate_input() -> None:
    """scale_recipe is pure: original RecipeOut must be unchanged after call."""
    line = _make_line(
        original_text="1 cup flour",
        quantity=1.0,
        unit="cup",
        normalized_amount=120.0,
        normalized_unit="g",
        is_approx=True,
    )
    recipe = _make_recipe([_make_group([line])])

    original_quantity = recipe.groups[0].lines[0].quantity
    original_normalized = recipe.groups[0].lines[0].normalized_amount
    original_display = recipe.groups[0].lines[0].display
    original_servings_amount = recipe.servings.amount if recipe.servings else None

    scale_recipe(recipe, factor=3.0)

    # originals must be unchanged
    assert recipe.groups[0].lines[0].quantity == original_quantity
    assert recipe.groups[0].lines[0].normalized_amount == original_normalized
    assert recipe.groups[0].lines[0].display == original_display
    if recipe.servings:
        assert recipe.servings.amount == original_servings_amount


def test_scale_recipe_invalid_factor() -> None:
    """factor ≤ 0 or > 100 → ValueError."""
    recipe = _make_recipe(
        [_make_group([_make_line(original_text="1 cup sugar", quantity=1.0, unit="cup")])]
    )
    with pytest.raises(ValueError):
        scale_recipe(recipe, factor=0)
    with pytest.raises(ValueError):
        scale_recipe(recipe, factor=-1.0)
    with pytest.raises(ValueError):
        scale_recipe(recipe, factor=101.0)


def test_scale_recipe_factor_stored() -> None:
    """ScaledRecipeOut carries the factor used."""
    recipe = _make_recipe(
        [_make_group([_make_line(original_text="1 cup sugar", quantity=1.0, unit="cup")])]
    )
    result = scale_recipe(recipe, factor=2.0)
    assert result.factor == 2.0


def test_scale_recipe_no_servings() -> None:
    """If base recipe has no servings, scaled result servings is None."""
    recipe = _make_recipe(
        [_make_group([_make_line(original_text="1 cup sugar", quantity=1.0, unit="cup")])],
        servings_amount=None,
        servings_unit_text=None,
    )
    result = scale_recipe(recipe, factor=2.0)
    assert result.servings is None


def test_scale_recipe_line_with_normalized_no_is_approx() -> None:
    """Exact conversion (is_approx=False): no ~ prefix, no (approx.) suffix."""
    line = _make_line(
        original_text="1 oz gin",
        quantity=1.0,
        unit="oz",
        normalized_amount=29.5735,
        normalized_unit="ml",
        is_approx=False,
    )
    recipe = _make_recipe([_make_group([line])])
    result = scale_recipe(recipe, factor=2.0)

    scaled_line = result.groups[0].lines[0]
    assert scaled_line.normalized_amount is not None
    assert abs(scaled_line.normalized_amount - 59.147) < 0.01
    assert "~" not in scaled_line.display
    assert "(approx.)" not in scaled_line.display
    assert "→" in scaled_line.display
