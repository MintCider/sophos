"""JSON Schema transformations used at provider boundaries."""

from copy import deepcopy
from typing import Any


def close_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively disallow undeclared object properties without changing optionality."""
    result = deepcopy(schema)
    _close_objects(result)
    return result


def compile_openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Compile canonical tool parameters to OpenAI strict-mode JSON Schema.

    OpenAI strict function calling requires every declared property to appear in
    ``required``. Canonically optional fields therefore become required-but-nullable
    at the API boundary. The canonical schema remains unchanged.
    """
    result = deepcopy(schema)
    _compile_openai_node(result)
    return result


def compile_openai_strict_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return OpenAI tools with strict mode enabled and compliant parameters."""
    compiled = deepcopy(tools)
    for tool in compiled:
        if tool.get("type") != "function":
            continue
        function = tool.get("function", {})
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            function["parameters"] = compile_openai_strict_schema(parameters)
        function["strict"] = True
    return compiled


def strip_boundary_nulls(value: Any, canonical_schema: dict[str, Any]) -> Any:
    """Remove null placeholders introduced only by strict boundary compilation."""
    if isinstance(value, dict):
        properties = canonical_schema.get("properties", {})
        required = set(canonical_schema.get("required", []))
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            child_schema = properties.get(key, {}) if isinstance(properties, dict) else {}
            if item is None and key not in required:
                continue
            cleaned[key] = strip_boundary_nulls(item, child_schema)
        return cleaned
    if isinstance(value, list):
        item_schema = canonical_schema.get("items", {})
        return [strip_boundary_nulls(item, item_schema) for item in value]
    return value


def _close_objects(schema: Any) -> None:
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" or "properties" in schema:
        schema["additionalProperties"] = False
        for child in schema.get("properties", {}).values():
            _close_objects(child)
    if "items" in schema:
        _close_objects(schema["items"])
    for keyword in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(keyword, []):
            _close_objects(branch)


def _compile_openai_node(schema: Any) -> None:
    if not isinstance(schema, dict):
        return

    if schema.get("type") == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        canonical_required = set(schema.get("required", []))
        schema["additionalProperties"] = False
        schema["required"] = list(properties)
        for name, child in properties.items():
            _compile_openai_node(child)
            if name not in canonical_required:
                properties[name] = _make_nullable(child)

    if "items" in schema:
        _compile_openai_node(schema["items"])
    for keyword in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(keyword, []):
            _compile_openai_node(branch)


def _make_nullable(schema: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(schema)
    schema_type = result.get("type")
    if isinstance(schema_type, str):
        result["type"] = [schema_type, "null"]
        return result
    if isinstance(schema_type, list):
        if "null" not in schema_type:
            result["type"] = [*schema_type, "null"]
        return result
    if "anyOf" in result:
        if not any(branch == {"type": "null"} for branch in result["anyOf"]):
            result["anyOf"].append({"type": "null"})
        return result
    return {"anyOf": [result, {"type": "null"}]}
