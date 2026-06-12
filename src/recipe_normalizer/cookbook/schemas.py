"""Pydantic schemas for cookbook API request/response bodies.

Source of truth for dual-quantity display: ``build_display`` lives here.
Routers and services MUST call this function — never hand-roll the arrow string.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import (
    BaseModel,
    Field,
    computed_field,
    field_validator,
    model_validator,
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
    lines: list[IngredientLineOut] = []

    model_config = {"from_attributes": True}

    @field_validator("lines", mode="before")
    @classmethod
    def collect_lines(cls, v: Any) -> Any:
        # ORM relationship is named ingredient_lines; accept either attribute name
        return v

    @model_validator(mode="before")
    @classmethod
    def remap_ingredient_lines(cls, data: Any) -> Any:
        # When coming from ORM (from_attributes=True), pydantic reads attribute names
        # directly, so we need to map ingredient_lines -> lines if it's an ORM object.
        if hasattr(data, "ingredient_lines") and not hasattr(data, "lines"):
            # Create a wrapper namespace so pydantic can read `lines`
            import types

            ns = types.SimpleNamespace(
                **{k: getattr(data, k) for k in vars(data) if not k.startswith("_")}
            )
            ns.lines = data.ingredient_lines
            return ns
        return data


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
    servings: ServingsOut | None = None
    prep_min: int | None = None
    cook_min: int | None = None
    total_min: int | None = None
    cuisines: list[str] = []
    dish_types: list[str] = []
    tags: list[str] = []
    groups: list[IngredientGroupOut] = []
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
    def extract_vocab_names(cls, v: Any) -> list[str]:
        if not v:
            return []
        result: list[str] = []
        for item in v:
            if isinstance(item, str):
                result.append(item)
            elif hasattr(item, "name"):
                result.append(item.name)
            else:
                result.append(str(item))
        return result

    @field_validator("groups", mode="before")
    @classmethod
    def collect_groups(cls, v: Any) -> Any:
        # ORM attribute is ingredient_groups; accept either
        return v

    @model_validator(mode="before")
    @classmethod
    def remap_orm_fields(cls, data: Any) -> Any:
        """Remap ORM attribute names and build servings sub-object."""
        if hasattr(data, "__dict__") or hasattr(data, "ingredient_groups"):
            attrs: dict[str, Any] = {}
            for attr in dir(data):
                if attr.startswith("_"):
                    continue
                with contextlib.suppress(Exception):
                    attrs[attr] = getattr(data, attr)

            # Remap ingredient_groups -> groups
            if "ingredient_groups" in attrs and "groups" not in attrs:
                attrs["groups"] = attrs["ingredient_groups"]

            # Build servings from separate ORM columns if not already present
            if "servings" not in attrs or attrs.get("servings") is None:
                sa = attrs.get("servings_amount")
                su = attrs.get("servings_unit_text")
                if sa is not None or su is not None:
                    attrs["servings"] = ServingsOut(amount=sa, unit_text=su)

            return attrs
        return data


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
    def extract_dish_type_names(cls, v: Any) -> list[str]:
        if not v:
            return []
        result: list[str] = []
        for item in v:
            if isinstance(item, str):
                result.append(item)
            elif hasattr(item, "name"):
                result.append(item.name)
            else:
                result.append(str(item))
        return result
