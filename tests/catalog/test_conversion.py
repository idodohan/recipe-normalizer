import math

from recipe_normalizer.catalog.conversion import Converted, convert_to_normalized
from recipe_normalizer.catalog.models import (
    CanonicalIngredient,
    IngredientStatus,
    PreferredMeasure,
)


def ing(preferred="mass", density=None, gram_weights=None):
    """Minimal stand-in satisfying the IngredientConversionData protocol."""
    _gw = gram_weights or {}

    class _I:
        preferred_measure = preferred
        density_g_per_ml = density

        @property
        def gram_weights(self):
            return _gw

    return _I()


def test_solid_given_in_grams_is_exact():
    assert convert_to_normalized(500, "g", ing("mass")) == Converted(500, "g", is_approx=False)


def test_solid_given_in_kg_is_exact():
    assert convert_to_normalized(1.5, "kg", ing("mass")) == Converted(1500, "g", is_approx=False)


def test_liquid_given_in_volume_is_exact():
    assert convert_to_normalized(1, "fl oz", ing("volume")) == Converted(
        29.57, "ml", is_approx=False
    )


def test_solid_from_cups_uses_gram_weights_and_flags_approx():
    flour = ing("mass", density=0.53, gram_weights={"cup": 120})
    assert convert_to_normalized(1, "cup", flour) == Converted(120, "g", is_approx=True)


def test_gram_weights_take_precedence_over_density():
    flour = ing("mass", density=0.53, gram_weights={"cup": 120})
    out = convert_to_normalized(2, "cup", flour)
    assert out is not None and out.amount == 240  # 2*120, not 2*236.588*0.53


def test_solid_from_volume_falls_back_to_density():
    sugar = ing("mass", density=0.85)
    out = convert_to_normalized(2, "tbsp", sugar)
    assert out is not None and out.unit == "g" and out.is_approx is True
    assert abs(out.amount - 2 * 14.7868 * 0.85) < 0.01


def test_count_unit_uses_gram_weights():
    egg = ing("mass", gram_weights={"unit": 50})
    assert convert_to_normalized(3, "unit", egg) == Converted(150, "g", is_approx=True)


def test_named_count_unit():
    garlic = ing("mass", gram_weights={"clove": 5})
    assert convert_to_normalized(4, "cloves", garlic) == Converted(20, "g", is_approx=True)


def test_liquid_given_in_mass_converts_via_density():
    honey = ing("volume", density=1.42)
    out = convert_to_normalized(100, "g", honey)
    assert out is not None and out.unit == "ml" and out.is_approx is True
    assert abs(out.amount - 100 / 1.42) < 0.01


def test_unconvertible_returns_none():
    assert convert_to_normalized(None, None, ing("mass")) is None  # "to taste"
    assert convert_to_normalized(1, None, ing("mass")) is None
    assert convert_to_normalized(1, "part", ing("volume")) is None  # ratio recipes
    assert convert_to_normalized(2, "clove", ing("mass")) is None  # no gram weight known
    assert convert_to_normalized(1, "cup", ing("mass")) is None  # solid, no density/gw
    assert convert_to_normalized(100, "g", ing("volume")) is None  # liquid, no density
    assert convert_to_normalized(1, "nonsense-unit", ing("mass")) is None


def test_inherently_approx_units_flagged():
    bitters = ing("volume")
    assert convert_to_normalized(2, "dash", bitters) == Converted(1.84, "ml", is_approx=True)


def test_solid_pinch_is_approx_mass():
    salt = ing("mass")
    out = convert_to_normalized(1, "pinch", salt)
    assert out == Converted(0.36, "g", is_approx=True)


def test_rounding_two_decimals():
    out = convert_to_normalized(1, "tsp", ing("volume"))
    assert out is not None and out.amount == 4.93


def test_real_canonical_ingredient_satisfies_protocol():
    """Typing regression: the ORM model must structurally satisfy the protocol.

    No DB needed — mypy checking this call enforces the structural match.
    """
    flour = CanonicalIngredient(
        name="flour",
        category="baking",
        preferred_measure=PreferredMeasure.mass,
        status=IngredientStatus.seeded,
        dietary_flags=[],
        density_g_per_ml=0.53,
        gram_weights={"cup": 120},
    )
    assert convert_to_normalized(1, "cup", flour) == Converted(120, "g", is_approx=True)


def test_zero_density_yields_none_not_division_error():
    # Explicit density=0.0 is invalid data and must be treated as missing (no division by zero).
    # Mass to volume conversion with 0.0 density
    assert convert_to_normalized(100, "g", ing("volume", density=0.0)) is None
    # Count to volume with 0.0 density (even with gram_weights)
    egg = ing("volume", density=0.0, gram_weights={"unit": 50})
    assert convert_to_normalized(2, "unit", egg) is None


def test_none_density_yields_none():
    # density=None (missing data) yields None conversion for volume targets
    assert convert_to_normalized(100, "g", ing("volume", density=None)) is None


def test_positive_density_converts():
    # Positive density converts correctly
    out = convert_to_normalized(100, "g", ing("volume", density=1.42))
    assert out is not None
    assert out.unit == "ml"
    assert out.is_approx is True
    assert abs(out.amount - 100 / 1.42) < 0.01


def test_non_positive_or_nan_quantity_returns_none():
    assert convert_to_normalized(0, "g", ing("mass")) is None
    assert convert_to_normalized(-5, "g", ing("mass")) is None
    assert convert_to_normalized(math.nan, "g", ing("mass")) is None


def test_count_to_ml_via_gram_weights_and_density():
    egg = ing("volume", density=1.0, gram_weights={"unit": 50})
    assert convert_to_normalized(2, "unit", egg) == Converted(100, "ml", is_approx=True)


def test_count_to_ml_without_density_returns_none():
    egg = ing("volume", gram_weights={"unit": 50})
    assert convert_to_normalized(2, "unit", egg) is None
