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
from sqlalchemy import Boolean, and_, func, not_, or_, select
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement, Select

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
    "FuzzyCorpus",
    "IngredientStatus",
    "PreferredMeasure",
    "_MatchChoice",
    "convert_to_normalized",
    "create_unreviewed",
    "dietary_incompatible_ingredient_ids",
    "get_ingredient",
    "match",
    "match_or_create",
    "merge",
    "register_merge_hook",
    "search",
    "update_ingredient",
]

# ---------------------------------------------------------------------------
# Dietary flags
# ---------------------------------------------------------------------------
#
# ``CanonicalIngredient.dietary_flags`` (see catalog/models.py) is a JSONB
# list of raw *allergen/composition* tags drawn from
# ``seed_loader.ALLOWED_DIETARY_FLAGS``:
#   "contains-gluten", "dairy", "egg", "animal-product", "fish", "shellfish",
#   "nut", "peanut", "soy", "sesame", "alcohol".
# These are NOT the same strings as the user-facing diet filters
# ("vegan" / "vegetarian" / "gluten_free") — the mapping below derives one
# from the other. Every meat/poultry/fish/dairy/egg/honey entry in the seed
# data carries "animal-product" (dairy rows additionally carry "dairy", egg
# rows additionally carry "egg"); meat, poultry, and honey carry
# "animal-product" alone.
#
#   - gluten_free: disqualified by "contains-gluten".
#   - vegan:       disqualified by "animal-product" (which already covers
#                  dairy/egg/fish/shellfish/honey/meat) — dairy/egg/fish/
#                  shellfish are listed too for robustness against future
#                  seed data that might carry one without "animal-product".
#   - vegetarian:  disqualified by "fish" or "shellfish", OR by
#                  "animal-product" *without* "dairy"/"egg" alongside it
#                  (i.e. meat/poultry/honey — dairy and eggs remain allowed).
#                  Note: this is a conservative approximation — honey has no
#                  distinct tag from meat/poultry in the seed data, so it is
#                  (incorrectly, strictly speaking) treated as non-vegetarian
#                  rather than risk treating meat as vegetarian.
_VEGAN_DISQUALIFYING_FLAGS = frozenset({"animal-product", "dairy", "egg", "fish", "shellfish"})
_GLUTEN_FREE_DISQUALIFYING_FLAGS = frozenset({"contains-gluten"})


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


def dietary_incompatible_ingredient_ids(diet: str) -> Select[tuple[uuid.UUID]]:
    """Return a ``Select`` of ``CanonicalIngredient.id`` incompatible with *diet*.

    *diet* is one of ``"vegan"``, ``"vegetarian"``, ``"gluten_free"``.  See the
    module-level comment above for the raw-flag → diet mapping. Callers (e.g.
    ``cookbook.service.list_recipes``) embed this as a subquery — e.g. via
    ``IngredientLine.canonical_ingredient_id.in_(...)`` — so they never need to
    import ``CanonicalIngredient`` directly (import-linter boundary).

    Raises ``ValueError`` for an unrecognised *diet*.
    """
    has_any = CanonicalIngredient.dietary_flags.op("?|", return_type=Boolean)
    has_one = CanonicalIngredient.dietary_flags.op("?", return_type=Boolean)

    condition: ColumnElement[bool]
    if diet == "gluten_free":
        condition = has_any(pg_array(sorted(_GLUTEN_FREE_DISQUALIFYING_FLAGS)))
    elif diet == "vegan":
        condition = has_any(pg_array(sorted(_VEGAN_DISQUALIFYING_FLAGS)))
    elif diet == "vegetarian":
        fish_or_shellfish = has_any(pg_array(["fish", "shellfish"]))
        meat_or_honey = and_(
            has_one("animal-product"),
            not_(has_any(pg_array(["dairy", "egg"]))),
        )
        condition = or_(fish_or_shellfish, meat_or_honey)
    else:
        raise ValueError(f"Unknown dietary filter: {diet!r}")
    return select(CanonicalIngredient.id).where(condition)


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


def _load_corpus(db: Session) -> tuple[list[str], list[uuid.UUID]]:
    """Materialise (normalised_string, ingredient_id) for every live ingredient.

    Returns two parallel lists: the normalised strings rapidfuzz scores against
    and the ingredient id each one belongs to.

    This reads the *entire* alias table plus every canonical name (no LIMIT), so
    it is the expensive part of ``match_or_create`` — see ``FuzzyCorpus`` for how
    callers that resolve many names in one request amortise it.
    """
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

    strings: list[str] = []
    ids: list[uuid.UUID] = []
    for alias_text, ing_id in alias_rows:
        strings.append(_normalise(alias_text))
        ids.append(ing_id)
    for ing_name, ing_id in name_rows:
        strings.append(_normalise(ing_name))
        ids.append(ing_id)
    return strings, ids


class FuzzyCorpus:
    """Lazily-built, reusable fuzzy-matching corpus for one unit of work.

    ``match_or_create`` only needs the corpus when a name misses the exact/alias
    index, and building it costs a full read of the alias + canonical tables.
    Resolving a whole recipe one line at a time therefore used to rebuild it once
    per unmatched line (a 40-line recipe ≈ 35 full-catalog materialisations in a
    single HTTP request).

    Pass ONE instance to every ``match_or_create`` call of a multi-name operation
    (see ``cookbook.service._apply_groups``) and the load happens at most once.

    Matching behaviour is unchanged: rows this run creates are appended via
    ``add`` so a later name still fuzzy-matches an ingredient an earlier name
    created, exactly as a fresh per-call load would. Scope an instance to a
    single request/session — it is a snapshot, not a process-wide cache.
    """

    __slots__ = ("_ids", "_seen_ids", "_strings")

    def __init__(self) -> None:
        self._strings: list[str] | None = None
        self._ids: list[uuid.UUID] = []
        self._seen_ids: set[uuid.UUID] = set()

    def entries(self, db: Session) -> tuple[list[str], list[uuid.UUID]]:
        """Return (strings, ids), loading them on first use."""
        if self._strings is None:
            self._strings, self._ids = _load_corpus(db)
            self._seen_ids = set(self._ids)
        return self._strings, self._ids

    def add(self, ingredient_id: uuid.UUID, text: str) -> None:
        """Record an ingredient created after the snapshot was taken.

        No-op when the corpus has not been built yet (a later build reads the row
        straight from the DB) or when the id is already present. A row that a
        fresh load would list twice (alias == name) is kept once here — same id,
        so the resolved match is identical.
        """
        if self._strings is None or ingredient_id in self._seen_ids:
            return
        self._strings.append(_normalise(text))
        self._ids.append(ingredient_id)
        self._seen_ids.add(ingredient_id)


def match_or_create(
    db: Session,
    name: str,
    *,
    llm: LLMClient | None = None,
    corpus: FuzzyCorpus | None = None,
) -> CanonicalIngredient:
    """Return the best matching live ingredient for *name*, or create an unreviewed one.

    Resolution order:
    1. Exact / alias match via ``match()`` → return immediately.
    2. Corpus of (normalised_string, ingredient_id) for all live ingredients.
       Pass *corpus* (a ``FuzzyCorpus``) when resolving several names in one
       operation so the underlying full-table read happens once instead of once
       per name; omitting it builds a throwaway corpus for this call.
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

    # Step 2: corpus (shared across the caller's names when one was passed in)
    fuzzy = corpus if corpus is not None else FuzzyCorpus()
    corpus_strings, corpus_ids = fuzzy.entries(db)

    if not corpus_strings:
        created = create_unreviewed(db, name=name)
        fuzzy.add(created.id, created.name)
        return created

    # Step 3: fuzzy ≥ 90 → direct link
    best = process.extractOne(norm_name, corpus_strings, scorer=fuzz.WRatio, score_cutoff=90)
    if best is not None:
        _match_string, _score, corpus_idx = best
        ing_id = corpus_ids[corpus_idx]
        ingredient = _follow_merge_chain(db, ing_id)
        if ingredient is not None:
            return ingredient

    # Step 4: collect 70–90 band candidates
    band_results = process.extract(
        norm_name, corpus_strings, scorer=fuzz.WRatio, limit=5, score_cutoff=70
    )
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

    # Step 5: fallthrough — create unreviewed (idempotent). Keep the shared
    # corpus in step with the DB so the caller's remaining names can match it.
    created = create_unreviewed(db, name=name)
    fuzzy.add(created.id, created.name)
    return created


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
