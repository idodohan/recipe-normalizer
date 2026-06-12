"""Cookbook API routes: recipe CRUD, scaling, and vocab."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.cookbook import service
from recipe_normalizer.cookbook.models import Cuisine, DishType, SourceType, Tag
from recipe_normalizer.cookbook.scaling import ScaledRecipeOut, scale_factor_for, scale_recipe
from recipe_normalizer.cookbook.schemas import RecipeIn, RecipeOut, RecipeSummary
from recipe_normalizer.db import get_db
from recipe_normalizer.errors import ApiError

router = APIRouter(prefix="/api", tags=["recipes"])


# ---------------------------------------------------------------------------
# Recipe CRUD
# ---------------------------------------------------------------------------


@router.post("/recipes", status_code=201, response_model=RecipeOut)
def create_recipe(
    body: RecipeIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    return service.create_recipe(
        db,
        owner_id=current_user.id,
        data=body,
        source_type=SourceType.manual,
    )


@router.get("/recipes", response_model=list[RecipeSummary])
def list_recipes(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[RecipeSummary]:
    return service.list_recipes(db, owner_id=current_user.id)


@router.get("/recipes/{recipe_id}", response_model=RecipeOut)
def get_recipe(
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    return service.get_recipe(db, owner_id=current_user.id, recipe_id=recipe_id)


@router.patch("/recipes/{recipe_id}", response_model=RecipeOut)
def update_recipe(
    recipe_id: uuid.UUID,
    body: RecipeIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    return service.update_recipe(
        db,
        owner_id=current_user.id,
        recipe_id=recipe_id,
        data=body,
        editor_id=current_user.id,
    )


@router.delete("/recipes/{recipe_id}", status_code=204)
def delete_recipe(
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    service.delete_recipe(db, owner_id=current_user.id, recipe_id=recipe_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Scaling endpoint
# ---------------------------------------------------------------------------


@router.get("/recipes/{recipe_id}/scaled", response_model=ScaledRecipeOut)
def scale_recipe_endpoint(
    recipe_id: uuid.UUID,
    factor: float | None = Query(default=None, gt=0, le=100),
    target_servings: float | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> ScaledRecipeOut:
    # Exactly one param required
    if factor is not None and target_servings is not None:
        raise ApiError(
            422,
            "validation_error",
            "Provide exactly one of 'factor' or 'target_servings', not both.",
        )
    if factor is None and target_servings is None:
        raise ApiError(
            422,
            "validation_error",
            "Provide exactly one of 'factor' or 'target_servings'.",
        )

    recipe = service.get_recipe(db, owner_id=current_user.id, recipe_id=recipe_id)

    if target_servings is not None:
        if recipe.servings is None or recipe.servings.amount is None:
            raise ApiError(
                422,
                "validation_error",
                "Recipe has no servings_amount; cannot compute scale factor from target_servings.",
            )
        try:
            factor = scale_factor_for(
                base_servings=recipe.servings.amount,
                target_servings=target_servings,
            )
        except ValueError as exc:
            raise ApiError(422, "validation_error", str(exc)) from exc

    assert factor is not None
    try:
        return scale_recipe(recipe, factor)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc


# ---------------------------------------------------------------------------
# Vocab
# ---------------------------------------------------------------------------


@router.get("/vocab")
def get_vocab(
    db: Session = Depends(get_db),  # noqa: B008
    _current_user: Any = Depends(get_current_user),  # noqa: B008
) -> dict[str, list[str]]:
    cuisines = sorted(db.scalars(select(Cuisine.name).order_by(func.lower(Cuisine.name))).all())
    dish_types = sorted(db.scalars(select(DishType.name).order_by(func.lower(DishType.name))).all())
    tags = sorted(db.scalars(select(Tag.name).order_by(func.lower(Tag.name))).all())
    return {"cuisines": cuisines, "dish_types": dish_types, "tags": tags}
