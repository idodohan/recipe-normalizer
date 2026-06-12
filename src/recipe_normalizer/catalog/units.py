import enum
from dataclasses import dataclass, field


class UnitKind(enum.Enum):
    mass = "mass"  # base: gram
    volume = "volume"  # base: ml
    count = "count"  # needs per-ingredient gram_weights
    ratio = "ratio"  # "parts" — scales natively, never converts


@dataclass(frozen=True)
class Unit:
    token: str
    kind: UnitKind
    base_factor: float = 0.0  # → g (mass) or → ml (volume); 0 for count/ratio
    is_inherently_approx: bool = field(default=False, compare=False)


_UNITS: list[tuple[Unit, list[str]]] = [
    (Unit("g", UnitKind.mass, 1.0), ["g", "gram", "grams", "gr"]),
    (Unit("kg", UnitKind.mass, 1000.0), ["kg", "kilogram", "kilograms"]),
    (Unit("oz", UnitKind.mass, 28.3495), ["oz", "ounce", "ounces"]),
    (Unit("lb", UnitKind.mass, 453.592), ["lb", "lbs", "pound", "pounds"]),
    (
        Unit("ml", UnitKind.volume, 1.0),
        ["ml", "milliliter", "milliliters", "millilitre", "millilitres"],
    ),
    (Unit("l", UnitKind.volume, 1000.0), ["l", "liter", "liters", "litre", "litres"]),
    (Unit("tsp", UnitKind.volume, 4.92892), ["tsp", "teaspoon", "teaspoons"]),
    (Unit("tbsp", UnitKind.volume, 14.7868), ["tbsp", "tablespoon", "tablespoons", "tbs"]),
    (Unit("cup", UnitKind.volume, 236.588), ["cup", "cups"]),
    (
        Unit("fl_oz", UnitKind.volume, 29.5735),
        ["fl oz", "fluid ounce", "fluid ounces", "fl. oz.", "fl. oz", "fl oz."],
    ),
    (Unit("jigger", UnitKind.volume, 44.36), ["jigger", "jiggers"]),
    (Unit("shot", UnitKind.volume, 44.36), ["shot", "shots"]),
    (Unit("dash", UnitKind.volume, 0.92, is_inherently_approx=True), ["dash", "dashes"]),
    (Unit("splash", UnitKind.volume, 5.0, is_inherently_approx=True), ["splash", "splashes"]),
    (Unit("pinch", UnitKind.mass, 0.36, is_inherently_approx=True), ["pinch", "pinches"]),
    (Unit("part", UnitKind.ratio), ["part", "parts"]),
    (Unit("clove", UnitKind.count), ["clove", "cloves"]),
    (Unit("slice", UnitKind.count), ["slice", "slices"]),
    (Unit("unit", UnitKind.count), ["unit", "units", "piece", "pieces", "whole"]),
]

_LOOKUP: dict[str, Unit] = {alias: unit for unit, aliases in _UNITS for alias in aliases}


def parse_unit(text: str) -> Unit | None:
    normalized = " ".join(text.strip().lower().split())
    return _LOOKUP.get(normalized) or _LOOKUP.get(normalized.rstrip("."))
