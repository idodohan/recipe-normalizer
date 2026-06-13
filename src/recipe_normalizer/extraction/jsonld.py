"""Deterministic schema.org/Recipe JSON-LD parsing (URL tier 1, spec §6.1).

Pure functions, no LLM, no network: HTML in, NormalizeResult out. Ingredient
lines come out verbatim with name/quantity/unit left None — the url plugin
runs one cheap enrichment pass over the line strings afterwards; the structure
mapped here stays authoritative.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

from recipe_normalizer.extraction.normalize import (
    NormalizedGroup,
    NormalizedLine,
    NormalizedRecipe,
    NormalizedStep,
    NormalizeResult,
)

__all__ = [
    "find_recipe_jsonld",
    "is_complete",
    "jsonld_to_normalize_result",
    "parse_iso_duration",
]

# The product's closed dish_types vocabulary (see NORMALIZE_SYSTEM). A JSON-LD
# recipeCategory maps into dish_types only on an exact case-insensitive token
# match ("Dessert" → dessert); anything else ("Main Course", "ארוחת בוקר")
# lands in tags instead — tier 1 never guesses vocabulary mappings.
_DISH_TYPES = frozenset(
    {
        "cocktail",
        "drink",
        "smoothie",
        "main",
        "dessert",
        "side",
        "breakfast",
        "soup",
        "salad",
        "bread",
        "sauce",
        "snack",
    }
)


# ---------------------------------------------------------------------------
# Locating the Recipe node
# ---------------------------------------------------------------------------


class _LdJsonScriptCollector(HTMLParser):
    """Collects the text content of <script type="application/ld+json"> blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._in_ldjson = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        script_type = next((value for name, value in attrs if name == "type"), None)
        if script_type and script_type.strip().lower() == "application/ld+json":
            self._in_ldjson = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._in_ldjson:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ldjson:
            self._in_ldjson = False
            self.blocks.append("".join(self._buffer))


def _is_recipe_type(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    type_value = node.get("@type")
    types = type_value if isinstance(type_value, list) else [type_value]
    return any(isinstance(t, str) and t.strip().lower() == "recipe" for t in types)


def _candidate_nodes(root: Any) -> list[dict[str, Any]]:
    """Unwrap list roots and @graph containers into a flat candidate list."""
    roots = root if isinstance(root, list) else [root]
    candidates: list[dict[str, Any]] = []
    for item in roots:
        if not isinstance(item, dict):
            continue
        candidates.append(item)
        graph = item.get("@graph")
        if isinstance(graph, list):
            candidates.extend(node for node in graph if isinstance(node, dict))
    return candidates


def find_recipe_jsonld(html: str) -> dict[str, Any] | None:
    """Return the first schema.org Recipe node found in *html*, or None.

    Malformed JSON blocks are skipped silently; @graph containers and
    list roots are unwrapped; @type matching is case-insensitive and
    accepts list-valued types.
    """
    collector = _LdJsonScriptCollector()
    collector.feed(html)
    for block in collector.blocks:
        try:
            root = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _candidate_nodes(root):
            if _is_recipe_type(node):
                return node
    return None


# ---------------------------------------------------------------------------
# ISO-8601 durations
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^P(?:\d+Y)?(?:\d+M)?(?:\d+W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)


def parse_iso_duration(value: str) -> int | None:
    """Parse an ISO-8601 duration ("PT1H20M") into whole minutes.

    Rules (documented design decisions):
    - days/hours/minutes count; seconds round up to one minute when ≥30
      ("PT45S" → 1, "PT10S" → 0);
    - calendar years/months/weeks are ignored (meaningless for recipes);
    - at least one of D/H/M/S must be present — bare "P"/"PT" or any
      non-duration string returns None.
    """
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip())
    if match is None:
        return None
    parts = {k: int(v) for k, v in match.groupdict().items() if v is not None}
    if not parts:
        return None
    minutes = parts.get("days", 0) * 1440 + parts.get("hours", 0) * 60 + parts.get("minutes", 0)
    if parts.get("seconds", 0) >= 30:
        minutes += 1
    return minutes


# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _parse_yield(value: Any) -> tuple[float | None, str | None]:
    """recipeYield (str | number | list) → (servings_amount, servings_unit_text)."""
    first = _first(value)
    if isinstance(first, int | float):
        return float(first), "servings"
    if isinstance(first, str) and first.strip():
        text = first.strip()
        # Range ("4-6 servings", "4–6", "4 to 6 servings") → lower bound + unit text.
        range_match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(?:[-–—]|to)\s*\d+(?:\.\d+)?\s*(.*)$", text)
        if range_match:
            return float(range_match.group(1)), range_match.group(2).strip() or "servings"
        match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(.*)$", text)
        if match:
            remainder = match.group(2).strip()
            return float(match.group(1)), remainder or "servings"
        return None, text
    return None, None


def _step_texts(value: Any) -> list[str]:
    """recipeInstructions → ordered verbatim step texts.

    Handles HowToStep lists, HowToSection containers (flattened in order —
    NormalizedStep has no grouping), plain string lists, and a single string
    (split on newlines).
    """
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if not isinstance(value, list):
        return []
    steps: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                steps.append(item.strip())
        elif isinstance(item, dict):
            elements = item.get("itemListElement")
            if isinstance(elements, list):  # HowToSection → flatten its steps
                steps.extend(_step_texts(elements))
            else:
                text = _str_or_none(item.get("text"))
                if text:
                    steps.append(text)
    return steps


def _image_url(value: Any) -> str | None:
    """image (str | list | ImageObject) → first usable url."""
    first = _first(value)
    if isinstance(first, dict):
        first = first.get("url")
    return _str_or_none(first)


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def jsonld_to_normalize_result(
    data: dict[str, Any], *, fallback_language: str = "en"
) -> tuple[NormalizeResult, str | None]:
    """Map a schema.org Recipe node to (NormalizeResult, image_url).

    Deterministic — verbatim original_text everywhere; per-line name/quantity/
    unit are left None for the separate cheap enrichment pass. A missing name
    maps to an empty title so is_complete() rejects it. ``fallback_language``
    is used only when the node carries no explicit ``inLanguage``.
    """
    ingredient_texts = _string_list(data.get("recipeIngredient"))
    lines = [NormalizedLine(original_text=text) for text in ingredient_texts]
    steps = [
        NormalizedStep(original_text=text) for text in _step_texts(data.get("recipeInstructions"))
    ]
    servings_amount, servings_unit_text = _parse_yield(data.get("recipeYield"))

    dish_types: list[str] = []
    tags: list[str] = []
    for category in _string_list(data.get("recipeCategory")):
        lowered = category.lower()
        if lowered in _DISH_TYPES:
            dish_types.append(lowered)
        else:
            tags.append(lowered)

    recipe = NormalizedRecipe(
        title=_str_or_none(data.get("name")) or "",
        description=_str_or_none(data.get("description")),
        language=_str_or_none(data.get("inLanguage")) or fallback_language,
        servings_amount=servings_amount,
        servings_unit_text=servings_unit_text,
        prep_min=parse_iso_duration(data.get("prepTime", "")),
        cook_min=parse_iso_duration(data.get("cookTime", "")),
        total_min=parse_iso_duration(data.get("totalTime", "")),
        cuisines=[c.lower() for c in _string_list(data.get("recipeCuisine"))],
        dish_types=dish_types,
        tags=tags,
        groups=[NormalizedGroup(name=None, lines=lines)],
        steps=steps,
    )
    result = NormalizeResult(is_recipe=True, confidence=0.95, recipes=[recipe])
    return result, _image_url(data.get("image"))


def is_complete(result: NormalizeResult) -> bool:
    """Spec §6.1 "present and complete": title + ≥3 ingredient lines + ≥1 step."""
    if not result.recipes:
        return False
    recipe = result.recipes[0]
    line_count = sum(len(group.lines) for group in recipe.groups)
    return bool(recipe.title.strip()) and line_count >= 3 and len(recipe.steps) >= 1
