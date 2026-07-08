"""Sharing API routes: copy-on-share, public links, and shared cookbooks.

Three routers live here:

- ``router`` (prefix ``/api/share``) — authenticated management endpoints,
  gated by ``get_current_user`` like every other cookbook route.
- ``shared_cookbooks_router`` (prefix ``/api/shared-cookbooks``) —
  authenticated shared-cookbook CRUD + membership + recipe-linking
  endpoints. Every endpoint below the create/list pair is member-gated and
  404s (never 403s) for non-members — see sharing/service.py for why.
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
    AddSharedCookbookRecipeIn,
    CreatePublicLinkIn,
    CreateSharedCookbookIn,
    InviteMemberIn,
    PublicLinkOut,
    PublicRecipeOut,
    SharedCookbookDetailOut,
    SharedCookbookMemberOut,
    SharedCookbookOut,
    SharedCookbookRecipeOut,
    ShareOut,
    ShareRecipeIn,
)

router = APIRouter(prefix="/api/share", tags=["sharing"])
shared_cookbooks_router = APIRouter(prefix="/api/shared-cookbooks", tags=["shared-cookbooks"])
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
# Shared cookbooks
# ---------------------------------------------------------------------------


@shared_cookbooks_router.post("", status_code=201, response_model=SharedCookbookOut)
def create_shared_cookbook(
    body: CreateSharedCookbookIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> SharedCookbookOut:
    return service.create_shared_cookbook(db, creator_id=current_user.id, name=body.name)


@shared_cookbooks_router.get("", response_model=list[SharedCookbookOut])
def list_shared_cookbooks(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[SharedCookbookOut]:
    return service.list_my_shared_cookbooks(db, user_id=current_user.id)


@shared_cookbooks_router.get("/{cookbook_id}", response_model=SharedCookbookDetailOut)
def get_shared_cookbook(
    cookbook_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> SharedCookbookDetailOut:
    return service.get_shared_cookbook(db, user_id=current_user.id, cookbook_id=cookbook_id)


@shared_cookbooks_router.post(
    "/{cookbook_id}/members", status_code=201, response_model=SharedCookbookMemberOut
)
def invite_member(
    cookbook_id: uuid.UUID,
    body: InviteMemberIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> SharedCookbookMemberOut:
    return service.invite_member(
        db, user_id=current_user.id, cookbook_id=cookbook_id, email=body.email
    )


@shared_cookbooks_router.delete("/{cookbook_id}/members/{user_id}", status_code=204)
def remove_member(
    cookbook_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    service.remove_member(
        db, user_id=current_user.id, cookbook_id=cookbook_id, target_user_id=user_id
    )
    return Response(status_code=204)


@shared_cookbooks_router.post(
    "/{cookbook_id}/recipes", status_code=201, response_model=SharedCookbookRecipeOut
)
def add_shared_cookbook_recipe(
    cookbook_id: uuid.UUID,
    body: AddSharedCookbookRecipeIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> SharedCookbookRecipeOut:
    return service.add_recipe_to_shared_cookbook(
        db, user_id=current_user.id, cookbook_id=cookbook_id, recipe_id=body.recipe_id
    )


@shared_cookbooks_router.delete("/{cookbook_id}/recipes/{recipe_id}", status_code=204)
def remove_shared_cookbook_recipe(
    cookbook_id: uuid.UUID,
    recipe_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    service.remove_recipe_from_shared_cookbook(
        db, user_id=current_user.id, cookbook_id=cookbook_id, recipe_id=recipe_id
    )
    return Response(status_code=204)


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
