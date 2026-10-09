"""A tiny validator that checks JSON responses against the schemas in openapi.yaml.

It understands what the contract uses: $ref, allOf, type, nullable, required, properties, additionalProperties,
items, enum, minimum, maximum. Objects that list `properties` without `additionalProperties` must not carry extra
keys (so a stray `_id` or `id` fails the test).
"""
import pathlib

import yaml

SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "openapi.yaml").read_text(encoding="utf-8"))


def _deref(node):
    while isinstance(node, dict) and "$ref" in node:
        target = SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def resolve(schema):
    schema = _deref(schema)
    if isinstance(schema, dict) and "allOf" in schema:
        merged = {k: v for k, v in schema.items() if k != "allOf"}
        for part in schema["allOf"]:
            merged = {**resolve(part), **merged}
        return merged
    return schema


def check(value, schema, path="$"):
    schema = resolve(schema)
    if value is None:
        assert schema.get("nullable") or "type" not in schema, f"{path}: null not allowed"
        return
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        assert isinstance(value, dict), f"{path}: expected object, got {type(value).__name__}"
        for key in schema.get("required", []):
            assert key in value, f"{path}: missing required key {key!r}"
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                check(item, props[key], f"{path}.{key}")
            elif isinstance(extra, dict):
                check(item, extra, f"{path}.{key}")
            elif extra is None and props:
                raise AssertionError(f"{path}: unexpected key {key!r}")
    elif kind == "array":
        assert isinstance(value, list), f"{path}: expected array"
        for i, item in enumerate(value):
            check(item, schema.get("items", {}), f"{path}[{i}]")
    elif kind == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), f"{path}: expected integer, got {value!r}"
    elif kind == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{path}: expected number"
    elif kind == "string":
        assert isinstance(value, str), f"{path}: expected string, got {value!r}"
    elif kind == "boolean":
        assert isinstance(value, bool), f"{path}: expected boolean"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"
    if "minimum" in schema and isinstance(value, (int, float)):
        assert value >= schema["minimum"], f"{path}: {value} < minimum"
    if "maximum" in schema and isinstance(value, (int, float)):
        assert value <= schema["maximum"], f"{path}: {value} > maximum"


def check_response(response, path: str, method: str):
    """Validate an httpx/TestClient response against the documented response for its status code."""
    responses = SPEC["paths"][path][method.lower()]["responses"]
    code = str(response.status_code)
    assert code in responses, f"{method} {path}: status {code} is not documented ({list(responses)})"
    documented = _deref(responses[code])
    schema = documented.get("content", {}).get("application/json", {}).get("schema")
    if schema is not None:
        check(response.json(), schema)
    return response.json()
