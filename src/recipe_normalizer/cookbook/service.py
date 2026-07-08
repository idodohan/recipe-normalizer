"""Cookbook service: Recipe CRUD with catalog matching, deduplication, and merge hooks.

Transaction convention: service flushes; HTTP layer (or test) owns commit.
Import-linter enforces that we never import catalog.models or users.models directly —
we only import from catalog.service (which re-exports the conversion API).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from recipe_normalizer.catalog import service as catalog_service
from recipe_normalizer.catalog.service import convert_to_normalized

if TYPE_CHECKING:
    from recipe_normalizer.llm.client import LLMClient
from recipe_normalizer.cookbook.models import (
    Cuisine,
    DishType,
    IngredientGroup,
    IngredientLine,
    Recipe,
    SourceType,
    Step,
    Tag,
)
from recipe_normalizer.cookbook.schemas import (
    IngredientLineIn,
    RecipeIn,
    RecipeOut,
    RecipePage,
    RecipeSummary,
)
from recipe_normalizer.errors import ApiError
from recipe_normalizer.filestore import FileStore
from recipe_normalizer.sniff import detect_media_type

__all__ = [
    "MAX_IMAGE_BYTES",
    "UNSET",
    "DuplicateRecipeError",
    "SourceType",
    "are_verified",
    "clear_recipe_image",
    "create_recipe",
    "delete_recipe",
    "find_recipe_id_by_fingerprint",
    "get_recipe",
    "list_recipes",
    "register_hooks",
    "repoint_ingredient_lines",
    "set_personal",
    "set_recipe_image",
    "update_recipe",
    "verify_recipe",
]


# Public sentinel for "field not provided" in set_personal, so callers can
# explicitly pass notes=None (clearing it) vs. leaving it untouched.
UNSET: Any = object()


# ---------------------------------------------------------------------------
# Recipe image upload — validation constants
# ---------------------------------------------------------------------------

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB — the router bounds its read with this

# Images only — no PDF (that's an ingestion source type, not a recipe photo).
_ALLOWED_IMAGE_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

_IMAGE_MEDIA_TYPE_SUFFIX: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


# ---------------------------------------------------------------------------
# Custom error
# ---------------------------------------------------------------------------


class DuplicateRecipeError(ApiError):
    """Raised when a recipe with the same (owner_id, source_fingerprint) already exists."""

    existing_id: uuid.UUID

    def __init__(self, existing_id: uuid.UUID) -> None:
        super().__init__(
            status_code=409,
            code="duplicate_recipe",
            message=f"A recipe with this fingerprint already exists (id={existing_id}).",
            extra={"existing_id": str(existing_id)},
        )
        self.existing_id = existing_id


# ---------------------------------------------------------------------------
# Hook registration (idempotent) — called at bottom of module after
# repoint_ingredient_lines is defined.
# ---------------------------------------------------------------------------

_hooks_registered = False


def register_hooks() -> None:
    """Register cookbook merge-repoint hook with catalog service (idempotent)."""
    global _hooks_registered
    if _hooks_registered:
        return
    catalog_service.register_merge_hook(repoint_ingredient_lines)
    _hooks_registered = True


# ---------------------------------------------------------------------------
# Vocab helpers
# ---------------------------------------------------------------------------


def _get_or_create_vocab(db: Session, model: Any, name: str) -> Any:
    """Get or create a vocab row (Cuisine/DishType/Tag) by case-insensitive name.

    Lookup is case-insensitive (stripped + lowered); on insert the first-seen
    casing is preserved as given (stripped only).
    """
    stripped = name.strip()
    row = db.scalars(select(model).where(func.lower(model.name) == stripped.lower())).first()
    if row is None:
        row = model(name=stripped)
        db.add(row)
        db.flush()
    return row


# ---------------------------------------------------------------------------
# Eager-load option builders
# ---------------------------------------------------------------------------

_RECIPE_FULL_OPTIONS = [
    selectinload(Recipe.ingredient_groups).selectinload(IngredientGroup.ingredient_lines),
    selectinload(Recipe.steps),
    selectinload(Recipe.cuisines),
    selectinload(Recipe.dish_types),
    selectinload(Recipe.tags),
]

_RECIPE_SUMMARY_OPTIONS = [
    selectinload(Recipe.dish_types),
]


def _load_recipe_full(db: Session, recipe_id: uuid.UUID) -> Recipe | None:
    """Load a recipe with all relationships eagerly.

    Expires any cached instance first so selectinload re-fetches from the DB.
    """
    # Expire any stale identity-map entry so selectinload re-runs its queries
    existing = db.get(Recipe, recipe_id)
    if existing is not None:
        db.expire(existing)
    stmt = select(Recipe).where(Recipe.id == recipe_id).options(*_RECIPE_FULL_OPTIONS)
    recipe = db.scalars(stmt).first()
    if recipe is not None:
        _attach_canonical_names(db, recipe)
    return recipe


def _attach_canonical_names(db: Session, recipe: Recipe) -> None:
    """Stamp each line with its linked ingredient's name (transient attribute).

    IngredientLineOut reads this via its ``canonical_name`` validation alias, so
    the review editor can round-trip the catalog link on save. Done in one batch
    query (no cross-module relationship — boundary stays clean).
    """
    lines = [line for group in recipe.ingredient_groups for line in group.ingredient_lines]
    ids = {line.canonical_ingredient_id for line in lines if line.canonical_ingredient_id}
    names = catalog_service.names_for_ids(db, ids)
    for line in lines:
        line.canonical_name = (  # type: ignore[attr-defined]
            names.get(line.canonical_ingredient_id) if line.canonical_ingredient_id else None
        )


# ---------------------------------------------------------------------------
# Line / group helpers
# ---------------------------------------------------------------------------


def _process_line(
    db: Session,
    line_in: IngredientLineIn,
    *,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Resolve catalog match + normalization for a single ingredient line.

    Returns a dict of column values (excluding group_id and order_index).
    Uses match_or_create for fuzzy + LLM-assisted matching when *llm* is provided.
    """
    canonical_ingredient_id: uuid.UUID | None = None
    normalized_amount: float | None = None
    normalized_unit: str | None = None
    is_approx: bool = False

    if line_in.name:
        ingredient = catalog_service.match_or_create(db, line_in.name, llm=llm)
        canonical_ingredient_id = ingredient.id

        # Attempt unit conversion when quantity + unit are present
        if line_in.quantity is not None and line_in.unit is not None:
            converted = convert_to_normalized(float(line_in.quantity), line_in.unit, ingredient)
            if converted is not None:
                normalized_amount = converted.amount
                normalized_unit = converted.unit
                is_approx = converted.is_approx

    return {
        "original_text": line_in.original_text,
        "quantity": line_in.quantity,
        "unit": line_in.unit,
        "note": line_in.note,
        "is_optional": line_in.is_optional,
        "canonical_ingredient_id": canonical_ingredient_id,
        "normalized_amount": normalized_amount,
        "normalized_unit": normalized_unit,
        "is_approx": is_approx,
    }


def _apply_groups(
    db: Session,
    recipe: Recipe,
    data: RecipeIn,
    *,
    llm: LLMClient | None = None,
) -> None:
    """Build/replace ingredient groups and lines from RecipeIn.

    Existing groups/lines are not touched here — caller must ensure a clean state
    (either new recipe or after removing old groups via delete-orphan cascade).
    Passes *llm* to _process_line for fuzzy + LLM-assisted catalog matching.
    """
    for g_idx, group_in in enumerate(data.groups):
        group = IngredientGroup(
            recipe_id=recipe.id,
            name=group_in.name,
            order_index=g_idx,
        )
        db.add(group)
        db.flush()  # get group.id

        for l_idx, line_in in enumerate(group_in.lines):
            line_data = _process_line(db, line_in, llm=llm)
            line = IngredientLine(
                group_id=group.id,
                order_index=l_idx,
                **line_data,
            )
            db.add(line)

    # Flush all lines at once
    db.flush()


def _dedupe_names(names: list[str]) -> list[str]:
    """Dedupe names case-insensitively, preserving order and first-seen casing."""
    seen: dict[str, str] = {}
    for n in names:
        stripped = n.strip()
        seen.setdefault(stripped.lower(), stripped)
    return list(seen.values())


def _apply_vocab(db: Session, recipe: Recipe, data: RecipeIn) -> None:
    """Sync vocab many-to-many relationships from RecipeIn (deduped per request)."""
    recipe.cuisines = [_get_or_create_vocab(db, Cuisine, n) for n in _dedupe_names(data.cuisines)]
    recipe.dish_types = [
        _get_or_create_vocab(db, DishType, n) for n in _dedupe_names(data.dish_types)
    ]
    recipe.tags = [_get_or_create_vocab(db, Tag, n) for n in _dedupe_names(data.tags)]


def _apply_steps(db: Session, recipe: Recipe, data: RecipeIn) -> None:
    """Build/replace steps from RecipeIn."""
    for s_idx, step_in in enumerate(data.steps):
        step = Step(
            recipe_id=recipe.id,
            order_index=s_idx,
            original_text=step_in.original_text,
        )
        db.add(step)
    db.flush()


# ---------------------------------------------------------------------------
# Public CRUD operations
# ---------------------------------------------------------------------------


def create_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    data: RecipeIn,
    source_fingerprint: str | None = None,
    source: str | None = None,
    source_type: SourceType = SourceType.manual,
    llm: LLMClient | None = None,
    extraction_meta: dict[str, Any] | None = None,
    image_ref: str | None = None,
) -> RecipeOut:
    """Create a new recipe and return a fully-populated RecipeOut.

    - Fingerprint check first → DuplicateRecipeError if already exists for this owner.
    - Vocab rows get-or-created.
    - Ingredient lines: catalog-matched (or unreviewed created), normalized when possible.
    - extraction_meta / image_ref: provenance from the extraction pipeline (None for manual).
    - Flushes; caller owns commit.
    """
    # Fingerprint uniqueness check
    if source_fingerprint is not None:
        existing = db.scalars(
            select(Recipe).where(
                Recipe.owner_id == owner_id,
                Recipe.source_fingerprint == source_fingerprint,
            )
        ).first()
        if existing is not None:
            raise DuplicateRecipeError(existing_id=existing.id)

    recipe = Recipe(
        owner_id=owner_id,
        title=data.title,
        description=data.description,
        language=data.language,
        source=source,
        source_type=source_type,
        source_fingerprint=source_fingerprint,
        extraction_meta=extraction_meta,
        image_ref=image_ref,
        servings_amount=data.servings.amount if data.servings else None,
        servings_unit_text=data.servings.unit_text if data.servings else None,
        prep_min=data.prep_min,
        cook_min=data.cook_min,
        total_min=data.total_min,
    )
    db.add(recipe)
    db.flush()  # get recipe.id

    _apply_groups(db, recipe, data, llm=llm)
    _apply_steps(db, recipe, data)
    _apply_vocab(db, recipe, data)
    db.flush()

    # Re-fetch with all eager-loaded relationships
    loaded = _load_recipe_full(db, recipe.id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def get_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> RecipeOut:
    """Fetch a recipe by id, owner-scoped. Raises ApiError 404 if not found or wrong owner."""
    loaded = _load_recipe_full(db, recipe_id)
    if loaded is None or loaded.owner_id != owner_id:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    return RecipeOut.model_validate(loaded)


# Trigram similarity threshold shared by title and ingredient-line matching.
_TRIGRAM_THRESHOLD = 0.25

_VALID_DIETARY_FILTERS = frozenset({"vegan", "vegetarian", "gluten_free"})


def list_recipes(
    db: Session,
    *,
    owner_id: uuid.UUID,
    q: str | None = None,
    cuisine: str | None = None,
    dish_type: str | None = None,
    tag: str | None = None,
    dietary: str | None = None,
    max_total_min: int | None = None,
    source_type: SourceType | str | None = None,
    favorites: bool | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> RecipePage:
    """Return a page of recipes owned by owner_id, newest first, filters ANDed.

    Search (*q*): a recipe matches when ANY of the following hold —
      1. title/description full-text search: ``to_tsvector('simple', title ||
         ' ' || coalesce(description, '')) @@ websearch_to_tsquery('simple', q)``.
      2. title trigram similarity: ``similarity(title, q) > 0.25``.
      3. an ingredient line's ``original_text`` has trigram
         ``similarity(original_text, q) > 0.25``.

    Dietary rule (*dietary* — one of ``"vegan"``, ``"vegetarian"``,
    ``"gluten_free"``): a recipe qualifies when EVERY ingredient line that HAS
    a canonical-ingredient match is compatible with the diet, per
    ``catalog.service.dietary_incompatible_ingredient_ids``. Ingredient lines
    with no canonical match (free-text lines, or lines with no ``name``
    given) do NOT disqualify — they are simply not considered. See
    catalog/service.py for how the raw ``dietary_flags`` tags map to each
    diet.

    ``limit`` defaults to 1000 (offset 0) — high enough that, for realistic
    cookbook sizes, omitting both params reproduces the pre-pagination
    behavior of returning every recipe; the search/filter UI (next task)
    passes explicit page sizes.

    Raises ``ApiError`` 422 for an unrecognised *dietary* value.
    """
    conditions: list[Any] = [Recipe.owner_id == owner_id]

    if q and q.strip():
        q_stripped = q.strip()
        title_desc_tsvector = func.to_tsvector(
            "simple", func.concat(Recipe.title, " ", func.coalesce(Recipe.description, ""))
        )
        fts_match = title_desc_tsvector.op("@@")(func.websearch_to_tsquery("simple", q_stripped))
        title_trgm_match = func.similarity(Recipe.title, q_stripped) > _TRIGRAM_THRESHOLD
        ingredient_trgm_match = exists(
            select(1)
            .select_from(IngredientLine)
            .join(IngredientGroup, IngredientLine.group_id == IngredientGroup.id)
            .where(
                IngredientGroup.recipe_id == Recipe.id,
                func.similarity(IngredientLine.original_text, q_stripped) > _TRIGRAM_THRESHOLD,
            )
        )
        conditions.append(or_(fts_match, title_trgm_match, ingredient_trgm_match))

    if cuisine and cuisine.strip():
        conditions.append(Recipe.cuisines.any(func.lower(Cuisine.name) == cuisine.strip().lower()))

    if dish_type and dish_type.strip():
        conditions.append(
            Recipe.dish_types.any(func.lower(DishType.name) == dish_type.strip().lower())
        )

    if tag and tag.strip():
        conditions.append(Recipe.tags.any(func.lower(Tag.name) == tag.strip().lower()))

    if dietary is not None:
        if dietary not in _VALID_DIETARY_FILTERS:
            raise ApiError(
                422,
                "validation_error",
                f"Unknown dietary filter {dietary!r}; expected one of "
                f"{sorted(_VALID_DIETARY_FILTERS)}.",
            )
        disqualifying_ids = catalog_service.dietary_incompatible_ingredient_ids(dietary)
        conditions.append(
            ~exists(
                select(1)
                .select_from(IngredientLine)
                .join(IngredientGroup, IngredientLine.group_id == IngredientGroup.id)
                .where(
                    IngredientGroup.recipe_id == Recipe.id,
                    IngredientLine.canonical_ingredient_id.in_(disqualifying_ids),
                )
            )
        )

    if max_total_min is not None:
        conditions.append(Recipe.total_min <= max_total_min)

    if source_type is not None:
        conditions.append(Recipe.source_type == source_type)

    if favorites is not None:
        conditions.append(Recipe.is_favorite == favorites)

    total = db.scalar(select(func.count()).select_from(Recipe).where(*conditions)) or 0

    stmt = (
        select(Recipe)
        .where(*conditions)
        .options(*_RECIPE_SUMMARY_OPTIONS)
        .order_by(Recipe.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    recipes = db.scalars(stmt).all()
    return RecipePage(
        items=[RecipeSummary.model_validate(r) for r in recipes],
        total=total,
        limit=limit,
        offset=offset,
    )


def update_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
    data: RecipeIn,
    editor_id: uuid.UUID,
    llm: LLMClient | None = None,
) -> RecipeOut:
    """Replace a recipe's content wholesale; returns updated RecipeOut.

    - Owner-scoped: 404 if wrong owner or missing.
    - Replaces groups/lines/steps (delete-orphan cascade handles cleanup).
    - Re-runs catalog matching + normalization.
    - Sets last_edited_by and last_edited_at.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    # Clear existing groups/steps — delete-orphan cascade removes children
    recipe.ingredient_groups.clear()
    recipe.steps.clear()
    db.flush()

    # Update scalar fields
    recipe.title = data.title
    recipe.description = data.description
    recipe.language = data.language
    recipe.servings_amount = data.servings.amount if data.servings else None
    recipe.servings_unit_text = data.servings.unit_text if data.servings else None
    recipe.prep_min = data.prep_min
    recipe.cook_min = data.cook_min
    recipe.total_min = data.total_min
    recipe.last_edited_by = editor_id
    recipe.last_edited_at = datetime.now(UTC)

    db.flush()

    _apply_groups(db, recipe, data, llm=llm)
    _apply_steps(db, recipe, data)
    _apply_vocab(db, recipe, data)
    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def delete_recipe(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> None:
    """Delete a recipe (and its children via cascade). Raises 404 if not found/wrong owner."""
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    db.delete(recipe)
    db.flush()


# ---------------------------------------------------------------------------
# Fingerprint helpers (used by ingestion dedupe)
# ---------------------------------------------------------------------------


def find_recipe_id_by_fingerprint(
    db: Session,
    owner_id: uuid.UUID,
    fingerprint: str,
) -> uuid.UUID | None:
    """Return the id of the owner's recipe with *fingerprint*, or None.

    Owner-scoped single-query lookup.
    """
    row = db.scalars(
        select(Recipe.id).where(
            Recipe.owner_id == owner_id,
            Recipe.source_fingerprint == fingerprint,
        )
    ).first()
    return row


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def verify_recipe(
    db: Session,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> None:
    """Mark a recipe as verified (is_verified = True).

    Owner-scoped. Raises ApiError 404 if not found or wrong owner.
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")
    recipe.is_verified = True
    db.flush()


def set_personal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
    is_favorite: bool | Any = UNSET,
    notes: str | None | Any = UNSET,
) -> RecipeOut:
    """Patch personal metadata (is_favorite/notes) without touching recipe content.

    Owner-scoped: raises ApiError 404 if not found or wrong owner.
    Both fields use the ``UNSET`` sentinel so callers can explicitly pass
    ``notes=None`` (clearing it) vs. leaving it untouched.
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    if is_favorite is not UNSET:
        recipe.is_favorite = is_favorite

    if notes is not UNSET:
        recipe.notes = notes

    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


# ---------------------------------------------------------------------------
# Recipe image upload
# ---------------------------------------------------------------------------


def set_recipe_image(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
    data: bytes,
    store: FileStore,
) -> RecipeOut:
    """Upload (or replace) the recipe's photo.

    Owner-scoped: raises ApiError 404 if not found or wrong owner.
    Validates size, then sniffs the actual content from magic bytes — the
    client-supplied Content-Type is only a hint and is never trusted (mirrors
    ingestion.service.submit_file). Images only (png/jpeg/webp/gif); a PDF or
    any other content is rejected even though sniff.py recognizes it.
    Saves to the FileStore and points recipe.image_ref at the new ref
    (the old blob, if any, is left in place — content-addressed store, same
    convention as everywhere else in the codebase).
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    if len(data) > MAX_IMAGE_BYTES:
        raise ApiError(422, "file_too_large", "Image must be ≤ 10 MB.")

    sniffed_media_type = detect_media_type(data)
    if sniffed_media_type is None or sniffed_media_type not in _ALLOWED_IMAGE_TYPES:
        raise ApiError(
            422,
            "unsupported_file_type",
            f"File content is not a supported image type (detected: "
            f"{sniffed_media_type or 'unknown'}). "
            f"Allowed: {', '.join(sorted(_ALLOWED_IMAGE_TYPES))}",
        )

    suffix = _IMAGE_MEDIA_TYPE_SUFFIX[sniffed_media_type]
    recipe.image_ref = store.save(data, suffix=suffix)
    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def clear_recipe_image(
    db: Session,
    *,
    owner_id: uuid.UUID,
    recipe_id: uuid.UUID,
) -> RecipeOut:
    """Clear the recipe's photo (sets image_ref to None).

    Owner-scoped: raises ApiError 404 if not found or wrong owner.
    Idempotent — clearing an already-imageless recipe is a no-op.
    Flushes; caller owns commit.
    """
    recipe = db.scalars(
        select(Recipe).where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
    ).first()
    if recipe is None:
        raise ApiError(404, "not_found", f"Recipe {recipe_id} not found.")

    recipe.image_ref = None
    db.flush()

    loaded = _load_recipe_full(db, recipe_id)
    assert loaded is not None
    return RecipeOut.model_validate(loaded)


def are_verified(
    db: Session,
    owner_id: uuid.UUID,
    recipe_ids: list[uuid.UUID],
) -> dict[uuid.UUID, bool]:
    """Return a mapping of recipe_id → is_verified for the given owner-scoped ids.

    Missing (deleted) recipes are omitted from the result.
    """
    if not recipe_ids:
        return {}
    rows = db.execute(
        select(Recipe.id, Recipe.is_verified).where(
            Recipe.owner_id == owner_id,
            Recipe.id.in_(recipe_ids),
        )
    ).all()
    return {row.id: row.is_verified for row in rows}


# ---------------------------------------------------------------------------
# Merge hook: repoint ingredient_lines after catalog merge
# ---------------------------------------------------------------------------


def repoint_ingredient_lines(db: Session, source_id: uuid.UUID, target_id: uuid.UUID) -> None:
    """Bulk UPDATE ingredient_lines.canonical_ingredient_id from source to target.

    Called by catalog_service.merge via the registered hook.
    """
    db.execute(
        update(IngredientLine)
        .where(IngredientLine.canonical_ingredient_id == source_id)
        .values(canonical_ingredient_id=target_id)
    )
    db.flush()


# ---------------------------------------------------------------------------
# Register hooks at import time (after repoint_ingredient_lines is defined)
# ---------------------------------------------------------------------------

register_hooks()
