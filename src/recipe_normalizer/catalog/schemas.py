"""Pydantic schemas for catalog API request/response bodies."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from recipe_normalizer.catalog.models import IngredientStatus, PreferredMeasure


class AliasOut(BaseModel):
    alias: str
    language: str

    model_config = {"from_attributes": True}


class IngredientOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    preferred_measure: PreferredMeasure
    status: IngredientStatus
    dietary_flags: list[str]
    density_g_per_ml: float | None
    gram_weights: dict[str, float]
    aliases: list[AliasOut]

    model_config = {"from_attributes": True}


class IngredientPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    preferred_measure: PreferredMeasure | None = None
    status: IngredientStatus | None = None
    dietary_flags: list[str] | None = None
    density_g_per_ml: float | None = None
    gram_weights: dict[str, float] | None = None


class MergeIn(BaseModel):
    target_id: uuid.UUID
