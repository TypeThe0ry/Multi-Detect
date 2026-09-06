"""Hash-bind six HIL/REAL PX4 Mission feedback observations.

The field observer writes one receive-only report for one executable Mission.
This module joins those smallest trustworthy units into the exact matrix needed
by the release gate::

    HIL  x (fixed-wing, multirotor, VTOL)
    REAL x (fixed-wing, multirotor, VTOL)

The join deliberately remains a *sub-gate*.  It proves Mission identity,
airframe dispatch, MAVLink source identity, freshness at observation time and
zero transmitted messages.  It does not claim complete HIL, REAL, hardware,
flight or operator-visual acceptance.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .mission_planning import load_executable_mission_document
from .px4_mission import (
    PX4_FIXED_WING_ADAPTER_HASH,
    PX4_FIXED_WING_ADAPTER_ID,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    PX4_VTOL_ADAPTER_HASH,
    PX4_VTOL_ADAPTER_ID,
    normalize_px4_vehicle_class,
)
from .px4_mission_feedback_cli import load_px4_mission_feedback_observation

PX4_MISSION_FEEDBACK_BINDING_SCHEMA_VERSION = 2
PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES = ("hil", "real")
PX4_MISSION_FEEDBACK_REQUIRED_VEHICLE_CLASSES = (
    "fixed-wing",
    "multirotor",
    "vtol",
)
PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS = tuple(
    (mode, vehicle_class)
    for mode in PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES
    for vehicle_class in PX4_MISSION_FEEDBACK_REQUIRED_VEHICLE_CLASSES
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ADAPTER_BY_VEHICLE_CLASS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}
_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "release_label",
        "required_runtime_modes",
        "required_vehicle_classes",
        "px4_mission_path_hash",
        "observations",
        "read_only",
        "messages_transmitted",
        "hardware_acceptance",
        "hil_acceptance",
        "real_acceptance",
        "operator_visual_acceptance",
        "gate_passed",
        "checks",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "runtime_mode",
        "vehicle_class",
        "mission_path",
        "mission_sha256",
        "mission_bytes",
        "runtime_profile_path",
        "runtime_profile_sha256",
        "runtime_profile_bytes",
        "report_path",
        "report_sha256",
        "report_bytes",
        "mission_id",
        "mission_hash",
        "mission_item_count",
        "airframe_adapter_id",
        "airframe_adapter_hash",
        "airframe_profile_hash",
        "runtime_profile_id",
        "runtime_profile_hash",
        "runtime_profile_key_id",
        "vehicle_identity_hash",
        "firmware_identity_hash",
        "endpoint_binding_hash",
        "signing_identity_hash",
        "runtime_binding_sha256",
        "runtime_binding_gate_passed",
        "flight_control_enabled",
        "output_locked",
        "observed_at_utc",
        "source_system_id",
        "source_component_id",
        "feedback_sha256",
        "feedback_evidence_sha256",
        "read_only",
        "messages_transmitted",
        "feedback_gate_passed",
    }
)
_CHECK_KEYS = frozenset(
    {
        "binding_count",
        "coverage_complete",
        "report_paths_distinct",
        "runtime_profile_paths_distinct",
        "sources_hash_bound",
        "all_observations_qualified",
        "all_receive_only",
        "all_missions_identity_bound",
        "all_runtime_modes_match",
        "all_airframes_match",
        "all_runtime_profiles_bound",
        "all_vehicle_identities_bound",
        "all_firmware_identities_bound",
        "all_endpoints_bound",
        "all_output_locked",
        "px4_mission_path_bound",
        "no_errors",
    }
)


class Px4MissionFeedbackBindingError(ValueError):
    """Raised when the six-source PX4 feedback binding is invalid."""


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value.strip()) is not None


def _require_sha256(value: object, name: str) -> str:
    if not _is_sha256(value):
        raise Px4MissionFeedbackBindingError(
            f"{name} must contain exactly 64 hexadecimal characters"
        )
    return str(value).strip().lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_source(value: object, *, name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise Px4MissionFeedbackBindingError(f"{name} must be a filesystem path")
    source = Path(value).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise Px4MissionFeedbackBindingError(f"{name} does not exist: {source}") from exc
    if not resolved.is_file():
        raise Px4MissionFeedbackBindingError(f"{name} must be a regular file: {resolved}")
    return resolved


def _relative_path(path: Path, *, manifest_directory: Path) -> str:
    try:
        return Path(os.path.relpath(path, manifest_directory)).as_posix()
    except ValueError as exc:
        raise Px4MissionFeedbackBindingError(
            f"source and release manifest must be on the same filesystem: {path}"
        ) from exc


def _normalise_runtime_mode(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise Px4MissionFeedbackBindingError(f"{name} must be 'hil' or 'real'")
    mode = value.strip().lower()
    if mode not in PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES:
        raise Px4MissionFeedbackBindingError(f"{name} must be 'hil' or 'real'")
    return mode


def _normalise_binding_input(
    value: object,
    *,
    index: int,
) -> tuple[str, str, object, object, object]:
    if isinstance(value, Mapping):
        unknown = sorted(
            set(value).difference(
                {
                    "runtime_mode",
                    "vehicle_class",
                    "mission_path",
                    "runtime_profile_path",
                    "report_path",
                }
            )
        )
        missing = sorted(
            {
                "runtime_mode",
                "vehicle_class",
                "mission_path",
                "runtime_profile_path",
                "report_path",
            }.difference(value)
        )
        if unknown or missing:
            raise Px4MissionFeedbackBindingError(
                f"binding {index} fields are invalid; missing={missing!r}, unknown={unknown!r}"
            )
        raw_mode = value["runtime_mode"]
        raw_vehicle = value["vehicle_class"]
        mission_path = value["mission_path"]
        runtime_profile_path = value["runtime_profile_path"]
        report_path = value["report_path"]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) != 5:
            raise Px4MissionFeedbackBindingError(
                f"binding {index} must contain MODE, VEHICLE, MISSION, PROFILE and REPORT"
            )
        raw_mode, raw_vehicle, mission_path, runtime_profile_path, report_path = value
    else:
        raise Px4MissionFeedbackBindingError(
            f"binding {index} must be a mapping or five-item sequence"
        )
    mode = _normalise_runtime_mode(raw_mode, name=f"binding {index} runtime_mode")
    try:
        vehicle_class = normalize_px4_vehicle_class(raw_vehicle)
    except (TypeError, ValueError) as exc:
        raise Px4MissionFeedbackBindingError(
            f"binding {index} vehicle_class is invalid: {exc}"
        ) from exc
    return mode, vehicle_class, mission_path, runtime_profile_path, report_path


def _bound_path(
    value: object,
    *,
    manifest_directory: Path,
    name: str,
    reasons: list[str],
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        reasons.append(f"{name} is missing or empty")
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        reasons.append(f"{name} must be relative to the release manifest")
        return None
    try:
        return (manifest_directory / candidate).resolve()
    except (OSError, RuntimeError) as exc:
        reasons.append(f"{name} cannot be resolved: {exc}")
        return None


def _record_from_sources(
    *,
    runtime_mode: str,
    vehicle_class: str,
    mission_path: Path,
    runtime_profile_path: Path,
    report_path: Path,
    manifest_directory: Path,
) -> dict[str, object]:
    mission = load_executable_mission_document(mission_path)
    expected_adapter_id, expected_adapter_hash = _ADAPTER_BY_VEHICLE_CLASS[vehicle_class]
    if mission.airframe_adapter_id != expected_adapter_id:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} Mission uses adapter "
            f"{mission.airframe_adapter_id!r}, expected {expected_adapter_id!r}"
        )
    if mission.airframe_adapter_hash.lower() != expected_adapter_hash.lower():
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} Mission adapter hash is not current"
        )
    report = load_px4_mission_feedback_observation(
        report_path,
        expected_mission=mission,
        runtime_profile_path=runtime_profile_path,
    )
    if report.get("runtime_mode") != runtime_mode:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report runtime_mode does not match its binding"
        )
    if report.get("feedback_gate_passed") is not True:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report feedback_gate_passed is false"
        )
    transport = report.get("transport")
    feedback = report.get("feedback")
    runtime_binding = report.get("runtime_binding")
    if (
        not isinstance(transport, Mapping)
        or not isinstance(feedback, Mapping)
        or not isinstance(runtime_binding, Mapping)
    ):
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report has no verified "
            "transport/feedback/runtime binding"
        )
    if transport.get("read_only") is not True or transport.get("messages_transmitted") != 0:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report is not receive-only"
        )
    source_system_id = feedback.get("source_system_id")
    source_component_id = feedback.get("source_component_id")
    if type(source_system_id) is not int or not 1 <= source_system_id <= 255:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report has no bound source_system_id"
        )
    if type(source_component_id) is not int or not 1 <= source_component_id <= 255:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report has no bound source_component_id"
        )
    if runtime_binding.get("gate_passed") is not True:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} runtime binding gate is not qualified"
        )
    if runtime_binding.get("flight_control_enabled") is not False:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} observation enabled flight-control output"
        )
    if runtime_binding.get("output_locked") is not True:
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} observation did not keep output locked"
        )
    runtime_profile = runtime_binding.get("profile")
    observed_identity = runtime_binding.get("observed_identity")
    if not isinstance(runtime_profile, Mapping) or not isinstance(observed_identity, Mapping):
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report has no verified runtime identity"
        )
    report_mission = report.get("mission")
    if not isinstance(report_mission, Mapping):  # verified loader makes this defensive only
        raise Px4MissionFeedbackBindingError(
            f"{runtime_mode}/{vehicle_class} report has no Mission identity"
        )
    return {
        "runtime_mode": runtime_mode,
        "vehicle_class": vehicle_class,
        "mission_path": _relative_path(
            mission_path,
            manifest_directory=manifest_directory,
        ),
        "mission_sha256": _sha256_file(mission_path),
        "mission_bytes": mission_path.stat().st_size,
        "runtime_profile_path": _relative_path(
            runtime_profile_path,
            manifest_directory=manifest_directory,
        ),
        "runtime_profile_sha256": _sha256_file(runtime_profile_path),
        "runtime_profile_bytes": runtime_profile_path.stat().st_size,
        "report_path": _relative_path(
            report_path,
            manifest_directory=manifest_directory,
        ),
        "report_sha256": _sha256_file(report_path),
        "report_bytes": report_path.stat().st_size,
        "mission_id": mission.mission_id,
        "mission_hash": mission.canonical_hash,
        "mission_item_count": len(mission.items),
        "airframe_adapter_id": mission.airframe_adapter_id,
        "airframe_adapter_hash": mission.airframe_adapter_hash,
        "airframe_profile_hash": mission.airframe_profile_hash,
        "runtime_profile_id": runtime_profile.get("profile_id"),
        "runtime_profile_hash": runtime_binding.get("profile_sha256"),
        "runtime_profile_key_id": runtime_binding.get("key_id"),
        "vehicle_identity_hash": observed_identity.get("vehicle_identity_hash"),
        "firmware_identity_hash": observed_identity.get("firmware_identity_hash"),
        "endpoint_binding_hash": observed_identity.get("endpoint_binding_hash"),
        "signing_identity_hash": observed_identity.get("signing_identity_hash"),
        "runtime_binding_sha256": runtime_binding.get("binding_sha256"),
        "runtime_binding_gate_passed": True,
        "flight_control_enabled": False,
        "output_locked": True,
        "observed_at_utc": report["observed_at_utc"],
        "source_system_id": source_system_id,
        "source_component_id": source_component_id,
        "feedback_sha256": report["feedback_sha256"],
        "feedback_evidence_sha256": report["evidence_sha256"],
        "read_only": True,
        "messages_transmitted": 0,
        "feedback_gate_passed": True,
    }


def build_px4_mission_feedback_binding(
    bindings: Sequence[object],
    *,
    manifest_directory: str | Path,
    output_identity: str | Path,
    release_label: str,
    px4_mission_path_hash: str,
) -> dict[str, object]:
    """Build and independently re-open the six-source release record."""

    if not isinstance(release_label, str) or not release_label.strip():
        raise Px4MissionFeedbackBindingError("release_label must be non-empty")
    normalized_px4_hash = _require_sha256(
        px4_mission_path_hash,
        "px4_mission_path_hash",
    )
    directory = Path(manifest_directory).expanduser().resolve()
    output = Path(output_identity).expanduser().resolve()
    if len(bindings) != len(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS):
        raise Px4MissionFeedbackBindingError(
            "PX4 Mission feedback binding requires exactly six observations: "
            "HIL/REAL x fixed-wing/multirotor/VTOL"
        )

    normalized: dict[tuple[str, str], tuple[Path, Path, Path]] = {}
    runtime_profile_paths: set[Path] = set()
    report_paths: set[Path] = set()
    for index, raw in enumerate(bindings):
        (
            mode,
            vehicle_class,
            raw_mission,
            raw_runtime_profile,
            raw_report,
        ) = _normalise_binding_input(
            raw,
            index=index,
        )
        combination = (mode, vehicle_class)
        if combination in normalized:
            raise Px4MissionFeedbackBindingError(
                f"duplicate PX4 Mission feedback binding for {mode}/{vehicle_class}"
            )
        mission_path = _resolve_source(
            raw_mission,
            name=f"{mode}/{vehicle_class} Mission",
        )
        runtime_profile_path = _resolve_source(
            raw_runtime_profile,
            name=f"{mode}/{vehicle_class} runtime profile",
        )
        report_path = _resolve_source(
            raw_report,
            name=f"{mode}/{vehicle_class} feedback report",
        )
        if output in {mission_path, runtime_profile_path, report_path}:
            raise Px4MissionFeedbackBindingError(
                "release-gate output cannot also be Mission feedback source evidence"
            )
        if len({mission_path, runtime_profile_path, report_path}) != 3:
            raise Px4MissionFeedbackBindingError(
                f"{mode}/{vehicle_class} Mission, runtime profile and feedback report "
                "must be distinct"
            )
        if runtime_profile_path in runtime_profile_paths:
            raise Px4MissionFeedbackBindingError(
                "each HIL/REAL airframe requires a distinct signed runtime profile"
            )
        if report_path in report_paths:
            raise Px4MissionFeedbackBindingError(
                "each HIL/REAL airframe requires a distinct feedback report"
            )
        runtime_profile_paths.add(runtime_profile_path)
        report_paths.add(report_path)
        normalized[combination] = (
            mission_path,
            runtime_profile_path,
            report_path,
        )

    if set(normalized) != set(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS):
        missing = sorted(set(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS).difference(normalized))
        unknown = sorted(set(normalized).difference(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS))
        raise Px4MissionFeedbackBindingError(
            f"PX4 Mission feedback coverage is incomplete; missing={missing!r}, unknown={unknown!r}"
        )

    observations = [
        _record_from_sources(
            runtime_mode=mode,
            vehicle_class=vehicle_class,
            mission_path=normalized[(mode, vehicle_class)][0],
            runtime_profile_path=normalized[(mode, vehicle_class)][1],
            report_path=normalized[(mode, vehicle_class)][2],
            manifest_directory=directory,
        )
        for mode, vehicle_class in PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS
    ]
    checks = {name: True for name in sorted(_CHECK_KEYS)}
    record: dict[str, object] = {
        "schema_version": PX4_MISSION_FEEDBACK_BINDING_SCHEMA_VERSION,
        "release_label": release_label.strip(),
        "required_runtime_modes": list(PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES),
        "required_vehicle_classes": list(PX4_MISSION_FEEDBACK_REQUIRED_VEHICLE_CLASSES),
        "px4_mission_path_hash": normalized_px4_hash,
        "observations": observations,
        "read_only": True,
        "messages_transmitted": 0,
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "operator_visual_acceptance": False,
        "gate_passed": True,
        "checks": checks,
    }
    verification = verify_px4_mission_feedback_binding(
        record,
        manifest_directory=directory,
        expected_release_label=release_label.strip(),
        expected_px4_mission_path_hash=normalized_px4_hash,
    )
    if verification["passed"] is not True:
        raise Px4MissionFeedbackBindingError(
            "PX4 Mission feedback binding verification failed: "
            + "; ".join(str(reason) for reason in verification["reasons"])
        )
    return record


def verify_px4_mission_feedback_binding(
    raw_record: object,
    *,
    manifest_directory: str | Path,
    expected_release_label: str | None = None,
    expected_px4_mission_path_hash: str | None = None,
) -> dict[str, Any]:
    """Re-open every bound Mission/report pair and recompute the sub-gate."""

    directory = Path(manifest_directory).expanduser().resolve()
    reasons: list[str] = []
    observations_result: list[dict[str, object]] = []
    if not isinstance(raw_record, Mapping):
        return {
            "passed": False,
            "reasons": ["px4_mission_feedback must be an object"],
            "checks": {name: False for name in sorted(_CHECK_KEYS)},
            "observations": observations_result,
            "px4_mission_path_hash": None,
        }

    unknown_binding_fields = sorted(set(raw_record).difference(_BINDING_KEYS))
    missing_binding_fields = sorted(_BINDING_KEYS.difference(raw_record))
    if unknown_binding_fields:
        reasons.append(f"unknown binding field(s): {unknown_binding_fields!r}")
    if missing_binding_fields:
        reasons.append(f"missing binding field(s): {missing_binding_fields!r}")
    if raw_record.get("schema_version") != PX4_MISSION_FEEDBACK_BINDING_SCHEMA_VERSION:
        reasons.append("unsupported PX4 Mission feedback binding schema_version")
    release_label = raw_record.get("release_label")
    if not isinstance(release_label, str) or not release_label.strip():
        reasons.append("release_label is missing or empty")
    elif expected_release_label is not None and release_label != expected_release_label:
        reasons.append("release_label does not match the product manifest")
    if raw_record.get("required_runtime_modes") != list(
        PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES
    ):
        reasons.append("required_runtime_modes must be exactly ['hil', 'real']")
    if raw_record.get("required_vehicle_classes") != list(
        PX4_MISSION_FEEDBACK_REQUIRED_VEHICLE_CLASSES
    ):
        reasons.append("required_vehicle_classes must be exactly fixed-wing, multirotor and vtol")
    declared_px4_hash = raw_record.get("px4_mission_path_hash")
    normalized_px4_hash = (
        str(declared_px4_hash).strip().lower() if _is_sha256(declared_px4_hash) else None
    )
    if normalized_px4_hash is None:
        reasons.append("px4_mission_path_hash is missing or invalid")
    expected_px4_hash = None
    if expected_px4_mission_path_hash is not None:
        try:
            expected_px4_hash = _require_sha256(
                expected_px4_mission_path_hash,
                "expected_px4_mission_path_hash",
            )
        except Px4MissionFeedbackBindingError as exc:
            reasons.append(str(exc))
    px4_path_bound = normalized_px4_hash is not None and (
        expected_px4_hash is None or normalized_px4_hash == expected_px4_hash
    )
    if not px4_path_bound and expected_px4_hash is not None:
        reasons.append("px4_mission_path_hash does not match the four-mode E7 parity")

    for name, expected in (
        ("read_only", True),
        ("messages_transmitted", 0),
        ("hardware_acceptance", False),
        ("hil_acceptance", False),
        ("real_acceptance", False),
        ("operator_visual_acceptance", False),
        ("gate_passed", True),
    ):
        if raw_record.get(name) != expected or type(raw_record.get(name)) is not type(expected):
            reasons.append(f"{name} must be {expected!r}")

    raw_observations = raw_record.get("observations")
    observations = raw_observations if isinstance(raw_observations, list) else []
    if not isinstance(raw_observations, list):
        reasons.append("observations must be an array")
    if len(observations) != len(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS):
        reasons.append("observations must contain exactly six bindings")

    observed_combinations: list[tuple[str, str]] = []
    resolved_runtime_profile_paths: list[Path] = []
    resolved_report_paths: list[Path] = []
    sources_hash_bound = True
    all_observations_qualified = True
    all_receive_only = True
    all_missions_identity_bound = True
    all_runtime_modes_match = True
    all_airframes_match = True
    all_runtime_profiles_bound = True
    all_vehicle_identities_bound = True
    all_firmware_identities_bound = True
    all_endpoints_bound = True
    all_output_locked = True

    for index, raw_observation in enumerate(observations):
        prefix = f"observation {index}"
        entry_reasons: list[str] = []
        if not isinstance(raw_observation, Mapping):
            reasons.append(f"{prefix} must be an object")
            sources_hash_bound = False
            all_observations_qualified = False
            all_receive_only = False
            all_missions_identity_bound = False
            all_runtime_modes_match = False
            all_airframes_match = False
            all_runtime_profiles_bound = False
            all_vehicle_identities_bound = False
            all_firmware_identities_bound = False
            all_endpoints_bound = False
            all_output_locked = False
            continue
        unknown = sorted(set(raw_observation).difference(_OBSERVATION_KEYS))
        missing = sorted(_OBSERVATION_KEYS.difference(raw_observation))
        if unknown:
            entry_reasons.append(f"unknown field(s): {unknown!r}")
        if missing:
            entry_reasons.append(f"missing field(s): {missing!r}")

        try:
            mode = _normalise_runtime_mode(
                raw_observation.get("runtime_mode"),
                name=f"{prefix}.runtime_mode",
            )
        except Px4MissionFeedbackBindingError as exc:
            mode = ""
            entry_reasons.append(str(exc))
            all_runtime_modes_match = False
        try:
            vehicle_class = normalize_px4_vehicle_class(raw_observation.get("vehicle_class"))
        except (TypeError, ValueError) as exc:
            vehicle_class = ""
            entry_reasons.append(f"{prefix}.vehicle_class is invalid: {exc}")
            all_airframes_match = False
        if mode and vehicle_class:
            observed_combinations.append((mode, vehicle_class))

        mission_path = _bound_path(
            raw_observation.get("mission_path"),
            manifest_directory=directory,
            name=f"{prefix}.mission_path",
            reasons=entry_reasons,
        )
        runtime_profile_path = _bound_path(
            raw_observation.get("runtime_profile_path"),
            manifest_directory=directory,
            name=f"{prefix}.runtime_profile_path",
            reasons=entry_reasons,
        )
        if runtime_profile_path is not None:
            resolved_runtime_profile_paths.append(runtime_profile_path)
        report_path = _bound_path(
            raw_observation.get("report_path"),
            manifest_directory=directory,
            name=f"{prefix}.report_path",
            reasons=entry_reasons,
        )
        if report_path is not None:
            resolved_report_paths.append(report_path)
        mission = None
        report: Mapping[str, object] | None = None
        source_summary: dict[str, object] = {
            "runtime_mode": mode or raw_observation.get("runtime_mode"),
            "vehicle_class": vehicle_class or raw_observation.get("vehicle_class"),
            "mission_path": str(mission_path) if mission_path is not None else None,
            "runtime_profile_path": (
                str(runtime_profile_path) if runtime_profile_path is not None else None
            ),
            "report_path": str(report_path) if report_path is not None else None,
        }

        for source_name, source_path, sha_field, bytes_field in (
            (
                "Mission",
                mission_path,
                "mission_sha256",
                "mission_bytes",
            ),
            (
                "runtime profile",
                runtime_profile_path,
                "runtime_profile_sha256",
                "runtime_profile_bytes",
            ),
            (
                "feedback report",
                report_path,
                "report_sha256",
                "report_bytes",
            ),
        ):
            expected_sha = raw_observation.get(sha_field)
            expected_bytes = raw_observation.get(bytes_field)
            if not _is_sha256(expected_sha):
                entry_reasons.append(f"{sha_field} is missing or invalid")
                sources_hash_bound = False
            if type(expected_bytes) is not int or expected_bytes < 0:
                entry_reasons.append(f"{bytes_field} must be a non-negative integer")
                sources_hash_bound = False
            if source_path is None or not source_path.is_file():
                entry_reasons.append(f"{source_name} file does not exist")
                sources_hash_bound = False
                continue
            try:
                actual_sha = _sha256_file(source_path)
                actual_bytes = source_path.stat().st_size
            except OSError as exc:
                entry_reasons.append(f"{source_name} cannot be read: {exc}")
                sources_hash_bound = False
                continue
            source_summary[f"{sha_field}_actual"] = actual_sha
            source_summary[f"{bytes_field}_actual"] = actual_bytes
            if _is_sha256(expected_sha) and actual_sha != str(expected_sha).lower():
                entry_reasons.append(f"{source_name} SHA-256 does not match")
                sources_hash_bound = False
            if type(expected_bytes) is int and actual_bytes != expected_bytes:
                entry_reasons.append(f"{source_name} byte count does not match")
                sources_hash_bound = False

        if mission_path is not None and mission_path.is_file():
            try:
                mission = load_executable_mission_document(mission_path)
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                entry_reasons.append(f"Mission cannot be verified: {exc}")
                all_missions_identity_bound = False
        else:
            all_missions_identity_bound = False

        if mission is not None:
            expected_adapter = _ADAPTER_BY_VEHICLE_CLASS.get(vehicle_class)
            if expected_adapter is None or (
                mission.airframe_adapter_id,
                mission.airframe_adapter_hash.lower(),
            ) != (expected_adapter[0], expected_adapter[1].lower()):
                entry_reasons.append("Mission airframe adapter does not match vehicle_class")
                all_airframes_match = False
            for field, actual in (
                ("mission_id", mission.mission_id),
                ("mission_hash", mission.canonical_hash),
                ("mission_item_count", len(mission.items)),
                ("airframe_adapter_id", mission.airframe_adapter_id),
                ("airframe_adapter_hash", mission.airframe_adapter_hash.lower()),
            ):
                declared = raw_observation.get(field)
                if field == "mission_item_count" and type(declared) is not int:
                    entry_reasons.append("mission_item_count must be an integer")
                    all_missions_identity_bound = False
                    continue
                comparable = (
                    declared.lower() if isinstance(declared, str) and "hash" in field else declared
                )
                if comparable != actual:
                    entry_reasons.append(f"{field} does not match the Mission")
                    all_missions_identity_bound = False

        if (
            report_path is not None
            and report_path.is_file()
            and runtime_profile_path is not None
            and runtime_profile_path.is_file()
            and mission is not None
        ):
            try:
                report = load_px4_mission_feedback_observation(
                    report_path,
                    expected_mission=mission,
                    runtime_profile_path=runtime_profile_path,
                )
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                entry_reasons.append(f"feedback report cannot be verified: {exc}")
                all_observations_qualified = False
                all_runtime_profiles_bound = False
                all_vehicle_identities_bound = False
                all_firmware_identities_bound = False
                all_endpoints_bound = False
                all_output_locked = False
        else:
            all_observations_qualified = False
            all_runtime_profiles_bound = False
            all_vehicle_identities_bound = False
            all_firmware_identities_bound = False
            all_endpoints_bound = False
            all_output_locked = False

        if report is not None:
            if report.get("runtime_mode") != mode:
                entry_reasons.append("feedback report runtime_mode does not match binding")
                all_runtime_modes_match = False
            if report.get("feedback_gate_passed") is not True:
                entry_reasons.append("feedback report gate is not qualified")
                all_observations_qualified = False
            transport = report.get("transport")
            feedback = report.get("feedback")
            runtime_binding = report.get("runtime_binding")
            if not isinstance(transport, Mapping) or (
                transport.get("read_only") is not True or transport.get("messages_transmitted") != 0
            ):
                entry_reasons.append("feedback report is not receive-only")
                all_receive_only = False
            if not isinstance(feedback, Mapping):
                entry_reasons.append("feedback report has no feedback snapshot")
                all_observations_qualified = False
            else:
                for field, report_value in (
                    ("source_system_id", feedback.get("source_system_id")),
                    ("source_component_id", feedback.get("source_component_id")),
                    ("feedback_sha256", report.get("feedback_sha256")),
                    ("feedback_evidence_sha256", report.get("evidence_sha256")),
                    ("observed_at_utc", report.get("observed_at_utc")),
                ):
                    declared = raw_observation.get(field)
                    if field in {"source_system_id", "source_component_id"} and (
                        type(declared) is not int or not 1 <= declared <= 255
                    ):
                        entry_reasons.append(f"{field} must be a MAVLink uint8 identity")
                        all_observations_qualified = False
                    elif declared != report_value:
                        entry_reasons.append(f"{field} does not match the feedback report")
                        all_observations_qualified = False
            if not isinstance(runtime_binding, Mapping):
                entry_reasons.append("feedback report has no runtime binding")
                all_runtime_profiles_bound = False
                all_vehicle_identities_bound = False
                all_firmware_identities_bound = False
                all_endpoints_bound = False
                all_output_locked = False
            else:
                profile = runtime_binding.get("profile")
                identity = runtime_binding.get("observed_identity")
                if not isinstance(profile, Mapping) or not isinstance(identity, Mapping):
                    entry_reasons.append("feedback report runtime binding has no profile/identity")
                    all_runtime_profiles_bound = False
                    all_vehicle_identities_bound = False
                    all_firmware_identities_bound = False
                    all_endpoints_bound = False
                else:
                    expected_profile_id = "hil" if mode == "hil" else "flight"
                    comparisons = (
                        (
                            "runtime_profile_id",
                            profile.get("profile_id"),
                            raw_observation.get("runtime_profile_id"),
                        ),
                        (
                            "runtime_profile_sha256",
                            runtime_binding.get("profile_document_sha256"),
                            raw_observation.get("runtime_profile_sha256"),
                        ),
                        (
                            "runtime_profile_hash",
                            runtime_binding.get("profile_sha256"),
                            raw_observation.get("runtime_profile_hash"),
                        ),
                        (
                            "runtime_profile_key_id",
                            runtime_binding.get("key_id"),
                            raw_observation.get("runtime_profile_key_id"),
                        ),
                        (
                            "vehicle_identity_hash",
                            identity.get("vehicle_identity_hash"),
                            raw_observation.get("vehicle_identity_hash"),
                        ),
                        (
                            "firmware_identity_hash",
                            identity.get("firmware_identity_hash"),
                            raw_observation.get("firmware_identity_hash"),
                        ),
                        (
                            "endpoint_binding_hash",
                            identity.get("endpoint_binding_hash"),
                            raw_observation.get("endpoint_binding_hash"),
                        ),
                        (
                            "signing_identity_hash",
                            identity.get("signing_identity_hash"),
                            raw_observation.get("signing_identity_hash"),
                        ),
                        (
                            "runtime_binding_sha256",
                            runtime_binding.get("binding_sha256"),
                            raw_observation.get("runtime_binding_sha256"),
                        ),
                    )
                    for field, report_value, expected_value in comparisons:
                        if report_value != expected_value:
                            entry_reasons.append(f"{field} does not match the runtime binding")
                            if field == "vehicle_identity_hash":
                                all_vehicle_identities_bound = False
                            elif field == "firmware_identity_hash":
                                all_firmware_identities_bound = False
                            elif field == "endpoint_binding_hash":
                                all_endpoints_bound = False
                            else:
                                all_runtime_profiles_bound = False
                    if profile.get("profile_id") != expected_profile_id:
                        entry_reasons.append("runtime profile kind does not match HIL/REAL mode")
                        all_runtime_profiles_bound = False
                    if raw_observation.get("runtime_profile_id") != expected_profile_id:
                        entry_reasons.append(
                            "bound runtime_profile_id does not match HIL/REAL mode"
                        )
                        all_runtime_profiles_bound = False
                    if identity.get("airframe_profile_hash") != mission.airframe_profile_hash:
                        entry_reasons.append("runtime airframe identity does not match the Mission")
                        all_airframes_match = False
                    if raw_observation.get("airframe_profile_hash") != (
                        mission.airframe_profile_hash
                    ):
                        entry_reasons.append("airframe_profile_hash does not match the Mission")
                        all_airframes_match = False
                    vehicle_identity_hash = raw_observation.get("vehicle_identity_hash")
                    if not _is_sha256(vehicle_identity_hash):
                        entry_reasons.append("vehicle_identity_hash is missing or invalid")
                        all_vehicle_identities_bound = False
                    firmware_identity_hash = raw_observation.get("firmware_identity_hash")
                    if not _is_sha256(firmware_identity_hash):
                        entry_reasons.append("firmware_identity_hash is missing or invalid")
                        all_firmware_identities_bound = False
                    endpoint_binding_hash = raw_observation.get("endpoint_binding_hash")
                    if not _is_sha256(endpoint_binding_hash):
                        entry_reasons.append("endpoint_binding_hash is missing or invalid")
                        all_endpoints_bound = False
                    signing_identity_hash = raw_observation.get("signing_identity_hash")
                    if mode == "real" and not _is_sha256(signing_identity_hash):
                        entry_reasons.append("REAL signing_identity_hash is missing or invalid")
                        all_runtime_profiles_bound = False
                    if mode == "hil" and signing_identity_hash is not None:
                        entry_reasons.append("HIL signing_identity_hash must remain null")
                        all_runtime_profiles_bound = False
                if runtime_binding.get("gate_passed") is not True or (
                    raw_observation.get("runtime_binding_gate_passed") is not True
                ):
                    entry_reasons.append("runtime binding gate is not qualified")
                    all_runtime_profiles_bound = False
                for field, expected in (
                    ("flight_control_enabled", False),
                    ("output_locked", True),
                ):
                    if (
                        runtime_binding.get(field) is not expected
                        or raw_observation.get(field) is not expected
                    ):
                        entry_reasons.append(
                            f"{field} does not match the receive-only runtime decision"
                        )
                        all_output_locked = False
            if raw_observation.get("read_only") is not True:
                entry_reasons.append("read_only must be true")
                all_receive_only = False
            if (
                type(raw_observation.get("messages_transmitted")) is not int
                or raw_observation.get("messages_transmitted") != 0
            ):
                entry_reasons.append("messages_transmitted must be zero")
                all_receive_only = False
            if raw_observation.get("feedback_gate_passed") is not True:
                entry_reasons.append("feedback_gate_passed must be true")
                all_observations_qualified = False

        if entry_reasons:
            reasons.extend(f"{prefix}: {reason}" for reason in entry_reasons)
        source_summary["passed"] = not entry_reasons
        observations_result.append(source_summary)

    coverage_complete = len(observed_combinations) == len(
        PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS
    ) and set(observed_combinations) == set(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS)
    report_paths_distinct = len(resolved_report_paths) == len(
        PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS
    ) and len(set(resolved_report_paths)) == len(resolved_report_paths)
    runtime_profile_paths_distinct = len(resolved_runtime_profile_paths) == len(
        PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS
    ) and len(set(resolved_runtime_profile_paths)) == len(resolved_runtime_profile_paths)
    if not coverage_complete:
        reasons.append("HIL/REAL x fixed-wing/multirotor/VTOL coverage is incomplete")
    if not report_paths_distinct:
        reasons.append("the six feedback reports must resolve to distinct files")
    if not runtime_profile_paths_distinct:
        reasons.append("the six runtime profiles must resolve to distinct files")

    checks: dict[str, bool] = {
        "binding_count": len(observations) == len(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS),
        "coverage_complete": coverage_complete,
        "report_paths_distinct": report_paths_distinct,
        "runtime_profile_paths_distinct": runtime_profile_paths_distinct,
        "sources_hash_bound": sources_hash_bound,
        "all_observations_qualified": all_observations_qualified,
        "all_receive_only": all_receive_only,
        "all_missions_identity_bound": all_missions_identity_bound,
        "all_runtime_modes_match": all_runtime_modes_match,
        "all_airframes_match": all_airframes_match,
        "all_runtime_profiles_bound": all_runtime_profiles_bound,
        "all_vehicle_identities_bound": all_vehicle_identities_bound,
        "all_firmware_identities_bound": all_firmware_identities_bound,
        "all_endpoints_bound": all_endpoints_bound,
        "all_output_locked": all_output_locked,
        "px4_mission_path_bound": px4_path_bound,
        "no_errors": not reasons,
    }
    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("checks must be an object")
    else:
        unknown_checks = sorted(set(declared_checks).difference(_CHECK_KEYS))
        missing_checks = sorted(_CHECK_KEYS.difference(declared_checks))
        if unknown_checks or missing_checks:
            reasons.append(
                f"checks fields are invalid; missing={missing_checks!r}, unknown={unknown_checks!r}"
            )
        if any(value is not True for value in declared_checks.values()):
            reasons.append("declared checks must all be true")
        if dict(declared_checks) != checks:
            reasons.append("declared checks do not match recomputed checks")

    passed = not reasons and all(checks.values())
    return {
        "passed": passed,
        "reasons": reasons,
        "checks": checks,
        "observations": observations_result,
        "px4_mission_path_hash": normalized_px4_hash,
    }


__all__ = [
    "PX4_MISSION_FEEDBACK_BINDING_SCHEMA_VERSION",
    "PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS",
    "PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES",
    "PX4_MISSION_FEEDBACK_REQUIRED_VEHICLE_CLASSES",
    "Px4MissionFeedbackBindingError",
    "build_px4_mission_feedback_binding",
    "verify_px4_mission_feedback_binding",
]
