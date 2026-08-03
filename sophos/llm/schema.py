"""JSON Schema transformations used at provider boundaries."""

from copy import deepcopy
from typing import Any


def close_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively disallow undeclared object properties without changing optionality."""
    result = deepcopy(schema)
    _close_objects(result)
    return result


def compile_openai_strict_schema(
    schema: dict[str, Any],
    *,
    optional_mode: str = "nullable",
) -> dict[str, Any]:
    """Compile canonical tool parameters to OpenAI strict-mode JSON Schema.

    Strict function calling requires every declared property to appear in
    ``required``. ``nullable`` mode preserves canonical optionality using null;
    ``required`` mode retains the original type for providers without null support.
    The canonical schema remains unchanged.
    """
    result = deepcopy(schema)
    _compile_openai_node(result, optional_mode=optional_mode)
    return result


def compile_openai_strict_tools(
    tools: list[dict[str, Any]],
    *,
    optional_mode: str = "nullable",
) -> list[dict[str, Any]]:
    """Return OpenAI tools with strict mode enabled and compliant parameters."""
    compiled = deepcopy(tools)
    for tool in compiled:
        if tool.get("type") != "function":
            continue
        function = tool.get("function", {})
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            function["parameters"] = compile_openai_strict_schema(
                parameters,
                optional_mode=optional_mode,
            )
        function["strict"] = True
    return compiled


def compile_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Compile canonical JSON Schema to Gemini's supported schema subset.

    Gemini's native ``FunctionDeclaration.parameters`` schema rejects the
    standard ``additionalProperties`` keyword.  Remove it at every schema node
    without mutating the canonical tool definition.
    """
    result = deepcopy(schema)
    _compile_gemini_node(result)
    return result


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


def _compile_gemini_node(schema: Any) -> None:
    if not isinstance(schema, dict):
        return

    schema.pop("additionalProperties", None)

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for child in properties.values():
            _compile_gemini_node(child)

    for keyword in ("items", "contains", "not", "if", "then", "else"):
        _compile_gemini_node(schema.get(keyword))

    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                _compile_gemini_node(branch)

    for keyword in ("$defs", "definitions", "dependentSchemas", "patternProperties"):
        definitions = schema.get(keyword)
        if isinstance(definitions, dict):
            for child in definitions.values():
                _compile_gemini_node(child)


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


def _compile_openai_node(schema: Any, *, optional_mode: str) -> None:
    if not isinstance(schema, dict):
        return

    if schema.get("type") == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        canonical_required = set(schema.get("required", []))
        schema["additionalProperties"] = False
        schema["required"] = list(properties)
        for name, child in properties.items():
            _compile_openai_node(child, optional_mode=optional_mode)
            if name not in canonical_required and optional_mode == "nullable":
                properties[name] = _make_nullable(child)

    if "items" in schema:
        _compile_openai_node(schema["items"], optional_mode=optional_mode)
    for keyword in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(keyword, []):
            _compile_openai_node(branch, optional_mode=optional_mode)


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
