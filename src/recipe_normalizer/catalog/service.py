"""Catalog service: matching, creation, search, merge, and update operations.

Re-exports CanonicalIngredient, IngredientStatus, convert_to_normalized, and
Converted so that non-catalog modules can import from here and never need to
reach into catalog.models or catalog.conversion directly (import-linter
enforces this boundary).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from recipe_normalizer.catalog.conversion import Converted, convert_to_normalized
from recipe_normalizer.catalog.models import (
    CanonicalIngredient,
    IngredientAlias,
    IngredientStatus,
    PreferredMeasure,
)
from recipe_normalizer.catalog.units import parse_unit
from recipe_normalizer.errors import ApiError

__all__ = [
    "UNSET",
    "CanonicalIngredient",
    "Converted",
    "IngredientStatus",
    "PreferredMeasure",
    "convert_to_normalized",
    "create_unreviewed",
    "get_ingredient",
    "match",
    "merge",
    "register_merge_hook",
    "search",
    "update_ingredient",
]

# Public sentinel for "field not provided" in update_ingredient, allowing
# callers to explicitly pass None (e.g. to clear density_g_per_ml).
UNSET: Any = object()

# ---------------------------------------------------------------------------
# Merge hook registry (module-level list; mutated by register_merge_hook)
# ---------------------------------------------------------------------------

_MERGE_HOOKS: list[Callable[[Session, uuid.UUID, uuid.UUID], None]] = []


def register_merge_hook(cb: Callable[[Session, uuid.UUID, uuid.UUID], None]) -> None:
    """Register *cb* to be called after every successful merge.

    Hooks are called in registration order with (db, source_id, target_id).
    """
    _MERGE_HOOKS.append(cb)


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Strip, collapse internal whitespace, and lowercase.

    Uses str.lower() — NOT casefold() — so the Python-side normalisation
    agrees with the SQL ``func.lower()`` comparisons it is matched against
    (casefold diverges from SQL lower, e.g. German ß → "ss").
    """
    return " ".join(text.strip().split()).lower()


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------


def match(db: Session, text: str) -> CanonicalIngredient | None:
    """Return the live CanonicalIngredient matching *text*, or None.

    Matching is case-insensitive and whitespace-insensitive.  Checks aliases
    first, then falls back to the ingredient name.  If the matched row has
    ``merged_into_id`` set the chain is followed to the live target (cycle-safe).
    """
    norm = _normalise(text)
    if not norm:
        return None

    # Try alias table first
    alias_row = db.scalars(
        select(IngredientAlias).where(func.lower(IngredientAlias.alias) == norm)
    ).first()

    if alias_row is not None:
        ingredient = db.get(CanonicalIngredient, alias_row.ingredient_id)
    else:
        # Fall back to canonical name
        ingredient = db.scalars(
            select(CanonicalIngredient).where(func.lower(CanonicalIngredient.name) == norm)
        ).first()

    if ingredient is None:
        return None

    # Follow merged_into_id chain; on a cycle there is no live target,
    # so return None rather than a dead (merged) node.
    visited: set[uuid.UUID] = {ingredient.id}
    while ingredient.merged_into_id is not None:
        next_id = ingredient.merged_into_id
        if next_id in visited:
            return None  # cycle detected — no live ingredient exists
        visited.add(next_id)
        next_ing = db.get(CanonicalIngredient, next_id)
        if next_ing is None:
            return None  # dangling pointer — no live ingredient exists
        ingredient = next_ing

    return ingredient


# ---------------------------------------------------------------------------
# create_unreviewed
# ---------------------------------------------------------------------------


def create_unreviewed(db: Session, *, name: str) -> CanonicalIngredient:
    """Return an existing or newly created unreviewed ingredient for *name*.

    The name itself is stored as an alias with language ``"und"`` (undetermined).
    Calling this twice with the same name returns the existing row unchanged.
    """
    existing = db.scalars(
        select(CanonicalIngredient).where(CanonicalIngredient.name == name)
    ).first()
    if existing is not None:
        return existing

    ingredient = CanonicalIngredient(
        name=name,
        category="uncategorized",
        preferred_measure=PreferredMeasure.mass,
        status=IngredientStatus.unreviewed,
        dietary_flags=[],
        gram_weights={},
    )
    ingredient.aliases = [IngredientAlias(alias=name, language="und")]
    db.add(ingredient)
    db.flush()
    return ingredient


# ---------------------------------------------------------------------------
# get_ingredient
# ---------------------------------------------------------------------------


def get_ingredient(db: Session, ingredient_id: uuid.UUID) -> CanonicalIngredient:
    """Return the ingredient with *ingredient_id* or raise ApiError 404."""
    ing = db.get(CanonicalIngredient, ingredient_id)
    if ing is None:
        raise ApiError(404, "not_found", f"Ingredient {ingredient_id} not found.")
    return ing


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def search(
    db: Session,
    q: str,
    *,
    status: IngredientStatus | None = None,
    limit: int = 50,
) -> list[CanonicalIngredient]:
    """Return up to *limit* ingredients whose name or any alias contains *q*.

    The search is case-insensitive substring matching.  Pass *status* to
    filter by lifecycle status.
    """
    stmt = select(CanonicalIngredient)

    if q:
        # lower() (not casefold) to agree with the SQL func.lower() side;
        # escape LIKE wildcards so user input is matched literally.
        escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        norm_q = f"%{escaped}%"
        # Match on name OR any alias
        alias_match = select(IngredientAlias.ingredient_id).where(
            func.lower(IngredientAlias.alias).like(norm_q, escape="\\")
        )
        stmt = stmt.where(
            or_(
                func.lower(CanonicalIngredient.name).like(norm_q, escape="\\"),
                CanonicalIngredient.id.in_(alias_match),
            )
        )

    if status is not None:
        stmt = stmt.where(CanonicalIngredient.status == status)

    stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def merge(
    db: Session,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> CanonicalIngredient:
    """Merge *source* into *target*.

    - Re-points all source aliases to target.
    - Adds source.name as a new alias on target (language "und").
    - Sets source.merged_into_id = target_id.
    - Calls registered hooks in order.
    - Flushes (caller owns the transaction/commit).

    Guards:
    - source == target → ApiError 422
    - source already merged → ApiError 409
    - unknown ids → ApiError 404
    """
    if source_id == target_id:
        raise ApiError(422, "invalid_merge", "Cannot merge an ingredient into itself.")

    source = db.get(CanonicalIngredient, source_id)
    if source is None:
        raise ApiError(404, "not_found", f"Source ingredient {source_id} not found.")

    target = db.get(CanonicalIngredient, target_id)
    if target is None:
        raise ApiError(404, "not_found", f"Target ingredient {target_id} not found.")

    if source.merged_into_id is not None:
        raise ApiError(
            409,
            "already_merged",
            f"Source ingredient {source_id} has already been merged.",
        )

    # Re-point all source aliases to target
    for alias in list(source.aliases):
        alias.ingredient_id = target_id

    # Add source name as alias on target — but only if not already present
    # (case-normalized) among target's aliases after re-pointing, otherwise
    # a source alias equal to source.name would be duplicated.
    existing_aliases = {_normalise(a.alias) for a in target.aliases}
    existing_aliases |= {_normalise(a.alias) for a in source.aliases}
    if _normalise(source.name) not in existing_aliases:
        db.add(IngredientAlias(ingredient_id=target_id, alias=source.name, language="und"))

    # Mark source as merged
    source.merged_into_id = target_id

    db.flush()

    # Refresh target to pick up new aliases
    db.refresh(target)

    # Fire hooks in registration order
    for hook in _MERGE_HOOKS:
        hook(db, source_id, target_id)

    return target


# ---------------------------------------------------------------------------
# update_ingredient
# ---------------------------------------------------------------------------


def update_ingredient(
    db: Session,
    ingredient_id: uuid.UUID,
    *,
    name: str | None = None,
    category: str | None = None,
    preferred_measure: PreferredMeasure | None = None,
    status: IngredientStatus | None = None,
    dietary_flags: list[str] | None = None,
    density_g_per_ml: float | None | Any = UNSET,
    gram_weights: dict[str, float] | None = None,
) -> CanonicalIngredient:
    """Patch *ingredient_id* with the supplied fields.

    Only supplied (non-None) fields are updated.  ``density_g_per_ml`` uses
    the public ``UNSET`` sentinel so callers can explicitly set it to ``None``
    (clearing it).

    Raises ApiError 404 if the ingredient does not exist.
    Raises ApiError 422 for invalid gram_weights keys or zero/negative density.
    """
    ing = get_ingredient(db, ingredient_id)

    if name is not None:
        ing.name = name

    if category is not None:
        ing.category = category

    if preferred_measure is not None:
        ing.preferred_measure = preferred_measure

    if status is not None:
        ing.status = status

    # dietary_flags: assign new list (JSONB change-tracking)
    if dietary_flags is not None:
        ing.dietary_flags = list(dietary_flags)

    # density_g_per_ml: UNSET sentinel distinguishes "not provided" from None
    if density_g_per_ml is not UNSET:
        if density_g_per_ml is not None and density_g_per_ml <= 0:
            raise ApiError(
                422,
                "invalid_density",
                "density_g_per_ml must be positive.",
            )
        ing.density_g_per_ml = density_g_per_ml

    # gram_weights: validate keys, then assign new dict (JSONB change-tracking)
    if gram_weights is not None:
        for key, grams in gram_weights.items():
            unit = parse_unit(key)
            if unit is None or unit.token != key:
                raise ApiError(
                    422,
                    "invalid_gram_weight_key",
                    f"gram_weights key {key!r} is not a canonical unit token.",
                )
            if grams <= 0:
                raise ApiError(
                    422,
                    "invalid_gram_weight_value",
                    f"gram_weights[{key!r}] must be positive.",
                )
        ing.gram_weights = dict(gram_weights)

    db.flush()
    return ing
