"""Bind the six field PX4 attitude-covariance observations required by E7.

The individual capture report is deliberately verified by the capture tool's
schema-v3 verifier before any field is copied into this matrix.  The matrix is
therefore an index over independently reproducible evidence, not a second
source of truth.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from .mission_planning import load_executable_mission_document
from .px4_mission import (
    PX4_FIXED_WING_ADAPTER_HASH,
    PX4_FIXED_WING_ADAPTER_ID,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    PX4_VTOL_ADAPTER_HASH,
    PX4_VTOL_ADAPTER_ID,
)

SCHEMA_VERSION = 1
REQUIRED_RUNTIME_MODES = ("hil", "real")
REQUIRED_VEHICLE_CLASSES = ("fixed-wing", "multirotor", "vtol")
REQUIRED_COMBINATIONS = tuple(
    (mode, vehicle) for mode in REQUIRED_RUNTIME_MODES for vehicle in REQUIRED_VEHICLE_CLASSES
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADAPTERS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}
_ROOT = Path(__file__).resolve().parents[2]
_CAPTURE_SCRIPT = _ROOT / "scripts" / "capture_pixhawk_odometry_covariance.py"
_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "release_label",
        "required_runtime_modes",
        "required_vehicle_classes",
        "px4_mission_path_hash",
        "observations",
        "read_only",
        "hardware_control_enabled",
        "gate_passed",
        "checks",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "runtime_mode",
        "vehicle_class",
        "report_path",
        "report_sha256",
        "report_bytes",
        "evidence_sha256",
        "records_verified",
        "endpoint",
        "baud",
        "mission_path",
        "mission_file_sha256",
        "mission_id",
        "mission_sha256",
        "airframe_adapter_id",
        "airframe_adapter_hash",
        "mission_feedback_path",
        "mission_feedback_file_sha256",
        "mission_feedback_evidence_sha256",
        "runtime_profile_path",
        "runtime_profile_file_sha256",
        "runtime_profile_sha256",
        "runtime_profile_id",
        "heartbeat_identity",
        "firmware_identity_hash",
        "mode_binding_sha256",
        "read_only",
        "hardware_control_enabled",
        "transport_link_healthy",
        "qualified_link_healthy",
        "gate_passed",
    }
)
_CHECK_KEYS = frozenset(
    {
        "binding_count",
        "coverage_complete",
        "reports_distinct",
        "reports_verified",
        "sources_hash_bound",
        "missions_current",
        "feedback_bound",
        "profiles_bound",
        "runtime_modes_exact",
        "airframes_exact",
        "links_current",
        "firmware_current",
        "read_only",
        "hardware_control_disabled",
        "px4_mission_path_bound",
        "no_errors",
    }
)


class PixhawkCovarianceBindingError(ValueError):
    """Raised when field covariance evidence cannot form a strict matrix."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _relative(path: Path, directory: Path) -> str:
    try:
        return str(path.relative_to(directory)).replace("\\", "/")
    except ValueError:
        return str(path)


def _resolve(path: object, name: str) -> Path:
    if not isinstance(path, (str, Path)):
        raise PixhawkCovarianceBindingError(f"{name} path is invalid")
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise PixhawkCovarianceBindingError(f"{name} cannot be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PixhawkCovarianceBindingError(f"{name} cannot be resolved: {exc}") from exc
    if not resolved.is_file():
        raise PixhawkCovarianceBindingError(f"{name} must be a file")
    return resolved


@lru_cache(maxsize=1)
def _capture_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_multidetect_covariance_capture_verifier", _CAPTURE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise PixhawkCovarianceBindingError("cannot load covariance capture verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_covariance_file(path: Path) -> Mapping[str, object]:
    try:
        result = _capture_module().verify_observation_file(path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PixhawkCovarianceBindingError(f"covariance report verifier failed: {exc}") from exc
    if not isinstance(result, Mapping) or result.get("verified") is not True:
        raise PixhawkCovarianceBindingError("covariance report verifier did not pass")
    if result.get("gate_passed") is not True:
        raise PixhawkCovarianceBindingError("covariance report gate_passed is false")
    return result


def _load_report(path: Path) -> Mapping[str, object]:
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PixhawkCovarianceBindingError(f"cannot read covariance report: {exc}") from exc
    if not isinstance(report, Mapping):
        raise PixhawkCovarianceBindingError("covariance report must be an object")
    return report


def _bound_path(binding: Mapping[str, object], field: str) -> Path:
    value = binding.get(field)
    if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
        raise PixhawkCovarianceBindingError(f"mode_binding.{field}.path is missing")
    return _resolve(value["path"], f"mode_binding.{field}")


def _record(
    mode: str,
    vehicle: str,
    report_path: Path,
    *,
    manifest_directory: Path,
) -> dict[str, object]:
    verification = _verify_covariance_file(report_path)
    report = _load_report(report_path)
    if report.get("runtime_mode") != mode:
        raise PixhawkCovarianceBindingError(f"{mode}/{vehicle} runtime_mode mismatch")
    binding = report.get("mode_binding")
    diagnostics = report.get("final_diagnostics")
    if not isinstance(binding, Mapping) or not isinstance(diagnostics, Mapping):
        raise PixhawkCovarianceBindingError(f"{mode}/{vehicle} has no field mode binding")
    mission_binding = binding.get("mission")
    feedback_binding = binding.get("mission_feedback")
    profile_binding = binding.get("runtime_profile")
    heartbeat = binding.get("current_heartbeat_identity")
    if not all(
        isinstance(value, Mapping)
        for value in (mission_binding, feedback_binding, profile_binding, heartbeat)
    ):
        raise PixhawkCovarianceBindingError(f"{mode}/{vehicle} mode binding is incomplete")
    mission_path = _bound_path(binding, "mission")
    feedback_path = _bound_path(binding, "mission_feedback")
    profile_path = _bound_path(binding, "runtime_profile")
    mission = load_executable_mission_document(mission_path)
    adapter_id, adapter_hash = _ADAPTERS[vehicle]
    if mission.airframe_adapter_id != adapter_id or mission.airframe_adapter_hash != adapter_hash:
        raise PixhawkCovarianceBindingError(f"{mode}/{vehicle} Mission airframe mismatch")
    qualification = diagnostics.get("qualification")
    if not isinstance(qualification, Mapping) or qualification.get("passed") is not True:
        raise PixhawkCovarianceBindingError(f"{mode}/{vehicle} link is not qualified")
    return {
        "runtime_mode": mode,
        "vehicle_class": vehicle,
        "report_path": _relative(report_path, manifest_directory),
        "report_sha256": _sha256(report_path),
        "report_bytes": report_path.stat().st_size,
        "evidence_sha256": report["evidence_sha256"],
        "records_verified": verification["records_verified"],
        "endpoint": report["endpoint"],
        "baud": report["baud"],
        "mission_path": _relative(mission_path, manifest_directory),
        "mission_file_sha256": _sha256(mission_path),
        "mission_id": mission.mission_id,
        "mission_sha256": mission.canonical_hash,
        "airframe_adapter_id": adapter_id,
        "airframe_adapter_hash": adapter_hash,
        "mission_feedback_path": _relative(feedback_path, manifest_directory),
        "mission_feedback_file_sha256": _sha256(feedback_path),
        "mission_feedback_evidence_sha256": feedback_binding["evidence_sha256"],
        "runtime_profile_path": _relative(profile_path, manifest_directory),
        "runtime_profile_file_sha256": _sha256(profile_path),
        "runtime_profile_sha256": profile_binding["profile_sha256"],
        "runtime_profile_id": profile_binding["profile_id"],
        "heartbeat_identity": dict(heartbeat),
        "firmware_identity_hash": binding["current_firmware_identity_hash"],
        "mode_binding_sha256": binding["binding_sha256"],
        "read_only": diagnostics.get("read_only") is True,
        "hardware_control_enabled": diagnostics.get("hardware_control_enabled") is True,
        "transport_link_healthy": diagnostics.get("transport_link_healthy") is True,
        "qualified_link_healthy": diagnostics.get("qualified_link_healthy") is True,
        "gate_passed": report.get("gate_passed") is True,
    }


def verify_pixhawk_covariance_leaf(
    runtime_mode: str,
    vehicle_class: str,
    report_path: str | Path,
    *,
    manifest_directory: str | Path,
) -> dict[str, object]:
    """Re-open one field leaf and return its source-derived compact record."""

    mode = runtime_mode.strip().lower()
    vehicle = vehicle_class.strip().lower()
    if (mode, vehicle) not in REQUIRED_COMBINATIONS:
        raise PixhawkCovarianceBindingError(f"unknown covariance leaf {mode}/{vehicle}")
    return _record(
        mode,
        vehicle,
        _resolve(report_path, f"{mode}/{vehicle} covariance report"),
        manifest_directory=Path(manifest_directory).resolve(),
    )


def _normalise(raw: object, index: int) -> tuple[str, str, object]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)) or len(raw) != 3:
        raise PixhawkCovarianceBindingError(f"binding {index} must be (MODE, VEHICLE, REPORT)")
    mode, vehicle, report = raw
    return str(mode).strip().lower(), str(vehicle).strip().lower(), report


def build_pixhawk_covariance_binding(
    bindings: Sequence[object],
    *,
    manifest_directory: str | Path,
    output_identity: str | Path,
    release_label: str,
    px4_mission_path_hash: str,
) -> dict[str, object]:
    """Build the exact HIL/REAL x three-airframe covariance matrix."""

    if len(bindings) != 6:
        raise PixhawkCovarianceBindingError("covariance binding requires exactly six reports")
    if not isinstance(release_label, str) or not release_label.strip():
        raise PixhawkCovarianceBindingError("release_label must be non-empty")
    px4_hash = str(px4_mission_path_hash).lower()
    if _SHA256.fullmatch(px4_hash) is None:
        raise PixhawkCovarianceBindingError("px4_mission_path_hash is invalid")
    directory = Path(manifest_directory).resolve()
    output = Path(output_identity).resolve()
    sources: dict[tuple[str, str], Path] = {}
    for index, raw in enumerate(bindings):
        mode, vehicle, raw_path = _normalise(raw, index)
        key = (mode, vehicle)
        if key not in REQUIRED_COMBINATIONS:
            raise PixhawkCovarianceBindingError(f"unknown covariance binding {key!r}")
        if key in sources:
            raise PixhawkCovarianceBindingError(f"duplicate covariance binding {mode}/{vehicle}")
        path = _resolve(raw_path, f"{mode}/{vehicle} covariance report")
        if path == output:
            raise PixhawkCovarianceBindingError("release output cannot also be covariance evidence")
        if path in sources.values():
            raise PixhawkCovarianceBindingError("each matrix leaf requires a distinct report")
        sources[key] = path
    if set(sources) != set(REQUIRED_COMBINATIONS):
        raise PixhawkCovarianceBindingError("covariance matrix coverage is incomplete")
    observations = [
        _record(mode, vehicle, sources[(mode, vehicle)], manifest_directory=directory)
        for mode, vehicle in REQUIRED_COMBINATIONS
    ]
    checks = {name: True for name in sorted(_CHECK_KEYS)}
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "release_label": release_label.strip(),
        "required_runtime_modes": list(REQUIRED_RUNTIME_MODES),
        "required_vehicle_classes": list(REQUIRED_VEHICLE_CLASSES),
        "px4_mission_path_hash": px4_hash,
        "observations": observations,
        "read_only": True,
        "hardware_control_enabled": False,
        "gate_passed": True,
        "checks": checks,
    }
    verification = verify_pixhawk_covariance_binding(
        record,
        manifest_directory=directory,
        expected_release_label=release_label,
        expected_px4_mission_path_hash=px4_hash,
    )
    if verification["passed"] is not True:
        raise PixhawkCovarianceBindingError("; ".join(verification["reasons"]))
    return record


def verify_pixhawk_covariance_binding(
    raw: object,
    *,
    manifest_directory: str | Path,
    expected_release_label: str | None = None,
    expected_px4_mission_path_hash: str | None = None,
) -> dict[str, Any]:
    """Re-open and independently verify every report and bound source."""

    reasons: list[str] = []
    checks = {name: False for name in sorted(_CHECK_KEYS)}
    result: dict[str, Any] = {
        "passed": False,
        "reasons": reasons,
        "checks": checks,
        "observations": [],
        "px4_mission_path_hash": None,
    }
    if not isinstance(raw, Mapping):
        reasons.append("pixhawk_covariance must be an object")
        return result
    unknown = sorted(set(raw).difference(_BINDING_KEYS))
    missing = sorted(_BINDING_KEYS.difference(raw))
    if unknown:
        reasons.append(f"unknown binding field(s): {unknown!r}")
    if missing:
        reasons.append(f"missing binding field(s): {missing!r}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        reasons.append("unsupported covariance binding schema_version")
    if expected_release_label is not None and raw.get("release_label") != expected_release_label:
        reasons.append("release_label does not match product manifest")
    px4_hash = raw.get("px4_mission_path_hash")
    result["px4_mission_path_hash"] = px4_hash
    if not isinstance(px4_hash, str) or _SHA256.fullmatch(px4_hash) is None:
        reasons.append("px4_mission_path_hash is invalid")
    if expected_px4_mission_path_hash is not None and px4_hash != expected_px4_mission_path_hash:
        reasons.append("px4_mission_path_hash does not match E7 parity")
    if raw.get("required_runtime_modes") != list(REQUIRED_RUNTIME_MODES):
        reasons.append("required_runtime_modes is not exact")
    if raw.get("required_vehicle_classes") != list(REQUIRED_VEHICLE_CLASSES):
        reasons.append("required_vehicle_classes is not exact")
    if raw.get("read_only") is not True or raw.get("hardware_control_enabled") is not False:
        reasons.append("covariance matrix is not receive-only")
    observations = raw.get("observations")
    rebuilt: list[dict[str, object]] = []
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        reasons.append("observations must be an array")
    else:
        directory = Path(manifest_directory).resolve()
        seen: set[tuple[str, str]] = set()
        for index, stored in enumerate(observations):
            if not isinstance(stored, Mapping):
                reasons.append(f"observation {index} must be an object")
                continue
            unknown_fields = sorted(set(stored).difference(_OBSERVATION_KEYS))
            missing_fields = sorted(_OBSERVATION_KEYS.difference(stored))
            if unknown_fields or missing_fields:
                reasons.append(
                    f"observation {index} fields invalid: "
                    f"unknown={unknown_fields!r}, missing={missing_fields!r}"
                )
                continue
            mode, vehicle = stored.get("runtime_mode"), stored.get("vehicle_class")
            key = (str(mode), str(vehicle))
            if key in seen:
                reasons.append(f"duplicate observation {key!r}")
                continue
            seen.add(key)
            path = _resolve(directory / str(stored["report_path"]), f"observation {index}")
            try:
                current = _record(key[0], key[1], path, manifest_directory=directory)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                reasons.append(f"{key[0]}/{key[1]} verification failed: {exc}")
                continue
            rebuilt.append(current)
            if dict(stored) != current:
                reasons.append(f"{key[0]}/{key[1]} compact record does not match sources")
        if seen != set(REQUIRED_COMBINATIONS):
            reasons.append("covariance matrix coverage is incomplete")
    result["observations"] = rebuilt
    declared_checks = raw.get("checks")
    if not isinstance(declared_checks, Mapping) or set(declared_checks) != _CHECK_KEYS:
        reasons.append("checks are incomplete or contain unknown fields")
    elif any(value is not True for value in declared_checks.values()):
        reasons.append("all declared checks must be true")
    if raw.get("gate_passed") is not True:
        reasons.append("gate_passed must be true")
    if not reasons:
        checks.update({name: True for name in checks})
        result["passed"] = True
    return result


__all__ = [
    "PixhawkCovarianceBindingError",
    "REQUIRED_COMBINATIONS",
    "build_pixhawk_covariance_binding",
    "verify_pixhawk_covariance_leaf",
    "verify_pixhawk_covariance_binding",
]
