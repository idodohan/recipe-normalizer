"""Sharing API routes: copy-on-share and public links.

Two routers live here:

- ``router`` (prefix ``/api/share``) — authenticated management endpoints,
  gated by ``get_current_user`` like every other cookbook route.
- ``public_router`` (prefix ``/api/public``) — the unauthenticated, token-only
  surface. These routes deliberately carry NO ``get_current_user`` dependency
  anywhere in their signature — see ``tests/sharing/test_router.py`` for a
  test that asserts this by inspecting the route's dependants.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user, limit_by_ip, limit_by_user
from recipe_normalizer.cookbook.scaling import ScaledRecipeOut, scale_factor_for, scale_recipe
from recipe_normalizer.db import get_db
from recipe_normalizer.errors import ApiError
from recipe_normalizer.sharing import service
from recipe_normalizer.sharing.schemas import (
    CreatePublicLinkIn,
    PublicLinkOut,
    PublicRecipeOut,
    ShareOut,
    ShareRecipeIn,
)

router = APIRouter(prefix="/api/share", tags=["sharing"])
public_router = APIRouter(prefix="/api/public", tags=["public"])

_share_limit = limit_by_user("share", 30, 3600.0)
_public_limit = limit_by_ip("public", 60, 60.0)


@router.post("/recipe", status_code=201, response_model=ShareOut)
def share_recipe(
    body: ShareRecipeIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
    _: None = Depends(_share_limit),  # noqa: B008
) -> ShareOut:
    return service.share_recipe(
        db,
        from_user_id=current_user.id,
        from_email=current_user.email,
        recipe_id=body.recipe_id,
        to_email=body.to_email,
    )


# ---------------------------------------------------------------------------
# Public links — authenticated management
# ---------------------------------------------------------------------------


@router.post("/public", status_code=201, response_model=PublicLinkOut)
def create_public_link(
    body: CreatePublicLinkIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> PublicLinkOut:
    return service.create_public_link(db, owner_id=current_user.id, recipe_id=body.recipe_id)


@router.get("/public", response_model=list[PublicLinkOut])
def list_public_links(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[PublicLinkOut]:
    return service.list_public_links(db, owner_id=current_user.id)


@router.delete("/public/{link_id}", status_code=204)
def revoke_public_link(
    link_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    service.revoke_public_link(db, owner_id=current_user.id, link_id=link_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Public links — unauthenticated public surface
# ---------------------------------------------------------------------------


@public_router.get("/{token}", response_model=PublicRecipeOut)
def get_public_recipe(
    token: str,
    db: Session = Depends(get_db),  # noqa: B008
    _: None = Depends(_public_limit),  # noqa: B008
) -> PublicRecipeOut:
    return service.get_public_recipe(db, token=token)


@public_router.get("/{token}/scaled", response_model=ScaledRecipeOut)
def get_public_recipe_scaled(
    token: str,
    factor: float | None = Query(default=None, gt=0, le=100),
    target_servings: float | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),  # noqa: B008
    _: None = Depends(_public_limit),  # noqa: B008
) -> ScaledRecipeOut:
    # Exactly one param required — mirrors the authenticated scaled endpoint.
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

    recipe = service.get_public_recipe_for_scaling(db, token=token)

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
