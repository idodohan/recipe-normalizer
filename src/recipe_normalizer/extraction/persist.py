"""Persist NormalizeResult drafts into the cookbook (spec §6.3 steps 2-3, §6.4 gate).

Extraction stays stateless: this module receives plain values (owner, source,
fingerprint, meta) and returns created recipe ids — the worker owns job
bookkeeping. Deterministic post-processing (catalog matching, unit conversion)
happens inside cookbook.service.create_recipe.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from recipe_normalizer.cookbook import service as cookbook_service
from recipe_normalizer.cookbook.schemas import (
    IngredientGroupIn,
    IngredientLineIn,
    RecipeIn,
    ServingsIn,
    StepIn,
)
from recipe_normalizer.cookbook.service import SourceType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from recipe_normalizer.extraction.normalize import NormalizedRecipe, NormalizeResult
    from recipe_normalizer.llm.client import LLMClient

__all__ = ["persist_drafts"]

logger = logging.getLogger(__name__)

_MAX_TITLE_LEN = 300


def _valid_minutes(value: int | None) -> int | None:
    """Coerce negative LLM time fields to None (mirror the quantity pattern)."""
    return value if value is not None and value >= 0 else None


def _to_recipe_in(normalized: NormalizedRecipe) -> RecipeIn | None:
    """Map a NormalizedRecipe to RecipeIn, dropping empty groups/lines/steps.

    Returns None when no valid ingredient line survives (caller skips the draft).
    """
    groups: list[IngredientGroupIn] = []
    for group in normalized.groups:
        lines = [
            IngredientLineIn(
                original_text=line.original_text,
                # str() first so 0.1 becomes Decimal("0.1"), not binary-float noise.
                quantity=(
                    Decimal(str(line.quantity)) if line.quantity and line.quantity > 0 else None
                ),
                unit=line.unit,
                name=line.name,
                note=line.note,
                is_optional=line.is_optional,
            )
            for line in group.lines
            if line.original_text.strip()
        ]
        if lines:
            groups.append(IngredientGroupIn(name=group.name, lines=lines))

    if not groups:
        return None

    amount = normalized.servings_amount
    servings: ServingsIn | None = None
    if (amount is not None and amount > 0) or normalized.servings_unit_text:
        servings = ServingsIn(
            amount=amount if amount is not None and amount > 0 else None,
            unit_text=normalized.servings_unit_text,
        )

    return RecipeIn(
        title=normalized.title.strip()[:_MAX_TITLE_LEN] or "Untitled recipe",
        description=normalized.description,
        language=normalized.language,
        servings=servings,
        prep_min=_valid_minutes(normalized.prep_min),
        cook_min=_valid_minutes(normalized.cook_min),
        total_min=_valid_minutes(normalized.total_min),
        cuisines=normalized.cuisines,
        dish_types=normalized.dish_types,
        tags=normalized.tags,
        groups=groups,
        steps=[
            StepIn(original_text=step.original_text)
            for step in normalized.steps
            if step.original_text.strip()
        ],
    )


def _content_fingerprint(data: RecipeIn) -> str:
    """A stable content key for within-result duplicate detection.

    Sources like social posts can carry the same recipe text twice (e.g. an
    embedded/suggested copy rendered in the DOM), and the LLM then returns two
    identical recipes. Compare on the *normalized* content the model emitted —
    title, ingredient text lines, and steps — so two copies of the same dish
    collapse into one draft.
    """
    lines = "\n".join(f"{g.name or ''}:{line.original_text}" for g in data.groups for line in g.lines)
    steps = "\n".join(s.original_text for s in data.steps)
    return f"{data.title}|{lines}|{steps}".casefold()


def persist_drafts(
    db: Session,
    *,
    owner_id: uuid.UUID,
    result: NormalizeResult,
    llm: LLMClient,
    source: str | None,
    source_type: SourceType,
    source_fingerprint: str | None,
    extraction_meta: dict[str, Any] | None,
    image_ref: str | None,
    derived_from: uuid.UUID | None = None,
    provenance: dict[str, Any] | None = None,
) -> list[uuid.UUID]:
    """Persist each extracted recipe as an unverified draft; return ids in order.

    - Catalog matching runs with the provided *llm* (fuzzy + LLM-assisted).
    - The fingerprint goes ONLY on the first created draft — the partial unique
      index on (owner_id, source_fingerprint) is per-recipe, so N drafts from
      one source would collide.
    - DuplicateRecipeError from the fingerprinted draft propagates to the caller.
    - Within one *result*, recipes whose normalized content is identical (same
      title, ingredient lines, and steps) collapse to a single draft — e.g. a
      social page that renders the same recipe text twice.
    - *derived_from* / *provenance*, when given, are stamped on EVERY draft
      produced from *result* (plural results are rare — a transform's prompt
      asks for exactly one, but this stays correct if the model returns more).
      None/None (the default) for every normal extraction call site — this
      parameter pair exists for `ai.service.transform_recipe`, which reuses
      this function to convert its transformed `NormalizeResult` into a
      reviewable draft exactly like an extracted one (see cookbook.service.
      create_recipe's docstring for what these fields mean).
    """
    seen: set[str] = set()
    created: list[uuid.UUID] = []
    for index, normalized in enumerate(result.recipes):
        data = _to_recipe_in(normalized)
        if data is None:
            logger.warning(
                "skipping draft %d (%r): no valid ingredient lines", index, normalized.title
            )
            continue
        key = _content_fingerprint(data)
        if key in seen:
            logger.info(
                "skipping draft %d (%r): duplicate of an earlier recipe in the same result",
                index,
                normalized.title,
            )
            continue
        seen.add(key)
        out = cookbook_service.create_recipe(
            db,
            owner_id=owner_id,
            data=data,
            source_fingerprint=source_fingerprint if not created else None,
            source=source,
            source_type=source_type,
            llm=llm,
            extraction_meta=extraction_meta,
            image_ref=image_ref,
            derived_from=derived_from,
            provenance=provenance,
        )
        created.append(out.id)
    return created
