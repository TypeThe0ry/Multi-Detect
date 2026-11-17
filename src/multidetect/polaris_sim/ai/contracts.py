"""Pinned Spica 1.0 JSON Schema validation for the Polaris AI API."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

SCHEMA_FILES = ("target_query", "target_result", "query_target", "common", "envelope")
SCHEMA_ROOT = Path(__file__).with_name("schemas")


class ContractError(ValueError):
    """A request or response does not satisfy the pinned Spica schema."""


def _json_value(value: object, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ContractError(f"{path} contains a non-finite number")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{path} contains a non-string key")
            _json_value(item, f"{path}.{key}")
        return
    raise ContractError(f"{path} is not a JSON value")


@lru_cache(maxsize=1)
def _schemas() -> tuple[dict[str, dict], Registry]:
    schemas = {
        name: json.loads((SCHEMA_ROOT / f"{name}.json").read_text(encoding="utf-8"))
        for name in SCHEMA_FILES
    }
    registry = Registry()
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return schemas, registry


def _validate(schema: Mapping[str, object], value: Mapping[str, object]) -> None:
    _json_value(value)
    validator = Draft202012Validator(
        schema,
        registry=_schemas()[1],
        format_checker=FormatChecker(),
    )
    try:
        validator.validate(value)
    except ValidationError as exc:
        raise ContractError(exc.message) from exc


def validate_spica_query(query: Mapping[str, object]) -> None:
    _validate(_schemas()[0]["target_query"], query)


def validate_spica_request(request: Mapping[str, object]) -> None:
    _validate(_schemas()[0]["query_target"], request)
    validate_spica_query(request["query"])


def validate_spica_response(response: Mapping[str, object]) -> None:
    schema = _schemas()[0]["query_target"]["$defs"]["Response"]
    _validate(schema, response)


def validate_spica_result(result: Mapping[str, object]) -> None:
    _validate(_schemas()[0]["target_result"], result)
