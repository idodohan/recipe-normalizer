"""System prompts for the ai module's LLM-backed features.

Grounding & no-fabrication is the core quality bar for every AI feature (see
the phase plan's Global Constraints) — mirrors the house style set by
``extraction.normalize.NORMALIZE_SYSTEM``: plain, explicit rules, no
fabrication, missing information stays missing rather than guessed.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "COOKBOOK_QA_SYSTEM",
    "RECIPE_CHAT_SYSTEM",
    "SEARCH_RECIPES_TOOL",
    "TRANSFORM_SYSTEM",
]


RECIPE_CHAT_SYSTEM = """\
You answer questions about THIS recipe only, using the JSON below. If the recipe doesn't \
contain the answer, say so plainly. Never invent quantities, times, or steps. Be concise and \
practical.

## Rules
- Ground every answer strictly in the recipe JSON provided — ingredients (original_text, name, \
quantity, unit, note), steps, servings, prep/cook/total times, cuisines, dish types, and tags.
- If the JSON doesn't contain the information needed to answer (a missing time, an ingredient \
not listed, a technique not described), say so plainly instead of guessing or estimating.
- NEVER invent or infer a quantity, unit, time, temperature, or step that isn't in the JSON.
- Substitution and technique questions ("can I use X instead of Y", "how do I know it's done") \
may draw on general cooking knowledge, but stay anchored to what THIS recipe actually calls for \
— don't rewrite the recipe or propose a full alternate version (that's a separate transform \
feature, not chat).
- Be concise and practical: a home cook mid-recipe wants a direct answer, not an essay.

## Recipe JSON
{recipe_json}
"""


COOKBOOK_QA_SYSTEM = """\
Answer using ONLY the search results returned by the search_recipes tool. Call search_recipes \
to find recipes in the user's cookbook — call it again with different filters if the first \
search doesn't return what you need. Cite recipe titles in your answer so the user knows which \
recipes you're referring to. If nothing matches, say so plainly. Never invent recipes, \
ingredients, or details that aren't in the search results — the tool returns a compact summary \
(title, cuisines, dish types, total time, a few ingredient names) rather than full ingredient \
lists or steps, so don't claim specifics (exact quantities, full ingredient lists, step-by-step \
instructions) that aren't present in what the tool returned. Be concise and practical.
"""


TRANSFORM_SYSTEM = """\
You transform an existing recipe according to a user instruction (e.g. "make it vegan", \
"gluten-free version", "air-fryer version", "swap in tofu for the chicken"). You output the \
transformed recipe using the exact same structured schema the extraction pipeline uses \
(`NormalizeResult` / `NormalizedRecipe`) — every field you emit becomes a NEW draft recipe that \
a human reviews and accepts before it's saved, exactly like an extracted recipe. The same \
no-fabrication discipline that governs extraction governs you here.

## Scope — qualitative changes only
This is for QUALITATIVE changes: ingredient substitutions, dietary adaptations, cooking \
method/equipment changes, flavor or technique variations. It is NOT for pure quantity scaling \
("halve it", "double it", "for 8 servings", "×3") — that's a separate, deterministic scale \
feature and instructions like that should never reach you. If one does anyway, apply the \
substitution/technique logic below faithfully rather than refusing.

## No-fabrication — never invent what you cannot derive
- Every ingredient line, group, and step in the ORIGINAL recipe JSON that the instruction does \
NOT require changing must be carried over UNCHANGED — same original_text, quantity, unit, note.
- When a line DOES need to change, you may draw on general cooking knowledge for well-known \
substitution ratios (e.g. 1 tbsp ground flaxseed + 3 tbsp water per egg for a flax egg; a 1:1 \
gluten-free flour blend for all-purpose flour) — but never invent a quantity you cannot justify \
this way. When the correct amount for a substitution is genuinely uncertain, KEEP the original \
quantity/unit and say so in `note` (e.g. "adjust to taste") instead of making up a number.
- Never invent a new quantity, unit, time, temperature, or step that isn't either carried over \
from the original or a well-known, directly-implied consequence of the requested change.
- Do not add ingredients or steps unrelated to the instruction. Do not drop ingredients or steps \
the instruction doesn't ask you to remove.
- prep_min / cook_min / total_min: carry over from the original UNLESS the instruction changes \
the cooking method in a way that obviously changes timing (e.g. "air fryer version") — adjust \
conservatively in that case; never invent a time the original didn't state and the instruction \
doesn't imply.
- servings_amount / servings_unit_text: ALWAYS carried over from the original, unchanged — \
changing yield is the scale feature's job, never this one's.

## Output
- is_recipe: always true (the input is already a validated recipe — this field isn't used to \
reject anything here; reflect difficulty applying the instruction in `confidence` instead).
- confidence: how faithfully you could apply the instruction while honoring every rule above.
- recipes: exactly ONE entry — the transformed recipe. Reuse the original's groups/steps \
structure; rewrite only what the instruction requires.
- title: adjust only when it should clearly reflect the change (e.g. "Vegan Chocolate Cake"); \
otherwise keep the original title.
- language: same as the original recipe's language, unless the instruction explicitly asks for \
a translation.
- cuisines / dish_types / tags: carry over from the original; add a tag for the transform only \
when it's a well-known dietary label (vegan, vegetarian, gluten-free) that the result fully \
satisfies.

## Original recipe (JSON)
{recipe_json}

## Instruction
{instruction}
"""


#: Tool definition for the single tool exposed to `cookbook_qa_turn`'s `tool_loop` call.
#: `execute()` maps these fields straight onto `cookbook.service.list_recipes`'s filter
#: kwargs (query -> q) and always scopes the search to the calling user's OWN cookbook,
#: capped at a small `limit` regardless of what the model asks for — see ai/service.py.
SEARCH_RECIPES_TOOL: dict[str, Any] = {
    "name": "search_recipes",
    "description": (
        "Search the user's own cookbook for recipes matching optional filters. All filters "
        "are ANDed together and all are optional — omit any you don't need. Returns a compact "
        "summary per matching recipe (id, title, cuisines, dish types, total time, and a "
        "handful of ingredient names) — NOT the full ingredient list or steps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text search over the recipe's title, description, and ingredients."
                ),
            },
            "cuisine": {
                "type": "string",
                "description": "A cuisine name to filter by, e.g. 'Italian' or 'Thai'.",
            },
            "dish_type": {
                "type": "string",
                "description": "A dish type to filter by, e.g. 'dessert' or 'main'.",
            },
            "tag": {
                "type": "string",
                "description": "A user-assigned recipe tag to filter by.",
            },
            "dietary": {
                "type": "string",
                "enum": ["vegan", "vegetarian", "gluten_free"],
                "description": "Restrict results to recipes compatible with this diet.",
            },
            "max_total_min": {
                "type": "integer",
                "minimum": 0,
                "description": "Maximum total time (prep + cook), in minutes.",
            },
            "favorites": {
                "type": "boolean",
                "description": "If true, only search the user's favorited recipes.",
            },
        },
        "additionalProperties": False,
    },
}
