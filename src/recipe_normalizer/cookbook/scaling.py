"""Deterministic recipe scaling — spec §7.

Scaling is a VIEW, never a mutation.  ``scale_recipe`` is a pure function:
it accepts a ``RecipeOut`` DTO and a float factor and returns a new
``ScaledRecipeOut`` without touching the input object.
"""

from __future__ import annotations

import uuid
from fractions import Fraction

from pydantic import BaseModel

from recipe_normalizer.cookbook.schemas import (
    IngredientLineOut,
    RecipeOut,
    ServingsOut,
    StepOut,
    format_amount,
)

# ---------------------------------------------------------------------------
# Fraction glyph table
# ---------------------------------------------------------------------------

FRACTION_GLYPHS: dict[Fraction, str] = {
    Fraction(1, 8): "⅛",
    Fraction(1, 4): "¼",
    Fraction(1, 3): "⅓",
    Fraction(3, 8): "⅜",
    Fraction(1, 2): "½",
    Fraction(5, 8): "⅝",
    Fraction(2, 3): "⅔",
    Fraction(3, 4): "¾",
    Fraction(7, 8): "⅞",
}

_SNAP_TOLERANCE = 0.015


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def format_quantity(x: float) -> str:
    """Format a float as a friendly quantity string.

    Integers are rendered without a decimal point.  Fractional parts that lie
    within ``_SNAP_TOLERANCE`` of a known glyph fraction are rendered as the
    corresponding Unicode fraction character (e.g. "2¼").  All other values
    are rendered with one decimal place, trailing zero stripped.

    Args:
        x: Non-negative float quantity.

    Returns:
        Human-readable quantity string.
    """
    whole = int(x)
    frac_part = x - whole

    # Try to snap the fractional part to a known glyph
    best_frac: Fraction | None = None
    best_dist = _SNAP_TOLERANCE + 1.0  # sentinel > tolerance

    for frac in FRACTION_GLYPHS:
        dist = abs(frac_part - float(frac))
        if dist < best_dist:
            best_dist = dist
            best_frac = frac

    if best_dist <= _SNAP_TOLERANCE and best_frac is not None:
        if float(best_frac) == 0.0:
            # snapped to zero — whole number
            return str(whole)
        glyph = FRACTION_GLYPHS[best_frac]
        return f"{whole}{glyph}" if whole else glyph

    # frac_part is near zero but didn't snap (e.g. 0.0 exactly → whole int check).
    # Guard: a positive quantity must never collapse to "0" — fall through to
    # the decimal chain instead so e.g. 0.01 renders as "0.01", not "0".
    if abs(frac_part) < _SNAP_TOLERANCE and not (whole == 0 and x > 0):
        return str(whole)

    # Fallback: one decimal place, trim trailing zero
    formatted = f"{x:.1f}".rstrip("0").rstrip(".")
    if formatted == "0" and x > 0:
        # 1 dp rounded a positive quantity to zero — try 2 dp, then 3 sig figs
        formatted = f"{x:.2f}".rstrip("0").rstrip(".")
        if formatted == "0":
            formatted = f"{x:.3g}"
    return formatted


def scale_factor_for(*, base_servings: float, target_servings: float) -> float:
    """Compute the multiplicative scale factor between two serving counts.

    Args:
        base_servings: Original serving count (must be > 0).
        target_servings: Desired serving count (must be > 0).

    Returns:
        ``target_servings / base_servings``

    Raises:
        ValueError: If either argument is not strictly positive.
    """
    if base_servings <= 0:
        raise ValueError(f"base_servings must be > 0, got {base_servings}")
    if target_servings <= 0:
        raise ValueError(f"target_servings must be > 0, got {target_servings}")
    return target_servings / base_servings


# ---------------------------------------------------------------------------
# Output DTOs
# ---------------------------------------------------------------------------


class ScaledLineOut(BaseModel):
    """A single scaled ingredient line."""

    original_text: str
    quantity_display: str | None = None
    unit: str | None = None
    normalized_amount: float | None = None
    normalized_unit: str | None = None
    is_approx: bool = False
    note: str | None = None
    is_optional: bool = False
    passes_through: bool = False
    display: str


class ScaledGroupOut(BaseModel):
    """A scaled ingredient group."""

    name: str | None = None
    lines: list[ScaledLineOut] = []


class ScaledRecipeOut(BaseModel):
    """Result of scaling a recipe by a factor."""

    recipe_id: uuid.UUID
    factor: float
    servings: ServingsOut | None = None
    groups: list[ScaledGroupOut] = []
    steps: list[StepOut] = []
    step_text_disclaimer: bool = True


# ---------------------------------------------------------------------------
# Core scaling logic
# ---------------------------------------------------------------------------


def _build_scaled_display(
    *,
    quantity_display: str,
    unit: str | None,
    normalized_amount: float | None,
    normalized_unit: str | None,
    is_approx: bool,
) -> str:
    """Reconstruct the display string for a scaled line.

    Rules:
    - quantity + unit + normalized → ``"{qd} {unit} → {~?}{amt} {nu}{ (approx.)?}"``
    - quantity + unit, no normalized  → ``"{qd} {unit}"``
    - quantity only (no unit)         → ``"{qd}"``
    """
    left = f"{quantity_display} {unit}" if unit is not None else quantity_display

    if normalized_amount is None:
        return left

    prefix = "~" if is_approx else ""
    suffix = " (approx.)" if is_approx else ""
    return f"{left} → {prefix}{format_amount(normalized_amount)} {normalized_unit}{suffix}"


def _scale_line(line_out: IngredientLineOut, factor: float) -> ScaledLineOut:
    """Scale a single ``IngredientLineOut`` by *factor*.

    The *line_out* is treated as read-only; a new ``ScaledLineOut`` is
    returned.
    """
    original_text: str = line_out.original_text
    quantity: float | None = line_out.quantity
    unit: str | None = line_out.unit
    normalized_amount: float | None = line_out.normalized_amount
    normalized_unit: str | None = line_out.normalized_unit
    is_approx: bool = line_out.is_approx
    note: str | None = line_out.note
    is_optional: bool = line_out.is_optional

    if quantity is None:
        # Unconvertible / no-quantity line — pass through unchanged
        return ScaledLineOut(
            original_text=original_text,
            quantity_display=None,
            unit=unit,
            normalized_amount=normalized_amount,
            normalized_unit=normalized_unit,
            is_approx=is_approx,
            note=note,
            is_optional=is_optional,
            passes_through=True,
            display=original_text,
        )

    # Quantity is present — scale it
    scaled_qty = quantity * factor
    quantity_display = format_quantity(scaled_qty)

    scaled_normalized: float | None = None
    if normalized_amount is not None:
        scaled_normalized = normalized_amount * factor

    display = _build_scaled_display(
        quantity_display=quantity_display,
        unit=unit,
        normalized_amount=scaled_normalized,
        normalized_unit=normalized_unit,
        is_approx=is_approx,
    )

    return ScaledLineOut(
        original_text=original_text,
        quantity_display=quantity_display,
        unit=unit,
        normalized_amount=scaled_normalized,
        normalized_unit=normalized_unit,
        is_approx=is_approx,
        note=note,
        is_optional=is_optional,
        passes_through=False,
        display=display,
    )


def scale_recipe(recipe: RecipeOut, factor: float) -> ScaledRecipeOut:
    """Scale *recipe* by *factor*, returning a new DTO (no mutation).

    Args:
        recipe: The base ``RecipeOut`` to scale.
        factor: Multiplicative scale factor, must be in (0, 100].

    Returns:
        A ``ScaledRecipeOut`` with all amounts scaled deterministically.

    Raises:
        ValueError: If *factor* is ≤ 0 or > 100.
    """
    if factor <= 0:
        raise ValueError(f"factor must be > 0, got {factor}")
    if factor > 100:
        raise ValueError(f"factor must be ≤ 100, got {factor}")

    # Scale servings
    scaled_servings: ServingsOut | None = None
    if recipe.servings is not None:
        base_amount = recipe.servings.amount
        scaled_servings = ServingsOut(
            amount=(base_amount * factor) if base_amount is not None else None,
            unit_text=recipe.servings.unit_text,
        )

    # Scale groups
    scaled_groups: list[ScaledGroupOut] = []
    for group in recipe.groups:
        scaled_lines = [_scale_line(ln, factor) for ln in group.lines]
        scaled_groups.append(ScaledGroupOut(name=group.name, lines=scaled_lines))

    return ScaledRecipeOut(
        recipe_id=recipe.id,
        factor=factor,
        servings=scaled_servings,
        groups=scaled_groups,
        steps=list(recipe.steps),  # verbatim copy, no mutation
        step_text_disclaimer=True,
    )
