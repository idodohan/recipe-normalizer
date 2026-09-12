"""The shared normalize stage (spec §6.3): acquired content → structured recipes.

Every input type funnels through ``normalize()`` exactly once — one
structured-output LLM pass, Pydantic-validated. Deterministic post-processing
(unit conversion, catalog matching) happens later in persist via the cookbook
service; the LLM never does unit math.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from recipe_normalizer.llm.client import image_block

if TYPE_CHECKING:
    from recipe_normalizer.extraction.base import Acquired
    from recipe_normalizer.llm.client import LLMClient

__all__ = [
    "NORMALIZE_SYSTEM",
    "NormalizeResult",
    "NormalizedGroup",
    "NormalizedLine",
    "NormalizedRecipe",
    "NormalizedStep",
    "normalize",
]


# ---------------------------------------------------------------------------
# Structured-output schema (mirrors RecipeIn; plain floats/strings for the LLM)
# ---------------------------------------------------------------------------


class NormalizedLine(BaseModel):
    original_text: str
    name: str | None = None  # English canonical-ish ingredient name for catalog matching
    quantity: float | None = None
    unit: str | None = None
    note: str | None = None
    is_optional: bool = False


class NormalizedGroup(BaseModel):
    name: str | None = None
    lines: list[NormalizedLine]


class NormalizedStep(BaseModel):
    original_text: str


class NormalizedRecipe(BaseModel):
    title: str
    description: str | None = None
    language: str = "en"  # Always "en" — the stored recipe is English (translated when the source is not English).
    servings_amount: float | None = None
    servings_unit_text: str | None = None
    prep_min: int | None = None
    cook_min: int | None = None
    total_min: int | None = None
    cuisines: list[str] = Field(default_factory=list)
    dish_types: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    groups: list[NormalizedGroup] = Field(default_factory=list)
    steps: list[NormalizedStep] = Field(default_factory=list)


class NormalizeResult(BaseModel):
    is_recipe: bool
    confidence: float = 0.0  # 0..1
    reason: str | None = None  # human-readable when not a recipe / low confidence
    recipes: list[NormalizedRecipe] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompt — this IS the product's extraction quality. Edit with care.
# ---------------------------------------------------------------------------

NORMALIZE_SYSTEM = """\
You are the recipe extraction engine of a recipe-normalization product. You receive raw acquired \
content — text and/or images of a recipe source (web page text, scanned cookbook page, \
handwritten card, pasted text) — and convert it into structured recipe data. Follow these rules \
exactly.

## Recipe detection — never fabricate
- Set is_recipe=true only if the content actually contains at least one recipe (a dish or drink \
with ingredients and/or preparation instructions).
- Articles, news, menus, shopping lists, restaurant reviews, nutrition essays, unrelated photos, \
etc. are NOT recipes: return is_recipe=false, a short human-readable reason (e.g. "This is a \
restaurant menu, not a recipe"), and an empty recipes list.
- NEVER invent, complete, or guess a recipe that is not present in the content. Missing values \
stay null. Extracting less, correctly, always beats guessing.
- confidence: your overall extraction confidence, 0.0 to 1.0. A clean, complete recipe is 0.9+. \
Partial, blurry, truncated, contradictory, or ambiguous content lowers it accordingly.

## Multi-recipe sources
- Return one recipes[] entry per distinct recipe. Most sources contain exactly one; roundup \
pages, cookbook chapters, and multi-recipe documents yield several — extract each completely and \
separately.
- Do NOT split one recipe's sub-components (sauce, dough, frosting) into separate recipes; those \
are ingredient groups within the single recipe.

## English output (critical)
- The stored recipe is ALWAYS in English: title, description, group names, \
per-ingredient original_text, steps, servings_unit_text, and cuisines/tags are \
translated into natural, fluent English when the source is not English (Hebrew, \
French, etc. → English). Never leave non-English recipe text in the output.
- original_text fields: keep the MEANING and full detail of the source line \
(quantities, units, qualifiers, preparation notes) but render it in English. \
Only numbers, decimals and unicode fractions stay as-is.
- Keep the structure of the source: the same number of ingredient lines and \
steps, in the same order — translate, do not merge, split, or rewrite content.
- Group names are translated too ("לציפוי" → "For the glaze").
- language: always "en".

## Per ingredient line
- original_text: the complete line translated into English (e.g. "2 כוסות קמח \
לכל מטרה" → "2 cups all-purpose flour"). Keep quantities, units, and qualifiers.
- name: the canonical ingredient name in ENGLISH — this is used for catalog \
matching, not display. Translate to English when the source is non-English \
(קמח לכל מטרה → "all-purpose flour"). Keep it minimal: drop quantities, units, \
and preparation ("chopped", "sifted", "cold"). Null only when the line names no \
identifiable ingredient.
- quantity: a number. Convert fractions and unicode fractions to decimals (1/2 → 0.5, 1½ → 1.5, \
¾ → 0.75). For ranges ("2-3 cloves") use the lower bound and record the range in note. Null when \
no quantity is given ("salt to taste").
- unit: the unit in English as a short singular token ("cups" → cup, \
"tablespoons" → tbsp, "grams" → g; "כוס" → cup). Null when there is no unit \
(count items like "2 eggs").
- note: qualifiers and preparation, in English ("finely chopped", "room \
temperature", "or to taste", "2-3"). Null if none.
- is_optional: true when the source marks the ingredient optional ("optional", "if desired", \
"אופציונלי").

## Structure
- groups: preserve the source's ingredient groupings, translated to English \
("For the dough", "לציפוי" → "For the glaze"). When the source has no \
groupings, return exactly one group with name=null containing all lines.
- steps: split the instructions into individual steps, each translated to \
English. Numbered/paragraph breaks in the source define the split. Do not \
merge, reorder, summarize, or rewrite steps.

## Metadata
- servings_amount / servings_unit_text: the stated yield ("Serves 4" → 4 + "servings"; "makes \
12 cookies" → 12 + "cookies"). Null when not stated.
- prep_min / cook_min / total_min: stated times in whole minutes ("1 hour 20 min" → 80). Null \
when not stated — never estimate.
- dish_types: choose ONLY from this exact list, when applicable: cocktail, drink, smoothie, \
main, dessert, side, breakfast, soup, salad, bread, sauce, snack. Use the empty list when none \
clearly applies.
- cuisines and tags: free-form but conservative — include only what the source clearly supports \
(stated cuisine, prominent dietary labels). Empty lists are fine.
- title: required, in English (translate from the source when needed). description: a short \
description from the source translated into English when present; null otherwise — do not write \
your own.
"""


# ---------------------------------------------------------------------------
# The shared LLM pass
# ---------------------------------------------------------------------------


def normalize(acquired: Acquired, *, llm: LLMClient) -> NormalizeResult:
    """Run the one shared structured-output pass over acquired text/images.

    Empty input (no text, no images) short-circuits to is_recipe=False without
    spending an LLM call.
    """
    blocks: list[dict[str, Any]] = []
    if acquired.text and acquired.text.strip():
        blocks.append({"type": "text", "text": acquired.text})
    for data, media_type in acquired.images:
        blocks.append(image_block(data, media_type))

    if not blocks:
        return NormalizeResult(is_recipe=False, reason="no content acquired")

    return llm.structured(
        feature="extract.normalize",
        output_model=NormalizeResult,
        system=NORMALIZE_SYSTEM,
        content=blocks,
        max_tokens=16000,
    )
