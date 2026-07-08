"""Cookbook models: Recipe aggregate with ingredient groups, lines, steps, and vocab."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recipe_normalizer.db import Base, TimestampMixin, new_uuid


class SourceType(enum.StrEnum):
    web = "web"
    pdf = "pdf"
    image = "image"
    text = "text"
    manual = "manual"


# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------


class Cuisine(Base):
    __tablename__ = "cuisines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    recipes: Mapped[list["Recipe"]] = relationship(
        secondary="recipe_cuisines", back_populates="cuisines"
    )


class DishType(Base):
    __tablename__ = "dish_types"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    recipes: Mapped[list["Recipe"]] = relationship(
        secondary="recipe_dish_types", back_populates="dish_types"
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    recipes: Mapped[list["Recipe"]] = relationship(secondary="recipe_tags", back_populates="tags")


# ---------------------------------------------------------------------------
# Association tables  (composite PK, no separate ORM class needed)
# ---------------------------------------------------------------------------

recipe_cuisines = Table(
    "recipe_cuisines",
    Base.metadata,
    Column(
        "recipe_id",
        ForeignKey("recipes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "cuisine_id",
        ForeignKey("cuisines.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

recipe_dish_types = Table(
    "recipe_dish_types",
    Base.metadata,
    Column(
        "recipe_id",
        ForeignKey("recipes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "dish_type_id",
        ForeignKey("dish_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

recipe_tags = Table(
    "recipe_tags",
    Base.metadata,
    Column(
        "recipe_id",
        ForeignKey("recipes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


# ---------------------------------------------------------------------------
# Core recipe table
# ---------------------------------------------------------------------------


class Recipe(TimestampMixin, Base):
    __tablename__ = "recipes"
    __table_args__ = (
        # Partial unique index: only enforced when source_fingerprint IS NOT NULL
        Index(
            "uq_recipes_owner_fingerprint",
            "owner_id",
            "source_fingerprint",
            unique=True,
            postgresql_where="source_fingerprint IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="sourcetype"), nullable=False
    )
    language: Mapped[str] = mapped_column(String(20), default="en", server_default="en")
    servings_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    servings_unit_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prep_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cook_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extraction_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    derived_from: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True
    )
    last_edited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Personal metadata — owner-only, untouched by the full-replace PATCH
    # semantics of update_recipe() (only children + the listed scalar fields
    # there are rewritten; these two columns are deliberately left alone).
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    ingredient_groups: Mapped[list["IngredientGroup"]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="IngredientGroup.order_index",
    )
    steps: Mapped[list["Step"]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="Step.order_index",
    )
    cuisines: Mapped[list[Cuisine]] = relationship(
        secondary=recipe_cuisines, back_populates="recipes"
    )
    dish_types: Mapped[list[DishType]] = relationship(
        secondary=recipe_dish_types, back_populates="recipes"
    )
    tags: Mapped[list[Tag]] = relationship(secondary=recipe_tags, back_populates="recipes")


# ---------------------------------------------------------------------------
# Ingredient groups and lines
# ---------------------------------------------------------------------------


class IngredientGroup(Base):
    __tablename__ = "ingredient_groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredient_groups")
    ingredient_lines: Mapped[list["IngredientLine"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="IngredientLine.order_index",
    )


class IngredientLine(Base):
    __tablename__ = "ingredient_lines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredient_groups.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    # quantity is Numeric (exact, as parsed from the source text); normalized_amount
    # is Float (a derived approximation after unit conversion).
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    canonical_ingredient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonical_ingredients.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    normalized_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    normalized_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_approx: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    group: Mapped[IngredientGroup] = relationship(back_populates="ingredient_lines")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Deliberately a non-FK JSONB list of ingredient_line uuid strings (loose refs).
    ingredient_line_refs: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )

    recipe: Mapped[Recipe] = relationship(back_populates="steps")
