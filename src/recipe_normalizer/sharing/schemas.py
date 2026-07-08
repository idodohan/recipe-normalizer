"""Pydantic schemas for the sharing API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from recipe_normalizer.cookbook.schemas import IngredientGroupOut, RecipeOut, ServingsOut, StepOut


class ShareRecipeIn(BaseModel):
    """Body for POST /api/share/recipe."""

    recipe_id: uuid.UUID
    to_email: EmailStr = Field(max_length=320)


class ShareOut(BaseModel):
    id: uuid.UUID
    copied_recipe_id: uuid.UUID
    to_email: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Public links
# ---------------------------------------------------------------------------


class CreatePublicLinkIn(BaseModel):
    """Body for POST /api/share/public."""

    recipe_id: uuid.UUID


class PublicLinkOut(BaseModel):
    """One of the current user's public links (authenticated management view)."""

    id: uuid.UUID
    token: str
    recipe_id: uuid.UUID
    recipe_title: str
    revoked_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PublicRecipeOut(BaseModel):
    """Recipe payload served at the unauthenticated ``GET /api/public/{token}``.

    Deliberately an ALLOWLIST (not a blocklist on top of ``RecipeOut``):
    every field below is one this module has explicitly decided is safe to
    hand to an anonymous visitor holding a valid token. Anything on
    ``RecipeOut`` not listed here is dropped, including future additions —
    ``from_recipe_out`` round-trips through ``RecipeOut.model_dump()`` and
    pydantic silently ignores keys this model doesn't declare, so a new
    sensitive field added to ``RecipeOut`` later does NOT leak here by
    default; a maintainer must opt it in.

    Explicitly excluded, per the phase plan:
    - ``notes`` / ``is_favorite`` — the owner's personal data, not the
      recipe's.
    - ``collection_ids`` — the owner's personal organization, meaningless
      (and mildly revealing) to a stranger.
    - ``extraction_meta`` — internal LLM/ingestion-job debugging internals.
    - ``provenance`` — contains ``shared_by`` = the sharer's EMAIL ADDRESS.
      That's the one field here that would leak PII, so it's excluded
      outright rather than redacted field-by-field.
    """

    id: uuid.UUID
    owner_id: uuid.UUID
    schema_version: int = 1
    title: str
    description: str | None = None
    image_ref: str | None = None
    source: str | None = None
    source_type: str
    language: str = "en"
    servings: ServingsOut | None = None
    prep_min: int | None = None
    cook_min: int | None = None
    total_min: int | None = None
    cuisines: list[str] = []
    dish_types: list[str] = []
    tags: list[str] = []
    groups: list[IngredientGroupOut] = []
    steps: list[StepOut] = []
    is_verified: bool = False
    derived_from: uuid.UUID | None = None
    last_edited_by: uuid.UUID | None = None
    last_edited_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_recipe_out(cls, recipe: RecipeOut) -> PublicRecipeOut:
        data: dict[str, Any] = recipe.model_dump(mode="json")
        return cls.model_validate(data)


# ---------------------------------------------------------------------------
# Shared cookbooks
# ---------------------------------------------------------------------------


class CreateSharedCookbookIn(BaseModel):
    """Body for POST /api/shared-cookbooks."""

    name: str = Field(min_length=1, max_length=120)


class InviteMemberIn(BaseModel):
    """Body for POST /api/shared-cookbooks/{id}/members."""

    email: EmailStr = Field(max_length=320)


class AddSharedCookbookRecipeIn(BaseModel):
    """Body for POST /api/shared-cookbooks/{id}/recipes."""

    recipe_id: uuid.UUID


class SharedCookbookOut(BaseModel):
    """One shared cookbook, as it appears in the caller's own list."""

    id: uuid.UUID
    name: str
    created_by: uuid.UUID
    created_at: datetime
    member_count: int
    recipe_count: int


class SharedCookbookMemberOut(BaseModel):
    """One member of a shared cookbook — display_name only, deliberately NOT
    email. See sharing.service module docstring: friends already know each
    other, and there's no reason to hand every member everyone else's email
    address just to render a member list."""

    user_id: uuid.UUID
    display_name: str
    is_creator: bool


class SharedCookbookRecipeOut(BaseModel):
    """One recipe in a shared cookbook's detail view.

    RecipeSummary-shaped (id/title/image_ref/dish_types/total_min/
    is_verified/created_at), plus who last edited it. Deliberately does NOT
    include ``is_favorite`` — that column reflects the RECIPE OWNER's
    personal favorite flag, which is meaningless (and mildly confusing) to
    render to other members who aren't the owner.
    """

    id: uuid.UUID
    title: str
    image_ref: str | None = None
    dish_types: list[str] = []
    total_min: int | None = None
    is_verified: bool = False
    created_at: datetime
    last_edited_by: uuid.UUID | None = None
    last_edited_by_name: str | None = None


class SharedCookbookDetailOut(BaseModel):
    """GET /api/shared-cookbooks/{id} — full detail: members + recipes."""

    id: uuid.UUID
    name: str
    created_by: uuid.UUID
    created_at: datetime
    members: list[SharedCookbookMemberOut]
    recipes: list[SharedCookbookRecipeOut]
