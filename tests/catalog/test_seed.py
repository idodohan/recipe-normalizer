"""Tests for the canonical ingredient seed data and its idempotent loader."""

import json
from importlib import resources
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.catalog.models import (
    CanonicalIngredient,
    IngredientAlias,
    IngredientStatus,
    PreferredMeasure,
)
from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.catalog.units import parse_unit


def _raw_entries() -> list[dict[str, Any]]:
    text = (
        resources.files("recipe_normalizer.catalog.seed")
        .joinpath("ingredients.json")
        .read_text(encoding="utf-8")
    )
    data = json.loads(text)
    assert isinstance(data, list)
    return data


def test_seed_file_has_no_duplicate_names() -> None:
    names = [entry["name"] for entry in _raw_entries()]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"duplicate names in ingredients.json: {duplicates}"


def test_load_seed_inserts_at_least_200(db_session: Session) -> None:
    inserted = load_seed(db_session)
    assert inserted >= 200
    total = db_session.scalar(select(func.count()).select_from(CanonicalIngredient))
    assert total == inserted
    statuses = set(db_session.scalars(select(CanonicalIngredient.status)).all())
    assert statuses == {IngredientStatus.seeded}


def test_load_seed_is_idempotent(db_session: Session) -> None:
    first = load_seed(db_session)
    second = load_seed(db_session)
    assert second == 0
    total = db_session.scalar(select(func.count()).select_from(CanonicalIngredient))
    assert total == first


def test_ap_flour_alias_resolves_to_all_purpose_flour(db_session: Session) -> None:
    load_seed(db_session)
    ingredient = db_session.scalars(
        select(CanonicalIngredient).join(IngredientAlias).where(IngredientAlias.alias == "AP flour")
    ).one()
    assert ingredient.name == "all-purpose flour"


def test_ground_beef_exists_and_is_mass_preferred(db_session: Session) -> None:
    load_seed(db_session)
    beef = db_session.scalars(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "ground beef")
    ).one()
    assert beef.preferred_measure == PreferredMeasure.mass


def test_gin_is_a_volume_alcohol_with_density(db_session: Session) -> None:
    load_seed(db_session)
    gin = db_session.scalars(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "gin")
    ).one()
    assert gin.preferred_measure == PreferredMeasure.volume
    assert "alcohol" in gin.dietary_flags
    assert gin.density_g_per_ml is not None


def test_every_gram_weight_key_is_a_canonical_unit_token(db_session: Session) -> None:
    load_seed(db_session)
    for ingredient in db_session.scalars(select(CanonicalIngredient)):
        for key, grams in ingredient.gram_weights.items():
            unit = parse_unit(key)
            assert unit is not None, f"{ingredient.name}: unknown gram_weights key {key!r}"
            assert unit.token == key, (
                f"{ingredient.name}: gram_weights key {key!r} is not the canonical "
                f"token {unit.token!r}"
            )
            assert grams > 0, f"{ingredient.name}: non-positive gram weight for {key!r}"


def test_every_volume_preferred_ingredient_has_density(db_session: Session) -> None:
    load_seed(db_session)
    liquids = db_session.scalars(
        select(CanonicalIngredient).where(
            CanonicalIngredient.preferred_measure == PreferredMeasure.volume
        )
    ).all()
    assert liquids, "expected at least one volume-preferred ingredient"
    missing = [i.name for i in liquids if i.density_g_per_ml is None]
    assert not missing, f"volume-preferred ingredients without density: {missing}"


def test_at_least_35_ingredients_have_a_hebrew_alias(db_session: Session) -> None:
    load_seed(db_session)
    distinct_with_hebrew = db_session.scalar(
        select(func.count(func.distinct(IngredientAlias.ingredient_id))).where(
            IngredientAlias.language == "he"
        )
    )
    assert distinct_with_hebrew is not None
    assert distinct_with_hebrew >= 35
