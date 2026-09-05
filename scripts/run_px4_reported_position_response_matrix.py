#!/usr/bin/env python3
"""Build a source-bound three-airframe PX4 reported-position response matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "multidetect.px4-reported-position-response-matrix"
SCHEMA_VERSION = 1
REQUIRED_PARITY_FIELDS = (
    "ui_contract_hash",
    "core_algorithm_hash",
    "task_contract_hash",
    "safety_policy_hash",
    "px4_mission_path_hash",
)
EXPECTED_AIRFRAMES = {
    "fixed-wing": {
        "scenario_id": "coastal_patrol_fixed_wing_001",
        "scenario_path": "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json",
        "airframe_profile_id": "px4_fixed_wing_gazebo_v1",
        "vehicle_model": "px4_fixed_wing_standard",
        "model_name": "rc_cessna_mono_cam",
        "px4_sys_autostart": "4003",
    },
    "multirotor": {
        "scenario_id": "terrain_ranging_validation_001",
        "scenario_path": (
            "configs/scenarios/validation/terrain_ranging_validation_001.scenario.json"
        ),
        "airframe_profile_id": "px4_multirotor_gazebo_v1",
        "vehicle_model": "px4_multirotor_standard",
        "model_name": "x500_mono_cam",
        "px4_sys_autostart": "4001",
    },
    "vtol": {
        "scenario_id": "river_rescue_vtol_001",
        "scenario_path": "configs/scenarios/river_rescue_vtol_001.scenario.json",
        "airframe_profile_id": "px4_vtol_gazebo_v1",
        "vehicle_model": "px4_vtol_standard",
        "model_name": "standard_vtol_mono_cam",
        "px4_sys_autostart": "4004",
    },
}
EXPECTED_CONTROL_PATH = (
    "SimulatorTelemetryBridge.ingest_perception -> SimulatorAiExecutor -> "
    "SimulatorMavlinkControlAdapter -> MAVLink MANUAL_CONTROL -> PX4 SITL/Gazebo"
)
SOURCE_TOOL_PATHS = {
    "acceptance_runner": "scripts/run_simulator_docker_ai_acceptance.py",
    "matrix_builder": "scripts/run_px4_reported_position_response_matrix.py",
    "matrix_verifier": "scripts/verify_px4_reported_position_response_matrix.py",
}


class Px4ResponseMatrixError(ValueError):
    """Raised when an input cannot be safely represented as matrix evidence."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _relative_file(path: Path, root: Path, *, label: str) -> str:
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise Px4ResponseMatrixError(f"{label} must stay within the repository") from exc
    if not resolved.is_file():
        raise Px4ResponseMatrixError(f"{label} is unavailable: {resolved}")
    return relative.as_posix()


def _load_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Px4ResponseMatrixError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Px4ResponseMatrixError(f"{label} must be a JSON object")
    return value, raw


def _parity_identity(path: Path, root: Path) -> dict[str, object]:
    relative = _relative_file(path, root, label="release parity")
    document, raw = _load_object(path, label="release parity")
    parity = _mapping(document.get("parity"))
    if (
        document.get("schema_version") != 1
        or document.get("product") != "Multi-Detect"
        or not isinstance(document.get("release_label"), str)
        or set(parity) != set(REQUIRED_PARITY_FIELDS)
        or not all(_is_digest(parity.get(field)) for field in REQUIRED_PARITY_FIELDS)
    ):
        raise Px4ResponseMatrixError("release parity identity is incomplete or invalid")
    return {
        "path": relative,
        "file_sha256": _sha256(raw),
        "release_label": document["release_label"],
        "parity": {field: parity[field] for field in REQUIRED_PARITY_FIELDS},
    }


def _source_tool_identities(root: Path) -> dict[str, dict[str, object]]:
    identities: dict[str, dict[str, object]] = {}
    for name, relative_path in SOURCE_TOOL_PATHS.items():
        path = root / relative_path
        relative = _relative_file(path, root, label=f"{name} source tool")
        raw = path.read_bytes()
        identities[name] = {
            "path": relative,
            "bytes": len(raw),
            "sha256": _sha256(raw),
        }
    return identities


def _source_entry(path: Path, root: Path) -> tuple[str | None, dict[str, object], list[str]]:
    relative = _relative_file(path, root, label="response source")
    document, raw = _load_object(path, label="response source")
    scenario = _mapping(document.get("scenario"))
    plan = _mapping(document.get("plan"))
    vehicle = _mapping(plan.get("vehicle"))
    environment = _mapping(plan.get("environment"))
    flight = _mapping(document.get("flight_exercise"))
    mode = _mapping(flight.get("flight_mode"))
    runtime = _mapping(document.get("container_runtime"))
    cleanup = _mapping(runtime.get("cleanup"))
    inspect_checks = _mapping(runtime.get("inspect_checks"))
    hardware = _mapping(document.get("hardware"))
    claims = _mapping(document.get("claims"))
    safety = _mapping(document.get("safety"))

    profile = scenario.get("airframe_profile_id")
    vehicle_class = next(
        (
            name
            for name, expected in EXPECTED_AIRFRAMES.items()
            if expected["airframe_profile_id"] == profile
        ),
        None,
    )
    expected = EXPECTED_AIRFRAMES.get(vehicle_class or "", {})
    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    require(document.get("schema") == "multidetect.simulator-docker-acceptance", "schema")
    require(document.get("schema_version") == 1, "schema_version")
    require(vehicle_class is not None, "airframe_profile_id")
    require(document.get("execution_requested") is True, "execution_requested")
    for field in (
        "gate_passed",
        "runtime_gate_passed",
        "ai_policy_gate_passed",
        "target_policy_gate_passed",
        "flight_response_gate_passed",
    ):
        require(document.get(field) is True, field)
    require(document.get("flight_response_gate_status") == "passed", "flight_response_status")
    require(scenario.get("scenario_id") == expected.get("scenario_id"), "scenario_id")
    require(vehicle.get("model") == expected.get("vehicle_model"), "vehicle_model")
    require(vehicle.get("model_name") == expected.get("model_name"), "model_name")
    require(environment.get("PX4_SYS_AUTOSTART") == expected.get("px4_sys_autostart"), "autostart")

    expected_scenario = root / str(expected.get("scenario_path", "missing"))
    scenario_bytes = expected_scenario.read_bytes() if expected_scenario.is_file() else b""
    require(bool(scenario_bytes), "scenario_manifest_available")
    require(scenario.get("manifest_sha256") == _sha256(scenario_bytes), "scenario_manifest_sha256")
    require(scenario.get("truth_loaded_by_runtime") is False, "truth_not_loaded")
    require(scenario.get("expectations_loaded_by_runtime") is False, "expectations_not_loaded")

    require(runtime.get("status") == "passed", "container_runtime")
    require(runtime.get("container_running_observed") is True, "container_running_observed")
    require(cleanup.get("attempted") is True, "cleanup_attempted")
    require(cleanup.get("removed") is True, "container_removed")
    require(cleanup.get("errors") == [], "cleanup_errors")
    require(
        bool(inspect_checks) and all(value is True for value in inspect_checks.values()),
        "inspect_checks",
    )
    image = runtime.get("image")
    image_id = runtime.get("image_id")
    require(
        isinstance(image, str) and "@sha256:" in image and _is_digest(image.rsplit(":", 1)[-1]),
        "pinned_image",
    )
    require(
        isinstance(image_id, str) and image_id.startswith("sha256:") and _is_digest(image_id[7:]),
        "image_id",
    )

    require(flight.get("requested") is True, "flight_requested")
    require(flight.get("gate_passed") is True, "flight_gate")
    require(flight.get("status") == "passed", "flight_status")
    require(flight.get("completed") is True, "flight_completed")
    require(flight.get("position_source") == "MAVLink GLOBAL_POSITION_INT", "position_source")
    require(flight.get("control_path") == EXPECTED_CONTROL_PATH, "shared_control_path")
    require(flight.get("target_refresh_gate_passed") is True, "target_refresh_gate")
    require(flight.get("heartbeat_continuity_gate_passed") is True, "heartbeat_gate")
    require(flight.get("flight_mode_gate_passed") is True, "flight_mode_gate")
    require(mode.get("observed") is True and mode.get("loss_events") == 0, "flight_mode_clean")
    require(flight.get("gazebo_truth_consumed") is False, "gazebo_truth_not_consumed")
    require(flight.get("gazebo_pose_injection_used") is False, "gazebo_pose_not_injected")
    require(flight.get("synthetic_perception_input") is True, "truth_free_perception")
    require(
        isinstance(flight.get("target_updates_accepted"), int)
        and int(flight["target_updates_accepted"]) > 0
        and flight.get("target_updates_rejected") == 0,
        "target_updates",
    )
    displacement = flight.get("maximum_horizontal_displacement_m")
    minimum = flight.get("minimum_horizontal_displacement_m")
    require(
        isinstance(displacement, (int, float))
        and isinstance(minimum, (int, float))
        and math.isfinite(float(displacement))
        and float(displacement) >= float(minimum) > 0.0,
        "reported_position_displacement",
    )
    require(hardware.get("hardware_control_enabled") is False, "hardware_control_disabled")
    require(hardware.get("real_pixhawk_contacted") is False, "real_pixhawk_not_contacted")
    require(claims.get("px4_sim_vehicle_motion") == "passed", "sim_vehicle_motion_claim")
    require(claims.get("hardware") == "not_claimed", "hardware_not_claimed")
    require(safety.get("hardware_control_enabled") is False, "safety_hardware_disabled")

    entry: dict[str, object] = {
        "vehicle_class": vehicle_class,
        "source_path": relative,
        "source_bytes": len(raw),
        "source_sha256": _sha256(raw),
        "scenario_id": scenario.get("scenario_id"),
        "scenario_manifest_path": expected.get("scenario_path"),
        "scenario_manifest_sha256": _sha256(scenario_bytes) if scenario_bytes else None,
        "scenario_contract_sha256": scenario.get("scenario_manifest_hash"),
        "airframe_profile_id": profile,
        "vehicle_model": vehicle.get("model"),
        "model_name": vehicle.get("model_name"),
        "px4_sys_autostart": environment.get("PX4_SYS_AUTOSTART"),
        "image": image,
        "image_id": image_id,
        "flight_mode": mode.get("px4_main_mode"),
        "elapsed_duration_s": flight.get("elapsed_duration_s"),
        "target_updates_accepted": flight.get("target_updates_accepted"),
        "target_updates_rejected": flight.get("target_updates_rejected"),
        "maximum_target_refresh_gap_s": flight.get("maximum_target_refresh_gap_s"),
        "maximum_heartbeat_gap_s": flight.get("maximum_heartbeat_gap_s"),
        "position_sample_count": flight.get("position_sample_count"),
        "maximum_horizontal_displacement_m": displacement,
        "minimum_horizontal_displacement_m": minimum,
        "control_path": flight.get("control_path"),
        "checks_passed": not reasons,
        "reasons": reasons,
    }
    return vehicle_class, entry, reasons


def build_matrix(
    sources: list[str | Path],
    *,
    repository_root: str | Path = ROOT,
    release_parity_path: str | Path | None = None,
) -> dict[str, object]:
    root = Path(repository_root).expanduser().resolve()
    if len(sources) != 3:
        raise Px4ResponseMatrixError("exactly three airframe response sources are required")
    parity_path = (
        Path(release_parity_path).expanduser().resolve()
        if release_parity_path is not None
        else root / "configs" / "release-parity.json"
    )
    parity = _parity_identity(parity_path, root)
    source_tools = _source_tool_identities(root)
    entries: list[dict[str, object]] = []
    reasons: list[str] = []
    seen: list[str] = []
    for source in sources:
        vehicle_class, entry, source_reasons = _source_entry(Path(source), root)
        entries.append(entry)
        if vehicle_class is not None:
            seen.append(vehicle_class)
        reasons.extend(f"{vehicle_class or 'unknown'}: {reason}" for reason in source_reasons)
    entries.sort(key=lambda item: str(item.get("vehicle_class")))
    if sorted(seen) != sorted(EXPECTED_AIRFRAMES):
        reasons.append("vehicle classes must contain exactly fixed-wing, multirotor and vtol")
    image_ids = {entry.get("image_id") for entry in entries}
    control_paths = {entry.get("control_path") for entry in entries}
    shared_checks = {
        "vehicle_classes_complete": sorted(seen) == sorted(EXPECTED_AIRFRAMES),
        "unique_vehicle_classes": len(seen) == len(set(seen)) == 3,
        "single_pinned_px4_image": len(image_ids) == 1 and None not in image_ids,
        "shared_control_path": control_paths == {EXPECTED_CONTROL_PATH},
        "all_sources_passed": all(entry.get("checks_passed") is True for entry in entries),
        "all_truth_free": all(
            "gazebo_truth_not_consumed" not in entry.get("reasons", [])
            and "gazebo_pose_not_injected" not in entry.get("reasons", [])
            and "truth_free_perception" not in entry.get("reasons", [])
            for entry in entries
        ),
        "all_hardware_not_claimed": all(
            "hardware_control_disabled" not in entry.get("reasons", [])
            and "real_pixhawk_not_contacted" not in entry.get("reasons", [])
            and "hardware_not_claimed" not in entry.get("reasons", [])
            for entry in entries
        ),
    }
    reasons.extend(name for name, passed in shared_checks.items() if not passed)
    stable = {
        "release_identity": parity,
        "source_tools": source_tools,
        "entries": entries,
        "shared_checks": shared_checks,
    }
    gate_passed = not reasons and all(shared_checks.values())
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "product": "Multi-Detect",
        "release_label": parity["release_label"],
        **stable,
        "truth_free": True,
        "hardware_acceptance": "not_claimed",
        "hil_acceptance": "not_claimed",
        "real_acceptance": "not_claimed",
        "gate_passed": gate_passed,
        "reasons": reasons,
        "matrix_sha256": _canonical_sha256(stable),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--release-parity", type=Path)
    args = parser.parse_args(argv)
    try:
        document = build_matrix(
            args.source,
            repository_root=args.repository_root,
            release_parity_path=args.release_parity,
        )
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"PX4 response matrix failed: {exc}")
        return 2
    print(json.dumps({"gate_passed": document["gate_passed"], "output": str(output)}))
    return 0 if document["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
