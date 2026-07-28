"""Cookbook API routes: CRUD, visibility, and membership (Task 6).

Kept in its own module rather than growing ``cookbook/router.py`` (which owns
recipe/placement/vocab routes) further. Registered separately in main.py.
Every route here delegates to ``cookbook.service`` — the access-control
choke point (``require_cookbook_access``/``cookbook_access``) and every
ApiError this module can raise (404/409/422) already live there; this file
only wires HTTP verbs/paths/DTOs onto it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recipe_normalizer.api_deps import get_current_user
from recipe_normalizer.cookbook import service
from recipe_normalizer.cookbook.models import (
    Cookbook,
    CookbookRecipe,
    CookbookRole,
    CookbookVisibility,
    Recipe,
)
from recipe_normalizer.cookbook.schemas import (
    CookbookDetailOut,
    CookbookIn,
    CookbookMemberIn,
    CookbookMemberOut,
    CookbookMemberRoleIn,
    CookbookOut,
    CookbookPatchIn,
    CookbookSummary,
)
from recipe_normalizer.db import get_db

router = APIRouter(prefix="/api/cookbooks", tags=["cookbooks"])


# ---------------------------------------------------------------------------
# DTO builders
# ---------------------------------------------------------------------------


def _recipe_count(db: Session, cookbook_id: uuid.UUID) -> int:
    """Number of recipes PLACED in *cookbook_id* — one count over the boards join.

    ``cookbook_recipes`` is the sole source of placement, and its composite PK
    already guarantees one row per (cookbook, recipe) pair, so a plain COUNT is
    exact. A recipe placed here via ``add_recipe_to_cookbook`` counts the same
    as one created here.
    """
    return (
        db.scalar(
            select(func.count())
            .select_from(CookbookRecipe)
            .where(CookbookRecipe.cookbook_id == cookbook_id)
        )
        or 0
    )


def _to_cookbook_out(db: Session, cookbook: Cookbook, *, user_id: uuid.UUID) -> CookbookOut:
    """Build the CookbookOut response shape for an already-loaded, access-checked cookbook.

    ``public_token`` is deliberately owner-only: for an unlisted cookbook the
    token IS the shareable secret (anyone holding it can read the cookbook
    anonymously, forever, without being a member), and only the owner may
    decide who gets it. Anyone else who reaches this DTO — an editor/viewer
    member, or a non-member who resolved to viewer because the cookbook is
    unlisted/public — sees None.
    """
    access = service.cookbook_access(db, user_id=user_id, cookbook_id=cookbook.id)
    assert access is not None  # caller already established access
    is_owner = access == "owner"
    return CookbookOut(
        id=cookbook.id,
        name=cookbook.name,
        description=cookbook.description,
        visibility=str(cookbook.visibility),
        role=str(access),
        recipe_count=_recipe_count(db, cookbook.id),
        is_default=cookbook.is_default,
        cover_image_ref=cookbook.cover_image_ref,
        public_token=cookbook.public_token if is_owner else None,
    )


# ---------------------------------------------------------------------------
# Cookbook CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[CookbookSummary])
def list_cookbooks(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> list[CookbookSummary]:
    """List every cookbook the caller has a claim on: owned + member-of.

    Ensures the caller's default cookbook exists first (a brand-new user who
    hasn't created a recipe yet would otherwise see an empty list). Ordering:
    owned cookbooks before shared ones — already the order
    ``service.list_my_cookbooks`` returns, since it appends owned rows before
    member rows — then alphabetically by name (case-insensitive) within each
    group; the service does not sort, so that secondary sort happens here.
    """
    service.ensure_default_cookbook(db, current_user.id)
    summaries = service.list_my_cookbooks(db, current_user.id)
    owned = sorted((s for s in summaries if s.role == "owner"), key=lambda s: s.name.lower())
    shared = sorted((s for s in summaries if s.role != "owner"), key=lambda s: s.name.lower())
    return owned + shared


@router.post("", status_code=201, response_model=CookbookOut)
def create_cookbook(
    body: CookbookIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CookbookOut:
    cookbook = service.create_cookbook(
        db, owner_id=current_user.id, name=body.name, description=body.description
    )
    return _to_cookbook_out(db, cookbook, user_id=current_user.id)


@router.get("/{cookbook_id}", response_model=CookbookDetailOut)
def get_cookbook(
    cookbook_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CookbookDetailOut:
    """Fetch a cookbook + its recipes. Viewer+ access required (404 otherwise).

    Recipes come from the ``cookbook_recipes`` join, the sole source of
    placement — so a recipe merely PLACED into this cookbook via
    ``add_recipe_to_cookbook`` shows up exactly like one created here.
    """
    cookbook = service.require_cookbook_access(
        db, user_id=current_user.id, cookbook_id=cookbook_id, need=CookbookRole.viewer
    )
    recipe_ids = db.scalars(
        select(Recipe.id)
        .join(CookbookRecipe, CookbookRecipe.recipe_id == Recipe.id)
        .where(CookbookRecipe.cookbook_id == cookbook.id)
        # Total order — see list_recipes' ORDER BY comment. Unpaginated here,
        # so ties could only jitter the order between requests, but the two
        # recipe listings should sort identically.
        .order_by(Recipe.created_at.desc(), Recipe.id.desc())
    ).all()
    summaries = service.recipe_summaries_for_ids(db, list(recipe_ids), viewer_id=current_user.id)
    recipes = [summaries[rid] for rid in recipe_ids if rid in summaries]
    base = _to_cookbook_out(db, cookbook, user_id=current_user.id)
    return CookbookDetailOut(**base.model_dump(), recipes=recipes)


@router.patch("/{cookbook_id}", response_model=CookbookOut)
def update_cookbook(
    cookbook_id: uuid.UUID,
    body: CookbookPatchIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CookbookOut:
    """Patch name/description/visibility. Owner-only — each service call enforces it."""
    cookbook = service.require_cookbook_access(
        db, user_id=current_user.id, cookbook_id=cookbook_id, need="owner"
    )
    fields_set = body.model_fields_set
    if "name" in fields_set and body.name is not None:
        cookbook = service.rename_cookbook(db, cookbook_id, current_user.id, body.name)
    if "description" in fields_set:
        cookbook = service.set_cookbook_description(
            db, cookbook_id, current_user.id, body.description
        )
    if "visibility" in fields_set and body.visibility is not None:
        cookbook = service.set_cookbook_visibility(
            db, cookbook_id, current_user.id, CookbookVisibility(body.visibility)
        )
    return _to_cookbook_out(db, cookbook, user_id=current_user.id)


@router.delete("/{cookbook_id}", status_code=204)
def delete_cookbook(
    cookbook_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    """Delete a cookbook. Owner-only; 409 if it's the default cookbook."""
    service.delete_cookbook(db, cookbook_id, current_user.id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


@router.post("/{cookbook_id}/members", status_code=201, response_model=CookbookMemberOut)
def invite_member(
    cookbook_id: uuid.UUID,
    body: CookbookMemberIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CookbookMemberOut:
    """Invite a user by email. Owner-only. 404 if no account has that email,
    422 if inviting the owner's own email; re-inviting an existing member
    upserts their role — all enforced by ``service.invite_cookbook_member``.
    """
    member = service.invite_cookbook_member(
        db, cookbook_id, current_user.id, body.email, CookbookRole(body.role)
    )
    return CookbookMemberOut.model_validate(member)


@router.patch("/{cookbook_id}/members/{user_id}", response_model=CookbookMemberOut)
def update_member_role(
    cookbook_id: uuid.UUID,
    user_id: uuid.UUID,
    body: CookbookMemberRoleIn,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> CookbookMemberOut:
    """Change a member's role. Owner-only; 404 if user_id isn't a member."""
    member = service.set_cookbook_member_role(
        db, cookbook_id, current_user.id, user_id, CookbookRole(body.role)
    )
    return CookbookMemberOut.model_validate(member)


@router.delete("/{cookbook_id}/members/{user_id}", status_code=204)
def remove_member(
    cookbook_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    """Remove a member. Owner-only; idempotent — a no-op if not a member."""
    service.remove_cookbook_member(db, cookbook_id, current_user.id, user_id)
    return Response(status_code=204)


@router.post("/{cookbook_id}/leave", status_code=204)
def leave_cookbook(
    cookbook_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: Any = Depends(get_current_user),  # noqa: B008
) -> Response:
    """Remove the caller's own membership row. Idempotent; never checks access
    level — any signed-in caller may always remove their own membership.
    """
    service.leave_cookbook(db, cookbook_id, current_user.id)
    return Response(status_code=204)
