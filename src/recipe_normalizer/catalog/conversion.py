import math
from dataclasses import dataclass
from typing import Protocol

from recipe_normalizer.catalog.units import UnitKind, parse_unit


class IngredientConversionData(Protocol):
    """The slice of CanonicalIngredient the converter needs (keeps this module ORM-free).

    All members are read-only properties so the ORM model (with ``Mapped[...]``
    attributes) structurally satisfies the protocol under mypy.
    """

    @property
    def preferred_measure(self) -> str: ...

    @property
    def density_g_per_ml(self) -> float | None: ...

    @property
    def gram_weights(self) -> dict[str, float]: ...


@dataclass(frozen=True)
class Converted:
    amount: float
    unit: str  # "g" | "ml"
    is_approx: bool


def _round2(x: float) -> float:
    return round(x, 2)


def convert_to_normalized(
    quantity: float | None, unit_text: str | None, ingredient: IngredientConversionData
) -> Converted | None:
    if quantity is None or unit_text is None:
        return None
    if quantity <= 0 or math.isnan(quantity):
        return None
    unit = parse_unit(unit_text)
    if unit is None or unit.kind is UnitKind.ratio:
        return None

    target_mass = ingredient.preferred_measure == "mass"
    density = ingredient.density_g_per_ml

    if unit.kind is UnitKind.count:
        grams_each = ingredient.gram_weights.get(unit.token)
        if grams_each is None:
            return None
        grams = quantity * grams_each
        if target_mass:
            return Converted(_round2(grams), "g", is_approx=True)
        if not (density and density > 0):
            return None
        return Converted(_round2(grams / density), "ml", is_approx=True)

    if unit.kind is UnitKind.mass:
        grams = quantity * unit.base_factor
        if target_mass:
            return Converted(_round2(grams), "g", is_approx=unit.is_inherently_approx)
        if not (density and density > 0):
            return None
        return Converted(_round2(grams / density), "ml", is_approx=True)

    # volume
    ml = quantity * unit.base_factor
    if not target_mass:
        return Converted(_round2(ml), "ml", is_approx=unit.is_inherently_approx)
    grams_per_unit = ingredient.gram_weights.get(unit.token)
    if grams_per_unit is not None:
        return Converted(_round2(quantity * grams_per_unit), "g", is_approx=True)
    if density is not None and density > 0:
        return Converted(_round2(ml * density), "g", is_approx=True)
    return None
