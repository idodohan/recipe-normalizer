"""Catalog models for canonical ingredients and their aliases.

``CanonicalIngredient`` represents a globally shared, deduplicated ingredient
entry with multilingual aliases, density data for dual-quantity conversion
(mass ↔ volume), and a review/merge lifecycle.

``IngredientAlias`` holds alternative names (BCP-47 language-tagged) for a
canonical ingredient.

``gram_weights`` stores per-unit gram equivalents keyed by unit token:
  "cup", "tbsp", "tsp", "unit" (one whole item, e.g. egg ≈ 50 g), or
  count-unit names like "clove".
"""

import enum
import uuid

from sqlalchemy import Enum, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recipe_normalizer.db import Base, TimestampMixin, new_uuid


class PreferredMeasure(enum.StrEnum):
    mass = "mass"
    volume = "volume"


class IngredientStatus(enum.StrEnum):
    seeded = "seeded"
    unreviewed = "unreviewed"
    reviewed = "reviewed"


class CanonicalIngredient(TimestampMixin, Base):
    __tablename__ = "canonical_ingredients"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(50))
    preferred_measure: Mapped[PreferredMeasure] = mapped_column(Enum(PreferredMeasure))
    status: Mapped[IngredientStatus] = mapped_column(Enum(IngredientStatus))
    dietary_flags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    density_g_per_ml: Mapped[float | None] = mapped_column(Float, nullable=True)
    gram_weights: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonical_ingredients.id"), nullable=True
    )

    aliases: Mapped[list["IngredientAlias"]] = relationship(
        back_populates="ingredient", cascade="all, delete-orphan"
    )


class IngredientAlias(TimestampMixin, Base):
    __tablename__ = "ingredient_aliases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_ingredients.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(200), index=True)
    language: Mapped[str] = mapped_column(String(10))  # BCP-47

    ingredient: Mapped[CanonicalIngredient] = relationship(back_populates="aliases")
