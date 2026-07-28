"""Pydantic schemas for cookbook API request/response bodies.

Source of truth for dual-quantity display: ``build_display`` lives here.
Routers and services MUST call this function — never hand-roll the arrow string.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    EmailStr,
    Field,
    computed_field,
    field_validator,
)

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def format_amount(x: float) -> str:
    """Format a float amount, trimming trailing zeros after rounding to 2 dp.

    Examples:
        120.0  -> "120"
        29.57  -> "29.57"
        0.5    -> "0.5"
        0.004  -> "0.004" (tiny nonzero amounts shown with 2 sig figs)
    """
    rounded = round(x, 2)
    if rounded == 0 and x != 0:
        # Don't render tiny nonzero amounts as "0" — show 2 significant figures.
        return f"{x:.2g}"
    # Use g format to strip trailing zeros, but avoid scientific notation for
    # the range of quantities we handle (0.01 to 99999).
    formatted = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return formatted


_FILES_PREFIX = "/api/files/"


def _image_url(ref: str | None) -> str | None:
    """Expose a stored image ref as a servable /api/files/ URL path.

    Idempotent and pass-through for values that are already URLs (absolute
    http(s):// links or an existing /api/files/ path); only bare store refs
    ("sha16/sha.ext") get the prefix.
    """
    if ref is None or ref.startswith("/") or "://" in ref:
        return ref
    return f"{_FILES_PREFIX}{ref}"


def build_display(
    *,
    original_text: str,
    normalized_amount: float | None,
    normalized_unit: str | None,
    is_approx: bool,
) -> str:
    """Build the dual-quantity display string for an ingredient line.

    Rule (spec §5):
    - If normalized_amount is None (unconvertible): return original_text only.
    - Otherwise: ``"{original} → {~?}{amount} {unit}"`` — the leading tilde is
      the approximation marker; we don't also append "(approx.)".

    Examples:
        "1 cup flour", 120.0, "g", True  -> "1 cup flour → ~120 g"
        "1 oz gin",   29.57, "ml", False -> "1 oz gin → 29.57 ml"
        "salt to taste", None, None, False -> "salt to taste"
    """
    if normalized_amount is None:
        return original_text
    prefix = "~" if is_approx else ""
    return f"{original_text} → {prefix}{format_amount(normalized_amount)} {normalized_unit}"


# ---------------------------------------------------------------------------
# Servings sub-schema
# ---------------------------------------------------------------------------


class ServingsIn(BaseModel):
    amount: float | None = Field(default=None, gt=0)
    unit_text: str | None = None


class ServingsOut(BaseModel):
    amount: float | None = None
    unit_text: str | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Input DTOs
# ---------------------------------------------------------------------------


class IngredientLineIn(BaseModel):
    original_text: str = Field(min_length=1)
    quantity: Decimal | None = Field(default=None, gt=Decimal("0"))
    unit: str | None = None
    name: str | None = None  # ingredient name for catalog matching
    note: str | None = None
    is_optional: bool = False


class IngredientGroupIn(BaseModel):
    name: str | None = None
    lines: list[IngredientLineIn] = Field(min_length=1)


class StepIn(BaseModel):
    original_text: str = Field(min_length=1)


class RecipeIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    language: str = "en"
    servings: ServingsIn | None = None
    prep_min: int | None = Field(default=None, ge=0)
    cook_min: int | None = Field(default=None, ge=0)
    total_min: int | None = Field(default=None, ge=0)
    cuisines: list[str] = []
    dish_types: list[str] = []
    tags: list[str] = []
    groups: list[IngredientGroupIn] = Field(min_length=1)
    steps: list[StepIn] = []


class CollectionIn(BaseModel):
    """Body for POST/PATCH /api/collections."""

    name: str = Field(min_length=1, max_length=120)


class SetRecipeCollectionsIn(BaseModel):
    """Body for PUT /api/recipes/{recipe_id}/collections — full-replace semantics."""

    collection_ids: list[uuid.UUID] = []


class RecipePersonalPatch(BaseModel):
    """Lightweight patch for personal metadata (favorites/notes).

    Distinct from RecipeIn: does not trigger the full destructive-replace
    semantics of PATCH /api/recipes/{id}. ``notes`` uses model_fields_set at
    the router layer to distinguish "field absent" from "explicit null".
    """

    is_favorite: bool | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Output DTOs
# ---------------------------------------------------------------------------


def extract_vocab_names(v: Any) -> list[str]:
    """Extract `.name` from ORM vocab objects; pass plain strings through."""
    if not v:
        return []
    return [item if isinstance(item, str) else item.name for item in v]


class IngredientLineOut(BaseModel):
    id: uuid.UUID
    original_text: str
    # The canonical ingredient's name, when linked — lets the review editor
    # round-trip the catalog link on save (attached by the service layer).
    name: str | None = Field(default=None, validation_alias="canonical_name")
    # quantity comes from ORM as Decimal|None; expose as float|None for JSON friendliness
    quantity: float | None = None
    unit: str | None = None
    canonical_ingredient_id: uuid.UUID | None = None
    normalized_amount: float | None = None
    normalized_unit: str | None = None
    is_approx: bool = False
    note: str | None = None
    is_optional: bool = False

    model_config = {"from_attributes": True}

    @field_validator("quantity", mode="before")
    @classmethod
    def coerce_decimal_to_float(cls, v: Any) -> float | None:
        if v is None:
            return None
        return float(v)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display(self) -> str:
        return build_display(
            original_text=self.original_text,
            normalized_amount=self.normalized_amount,
            normalized_unit=self.normalized_unit,
            is_approx=self.is_approx,
        )


class IngredientGroupOut(BaseModel):
    id: uuid.UUID
    name: str | None = None
    # ORM relationship attribute is `ingredient_lines`; API field is `lines`.
    lines: list[IngredientLineOut] = Field(
        default=[], validation_alias=AliasChoices("lines", "ingredient_lines")
    )

    model_config = {"from_attributes": True}


class StepOut(BaseModel):
    id: uuid.UUID
    original_text: str
    ingredient_line_refs: list[str] = []

    model_config = {"from_attributes": True}


class RecipeOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    # Every recipe lives in exactly one cookbook — `recipes.cookbook_id` is
    # NOT NULL in the database (finalize migration b7d3f0c11a94), so this is
    # always present and clients may rely on it.
    cookbook_id: uuid.UUID
    schema_version: int = 1
    title: str
    description: str | None = None
    image_ref: str | None = None
    source: str | None = None
    source_type: str
    language: str = "en"
    # Raw ORM columns; excluded from output. The public `servings` object is computed.
    servings_amount: float | None = Field(default=None, exclude=True)
    servings_unit_text: str | None = Field(default=None, exclude=True)
    prep_min: int | None = None
    cook_min: int | None = None
    total_min: int | None = None
    cuisines: list[str] = []
    dish_types: list[str] = []
    tags: list[str] = []
    # ORM relationship attribute is `ingredient_groups`; API field is `groups`.
    groups: list[IngredientGroupOut] = Field(
        default=[], validation_alias=AliasChoices("groups", "ingredient_groups")
    )
    steps: list[StepOut] = []
    extraction_meta: dict[str, Any] | None = None
    is_verified: bool = False
    provenance: dict[str, Any] | None = None
    derived_from: uuid.UUID | None = None
    last_edited_by: uuid.UUID | None = None
    last_edited_at: datetime | None = None
    is_favorite: bool = False
    notes: str | None = None
    created_at: datetime
    # ORM relationship attribute is `collections` (list of Collection objects);
    # API field exposes just the ids. Detail-only (RecipeSummary does NOT get
    # this) — see cookbook.service._RECIPE_FULL_OPTIONS for the selectinload
    # that keeps this off the N+1 path for list_recipes.
    collection_ids: list[uuid.UUID] = Field(default=[], validation_alias="collections")

    model_config = {"from_attributes": True}

    @field_validator("collection_ids", mode="before")
    @classmethod
    def collections_to_ids(cls, v: Any) -> list[uuid.UUID]:
        if not v:
            return []
        return [item.id if hasattr(item, "id") else item for item in v]

    @field_validator("image_ref", mode="after")
    @classmethod
    def image_ref_to_url(cls, v: str | None) -> str | None:
        return _image_url(v)

    @field_validator("source_type", mode="before")
    @classmethod
    def coerce_source_type(cls, v: Any) -> str:
        # StrEnum values are already strings; handle both enum and str
        return str(v)

    @field_validator("cuisines", "dish_types", "tags", mode="before")
    @classmethod
    def vocab_to_names(cls, v: Any) -> list[str]:
        return extract_vocab_names(v)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def servings(self) -> ServingsOut | None:
        if self.servings_amount is None and self.servings_unit_text is None:
            return None
        return ServingsOut(amount=self.servings_amount, unit_text=self.servings_unit_text)


class RecipeSummary(BaseModel):
    id: uuid.UUID
    title: str
    image_ref: str | None = None
    dish_types: list[str] = []
    total_min: int | None = None
    is_verified: bool = False
    is_favorite: bool = False
    created_at: datetime
    # Already public on the detail view (RecipeOut), so exposing it here is
    # not a new leak class. It exists on the summary shape so a cookbook's
    # recipe list can render "last edited by X" without a bespoke query — the
    # one consumer today is cookbook_router's GET /api/cookbooks/{id}, via
    # cookbook.service.recipe_summaries_for_ids. Deliberately NOT carried into
    # the ANONYMOUS public-cookbook payload (PublicCookbookRecipeOut drops it):
    # an editor member never consented to their account id reaching strangers.
    last_edited_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}

    @field_validator("image_ref", mode="after")
    @classmethod
    def image_ref_to_url(cls, v: str | None) -> str | None:
        return _image_url(v)

    @field_validator("dish_types", mode="before")
    @classmethod
    def vocab_to_names(cls, v: Any) -> list[str]:
        return extract_vocab_names(v)


class RecipePage(BaseModel):
    """Paginated result of ``GET /api/recipes``."""

    items: list[RecipeSummary]
    total: int
    limit: int
    offset: int


class CookbookSummary(BaseModel):
    """Row shape for listing a user's cookbooks (owned + member-of).

    ``role`` is ``"owner"`` for cookbooks the caller owns outright, or the
    resolved ``CookbookRole`` value ("editor"/"viewer") for cookbooks the
    caller is merely a member of — see cookbook.service.list_my_cookbooks.
    """

    id: uuid.UUID
    name: str
    description: str | None = None
    visibility: str
    role: str
    recipe_count: int
    is_default: bool
    cover_image_ref: str | None = None

    model_config = {"from_attributes": True}

    @field_validator("cover_image_ref", mode="after")
    @classmethod
    def cover_image_ref_to_url(cls, v: str | None) -> str | None:
        return _image_url(v)


# ---------------------------------------------------------------------------
# Cookbook CRUD / membership DTOs (Task 6 — HTTP surface over Task 4/5 services)
# ---------------------------------------------------------------------------


class CookbookIn(BaseModel):
    """Body for POST /api/cookbooks."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class CookbookPatchIn(BaseModel):
    """Body for PATCH /api/cookbooks/{id} — every field optional (partial patch).

    The router distinguishes "field absent" from "explicitly provided" via
    ``model_fields_set`` (same convention as ``RecipePersonalPatch``), so a
    caller can clear ``description`` with an explicit ``null`` without
    touching name/visibility. ``name``/``visibility`` have no meaningful
    null value, so those are only applied when both present AND non-null.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    # Literal, not the CookbookVisibility enum — cookbook.schemas must stay
    # free of cookbook.models: sharing/extraction/ai/ingestion all import this
    # module and are import-linter-forbidden from reaching cookbook.models
    # even transitively. The router converts this to CookbookVisibility.
    visibility: Literal["private", "unlisted", "public"] | None = None


class CookbookOut(CookbookSummary):
    """CookbookSummary + ``public_token`` — the create/detail/PATCH response shape.

    ``public_token`` is None for a private cookbook, and the minted token for
    an unlisted/public one (see ``cookbook.service.set_cookbook_visibility``).
    Deliberately NOT part of ``CookbookSummary``/the ``GET /api/cookbooks``
    list response — the brief only calls for it on create/detail/PATCH.

    It is also OWNER-ONLY even on those three: the token is the shareable
    secret behind an unlisted cookbook, so every non-owner caller gets None
    here. The masking lives in the one place this DTO is built —
    ``cookbook_router._to_cookbook_out`` — which is the only code path that
    knows the caller's resolved role.
    """

    public_token: str | None = None


class CookbookDetailOut(CookbookOut):
    """GET /api/cookbooks/{id} — the cookbook plus its recipes (access-checked)."""

    recipes: list[RecipeSummary] = []


class CookbookMemberIn(BaseModel):
    """Body for POST /api/cookbooks/{id}/members."""

    email: EmailStr = Field(max_length=320)
    # Literal, not the CookbookRole enum — see CookbookPatchIn.visibility's
    # comment; the router converts this to CookbookRole.
    role: Literal["editor", "viewer"]


class CookbookMemberRoleIn(BaseModel):
    """Body for PATCH /api/cookbooks/{id}/members/{user_id}."""

    role: Literal["editor", "viewer"]


class CookbookMemberOut(BaseModel):
    """A single cookbook membership row, as returned by the invite/role-change routes."""

    cookbook_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    added_by: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("role", mode="before")
    @classmethod
    def coerce_role(cls, v: Any) -> str:
        return str(v)


class CollectionOut(BaseModel):
    """Response shape for the collections endpoints — includes a recipe count.

    Not built via ``from_attributes`` off the ORM ``Collection`` directly
    (the count comes from a separate aggregate in the service layer), but
    ``from_attributes`` is still enabled so ``Collection.id``/``.name`` can
    be read off the ORM row when constructing this.
    """

    id: uuid.UUID
    name: str
    recipe_count: int

    model_config = {"from_attributes": True}
