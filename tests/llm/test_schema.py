from pydantic import BaseModel, Field

from recipe_normalizer.llm.schema import strict_json_schema


class Ingredient(BaseModel):
    name: str = Field(max_length=50)
    grams: float = Field(ge=0)


class Recipe(BaseModel):
    title: str
    note: str | None = None
    ingredients: list[Ingredient]


def test_additional_properties_false_on_every_object_node() -> None:
    schema = strict_json_schema(Recipe)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Ingredient"]["additionalProperties"] is False


def test_unsupported_constraints_are_dropped() -> None:
    schema = strict_json_schema(Recipe)
    ingredient_props = schema["$defs"]["Ingredient"]["properties"]
    assert "maxLength" not in ingredient_props["name"]
    assert "minimum" not in ingredient_props["grams"]


def test_structure_is_preserved() -> None:
    schema = strict_json_schema(Recipe)
    assert set(schema["properties"]) == {"title", "note", "ingredients"}
    assert schema["properties"]["ingredients"]["type"] == "array"
    assert schema["properties"]["ingredients"]["items"] == {"$ref": "#/$defs/Ingredient"}
    # Optional field renders as anyOf — recursion must not mangle it.
    assert {entry["type"] for entry in schema["properties"]["note"]["anyOf"]} == {"string", "null"}


def test_fields_named_like_constraint_keywords_survive() -> None:
    class Weird(BaseModel):
        pattern: str
        minimum: int

    schema = strict_json_schema(Weird)
    assert set(schema["properties"]) == {"pattern", "minimum"}
    assert schema["properties"]["pattern"]["type"] == "string"


def test_nested_object_fields_get_strictified() -> None:
    class Inner(BaseModel):
        value: int = Field(le=10)

    class Outer(BaseModel):
        inner: Inner

    schema = strict_json_schema(Outer)
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert "maximum" not in inner["properties"]["value"]
