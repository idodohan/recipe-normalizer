"""Cookbook models: Recipe aggregate with ingredient groups, lines, steps, and vocab."""

import enum
import uuid
from datetime import UTC, datetime
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
    func,
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
    # An AI transform (ai.service.transform_recipe) produced this draft from
    # another recipe + an instruction — never from an external acquire tier.
    transform = "transform"


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
    # NOTE: there is deliberately no `cookbook_id` column. Phase 1 had one (a
    # recipe lived in exactly one cookbook, and that column was the sole source
    # of recipe access); the boards model replaced it with the
    # `cookbook_recipes` many-to-many, and migration a3f7c2d8e015 dropped it
    # after backfilling a join row per recipe. "Which cookbooks is this recipe
    # in" is `cookbook_placements` below / cookbook.service._recipe_cookbook_ids
    # and nothing else.

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
    # Every cookbook this recipe is placed in (the boards join, and the SOLE
    # source of "which cookbooks holds this"). `CookbookRecipe` is defined below
    # and resolved lazily at mapper-configuration time.
    cookbook_placements: Mapped[list["CookbookRecipe"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", passive_deletes=True
    )


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


# ---------------------------------------------------------------------------
# Cookbooks — owner-scoped containers with shared membership
#
# NOTE: `collections` / `collection_recipes` used to live here — owner-scoped
# named groups of recipes, a second, weaker organizational axis alongside the
# one-cookbook-per-recipe model. The boards pivot made cookbooks themselves
# many-to-many, which is exactly what a collection was, so the concept was
# retired: migration a3f7c2d8e015 turned every collection into a private
# cookbook and dropped both tables.
# ---------------------------------------------------------------------------


class CookbookVisibility(enum.StrEnum):
    private = "private"
    unlisted = "unlisted"
    public = "public"


class CookbookRole(enum.StrEnum):
    editor = "editor"
    viewer = "viewer"


class Cookbook(TimestampMixin, Base):
    __tablename__ = "cookbooks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    visibility: Mapped[CookbookVisibility] = mapped_column(
        Enum(CookbookVisibility, name="cookbookvisibility"),
        default=CookbookVisibility.private,
        server_default=CookbookVisibility.private.value,
        nullable=False,
    )
    # Minted lazily (secrets.token_urlsafe(24)) the first time a cookbook is
    # made public/unlisted — that minting logic lands in a later task; here
    # it's just a nullable, unique column.
    public_token: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # NOTE: there is deliberately no `recipes` relationship. A cookbook holds
    # recipes only through `recipe_placements` below, and deleting a cookbook
    # must NEVER delete a recipe — see cookbook.service.delete_cookbook, which
    # first re-files any recipe whose ONLY placement is this cookbook into its
    # owner's default one. Phase 1's `recipes` relationship cascaded the delete
    # straight into the recipe rows (via `recipes.cookbook_id`'s ON DELETE
    # CASCADE); that column and that cascade are both gone.
    members: Mapped[list["CookbookMember"]] = relationship(
        back_populates="cookbook", cascade="all, delete-orphan", passive_deletes=True
    )
    # The boards join rows pointing INTO this cookbook — placements, not
    # recipes. `passive_deletes=True` because `cookbook_id` is half of
    # `cookbook_recipes`' composite PK (as it is of `cookbook_members`'), so it
    # can never be NULLed out: without it SQLAlchemy loads the children on
    # parent delete and tries exactly that. With it, no UPDATE/DELETE is emitted
    # for the children at all and the FKs' ON DELETE CASCADE does the work in
    # one statement.
    recipe_placements: Mapped[list["CookbookRecipe"]] = relationship(
        back_populates="cookbook", cascade="all, delete-orphan", passive_deletes=True
    )


class CookbookMember(Base):
    """A user's membership in someone else's cookbook.

    The cookbook's OWNER is implicit and never has a row here — see
    `cookbook.service.list_my_cookbooks`, which unions owned cookbooks with
    member-of ones and would list an owner's own cookbook twice otherwise.
    """

    __tablename__ = "cookbook_members"
    __table_args__ = (
        # The composite PK (cookbook_id, user_id) can't serve a bare
        # `WHERE user_id = X` lookup (list_my_cookbooks' "member of" query) —
        # a standalone index on user_id is required for that access path.
        Index("ix_cookbook_members_user_id", "user_id"),
    )

    cookbook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cookbooks.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[CookbookRole] = mapped_column(
        Enum(CookbookRole, name="cookbookrole"), nullable=False
    )
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )

    cookbook: Mapped[Cookbook] = relationship(back_populates="members")


class CookbookRecipe(Base):
    """A recipe's placement in a cookbook — the boards many-to-many join.

    A recipe may sit in several cookbooks at once (Pinterest-style boards), and
    every recipe is in at least one at all times. The ">= 1 placement"
    invariant is enforced in the service (``remove_recipe_from_cookbook``
    refuses to drop the last one), NOT in the database — there is no
    constraint expressible here that spans rows this way.

    A full ORM class rather than a bare ``Table`` (unlike ``recipe_cuisines``
    et al.) because the join carries its own payload — ``added_by`` and
    ``added_at``, the "who pinned this here, when" provenance the social phase
    surfaces — and because the service inserts/deletes rows by hand
    (idempotent ``ON CONFLICT DO NOTHING`` adds) rather than through a
    ``secondary`` collection.

    This table is the SOLE answer to "which cookbooks is this recipe in" — the
    transitional ``recipes.cookbook_id`` was backfilled into it and dropped by
    migration a3f7c2d8e015. Consequently a cookbook's deletion cascades away
    only these placement rows, never a recipe (see
    ``cookbook.service.delete_cookbook``).
    """

    __tablename__ = "cookbook_recipes"
    __table_args__ = (
        # The composite PK is (cookbook_id, recipe_id), so it can't serve a bare
        # `WHERE recipe_id = X` lookup — which is THE access-path of the boards
        # model ("which cookbooks is this recipe in", asked on every recipe
        # read to derive access). Hence a standalone index on recipe_id, the
        # same reasoning as ix_cookbook_members_user_id.
        Index("ix_cookbook_recipes_recipe_id", "recipe_id"),
    )

    cookbook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cookbooks.id", ondelete="CASCADE"), primary_key=True
    )
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True
    )
    # SET NULL, not CASCADE: the person who added the recipe leaving the system
    # must not silently un-place the recipe from the cookbook.
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )

    cookbook: Mapped[Cookbook] = relationship(back_populates="recipe_placements")
    recipe: Mapped[Recipe] = relationship(back_populates="cookbook_placements")
