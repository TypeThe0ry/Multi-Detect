"""Strict, deterministic JSON helpers for autonomy contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from math import isfinite
from numbers import Real
from typing import Any


class AutonomyContractError(ValueError):
    """Raised when an autonomy document violates its versioned contract."""


def strict_object(
    value: object,
    *,
    name: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AutonomyContractError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise AutonomyContractError(f"{name} keys must be strings")
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise AutonomyContractError(f"{name} is missing required field(s): {', '.join(missing)}")
    if unknown:
        raise AutonomyContractError(f"{name} has unknown field(s): {', '.join(unknown)}")
    return value


def require_schema_version(value: object, *, expected: int = 1) -> int:
    version = require_int(value, "schema_version", minimum=1)
    if version != expected:
        raise AutonomyContractError(f"unsupported schema_version {version}; expected {expected}")
    return version


def require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutonomyContractError(f"{name} must be a non-empty string")
    return value.strip()


def optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return require_text(value, name)


def require_sha256(value: object, name: str) -> str:
    text = require_text(value, name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise AutonomyContractError(f"{name} must be 64 hexadecimal characters")
    return text


def optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return require_sha256(value, name)


def require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise AutonomyContractError(f"{name} must be a boolean")
    return value


def require_int(
    value: object,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutonomyContractError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise AutonomyContractError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise AutonomyContractError(f"{name} must be at most {maximum}")
    return value


def require_real(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AutonomyContractError(f"{name} must be a number")
    result = float(value)
    if not isfinite(result):
        raise AutonomyContractError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise AutonomyContractError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise AutonomyContractError(f"{name} must be at most {maximum}")
    return result


def optional_real(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return require_real(value, name, minimum=minimum, maximum=maximum)


def require_string_array(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AutonomyContractError(f"{name} must be an array")
    result = tuple(require_text(item, f"{name}[{index}]") for index, item in enumerate(value))
    return result


def require_array(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AutonomyContractError(f"{name} must be an array")
    return value


def require_aware_timestamp(value: object, name: str) -> str:
    text = require_text(value, name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomyContractError(f"{name} must include a timezone")
    return text


def canonical_json(document: Mapping[str, Any]) -> str:
    """Return one canonical representation suitable for hashing and grants."""

    _validate_json_value(document, "document")
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def strict_json_loads(
    payload: str | bytes | bytearray,
    *,
    name: str = "JSON document",
) -> Any:
    """Decode JSON while rejecting ambiguous objects and non-finite numbers.

    Python's standard decoder intentionally accepts JavaScript-only constants
    (``NaN``/``Infinity``) and silently keeps the last value for duplicate
    object members.  That is convenient for general application data, but it
    is unsafe for hash-bound field, simulator, and release evidence.  This
    helper centralizes the fail-closed wire policy used by those loaders.

    ``bytes`` are decoded as UTF-8 with an optional BOM, matching the files
    emitted by Windows tooling.  A leading BOM in a ``str`` is accepted too so
    callers can pass text read with ordinary ``encoding="utf-8"``.
    """

    if isinstance(payload, (bytes, bytearray)):
        try:
            text = bytes(payload).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AutonomyContractError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    elif isinstance(payload, str):
        text = payload[1:] if payload.startswith("\ufeff") else payload
    else:
        raise TypeError(f"{name} payload must be text or bytes")

    def reject_constant(value: str) -> None:
        raise AutonomyContractError(f"{name} contains non-finite JSON number {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AutonomyContractError(f"{name} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise AutonomyContractError(f"{name} is not valid JSON: {exc}") from exc


def _validate_json_value(value: object, path: str) -> None:
    if value is None or isinstance(value, str) or type(value) is bool:
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise AutonomyContractError(f"{path} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise AutonomyContractError(f"{path} contains a non-string key")
            _validate_json_value(nested, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            _validate_json_value(nested, f"{path}[{index}]")
        return
    raise AutonomyContractError(f"{path} contains a non-JSON value")


__all__ = [
    "AutonomyContractError",
    "canonical_json",
    "canonical_sha256",
    "optional_real",
    "optional_sha256",
    "optional_text",
    "require_array",
    "require_aware_timestamp",
    "require_bool",
    "require_int",
    "require_real",
    "require_schema_version",
    "require_sha256",
    "require_string_array",
    "require_text",
    "strict_json_loads",
    "strict_object",
]
