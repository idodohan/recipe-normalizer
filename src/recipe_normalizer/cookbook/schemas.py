"""Pydantic schemas for cookbook API request/response bodies.

Source of truth for dual-quantity display: ``build_display`` lives here.
Routers and services MUST call this function — never hand-roll the arrow string.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
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
    """
    rounded = round(x, 2)
    # Use g format to strip trailing zeros, but avoid scientific notation for
    # the range of quantities we handle (0.01 to 99999).
    formatted = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return formatted


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
    - Otherwise: ``"{original} → {~?}{amount} {unit}{ (approx.)?}"``

    Examples:
        "1 cup flour", 120.0, "g", True  -> "1 cup flour → ~120 g (approx.)"
        "1 oz gin",   29.57, "ml", False -> "1 oz gin → 29.57 ml"
        "salt to taste", None, None, False -> "salt to taste"
    """
    if normalized_amount is None:
        return original_text
    prefix = "~" if is_approx else ""
    suffix = " (approx.)" if is_approx else ""
    return f"{original_text} → {prefix}{format_amount(normalized_amount)} {normalized_unit}{suffix}"


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
    created_at: datetime

    model_config = {"from_attributes": True}

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
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("dish_types", mode="before")
    @classmethod
    def vocab_to_names(cls, v: Any) -> list[str]:
        return extract_vocab_names(v)
