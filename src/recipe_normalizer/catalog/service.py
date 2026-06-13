"""Catalog service: matching, creation, search, merge, and update operations.

Re-exports CanonicalIngredient, IngredientStatus, convert_to_normalized, and
Converted so that non-catalog modules can import from here and never need to
reach into catalog.models or catalog.conversion directly (import-linter
enforces this boundary).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from rapidfuzz import fuzz, process
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

if TYPE_CHECKING:
    from recipe_normalizer.llm.client import LLMClient

__all__ = [
    "UNSET",
    "CanonicalIngredient",
    "Converted",
    "IngredientStatus",
    "PreferredMeasure",
    "_MatchChoice",
    "convert_to_normalized",
    "create_unreviewed",
    "get_ingredient",
    "match",
    "match_or_create",
    "merge",
    "register_merge_hook",
    "search",
    "update_ingredient",
]


# ---------------------------------------------------------------------------
# LLM output model for catalog matching
# ---------------------------------------------------------------------------


class _MatchChoice(BaseModel):
    index: int | None  # 0-based index into the candidate list, or null if none match


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


def names_for_ids(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Map canonical-ingredient ids to their names (for round-tripping links)."""
    if not ids:
        return {}
    rows = db.scalars(select(CanonicalIngredient).where(CanonicalIngredient.id.in_(ids))).all()
    return {row.id: row.name for row in rows}


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
# match_or_create
# ---------------------------------------------------------------------------


def match_or_create(
    db: Session,
    name: str,
    *,
    llm: LLMClient | None = None,
) -> CanonicalIngredient:
    """Return the best matching live ingredient for *name*, or create an unreviewed one.

    Resolution order:
    1. Exact / alias match via ``match()`` → return immediately.
    2. Build a corpus of (normalised_string, ingredient_id) for all live ingredients
       — fetched once per call (~1 000 rows fine; add a caching layer later if needed).
    3. ``rapidfuzz`` ``WRatio ≥ 90`` hit → follow merge chain and return.
    4. Top-5 candidates in the 70–90 band → if *llm* is provided, ask once:
       valid index in range → follow merge chain and return.
    5. ``create_unreviewed(db, name=name)`` — idempotent.
    """
    # Step 1: exact / alias match
    exact = match(db, name)
    if exact is not None:
        return exact

    norm_name = _normalise(name)

    # Step 2: build corpus — all (alias_or_name, ingredient_id) for live ingredients
    # Fetch every (alias_text, ingredient_id) pair, plus (name, ingredient_id) as fallback.
    # JOIN filters to live (unmerged) ingredients — merge() re-points aliases so this
    # holds by invariant today, but we enforce it here rather than relying on it.
    alias_rows = db.execute(
        select(IngredientAlias.alias, IngredientAlias.ingredient_id)
        .join(CanonicalIngredient, IngredientAlias.ingredient_id == CanonicalIngredient.id)
        .where(CanonicalIngredient.merged_into_id.is_(None))
    ).all()
    # Include canonical names that have no aliases (alias table may omit them).
    name_rows = db.execute(
        select(CanonicalIngredient.name, CanonicalIngredient.id).where(
            CanonicalIngredient.merged_into_id.is_(None)
        )
    ).all()

    # corpus: list of normalised strings; corpus_ids: parallel list of ingredient UUIDs
    corpus: list[str] = []
    corpus_ids: list[uuid.UUID] = []
    for alias_text, ing_id in alias_rows:
        corpus.append(_normalise(alias_text))
        corpus_ids.append(ing_id)
    for ing_name, ing_id in name_rows:
        corpus.append(_normalise(ing_name))
        corpus_ids.append(ing_id)

    if not corpus:
        return create_unreviewed(db, name=name)

    # Step 3: fuzzy ≥ 90 → direct link
    best = process.extractOne(norm_name, corpus, scorer=fuzz.WRatio, score_cutoff=90)
    if best is not None:
        _match_string, _score, corpus_idx = best
        ing_id = corpus_ids[corpus_idx]
        ingredient = _follow_merge_chain(db, ing_id)
        if ingredient is not None:
            return ingredient

    # Step 4: collect 70–90 band candidates
    band_results = process.extract(norm_name, corpus, scorer=fuzz.WRatio, limit=5, score_cutoff=70)
    band_candidates = [r for r in band_results if r[1] < 90]

    if band_candidates and llm is not None:
        numbered = "\n".join(f"{i}. {r[0]}" for i, r in enumerate(band_candidates))
        content = (
            f"Ingredient: '{name}'. Candidates:\n{numbered}\n"
            "Which candidate (0-based index) IS this ingredient "
            "(same food, ignoring qualifiers like fat % or brand)? null if none."
        )
        choice = llm.structured(
            feature="catalog.match",
            fast=True,
            output_model=_MatchChoice,
            system="You match a recipe ingredient name to a catalog entry.",
            content=content,
        )
        if choice.index is not None and 0 <= choice.index < len(band_candidates):
            _match_string, _score, corpus_idx = band_candidates[choice.index]
            ing_id = corpus_ids[corpus_idx]
            ingredient = _follow_merge_chain(db, ing_id)
            if ingredient is not None:
                return ingredient

    # Step 5: fallthrough — create unreviewed (idempotent)
    return create_unreviewed(db, name=name)


def _follow_merge_chain(db: Session, ingredient_id: uuid.UUID) -> CanonicalIngredient | None:
    """Follow merged_into_id chain from *ingredient_id* to a live ingredient.

    Returns None on dangling pointers or cycles.
    """
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        return None
    visited: set[uuid.UUID] = {ingredient.id}
    while ingredient.merged_into_id is not None:
        next_id = ingredient.merged_into_id
        if next_id in visited:
            return None
        visited.add(next_id)
        next_ing = db.get(CanonicalIngredient, next_id)
        if next_ing is None:
            return None
        ingredient = next_ing
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
