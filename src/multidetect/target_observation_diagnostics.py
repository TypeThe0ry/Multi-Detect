"""Strict offline verification for QGC target-observation diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .compat import UTC

SCHEMA = "multidetect.target-observation-diagnostics.v1"
SCHEMA_VERSION = 1
MAXIMUM_FILE_BYTES = 1024 * 1024
MAXIMUM_AGE_MS = 1500
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "target-observation-diagnostics.schema.json"


class TargetObservationDiagnosticsError(ValueError):
    """Raised when a diagnostics snapshot cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise TargetObservationDiagnosticsError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TargetObservationDiagnosticsError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _json_object(path: Path, *, name: str, strict: bool = False) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object if strict else None,
            parse_constant=_reject_constant if strict else None,
        )
    except TargetObservationDiagnosticsError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TargetObservationDiagnosticsError(f"cannot read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise TargetObservationDiagnosticsError(f"{name} must be a JSON object")
    return value


def _resolved_input(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TargetObservationDiagnosticsError("diagnostics input cannot be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TargetObservationDiagnosticsError(
            f"diagnostics input cannot be resolved: {exc}"
        ) from exc
    if not resolved.is_file():
        raise TargetObservationDiagnosticsError("diagnostics input must be a regular file")
    size = resolved.stat().st_size
    if size <= 0:
        raise TargetObservationDiagnosticsError("diagnostics input is empty")
    if size > MAXIMUM_FILE_BYTES:
        raise TargetObservationDiagnosticsError(
            f"diagnostics input exceeds {MAXIMUM_FILE_BYTES} bytes"
        )
    return resolved


def _schema_validator() -> Draft202012Validator:
    schema = _json_object(SCHEMA_PATH, name="target-observation diagnostics JSON Schema")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise TargetObservationDiagnosticsError(f"invalid diagnostics schema: {exc}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_schema(document: Mapping[str, object]) -> None:
    errors = sorted(_schema_validator().iter_errors(document), key=lambda item: list(item.path))
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(item) for item in first.absolute_path) or "<root>"
    raise TargetObservationDiagnosticsError(
        f"diagnostics schema validation failed at {location}: {first.message}"
    )


def _mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TargetObservationDiagnosticsError(f"{name} must be an object")
    return value


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TargetObservationDiagnosticsError(f"{name} must be a non-negative integer")
    return value


def _finite(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetObservationDiagnosticsError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise TargetObservationDiagnosticsError(f"{name} must be a finite number")
    return result


def _validate_finite_tree(value: object, *, path: str = "<root>") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise TargetObservationDiagnosticsError(f"{path} must be a finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_finite_tree(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_finite_tree(item, path=f"{path}[{index}]")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TargetObservationDiagnosticsError("generated_at_utc must be a date-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetObservationDiagnosticsError("generated_at_utc must be a date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TargetObservationDiagnosticsError("generated_at_utc must include a timezone")
    return parsed.astimezone(UTC)


def _validate_receive_entry(
    entry: Mapping[str, object], *, channel: str, local_now_ms: int, maximum_age_ms: int
) -> bool:
    received_at_ms = _integer(
        entry.get("received_at_ms"), name=f"local_receive.{channel}.received_at_ms"
    )
    age = entry.get("age_ms")
    if age is None:
        if received_at_ms != 0 and received_at_ms <= local_now_ms:
            raise TargetObservationDiagnosticsError(
                f"local_receive.{channel}.age_ms cannot be null for a valid receive time"
            )
        return False
    age_ms = _integer(age, name=f"local_receive.{channel}.age_ms")
    if received_at_ms > local_now_ms:
        raise TargetObservationDiagnosticsError(
            f"local_receive.{channel}.received_at_ms is in the future"
        )
    if age_ms != local_now_ms - received_at_ms:
        raise TargetObservationDiagnosticsError(
            f"local_receive.{channel}.age_ms does not match local_now_ms"
        )
    return age_ms <= maximum_age_ms


def _validate_atomic_identity(
    statuses: Sequence[Mapping[str, object]], *, declared_matched: bool
) -> bool:
    values: list[tuple[int, int, int]] = []
    atomic = True
    for index, status in enumerate(statuses):
        if status.get("atomicObservation") is not True:
            atomic = False
            continue
        values.append(
            (
                _integer(status.get("producerEpoch"), name=f"status[{index}].producerEpoch"),
                _integer(status.get("sequence"), name=f"status[{index}].sequence"),
                _integer(status.get("sentAtMs"), name=f"status[{index}].sentAtMs"),
            )
        )
    matched = atomic and len(values) == 3 and values[0][0] > 0 and len(set(values)) == 1
    if declared_matched is not matched:
        raise TargetObservationDiagnosticsError(
            "readiness.atomic_identity_matched contradicts the three status envelopes"
        )
    return matched


def validate_target_observation_diagnostics(document: Mapping[str, object]) -> None:
    """Validate the schema and fail-closed cross-field invariants."""

    _validate_finite_tree(document)
    _validate_schema(document)
    generated_at = _parse_utc(document.get("generated_at_utc"))
    local_now_ms = _integer(document.get("local_now_ms"), name="local_now_ms")
    maximum_age_ms = _integer(document.get("maximum_age_ms"), name="maximum_age_ms")
    generated_at_ms = int(generated_at.timestamp() * 1000)
    if abs(generated_at_ms - local_now_ms) > 5000:
        raise TargetObservationDiagnosticsError(
            "generated_at_utc and local_now_ms differ by more than 5000 ms"
        )

    readiness = _mapping(document.get("readiness"), name="readiness")
    range_status = _mapping(document.get("range_status"), name="range_status")
    geolocation = _mapping(
        document.get("target_geolocation_status"), name="target_geolocation_status"
    )
    gnss_status = _mapping(document.get("gnss_status"), name="gnss_status")
    local_receive = _mapping(document.get("local_receive"), name="local_receive")

    locatable = readiness.get("locatable") is True
    coordinates_included = readiness.get("coordinates_included") is True
    if locatable is not coordinates_included:
        raise TargetObservationDiagnosticsError(
            "readiness.locatable and coordinates_included must be identical"
        )
    raw_reasons = readiness.get("reasons")
    if not isinstance(raw_reasons, list):
        raise TargetObservationDiagnosticsError("readiness.reasons must be an array")
    if locatable and raw_reasons:
        raise TargetObservationDiagnosticsError(
            "locatable diagnostics cannot contain failure reasons"
        )
    if not locatable and not raw_reasons:
        raise TargetObservationDiagnosticsError(
            "non-locatable diagnostics require a failure reason"
        )

    atomic_matched = readiness.get("atomic_identity_matched") is True
    _validate_atomic_identity(
        (range_status, geolocation, gnss_status), declared_matched=atomic_matched
    )

    receive_fresh = all(
        _validate_receive_entry(
            _mapping(local_receive.get(channel), name=f"local_receive.{channel}"),
            channel=channel,
            local_now_ms=local_now_ms,
            maximum_age_ms=maximum_age_ms,
        )
        for channel in ("range", "target_geolocation", "gnss")
    )
    source_fresh = all(
        isinstance(status.get("sourceAgeMs"), int)
        and not isinstance(status.get("sourceAgeMs"), bool)
        and 0 <= int(status["sourceAgeMs"]) <= maximum_age_ms
        for status in (range_status, geolocation, gnss_status)
    )
    statuses_present = all(bool(status) for status in (range_status, geolocation, gnss_status))
    computed_status_fresh = statuses_present and receive_fresh and source_fresh
    if (readiness.get("status_fresh") is True) is not computed_status_fresh:
        raise TargetObservationDiagnosticsError(
            "readiness.status_fresh contradicts source or local receive ages"
        )

    coordinate_keys = {"latitudeDeg", "longitudeDeg", "horizontalSigmaM"}
    present_coordinate_keys = coordinate_keys.intersection(geolocation)
    if not locatable:
        if present_coordinate_keys:
            raise TargetObservationDiagnosticsError(
                "non-locatable diagnostics must omit latitude, longitude, and 1-sigma"
            )
        return

    if not atomic_matched or not computed_status_fresh:
        raise TargetObservationDiagnosticsError(
            "locatable diagnostics require matching atomic identity and fresh status"
        )
    if range_status.get("validity") not in {"VALID", "DEGRADED"}:
        raise TargetObservationDiagnosticsError("locatable diagnostics require qualified range")
    if geolocation.get("available") is not True or gnss_status.get("qualified") is not True:
        raise TargetObservationDiagnosticsError(
            "locatable diagnostics require available geolocation and qualified GNSS"
        )
    if range_status.get("targetId") != geolocation.get("targetId") or not range_status.get(
        "targetId"
    ):
        raise TargetObservationDiagnosticsError("locatable diagnostics require matching target IDs")
    if range_status.get("sourceFrameId") != geolocation.get(
        "sourceFrameId"
    ) or not range_status.get("sourceFrameId"):
        raise TargetObservationDiagnosticsError(
            "locatable diagnostics require matching source frame IDs"
        )
    if present_coordinate_keys != coordinate_keys:
        raise TargetObservationDiagnosticsError(
            "locatable diagnostics require latitude, longitude, and 1-sigma"
        )
    _finite(geolocation["latitudeDeg"], name="target_geolocation_status.latitudeDeg")
    _finite(geolocation["longitudeDeg"], name="target_geolocation_status.longitudeDeg")
    _finite(geolocation["horizontalSigmaM"], name="target_geolocation_status.horizontalSigmaM")


def verify_target_observation_diagnostics(
    path: str | Path, *, require_locatable: bool = False
) -> dict[str, object]:
    """Verify one saved QGC diagnostics file and return a stable audit record."""

    resolved, document = load_target_observation_diagnostics(path)
    readiness = _mapping(document["readiness"], name="readiness")
    locatable = readiness.get("locatable") is True
    diagnostic_reasons = list(readiness.get("reasons", []))
    gate_passed = locatable or not require_locatable
    reasons = [] if gate_passed else ["diagnostics snapshot is valid but not locatable"]
    return {
        "schema": "multidetect.target-observation-diagnostics-verification/v1",
        "schema_version": 1,
        "input_path": str(resolved),
        "input_bytes": resolved.stat().st_size,
        "input_sha256": _sha256(resolved),
        "diagnostics_schema": document["schema"],
        "diagnostics_generated_at_utc": document["generated_at_utc"],
        "diagnostics_locatable": locatable,
        "coordinates_included": readiness.get("coordinates_included") is True,
        "diagnostic_reasons": diagnostic_reasons,
        "require_locatable": require_locatable,
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": gate_passed,
        "reasons": reasons,
    }


def load_target_observation_diagnostics(
    path: str | Path,
) -> tuple[Path, dict[str, Any]]:
    """Open and validate one diagnostics snapshot without weakening file checks."""

    resolved = _resolved_input(path)
    document = _json_object(resolved, name="target-observation diagnostics", strict=True)
    validate_target_observation_diagnostics(document)
    return resolved, document


def write_json_atomic(path: Path, document: Mapping[str, object]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a saved MultiDetectGCS target-observation diagnostics JSON."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--require-locatable", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify_target_observation_diagnostics(
            args.input, require_locatable=args.require_locatable
        )
    except TargetObservationDiagnosticsError as exc:
        report = {
            "schema": "multidetect.target-observation-diagnostics-verification/v1",
            "schema_version": 1,
            "input_path": str(args.input.expanduser()),
            "require_locatable": args.require_locatable,
            "advisory_only": True,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
            "gate_passed": False,
            "reasons": [str(exc)],
        }
    if args.out is not None:
        write_json_atomic(args.out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAXIMUM_AGE_MS",
    "SCHEMA",
    "SCHEMA_PATH",
    "TargetObservationDiagnosticsError",
    "main",
    "load_target_observation_diagnostics",
    "validate_target_observation_diagnostics",
    "verify_target_observation_diagnostics",
    "write_json_atomic",
]
