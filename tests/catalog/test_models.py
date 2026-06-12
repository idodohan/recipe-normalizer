"""Tests for catalog models: CanonicalIngredient and IngredientAlias."""

from sqlalchemy.orm import Session

from recipe_normalizer.catalog.models import (
    CanonicalIngredient,
    IngredientAlias,
    IngredientStatus,
    PreferredMeasure,
)


def test_canonical_ingredient_with_aliases(db_session: Session) -> None:
    ingredient = CanonicalIngredient(
        name="all-purpose flour",
        category="baking",
        preferred_measure=PreferredMeasure.mass,
        status=IngredientStatus.seeded,
        dietary_flags=["contains-gluten"],
        density_g_per_ml=0.53,
        gram_weights={"cup": 120, "tbsp": 8},
    )
    db_session.add(ingredient)
    db_session.flush()

    alias_en = IngredientAlias(
        ingredient_id=ingredient.id,
        alias="AP flour",
        language="en",
    )
    alias_he = IngredientAlias(
        ingredient_id=ingredient.id,
        alias="קמח לבן",
        language="he",
    )
    db_session.add_all([alias_en, alias_he])
    db_session.flush()
    db_session.refresh(ingredient)

    assert len(ingredient.aliases) == 2

    # JSONB round-trip: dietary_flags
    assert ingredient.dietary_flags == ["contains-gluten"]

    # JSONB round-trip: gram_weights values
    assert ingredient.gram_weights["cup"] == 120
    assert ingredient.gram_weights["tbsp"] == 8

    # merged_into_id default
    assert ingredient.merged_into_id is None
