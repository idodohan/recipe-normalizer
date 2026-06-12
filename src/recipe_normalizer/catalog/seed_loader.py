"""Idempotent loader for the canonical-ingredient seed data.

Reads ``catalog/seed/ingredients.json`` via :mod:`importlib.resources`,
validates every entry with a Pydantic model (catching typos before any
insert), and upserts by name: ingredients whose name already exists are
skipped entirely, so repeated runs are safe.

Run directly to seed the configured database::

    python -m recipe_normalizer.catalog.seed_loader
"""

import json
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.catalog.models import (
    CanonicalIngredient,
    IngredientAlias,
    IngredientStatus,
    PreferredMeasure,
)
from recipe_normalizer.catalog.units import parse_unit

ALLOWED_DIETARY_FLAGS = frozenset(
    {
        "contains-gluten",
        "dairy",
        "egg",
        "animal-product",
        "fish",
        "shellfish",
        "nut",
        "peanut",
        "soy",
        "sesame",
        "alcohol",
    }
)


class SeedAlias(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=2, max_length=10)  # BCP-47


class SeedIngredient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    aliases: list[SeedAlias] = Field(default_factory=list)
    category: str = Field(min_length=1, max_length=50)
    preferred_measure: PreferredMeasure
    dietary_flags: list[str] = Field(default_factory=list)
    density_g_per_ml: float | None = Field(default=None, gt=0)
    gram_weights: dict[str, float] = Field(default_factory=dict)

    @field_validator("dietary_flags")
    @classmethod
    def _flags_in_vocabulary(cls, value: list[str]) -> list[str]:
        unknown = set(value) - ALLOWED_DIETARY_FLAGS
        if unknown:
            raise ValueError(f"unknown dietary flags: {sorted(unknown)}")
        return value

    @field_validator("gram_weights")
    @classmethod
    def _keys_are_canonical_unit_tokens(cls, value: dict[str, float]) -> dict[str, float]:
        for key, grams in value.items():
            unit = parse_unit(key)
            if unit is None or unit.token != key:
                raise ValueError(f"gram_weights key {key!r} is not a canonical unit token")
            if grams <= 0:
                raise ValueError(f"gram_weights[{key!r}] must be positive, got {grams}")
        return value


def _read_seed_entries() -> list[SeedIngredient]:
    text = (
        resources.files("recipe_normalizer.catalog.seed")
        .joinpath("ingredients.json")
        .read_text(encoding="utf-8")
    )
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("ingredients.json must contain a JSON array")
    entries = [SeedIngredient.model_validate(item) for item in raw]
    names = [entry.name for entry in entries]
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"duplicate ingredient names in seed data: {duplicates}")
    return entries


def load_seed(session: Session) -> int:
    """Insert seed ingredients that do not yet exist; return the inserted count.

    Existing names are skipped entirely (their data is never overwritten), so
    calling this repeatedly is idempotent. The caller owns the transaction;
    this function only flushes.
    """
    entries = _read_seed_entries()
    existing_names = set(session.scalars(select(CanonicalIngredient.name)).all())
    inserted = 0
    for entry in entries:
        if entry.name in existing_names:
            continue
        session.add(
            CanonicalIngredient(
                name=entry.name,
                category=entry.category,
                preferred_measure=entry.preferred_measure,
                status=IngredientStatus.seeded,
                dietary_flags=entry.dietary_flags,
                density_g_per_ml=entry.density_g_per_ml,
                gram_weights=entry.gram_weights,
                aliases=[
                    IngredientAlias(alias=alias.alias, language=alias.language)
                    for alias in entry.aliases
                ],
            )
        )
        inserted += 1
    session.flush()
    return inserted


def main() -> None:
    from recipe_normalizer.db import SessionLocal

    with SessionLocal() as session:
        count = load_seed(session)
        session.commit()
    print(f"Inserted {count} canonical ingredients")


if __name__ == "__main__":
    main()
