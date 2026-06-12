"""JSON-schema post-processing for structured outputs.

The structured-outputs API requires ``additionalProperties: false`` on every
object node and rejects numeric/string/array value constraints. Pydantic's
``model_json_schema()`` emits neither guarantee, so we post-process.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Constraint keywords the structured-outputs API does not support.
_UNSUPPORTED_KEYS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)

# Keys whose value is a mapping of name -> sub-schema (names are NOT schema keywords).
_NAMED_SUBSCHEMA_CONTAINERS = ("properties", "$defs", "definitions", "patternProperties")
# Keys whose value is a single sub-schema.
_DIRECT_SUBSCHEMAS = ("items", "contains", "not", "if", "then", "else", "propertyNames")
# Keys whose value is a list of sub-schemas.
_SUBSCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")


def strict_json_schema(output_model: type[BaseModel]) -> dict[str, Any]:
    """Return *output_model*'s JSON schema, adjusted for the structured-outputs API."""
    schema = output_model.model_json_schema()
    _strictify(schema)
    return schema


def _strictify(node: dict[str, Any]) -> None:
    for key in list(node):
        if key in _UNSUPPORTED_KEYS:
            del node[key]
    if node.get("type") == "object" or "properties" in node:
        node["additionalProperties"] = False

    for container_key in _NAMED_SUBSCHEMA_CONTAINERS:
        container = node.get(container_key)
        if isinstance(container, dict):
            for sub in container.values():
                if isinstance(sub, dict):
                    _strictify(sub)
    for direct_key in _DIRECT_SUBSCHEMAS:
        sub = node.get(direct_key)
        if isinstance(sub, dict):
            _strictify(sub)
    for list_key in _SUBSCHEMA_LISTS:
        subs = node.get(list_key)
        if isinstance(subs, list):
            for sub in subs:
                if isinstance(sub, dict):
                    _strictify(sub)
