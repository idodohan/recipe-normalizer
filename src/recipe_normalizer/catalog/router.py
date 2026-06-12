"""Catalog API routes: ingredient listing, get, patch, and merge."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.catalog import service
from recipe_normalizer.catalog.models import IngredientStatus
from recipe_normalizer.catalog.schemas import IngredientOut, IngredientPatch, MergeIn
from recipe_normalizer.db import get_db

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/ingredients", response_model=list[IngredientOut])
def list_ingredients(
    q: str = "",
    status: IngredientStatus | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    _user: object = Depends(get_current_user),  # noqa: B008
) -> list[IngredientOut]:
    results = service.search(db, q, status=status)
    return [IngredientOut.model_validate(r) for r in results]


@router.get("/ingredients/{ingredient_id}", response_model=IngredientOut)
def get_ingredient(
    ingredient_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    _user: object = Depends(get_current_user),  # noqa: B008
) -> IngredientOut:
    ing = service.get_ingredient(db, ingredient_id)
    return IngredientOut.model_validate(ing)


@router.patch("/ingredients/{ingredient_id}", response_model=IngredientOut)
def patch_ingredient(
    ingredient_id: uuid.UUID,
    body: IngredientPatch,
    db: Session = Depends(get_db),  # noqa: B008
    _user: object = Depends(get_current_user),  # noqa: B008
) -> IngredientOut:
    ing = service.update_ingredient(
        db,
        ingredient_id,
        name=body.name,
        category=body.category,
        preferred_measure=body.preferred_measure,
        status=body.status,
        dietary_flags=body.dietary_flags,
        density_g_per_ml=body.density_g_per_ml
        if body.density_g_per_ml is not None
        else service._SENTINEL,
        gram_weights=body.gram_weights,
    )
    db.commit()
    return IngredientOut.model_validate(ing)


@router.post("/ingredients/{ingredient_id}/merge", response_model=IngredientOut)
def merge_ingredient(
    ingredient_id: uuid.UUID,
    body: MergeIn,
    db: Session = Depends(get_db),  # noqa: B008
    _user: object = Depends(get_current_user),  # noqa: B008
) -> IngredientOut:
    target = service.merge(db, source_id=ingredient_id, target_id=body.target_id)
    db.commit()
    return IngredientOut.model_validate(target)
