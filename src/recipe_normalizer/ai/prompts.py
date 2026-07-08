"""System prompts for the ai module's LLM-backed features.

Grounding & no-fabrication is the core quality bar for every AI feature (see
the phase plan's Global Constraints) — mirrors the house style set by
``extraction.normalize.NORMALIZE_SYSTEM``: plain, explicit rules, no
fabrication, missing information stays missing rather than guessed.
"""

from __future__ import annotations

from typing import Any

__all__ = ["COOKBOOK_QA_SYSTEM", "RECIPE_CHAT_SYSTEM", "SEARCH_RECIPES_TOOL"]


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
