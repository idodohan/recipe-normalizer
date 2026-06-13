"""Unit tests for the deterministic JSON-LD recipe parser (tier 1, spec §6.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recipe_normalizer.extraction.jsonld import (
    find_recipe_jsonld,
    is_complete,
    jsonld_to_normalize_result,
    parse_iso_duration,
)

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# find_recipe_jsonld
# ---------------------------------------------------------------------------


def test_find_recipe_jsonld_plain_recipe_block() -> None:
    data = find_recipe_jsonld(_fixture("jsonld_full.html"))
    assert data is not None
    assert data["name"] == "Classic Vanilla Pound Cake"


def test_find_recipe_jsonld_skips_non_recipe_blocks() -> None:
    # jsonld_full.html has an Organization block BEFORE the Recipe block.
    data = find_recipe_jsonld(_fixture("jsonld_full.html"))
    assert data is not None
    assert data.get("@type") == "Recipe"


def test_find_recipe_jsonld_unwraps_graph_and_list_types() -> None:
    # jsonld_graph.html: Recipe nested in @graph with @type ["Recipe", "NewsArticle"],
    # preceded by a malformed JSON block that must be skipped silently.
    data = find_recipe_jsonld(_fixture("jsonld_graph.html"))
    assert data is not None
    assert data["name"] == "Slow-Braised Short Ribs"


def test_find_recipe_jsonld_case_insensitive_type() -> None:
    html = (
        '<html><head><script type="application/ld+json">'
        + json.dumps({"@type": "recipe", "name": "Lowercase"})
        + "</script></head><body></body></html>"
    )
    data = find_recipe_jsonld(html)
    assert data is not None
    assert data["name"] == "Lowercase"


def test_find_recipe_jsonld_list_root() -> None:
    html = (
        '<script type="application/LD+JSON">'
        + json.dumps([{"@type": "WebSite"}, {"@type": "Recipe", "name": "From List"}])
        + "</script>"
    )
    data = find_recipe_jsonld(html)
    assert data is not None
    assert data["name"] == "From List"


def test_find_recipe_jsonld_none_when_absent() -> None:
    assert find_recipe_jsonld(_fixture("readable_norecipe_jsonld.html")) is None
    assert find_recipe_jsonld(_fixture("article.html")) is None  # NewsArticle only
    assert find_recipe_jsonld("<html><body>no scripts at all</body></html>") is None


def test_find_recipe_jsonld_tolerates_only_malformed_json() -> None:
    html = '<script type="application/ld+json">{not json,}</script>'
    assert find_recipe_jsonld(html) is None


# ---------------------------------------------------------------------------
# parse_iso_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT15M", 15),
        ("PT1H20M", 80),
        ("PT2H", 120),
        ("PT45M", 45),
        ("PT45S", 1),  # ≥30s rounds up to one minute
        ("PT10S", 0),  # <30s drops
        ("PT1H20M45S", 81),
        ("P0DT1H", 60),  # date part tolerated when zero-ish prefix exists
    ],
)
def test_parse_iso_duration_valid(value: str, expected: int) -> None:
    assert parse_iso_duration(value) == expected


@pytest.mark.parametrize("value", ["", "garbage", "15 minutes", "PT", "P", "MT15"])
def test_parse_iso_duration_invalid(value: str) -> None:
    assert parse_iso_duration(value) is None


# ---------------------------------------------------------------------------
# jsonld_to_normalize_result
# ---------------------------------------------------------------------------


def test_maps_full_recipe() -> None:
    data = find_recipe_jsonld(_fixture("jsonld_full.html"))
    assert data is not None
    result, image_url = jsonld_to_normalize_result(data)

    assert result.is_recipe is True
    assert result.confidence == 0.95
    assert len(result.recipes) == 1
    recipe = result.recipes[0]
    assert recipe.title == "Classic Vanilla Pound Cake"
    assert recipe.description is not None and recipe.description.startswith("A buttery")
    assert recipe.language == "en"
    assert recipe.servings_amount == 4.0
    assert recipe.servings_unit_text == "servings"
    assert recipe.prep_min == 15
    assert recipe.cook_min == 30
    assert recipe.total_min == 45
    assert recipe.cuisines == ["american"]
    assert recipe.dish_types == ["dessert"]
    # One flat group, lines verbatim, parse fields left for enrichment.
    assert len(recipe.groups) == 1
    lines = recipe.groups[0].lines
    assert len(lines) == 8
    assert lines[0].original_text == "2 cups all-purpose flour"
    assert lines[-1].original_text == "salt to taste"
    assert all(line.name is None and line.quantity is None for line in lines)
    # HowToStep list → verbatim step texts.
    assert len(recipe.steps) == 5
    assert recipe.steps[0].original_text.startswith("Preheat the oven to 350")
    assert image_url == "https://cdn.dailycrumb.test/images/pound-cake-hero.jpg"


def test_maps_graph_variant() -> None:
    data = find_recipe_jsonld(_fixture("jsonld_graph.html"))
    assert data is not None
    result, image_url = jsonld_to_normalize_result(data)

    recipe = result.recipes[0]
    assert recipe.title == "Slow-Braised Short Ribs"
    # recipeYield as a bare number.
    assert recipe.servings_amount == 6.0
    assert recipe.servings_unit_text == "servings"
    # ISO durations with hours.
    assert recipe.prep_min == 20
    assert recipe.cook_min == 80
    assert recipe.total_min == 100
    # List-valued cuisine; categories not in the dish_types vocab land in tags.
    assert recipe.cuisines == ["french", "american"]
    assert recipe.dish_types == []
    assert recipe.tags == ["main course", "comfort food"]
    # Plain-string instructions.
    assert len(recipe.steps) == 5
    assert recipe.steps[0].original_text.startswith("Season the short ribs")
    # ImageObject image.
    assert image_url == "https://images.hearthvine.test/short-ribs-1200.jpg"
    # No inLanguage on this Recipe node → default en.
    assert recipe.language == "en"


def test_maps_hebrew_recipe_verbatim() -> None:
    data = find_recipe_jsonld(_fixture("jsonld_hebrew.html"))
    assert data is not None
    result, image_url = jsonld_to_normalize_result(data)

    recipe = result.recipes[0]
    assert recipe.title == "שקשוקה ביתית מושלמת"
    assert recipe.language == "he"
    lines = recipe.groups[0].lines
    assert lines[0].original_text == "2 כפות שמן זית"
    assert lines[-1].original_text == "פטרוזיליה קצוצה להגשה"
    assert recipe.servings_amount == 4.0
    assert recipe.servings_unit_text == "מנות"
    assert recipe.steps[0].original_text.startswith("מחממים את שמן הזית")
    assert image_url == "https://cdn.rinas-kitchen.test/images/shakshuka-hero.jpg"


def test_maps_howto_sections_flattened() -> None:
    data = {
        "@type": "Recipe",
        "name": "Sectioned",
        "recipeIngredient": ["a", "b", "c"],
        "recipeInstructions": [
            {
                "@type": "HowToSection",
                "name": "Dough",
                "itemListElement": [
                    {"@type": "HowToStep", "text": "Mix the dough."},
                    {"@type": "HowToStep", "text": "Rest the dough."},
                ],
            },
            {
                "@type": "HowToSection",
                "name": "Bake",
                "itemListElement": [{"@type": "HowToStep", "text": "Bake it."}],
            },
        ],
    }
    result, _ = jsonld_to_normalize_result(data)
    steps = [s.original_text for s in result.recipes[0].steps]
    assert steps == ["Mix the dough.", "Rest the dough.", "Bake it."]


def test_maps_single_string_instructions_split_on_newlines() -> None:
    data = {
        "@type": "Recipe",
        "name": "One String",
        "recipeIngredient": ["a", "b", "c"],
        "recipeInstructions": "Mix everything.\nBake for 20 minutes.\n\nServe warm.",
    }
    result, _ = jsonld_to_normalize_result(data)
    steps = [s.original_text for s in result.recipes[0].steps]
    assert steps == ["Mix everything.", "Bake for 20 minutes.", "Serve warm."]


def test_maps_yield_list_and_image_list() -> None:
    data = {
        "@type": "Recipe",
        "name": "Listy",
        "recipeYield": ["12 cookies", "1 dozen"],
        "image": ["https://img.test/1.jpg", "https://img.test/2.jpg"],
        "recipeIngredient": ["a", "b", "c"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Do it."}],
    }
    result, image_url = jsonld_to_normalize_result(data)
    recipe = result.recipes[0]
    assert recipe.servings_amount == 12.0
    assert recipe.servings_unit_text == "cookies"
    assert image_url == "https://img.test/1.jpg"


def test_maps_unparseable_yield_kept_as_text() -> None:
    data = {"@type": "Recipe", "name": "X", "recipeYield": "a few"}
    result, _ = jsonld_to_normalize_result(data)
    recipe = result.recipes[0]
    assert recipe.servings_amount is None
    assert recipe.servings_unit_text == "a few"


@pytest.mark.parametrize(
    "raw,amount,unit_text",
    [
        ("4-6 servings", 4.0, "servings"),
        ("4–6 servings", 4.0, "servings"),  # en dash
        ("4 to 6 servings", 4.0, "servings"),
        ("4-6", 4.0, "servings"),  # bare range, no unit text
    ],
)
def test_maps_range_yield_uses_lower_bound(raw: str, amount: float, unit_text: str) -> None:
    data = {"@type": "Recipe", "name": "X", "recipeYield": raw}
    result, _ = jsonld_to_normalize_result(data)
    recipe = result.recipes[0]
    assert recipe.servings_amount == amount
    assert recipe.servings_unit_text == unit_text


def test_missing_inlanguage_uses_fallback_language() -> None:
    data = {"@type": "Recipe", "name": "שקשוקה", "recipeIngredient": ["a", "b", "c"]}
    result, _ = jsonld_to_normalize_result(data, fallback_language="he")
    assert result.recipes[0].language == "he"


def test_missing_inlanguage_defaults_to_english() -> None:
    data = {"@type": "Recipe", "name": "X"}
    result, _ = jsonld_to_normalize_result(data)
    assert result.recipes[0].language == "en"


def test_explicit_inlanguage_wins_over_fallback() -> None:
    data = {"@type": "Recipe", "name": "X", "inLanguage": "fr"}
    result, _ = jsonld_to_normalize_result(data, fallback_language="he")
    assert result.recipes[0].language == "fr"


def test_missing_name_yields_incomplete_result() -> None:
    data = {
        "@type": "Recipe",
        "recipeIngredient": ["a", "b", "c", "d"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Do it."}],
    }
    result, _ = jsonld_to_normalize_result(data)
    assert is_complete(result) is False


# ---------------------------------------------------------------------------
# is_complete
# ---------------------------------------------------------------------------


def _minimal(ingredients: int = 3, steps: int = 1, name: str = "Ok") -> dict[str, object]:
    return {
        "@type": "Recipe",
        "name": name,
        "recipeIngredient": [f"ingredient {i}" for i in range(ingredients)],
        "recipeInstructions": [{"@type": "HowToStep", "text": f"step {i}"} for i in range(steps)],
    }


def test_is_complete_happy() -> None:
    result, _ = jsonld_to_normalize_result(_minimal())
    assert is_complete(result) is True


def test_is_complete_too_few_ingredients() -> None:
    result, _ = jsonld_to_normalize_result(_minimal(ingredients=2))
    assert is_complete(result) is False


def test_is_complete_no_steps() -> None:
    result, _ = jsonld_to_normalize_result(_minimal(steps=0))
    assert is_complete(result) is False
