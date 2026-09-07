"""Bind the RuntimeProfile policy set to the six-source PX4 Mission review.

``runtime_profile_binding_set`` is a mode-level software preflight: it proves
that the four runtime policies are complete, signed where required and locked
for output.  ``px4_mission_feedback_binding`` is the airframe-level reviewer:
it reopens the six HIL/REAL Mission and feedback leaves.  This module joins
those two evidence layers without pretending that one generic mode profile is
the same file as every airframe-specific profile.

The join is intentionally read-only.  It reopens both reports, re-hashes every
referenced source and checks that each six-source runtime binding obeys the
mode policy (profile kind, signer key and locked output).  It does not start a
runtime, connect to PX4 or enable flight-control output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .px4_mission_feedback_binding import (
    PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS,
    PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES,
    PX4_MISSION_FEEDBACK_REQUIRED_VEHICLE_CLASSES,
    verify_px4_mission_feedback_binding,
)
from .runtime_profile_binding_set import ALLOWED_MODES

SCHEMA = "multidetect.px4-mission-feedback-profile-binding"
SCHEMA_VERSION = 1
EVENT = "px4_mission_feedback_profile_binding_verified"
ERROR_EVENT = "px4_mission_feedback_profile_binding_error"
_PROFILE_SET_SCHEMA = "multidetect.runtime-profile-binding-set"
_PROFILE_SET_EVENT = "runtime_profile_binding_set_verified"
_EXPECTED_PROFILE_IDS = {
    "replay": "replay",
    "sim": "sim",
    "hil": "hil",
    "real": "flight",
}
_MODE_ORDER = {mode: index for index, mode in enumerate(ALLOWED_MODES)}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHECK_NAMES = (
    "profile_set_document_valid",
    "profile_set_gate_passed",
    "profile_set_sources_bound",
    "profile_modes_complete",
    "feedback_binding_document_valid",
    "feedback_binding_gate_passed",
    "feedback_sources_bound",
    "feedback_coverage_complete",
    "mode_profile_policies_match",
    "runtime_profile_ids_match",
    "runtime_profile_keys_match",
    "all_outputs_locked",
    "all_leaf_bindings_match",
    "no_errors",
)
_PROFILE_CHECK_NAMES = (
    "document_shape",
    "mode_coverage",
    "entries_unique",
    "sources_hash_bound",
    "all_entries_passed",
    "policies_locked",
    "no_errors",
)
_LEAF_CHECK_NAMES = (
    "profile_id_matches",
    "key_id_matches",
    "signature_valid",
    "identity_valid",
    "profile_valid",
    "output_locked",
    "flight_control_disabled",
    "runtime_binding_passed",
    "receive_only",
    "feedback_gate_passed",
)
_CLAIM_BOUNDARY = {
    "software_profile_policy_to_px4_feedback_identity_only": True,
    "hardware_acceptance": "not_claimed",
    "hil_acceptance": "not_claimed",
    "real_acceptance": "not_claimed",
    "flight_acceptance": "not_claimed",
    "operator_visual_acceptance": "not_claimed",
}


class Px4MissionFeedbackProfileBindingError(ValueError):
    """Raised when the joined profile/feedback evidence is unsafe."""


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value.strip()) is not None


def _normalise_mode(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise Px4MissionFeedbackProfileBindingError(f"{label} must be a string")
    mode = value.strip().lower()
    if mode not in ALLOWED_MODES:
        raise Px4MissionFeedbackProfileBindingError(
            f"{label} must be one of {', '.join(ALLOWED_MODES)}"
        )
    return mode


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_source(value: object, *, label: str, base_directory: Path) -> tuple[Path, str]:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise Px4MissionFeedbackProfileBindingError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    try:
        if candidate.is_symlink():
            raise Px4MissionFeedbackProfileBindingError(f"{label} must not be a symbolic link")
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base_directory)
    except Px4MissionFeedbackProfileBindingError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise Px4MissionFeedbackProfileBindingError(
            f"{label} must resolve inside base directory {base_directory}"
        ) from exc
    if not resolved.is_file():
        raise Px4MissionFeedbackProfileBindingError(f"{label} does not exist: {resolved}")
    return resolved, resolved.relative_to(base_directory).as_posix()


def _source_info(path: Path, *, relative: str) -> dict[str, object]:
    payload = path.read_bytes()
    if not payload:
        raise Px4MissionFeedbackProfileBindingError(f"source file is empty: {relative}")
    return {"path": relative, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _load_json_input(
    value: object,
    *,
    label: str,
    base_directory: Path,
) -> tuple[Mapping[str, Any], dict[str, object] | None, Path | None]:
    if isinstance(value, Mapping):
        return dict(value), None, None
    path, relative = _resolve_source(value, label=label, base_directory=base_directory)
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Px4MissionFeedbackProfileBindingError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise Px4MissionFeedbackProfileBindingError(f"{label} must contain a JSON object")
    return dict(raw), _source_info(path, relative=relative), path.parent


def _empty_profile_summary(source: dict[str, object] | None) -> dict[str, Any]:
    return {
        "source": source,
        "required_modes": [],
        "covered_modes": [],
        "binding_count": 0,
        "complete": False,
        "gate_passed": False,
        "checks": {name: False for name in _PROFILE_CHECK_NAMES},
    }


def _empty_feedback_summary(source: dict[str, object] | None) -> dict[str, Any]:
    return {
        "source": source,
        "schema_version": None,
        "px4_mission_path_hash": None,
        "observation_count": 0,
        "gate_passed": False,
        "checks": {},
    }


def _empty_document(
    *,
    event: str,
    release_label: str | None = None,
    profile_source: dict[str, object] | None = None,
    feedback_source: dict[str, object] | None = None,
    reasons: Sequence[str] = (),
    error: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "release_label": release_label,
        "profile_set": _empty_profile_summary(profile_source),
        "feedback_binding": _empty_feedback_summary(feedback_source),
        "mode_policies": [],
        "leaf_bindings": [],
        "checks": {name: False for name in _CHECK_NAMES},
        "gate_passed": False,
        "reasons": list(reasons),
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }
    if error is not None:
        document["error"] = dict(error)
    return document


def _validate_profile_set(
    raw: Mapping[str, Any],
    *,
    base_directory: Path,
    source: dict[str, object] | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    """Reopen the v184 mode policy report and return mode policy rows."""

    summary = _empty_profile_summary(source)
    reasons: list[str] = []
    policy_by_mode: dict[str, dict[str, Any]] = {}
    checks = {name: False for name in _PROFILE_CHECK_NAMES}
    summary["checks"] = checks
    allowed_fields = {
        "schema",
        "schema_version",
        "event",
        "required_modes",
        "covered_modes",
        "binding_count",
        "complete",
        "policies",
        "bindings",
        "checks",
        "gate_passed",
        "reasons",
        "claim_boundary",
        "error",
    }
    unknown = sorted(set(raw).difference(allowed_fields))
    if unknown:
        reasons.append(f"profile set has unknown field(s): {unknown!r}")
    shape_ok = (
        raw.get("schema") == _PROFILE_SET_SCHEMA
        and raw.get("schema_version") == 1
        and raw.get("event") == _PROFILE_SET_EVENT
        and isinstance(raw.get("required_modes"), list)
        and isinstance(raw.get("covered_modes"), list)
        and isinstance(raw.get("bindings"), list)
        and isinstance(raw.get("policies"), Mapping)
        and isinstance(raw.get("checks"), Mapping)
        and isinstance(raw.get("reasons"), list)
        and isinstance(raw.get("claim_boundary"), Mapping)
    )
    checks["document_shape"] = shape_ok and not unknown
    if not shape_ok:
        reasons.append("profile set document shape/schema is invalid")
    required_modes = raw.get("required_modes")
    covered_modes = raw.get("covered_modes")
    expected_modes = list(ALLOWED_MODES)
    mode_coverage_ok = required_modes == expected_modes and covered_modes == expected_modes
    if not mode_coverage_ok:
        reasons.append("profile set must cover REPLAY, SIM, HIL and REAL in fixed order")
    checks["mode_coverage"] = mode_coverage_ok
    summary["required_modes"] = list(required_modes) if isinstance(required_modes, list) else []
    summary["covered_modes"] = list(covered_modes) if isinstance(covered_modes, list) else []
    summary["binding_count"] = raw.get("binding_count", 0)
    summary["complete"] = raw.get("complete") is True
    summary["gate_passed"] = raw.get("gate_passed") is True
    if raw.get("complete") is not True:
        reasons.append("profile set complete must be true")
    if raw.get("gate_passed") is not True:
        reasons.append("profile set gate_passed must be true")

    policies = raw.get("policies") if isinstance(raw.get("policies"), Mapping) else {}
    policy_arrays_ok = (
        policies.get("require_signature_modes") == ["hil", "real"]
        and policies.get("require_firmware_identity_modes") == ["hil", "real"]
        and policies.get("require_output_locked_modes") == expected_modes
    )
    if not policy_arrays_ok:
        reasons.append("profile set policies do not require signed HIL/REAL locked output")

    bindings = raw.get("bindings") if isinstance(raw.get("bindings"), list) else []
    summary["binding_count"] = len(bindings) if isinstance(bindings, list) else 0
    seen_modes: set[str] = set()
    profile_paths: set[str] = set()
    sources_hash_bound = True
    entries_passed = True
    for index, entry in enumerate(bindings):
        prefix = f"profile set binding {index}"
        if not isinstance(entry, Mapping):
            reasons.append(f"{prefix} must be an object")
            entries_passed = False
            sources_hash_bound = False
            continue
        mode_raw = entry.get("runtime_mode")
        try:
            mode = _normalise_mode(mode_raw, label=f"{prefix}.runtime_mode")
        except Px4MissionFeedbackProfileBindingError as exc:
            reasons.append(str(exc))
            entries_passed = False
            sources_hash_bound = False
            continue
        if mode in seen_modes:
            reasons.append(f"profile set repeats runtime mode {mode}")
            entries_passed = False
        seen_modes.add(mode)
        profile_source = entry.get("profile")
        result = entry.get("result")
        entry_checks = entry.get("checks")
        if not isinstance(profile_source, Mapping) or not isinstance(result, Mapping):
            reasons.append(f"{prefix} has no profile source/result")
            entries_passed = False
            sources_hash_bound = False
            continue
        raw_path = profile_source.get("path")
        try:
            profile_path, profile_relative = _resolve_source(
                raw_path,
                label=f"{prefix}.profile.path",
                base_directory=base_directory,
            )
            actual_source = _source_info(profile_path, relative=profile_relative)
        except (OSError, ValueError, TypeError, Px4MissionFeedbackProfileBindingError) as exc:
            reasons.append(f"{prefix} profile source: {exc}")
            entries_passed = False
            sources_hash_bound = False
            continue
        profile_paths.add(profile_relative)
        for field in ("bytes", "sha256"):
            if profile_source.get(field) != actual_source[field]:
                reasons.append(f"{prefix} profile {field} does not match source")
                sources_hash_bound = False
        profile_document_sha = result.get("document_sha256")
        if profile_document_sha != actual_source["sha256"]:
            reasons.append(f"{prefix} result document_sha256 does not match source")
            sources_hash_bound = False
        if result.get("path") != profile_relative:
            reasons.append(f"{prefix} result path does not match source")
            sources_hash_bound = False
        profile_value = result.get("profile")
        expected_profile_id = _EXPECTED_PROFILE_IDS.get(mode)
        if (
            not isinstance(profile_value, Mapping)
            or profile_value.get("profile_id") != expected_profile_id
        ):
            reasons.append(f"{prefix} profile kind does not match {mode}")
            entries_passed = False
        if (
            result.get("flight_control_enabled") is not False
            or result.get("output_locked") is not True
        ):
            reasons.append(f"{prefix} profile output is not locked")
            entries_passed = False
        expected_signature = mode in {"hil", "real"}
        if result.get("signature_valid") is not expected_signature:
            reasons.append(f"{prefix} signature policy result does not match {mode}")
            entries_passed = False
        if result.get("profile_valid") is not True or result.get("identity_valid") is not True:
            reasons.append(f"{prefix} profile/identity result is not valid")
            entries_passed = False
        try:
            document = json.loads(profile_path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            reasons.append(f"{prefix} profile JSON cannot be reopened: {exc}")
            entries_passed = False
            continue
        document_key_id = document.get("key_id") if isinstance(document, Mapping) else None
        if expected_signature:
            if not isinstance(document_key_id, str) or not document_key_id.strip():
                reasons.append(f"{prefix} signed profile key_id is missing")
                entries_passed = False
        elif document_key_id is not None:
            reasons.append(f"{prefix} unsigned profile must not declare key_id")
            entries_passed = False
        if entry.get("document_key_id") != document_key_id:
            reasons.append(f"{prefix} document_key_id does not match source")
            entries_passed = False
        expectations = entry.get("expectations")
        if not isinstance(expectations, Mapping):
            reasons.append(f"{prefix} expectations are missing")
            entries_passed = False
        else:
            if expectations.get("require_signature") is not expected_signature:
                reasons.append(f"{prefix} signature expectation does not match mode")
                entries_passed = False
            if expectations.get("require_output_locked") is not True:
                reasons.append(f"{prefix} output-lock expectation is false")
                entries_passed = False
            if expected_signature and expectations.get("expected_key_id") != document_key_id:
                reasons.append(f"{prefix} expected key_id does not match source")
                entries_passed = False
        if not isinstance(entry_checks, Mapping) or any(
            value is not True for value in entry_checks.values()
        ):
            reasons.append(f"{prefix} declared checks are not all true")
            entries_passed = False
        policy_by_mode[mode] = {
            "profile_id": expected_profile_id,
            "key_id": document_key_id,
            "signature_required": expected_signature,
            "signature_valid": result.get("signature_valid") is True,
            "profile_valid": result.get("profile_valid") is True,
            "identity_valid": result.get("identity_valid") is True,
            "output_locked": result.get("output_locked") is True,
            "flight_control_enabled": result.get("flight_control_enabled") is True,
            "document_sha256": actual_source["sha256"],
        }

    entries_unique = len(bindings) == len(ALLOWED_MODES) and seen_modes == set(ALLOWED_MODES)
    if not entries_unique:
        reasons.append("profile set must contain exactly one entry for each runtime mode")
    paths_distinct = len(profile_paths) == len(bindings) == len(ALLOWED_MODES)
    if not paths_distinct:
        reasons.append("profile set profile sources must be distinct")
    checks["entries_unique"] = entries_unique and paths_distinct
    checks["sources_hash_bound"] = sources_hash_bound
    checks["all_entries_passed"] = entries_passed and all(
        isinstance(entry, Mapping) and entry.get("gate_passed") is True for entry in bindings
    )
    checks["policies_locked"] = policy_arrays_ok and all(
        policy.get("output_locked") is True and policy.get("flight_control_enabled") is False
        for policy in policy_by_mode.values()
    )
    declared_checks = raw.get("checks")
    if not isinstance(declared_checks, Mapping) or any(
        value is not True for value in declared_checks.values()
    ):
        reasons.append("profile set declared checks are not all true")
        checks["all_entries_passed"] = False
    checks["no_errors"] = not reasons
    summary["checks"] = checks
    summary["gate_passed"] = all(checks.values()) and raw.get("gate_passed") is True
    summary["complete"] = summary["gate_passed"]
    if not summary["gate_passed"] and not reasons:
        reasons.append("profile set gate did not pass")
    return summary, policy_by_mode, reasons


def _feedback_paths_bound(
    raw: Mapping[str, Any],
    *,
    base_directory: Path,
) -> tuple[bool, list[str], dict[tuple[str, str], Mapping[str, Any]]]:
    reasons: list[str] = []
    observations = raw.get("observations")
    if not isinstance(observations, list):
        return False, ["feedback binding observations must be an array"], {}
    by_combination: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            reasons.append(f"feedback observation {index} must be an object")
            continue
        mode_raw = observation.get("runtime_mode")
        vehicle = observation.get("vehicle_class")
        mode = str(mode_raw).strip().lower() if isinstance(mode_raw, str) else ""
        vehicle_name = str(vehicle).strip().lower() if isinstance(vehicle, str) else ""
        combination = (mode, vehicle_name)
        if combination in by_combination:
            reasons.append(f"feedback binding repeats {mode}/{vehicle_name}")
        by_combination[combination] = observation
        for field in ("mission_path", "runtime_profile_path", "report_path"):
            try:
                _resolve_source(
                    observation.get(field),
                    label=f"feedback observation {index}.{field}",
                    base_directory=base_directory,
                )
            except (OSError, TypeError, ValueError, Px4MissionFeedbackProfileBindingError) as exc:
                reasons.append(str(exc))
                continue
    expected = set(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS)
    coverage = len(observations) == len(expected) and set(by_combination) == expected
    if not coverage:
        reasons.append("feedback binding does not cover HIL/REAL x fixed-wing/multirotor/VTOL")
    return not reasons, reasons, by_combination


def _read_report_runtime_binding(
    observation: Mapping[str, Any],
    *,
    base_directory: Path,
    label: str,
) -> Mapping[str, Any] | None:
    try:
        report_path, _ = _resolve_source(
            observation.get("report_path"),
            label=f"{label}.report_path",
            base_directory=base_directory,
        )
        raw = json.loads(report_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    runtime_binding = raw.get("runtime_binding")
    return runtime_binding if isinstance(runtime_binding, Mapping) else None


def _leaf_bindings(
    by_combination: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    policy_by_mode: Mapping[str, Mapping[str, Any]],
    base_directory: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    leaves: list[dict[str, Any]] = []
    reasons: list[str] = []
    for mode, vehicle_class in PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS:
        observation = by_combination.get((mode, vehicle_class))
        policy = policy_by_mode.get(mode, {})
        checks = {name: False for name in _LEAF_CHECK_NAMES}
        leaf_reasons: list[str] = []
        profile_id = None
        key_id = None
        report_runtime_binding = None
        if observation is None:
            leaf_reasons.append("observation is missing")
        else:
            profile_id_value = observation.get("runtime_profile_id")
            profile_id = profile_id_value if isinstance(profile_id_value, str) else None
            key_value = observation.get("runtime_profile_key_id")
            key_id = key_value if isinstance(key_value, str) else None
            checks["profile_id_matches"] = profile_id == policy.get("profile_id")
            checks["key_id_matches"] = key_id == policy.get("key_id")
            if not checks["profile_id_matches"]:
                leaf_reasons.append("runtime_profile_id does not match mode policy")
            if not checks["key_id_matches"]:
                leaf_reasons.append("runtime_profile_key_id does not match mode policy")
            report_runtime_binding = _read_report_runtime_binding(
                observation,
                base_directory=base_directory,
                label=f"{mode}/{vehicle_class}",
            )
            if report_runtime_binding is None:
                leaf_reasons.append("feedback report runtime_binding cannot be reopened")
            else:
                report_profile = report_runtime_binding.get("profile")
                checks["signature_valid"] = report_runtime_binding.get("signature_valid") is True
                checks["identity_valid"] = report_runtime_binding.get("identity_valid") is True
                checks["profile_valid"] = report_runtime_binding.get("profile_valid") is True
                checks["output_locked"] = report_runtime_binding.get("output_locked") is True
                checks["flight_control_disabled"] = (
                    report_runtime_binding.get("flight_control_enabled") is False
                )
                checks["runtime_binding_passed"] = report_runtime_binding.get("gate_passed") is True
                checks["receive_only"] = (
                    observation.get("read_only") is True
                    and observation.get("messages_transmitted") == 0
                )
                checks["feedback_gate_passed"] = observation.get("feedback_gate_passed") is True
                if not isinstance(report_profile, Mapping) or report_profile.get(
                    "profile_id"
                ) != policy.get("profile_id"):
                    leaf_reasons.append("feedback runtime profile kind does not match mode policy")
                if report_runtime_binding.get("key_id") != policy.get("key_id"):
                    leaf_reasons.append(
                        "feedback runtime binding key_id does not match mode policy"
                    )
                if not checks["signature_valid"]:
                    leaf_reasons.append("feedback runtime profile signature is not valid")
                if not checks["identity_valid"] or not checks["profile_valid"]:
                    leaf_reasons.append("feedback runtime profile identity/profile is not valid")
                if not checks["output_locked"] or not checks["flight_control_disabled"]:
                    leaf_reasons.append("feedback runtime output is not locked")
                if not checks["runtime_binding_passed"]:
                    leaf_reasons.append("feedback runtime binding gate is not qualified")
                if not checks["receive_only"]:
                    leaf_reasons.append("feedback observation is not receive-only")
                if not checks["feedback_gate_passed"]:
                    leaf_reasons.append("feedback observation gate is not qualified")
        gate_passed = all(checks.values())
        if not gate_passed:
            reasons.extend(f"{mode}/{vehicle_class}: {reason}" for reason in leaf_reasons)
        leaves.append(
            {
                "runtime_mode": mode,
                "vehicle_class": vehicle_class,
                "runtime_profile_id": profile_id,
                "runtime_profile_key_id": key_id,
                "checks": checks,
                "gate_passed": gate_passed,
                "reasons": leaf_reasons,
            }
        )
    return leaves, reasons


def verify_px4_mission_feedback_profile_binding(
    profile_set: Mapping[str, Any] | str | Path,
    feedback_binding: Mapping[str, Any] | str | Path,
    *,
    base_directory: str | Path = ".",
    expected_release_label: str | None = None,
    expected_px4_mission_path_hash: str | None = None,
) -> dict[str, Any]:
    """Reopen and bind a RuntimeProfile policy set to PX4's six feedback leaves."""

    root = Path(base_directory).expanduser().resolve()
    profile_source: dict[str, object] | None = None
    feedback_source: dict[str, object] | None = None
    try:
        profile_raw, profile_source, profile_root = _load_json_input(
            profile_set,
            label="profile_set",
            base_directory=root,
        )
        feedback_raw, feedback_source, feedback_root = _load_json_input(
            feedback_binding,
            label="feedback_binding",
            base_directory=root,
        )
    except (OSError, TypeError, ValueError, Px4MissionFeedbackProfileBindingError) as exc:
        message = str(exc) or type(exc).__name__
        return _empty_document(
            event=ERROR_EVENT,
            release_label=expected_release_label,
            profile_source=profile_source,
            feedback_source=feedback_source,
            reasons=[f"{type(exc).__name__}: {message}"],
            error={"type": type(exc).__name__, "message": message},
        )

    profile_summary, policy_by_mode, profile_reasons = _validate_profile_set(
        profile_raw,
        base_directory=profile_root or root,
        source=profile_source,
    )
    feedback_paths_ok, feedback_path_reasons, by_combination = _feedback_paths_bound(
        feedback_raw,
        base_directory=feedback_root or root,
    )
    feedback_verification = verify_px4_mission_feedback_binding(
        feedback_raw,
        manifest_directory=feedback_root or root,
        expected_release_label=expected_release_label,
        expected_px4_mission_path_hash=expected_px4_mission_path_hash,
    )
    observations = feedback_raw.get("observations")
    observation_count = len(observations) if isinstance(observations, list) else 0
    feedback_summary = {
        "source": feedback_source,
        "schema_version": feedback_raw.get("schema_version"),
        "px4_mission_path_hash": feedback_raw.get("px4_mission_path_hash"),
        "observation_count": observation_count,
        "gate_passed": feedback_verification.get("passed") is True,
        "checks": dict(feedback_verification.get("checks", {})),
    }
    leaves, leaf_reasons = _leaf_bindings(
        by_combination,
        policy_by_mode=policy_by_mode,
        base_directory=feedback_root or root,
    )
    feedback_coverage_complete = observation_count == len(
        PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS
    ) and set(by_combination) == set(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS)
    mode_profile_policies_match = (
        profile_summary.get("gate_passed") is True
        and list(feedback_raw.get("required_runtime_modes", []))
        == list(PX4_MISSION_FEEDBACK_REQUIRED_RUNTIME_MODES)
        and list(feedback_raw.get("required_vehicle_classes", []))
        == list(PX4_MISSION_FEEDBACK_REQUIRED_VEHICLE_CLASSES)
        and set(policy_by_mode) == set(ALLOWED_MODES)
        and all(
            policy.get("output_locked") is True and policy.get("flight_control_enabled") is False
            for policy in policy_by_mode.values()
        )
    )
    runtime_profile_ids_match = bool(leaves) and all(
        leaf["checks"]["profile_id_matches"] for leaf in leaves
    )
    runtime_profile_keys_match = bool(leaves) and all(
        leaf["checks"]["key_id_matches"] for leaf in leaves
    )
    all_outputs_locked = bool(leaves) and all(
        leaf["checks"]["output_locked"] and leaf["checks"]["flight_control_disabled"]
        for leaf in leaves
    )
    all_leaf_bindings_match = bool(leaves) and all(leaf["gate_passed"] for leaf in leaves)
    release_label = expected_release_label
    if release_label is None and isinstance(feedback_raw.get("release_label"), str):
        release_label = feedback_raw["release_label"]
    checks = {
        "profile_set_document_valid": profile_summary["checks"]["document_shape"],
        "profile_set_gate_passed": profile_summary.get("gate_passed") is True,
        "profile_set_sources_bound": profile_summary["checks"]["sources_hash_bound"],
        "profile_modes_complete": profile_summary["checks"]["mode_coverage"],
        "feedback_binding_document_valid": isinstance(feedback_raw, Mapping)
        and feedback_raw.get("schema_version") == 2,
        "feedback_binding_gate_passed": feedback_verification.get("passed") is True,
        "feedback_sources_bound": feedback_paths_ok,
        "feedback_coverage_complete": feedback_coverage_complete,
        "mode_profile_policies_match": mode_profile_policies_match,
        "runtime_profile_ids_match": runtime_profile_ids_match,
        "runtime_profile_keys_match": runtime_profile_keys_match,
        "all_outputs_locked": all_outputs_locked,
        "all_leaf_bindings_match": all_leaf_bindings_match,
        "no_errors": False,
    }
    reasons = [*profile_reasons, *feedback_path_reasons]
    reasons.extend(
        f"feedback binding: {reason}" for reason in feedback_verification.get("reasons", [])
    )
    reasons.extend(leaf_reasons)
    checks["no_errors"] = not reasons and all(
        value is True for name, value in checks.items() if name != "no_errors"
    )
    document = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "release_label": release_label,
        "profile_set": profile_summary,
        "feedback_binding": feedback_summary,
        "mode_policies": [
            {
                "runtime_mode": mode,
                **policy_by_mode[mode],
            }
            for mode in ALLOWED_MODES
            if mode in policy_by_mode
        ],
        "leaf_bindings": leaves,
        "checks": checks,
        "gate_passed": all(checks.values()),
        "reasons": reasons if reasons else [name for name in _CHECK_NAMES if not checks[name]],
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }
    return document


def build_px4_mission_feedback_profile_binding(
    profile_set: Mapping[str, Any] | str | Path,
    feedback_binding: Mapping[str, Any] | str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build alias kept for callers that name evidence producers ``build``."""

    return verify_px4_mission_feedback_profile_binding(profile_set, feedback_binding, **kwargs)


def _write_json(path: Path, document: Mapping[str, Any]) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-set", type=Path, required=True)
    parser.add_argument("--feedback-binding", type=Path, required=True)
    parser.add_argument(
        "--base-directory",
        type=Path,
        default=Path.cwd(),
        help="root under which both reports and every referenced source must resolve",
    )
    parser.add_argument("--expected-release-label")
    parser.add_argument("--expected-px4-mission-path-hash")
    parser.add_argument("--out", type=Path, help="optional JSON verification report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    document = verify_px4_mission_feedback_profile_binding(
        args.profile_set,
        args.feedback_binding,
        base_directory=args.base_directory,
        expected_release_label=args.expected_release_label,
        expected_px4_mission_path_hash=args.expected_px4_mission_path_hash,
    )
    if args.out is not None:
        _write_json(args.out, document)
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if document["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ERROR_EVENT",
    "EVENT",
    "Px4MissionFeedbackProfileBindingError",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_parser",
    "build_px4_mission_feedback_profile_binding",
    "main",
    "verify_px4_mission_feedback_profile_binding",
]
