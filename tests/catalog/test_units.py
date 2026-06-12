import pytest

from recipe_normalizer.catalog.units import Unit, UnitKind, parse_unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("g", "g"),
        ("grams", "g"),
        ("gram", "g"),
        ("kg", "kg"),
        ("ml", "ml"),
        ("milliliters", "ml"),
        ("l", "l"),
        ("liter", "l"),
        ("cup", "cup"),
        ("cups", "cup"),
        ("tbsp", "tbsp"),
        ("tablespoon", "tbsp"),
        ("tsp", "tsp"),
        ("teaspoons", "tsp"),
        ("oz", "oz"),
        ("ounce", "oz"),
        ("fl oz", "fl_oz"),
        ("fluid ounce", "fl_oz"),
        ("lb", "lb"),
        ("pound", "lb"),
        ("jigger", "jigger"),
        ("dash", "dash"),
        ("splash", "splash"),
        ("shot", "shot"),
        ("part", "part"),
        ("parts", "part"),
        ("clove", "clove"),
        ("cloves", "clove"),
        ("slice", "slice"),
        ("pinch", "pinch"),
        ("Cup", "cup"),
        ("  TBSP ", "tbsp"),
        ("fl. oz.", "fl_oz"),
    ],
)
def test_parse_unit(text, expected):
    unit = parse_unit(text)
    assert unit is not None and unit.token == expected


def test_unknown_unit_returns_none():
    assert parse_unit("flibbertigibbets") is None
    assert parse_unit("") is None


def test_unit_kinds_and_factors():
    assert parse_unit("kg") == Unit("kg", UnitKind.mass, 1000.0)
    cup = parse_unit("cup")
    assert cup is not None and cup.kind is UnitKind.volume
    assert abs(cup.base_factor - 236.588) < 0.001
    floz = parse_unit("fl oz")
    assert floz is not None and abs(floz.base_factor - 29.5735) < 0.001
    part = parse_unit("part")
    assert part is not None and part.kind is UnitKind.ratio
    clove = parse_unit("clove")
    assert clove is not None and clove.kind is UnitKind.count
    dash = parse_unit("dash")
    assert dash is not None and dash.is_inherently_approx
