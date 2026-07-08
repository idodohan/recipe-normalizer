"""Cookbook API routes: recipe CRUD, scaling, and vocab."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.cookbook import service
from recipe_normalizer.cookbook.models import Cuisine, DishType, SourceType, Tag
from recipe_normalizer.cookbook.scaling import ScaledRecipeOut, scale_factor_for, scale_recipe
from recipe_normalizer.cookbook.schemas import (
    CollectionIn,
    CollectionOut,
    RecipeIn,
    RecipeOut,
    RecipePage,
    RecipePersonalPatch,
    SetRecipeCollectionsIn,
)
from recipe_normalizer.db import get_db
from recipe_normalizer.errors import ApiError
from recipe_normalizer.filestore import FileStore, get_file_store

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


@router.get("/recipes", response_model=RecipePage)
def list_recipes(
    q: str | None = Query(default=None, max_length=200),
    cuisine: str | None = Query(default=None, max_length=100),
    dish_type: str | None = Query(default=None, max_length=100),
    tag: str | None = Query(default=None, max_length=100),
    dietary: Literal["vegan", "vegetarian", "gluten_free"] | None = Query(default=None),
    max_total_min: int | None = Query(default=None, ge=0),
    source_type: SourceType | None = Query(default=None),  # noqa: B008
    favorites: bool | None = Query(default=None),
    collection: uuid.UUID | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=1000, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipePage:
    return service.list_recipes(
        db,
        owner_id=current_user.id,
        q=q,
        cuisine=cuisine,
        dish_type=dish_type,
        tag=tag,
        dietary=dietary,
        max_total_min=max_total_min,
        source_type=source_type,
        favorites=favorites,
        collection=collection,
        limit=limit,
        offset=offset,
    )


_MEMBER_SCRUBBED_FIELDS: dict[str, Any] = {
    "notes": None,
    "is_favorite": False,
    "collection_ids": [],
    "provenance": None,
    "extraction_meta": None,
}


def _scrub_for_member(recipe: RecipeOut, current_user_id: uuid.UUID) -> RecipeOut:
    """Scrub owner-only personal fields from a recipe response if the caller
    is a shared-cookbook member (not the owner).

    A member must never see the OWNER's notes/is_favorite/collection_ids/provenance/
    extraction_meta in any response, whether from GET or PATCH. This mirrors the
    anonymous PublicRecipeOut, which already drops extraction_meta so outsiders
    can't see the owner's ingestion internals (tier_used, confidence, etc.).
    This helper is applied to both response paths to ensure consistent redaction.
    """
    if recipe.owner_id != current_user_id:
        recipe = recipe.model_copy(update=_MEMBER_SCRUBBED_FIELDS)
    return recipe


@router.get("/recipes/{recipe_id}", response_model=RecipeOut)
def get_recipe(
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    """Fetch a recipe. ``service.get_recipe`` is widened to shared-cookbook
    members, but a member must never see the OWNER's personal
    notes/favorites/collections or the owner-facing provenance — those are
    scrubbed here for anyone who isn't the recipe's owner.

    No extra access-check call is needed: ``service.get_recipe`` already
    raises 404 unless the caller is the owner or a shared-cookbook member,
    and the returned ``RecipeOut.owner_id`` tells us which of those two it
    was — a member is exactly the case where ``owner_id != current_user.id``.
    """
    recipe = service.get_recipe(db, owner_id=current_user.id, recipe_id=recipe_id)
    return _scrub_for_member(recipe, current_user.id)


@router.patch("/recipes/{recipe_id}", response_model=RecipeOut)
def update_recipe(
    recipe_id: uuid.UUID,
    body: RecipeIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    recipe = service.update_recipe(
        db,
        owner_id=current_user.id,
        recipe_id=recipe_id,
        data=body,
        editor_id=current_user.id,
    )
    return _scrub_for_member(recipe, current_user.id)


@router.patch("/recipes/{recipe_id}/personal", response_model=RecipeOut)
def update_recipe_personal(
    recipe_id: uuid.UUID,
    body: RecipePersonalPatch,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    return service.set_personal(
        db,
        owner_id=current_user.id,
        recipe_id=recipe_id,
        is_favorite=body.is_favorite if body.is_favorite is not None else service.UNSET,
        # model_fields_set distinguishes an absent field from an explicit
        # null: {"notes": null} clears the value.
        notes=body.notes if "notes" in body.model_fields_set else service.UNSET,
    )


@router.delete("/recipes/{recipe_id}", status_code=204)
def delete_recipe(
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    service.delete_recipe(db, owner_id=current_user.id, recipe_id=recipe_id)
    return Response(status_code=204)


@router.put("/recipes/{recipe_id}/collections", response_model=RecipeOut)
def set_recipe_collections(
    recipe_id: uuid.UUID,
    body: SetRecipeCollectionsIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    """Full-replace the set of collections this recipe belongs to.

    Returns the updated RecipeOut (rather than 204) so the client can render
    the new collection_ids without a follow-up GET.
    """
    return service.set_recipe_collections(
        db,
        owner_id=current_user.id,
        recipe_id=recipe_id,
        collection_ids=body.collection_ids,
    )


# ---------------------------------------------------------------------------
# Recipe image
# ---------------------------------------------------------------------------


@router.put("/recipes/{recipe_id}/image", response_model=RecipeOut)
async def upload_recipe_image(
    recipe_id: uuid.UUID,
    file: UploadFile,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
    store: FileStore = Depends(get_file_store),  # noqa: B008
) -> RecipeOut:
    # Bounded read: never buffer more than the limit + 1 byte. If we got the
    # extra byte the body exceeds the limit and the service rejects it.
    data = await file.read(service.MAX_IMAGE_BYTES + 1)
    return service.set_recipe_image(
        db,
        owner_id=current_user.id,
        recipe_id=recipe_id,
        data=data,
        store=store,
    )


@router.delete("/recipes/{recipe_id}/image", response_model=RecipeOut)
def delete_recipe_image(
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> RecipeOut:
    return service.clear_recipe_image(db, owner_id=current_user.id, recipe_id=recipe_id)


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
# Collections
# ---------------------------------------------------------------------------


@router.get("/collections", response_model=list[CollectionOut])
def list_collections(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[CollectionOut]:
    return service.list_collections(db, owner_id=current_user.id)


@router.post("/collections", status_code=201, response_model=CollectionOut)
def create_collection(
    body: CollectionIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CollectionOut:
    return service.create_collection(db, owner_id=current_user.id, name=body.name)


@router.patch("/collections/{collection_id}", response_model=CollectionOut)
def rename_collection(
    collection_id: uuid.UUID,
    body: CollectionIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CollectionOut:
    return service.rename_collection(
        db, owner_id=current_user.id, collection_id=collection_id, name=body.name
    )


@router.delete("/collections/{collection_id}", status_code=204)
def delete_collection(
    collection_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    service.delete_collection(db, owner_id=current_user.id, collection_id=collection_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Vocab
# ---------------------------------------------------------------------------


@router.get("/vocab")
def get_vocab(
    db: Session = Depends(get_db),  # noqa: B008
    _current_user: Any = Depends(get_current_user),  # noqa: B008
) -> dict[str, list[str]]:
    cuisines = list(db.scalars(select(Cuisine.name).order_by(func.lower(Cuisine.name))).all())
    dish_types = list(db.scalars(select(DishType.name).order_by(func.lower(DishType.name))).all())
    tags = list(db.scalars(select(Tag.name).order_by(func.lower(Tag.name))).all())
    return {"cuisines": cuisines, "dish_types": dish_types, "tags": tags}
