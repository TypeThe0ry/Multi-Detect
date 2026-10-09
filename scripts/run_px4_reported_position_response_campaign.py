#!/usr/bin/env python3
"""Build a source-bound PX4 response repeatability and fault campaign."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

if __package__ in {None, ""}:
    _repository_root = Path(__file__).resolve().parents[1]
    if str(_repository_root) not in sys.path:
        sys.path.insert(0, str(_repository_root))

from scripts.run_px4_reported_position_response_matrix import (
    EXPECTED_AIRFRAMES,
    EXPECTED_CONTROL_PATH,
    ROOT,
    Px4ResponseMatrixError,
    _canonical_sha256,
    _load_object,
    _mapping,
    _parity_identity,
    _relative_file,
    _sha256,
    _source_entry,
)

SCHEMA = "multidetect.px4-reported-position-response-campaign"
SCHEMA_VERSION = 1
MINIMUM_POSITIVE_REPEATS = 2
EXPECTED_FAULT_STATUS = {
    "target_refresh_dropout": "target_refresh_not_sustained",
    "heartbeat_dropout": "heartbeat_continuity_failed",
    "flight_mode_loss": "flight_mode_loss_observed",
}
SOURCE_TOOL_PATHS = {
    "acceptance_runner": "scripts/run_simulator_docker_ai_acceptance.py",
    "campaign_builder": "scripts/run_px4_reported_position_response_campaign.py",
    "campaign_verifier": "scripts/verify_px4_reported_position_response_campaign.py",
}


def _source_tools(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name, relative in SOURCE_TOOL_PATHS.items():
        path = root / relative
        canonical = _relative_file(path, root, label=f"{name} source tool")
        raw = path.read_bytes()
        result[name] = {"path": canonical, "bytes": len(raw), "sha256": _sha256(raw)}
    return result


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise Px4ResponseMatrixError("cannot calculate a percentile from no values")
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 6)


def _positive_entry(path: Path, root: Path) -> tuple[str | None, dict[str, object], list[str]]:
    vehicle_class, base, reasons = _source_entry(path, root)
    document, _ = _load_object(path, label="positive response source")
    flight = _mapping(document.get("flight_exercise"))
    fault = _mapping(flight.get("fault_injection"))
    if fault and (fault.get("requested") is not False or fault.get("name") != "none"):
        reasons.append("positive_source_has_fault_injection")
    accepted = flight.get("target_updates_accepted")
    elapsed = flight.get("elapsed_duration_s")
    base["target_update_rate_hz"] = (
        round(float(accepted) / float(elapsed), 6)
        if isinstance(accepted, int) and isinstance(elapsed, (int, float)) and float(elapsed) > 0.0
        else 0.0
    )
    base["position_source"] = flight.get("position_source")
    base["validation_passed"] = not reasons
    base["validation_reasons"] = list(reasons)
    return vehicle_class, base, reasons


def _negative_entry(
    path: Path, root: Path
) -> tuple[str | None, str | None, dict[str, object], list[str]]:
    relative = _relative_file(path, root, label="fault response source")
    document, raw = _load_object(path, label="fault response source")
    scenario = _mapping(document.get("scenario"))
    plan = _mapping(document.get("plan"))
    vehicle = _mapping(plan.get("vehicle"))
    environment = _mapping(plan.get("environment"))
    runtime = _mapping(document.get("container_runtime"))
    cleanup = _mapping(runtime.get("cleanup"))
    flight = _mapping(document.get("flight_exercise"))
    fault = _mapping(flight.get("fault_injection"))
    hardware = _mapping(document.get("hardware"))
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
    fault_name = fault.get("name") if isinstance(fault.get("name"), str) else None
    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    require(document.get("schema") == "multidetect.simulator-docker-acceptance", "schema")
    require(document.get("schema_version") == 1, "schema_version")
    require(document.get("execution_requested") is True, "execution_requested")
    require(document.get("gate_passed") is False, "aggregate_gate_must_reject_fault")
    require(document.get("runtime_gate_passed") is True, "runtime_gate")
    require(vehicle_class is not None, "airframe_profile_id")
    require(scenario.get("scenario_id") == expected.get("scenario_id"), "scenario_id")
    require(vehicle.get("model") == expected.get("vehicle_model"), "vehicle_model")
    require(vehicle.get("model_name") == expected.get("model_name"), "model_name")
    require(environment.get("PX4_SYS_AUTOSTART") == expected.get("px4_sys_autostart"), "autostart")
    expected_scenario = root / str(expected.get("scenario_path", "missing"))
    scenario_bytes = expected_scenario.read_bytes() if expected_scenario.is_file() else b""
    require(bool(scenario_bytes), "scenario_manifest_available")
    require(
        scenario.get("manifest_sha256") == _sha256(scenario_bytes),
        "scenario_manifest_sha256",
    )
    require(scenario.get("truth_loaded_by_runtime") is False, "truth_not_loaded")
    require(
        scenario.get("expectations_loaded_by_runtime") is False,
        "expectations_not_loaded",
    )
    require(runtime.get("status") == "passed", "container_runtime")
    require(cleanup.get("removed") is True and cleanup.get("errors") == [], "cleanup")
    require(flight.get("requested") is True, "flight_requested")
    require(flight.get("gate_passed") is False, "flight_gate_must_reject_fault")
    require(flight.get("control_path") == EXPECTED_CONTROL_PATH, "shared_control_path")
    require(flight.get("position_source") == "MAVLink GLOBAL_POSITION_INT", "position_source")
    require(flight.get("gazebo_truth_consumed") is False, "gazebo_truth_not_consumed")
    require(flight.get("gazebo_pose_injection_used") is False, "pose_not_injected")
    require(fault_name in EXPECTED_FAULT_STATUS, "fault_name")
    require(fault.get("requested") is True, "fault_requested")
    require(fault.get("activated") is True, "fault_activated")
    require(fault.get("observed") is True, "fault_observed")
    require(fault.get("fail_closed") is True, "fault_fail_closed")
    require(fault.get("scope") == "disposable_loopback_px4_sitl_only", "fault_scope")
    require(flight.get("status") == EXPECTED_FAULT_STATUS.get(fault_name or ""), "fault_status")
    require(hardware.get("hardware_control_enabled") is False, "hardware_disabled")
    require(hardware.get("real_pixhawk_contacted") is False, "real_pixhawk_not_contacted")
    require(safety.get("hardware_control_enabled") is False, "safety_hardware_disabled")
    return (
        vehicle_class,
        fault_name,
        {
            "vehicle_class": vehicle_class,
            "fault": fault_name,
            "source_path": relative,
            "source_bytes": len(raw),
            "source_sha256": _sha256(raw),
            "scenario_id": scenario.get("scenario_id"),
            "status": flight.get("status"),
            "fault_injection": fault,
            "control_path": flight.get("control_path"),
            "position_source": flight.get("position_source"),
            "source_gate_passed": document.get("gate_passed"),
            "validation_passed": not reasons,
            "validation_reasons": reasons,
        },
        reasons,
    )


def build_campaign(
    positive_sources: Iterable[str | Path],
    fault_sources: Iterable[str | Path],
    *,
    repository_root: str | Path = ROOT,
    release_parity_path: str | Path | None = None,
) -> dict[str, object]:
    root = Path(repository_root).expanduser().resolve()
    parity_path = (
        root / "configs/release-parity.json"
        if release_parity_path is None
        else Path(release_parity_path).expanduser().resolve()
    )
    release_identity = _parity_identity(parity_path, root)
    positive_rows: list[dict[str, object]] = []
    fault_rows: list[dict[str, object]] = []
    reasons: list[str] = []
    positives_by_class: dict[str, list[dict[str, object]]] = defaultdict(list)
    faults_by_class: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)

    for source in positive_sources:
        vehicle_class, row, row_reasons = _positive_entry(Path(source), root)
        positive_rows.append(row)
        if vehicle_class is not None:
            positives_by_class[vehicle_class].append(row)
        reasons.extend(f"positive:{row.get('source_path')}:{reason}" for reason in row_reasons)
    for source in fault_sources:
        vehicle_class, fault_name, row, row_reasons = _negative_entry(Path(source), root)
        fault_rows.append(row)
        if vehicle_class is not None and fault_name is not None:
            if fault_name in faults_by_class[vehicle_class]:
                reasons.append(f"duplicate_fault:{vehicle_class}:{fault_name}")
            faults_by_class[vehicle_class][fault_name] = row
        reasons.extend(f"fault:{row.get('source_path')}:{reason}" for reason in row_reasons)

    expected_classes = set(EXPECTED_AIRFRAMES)
    for vehicle_class in sorted(expected_classes):
        rows = positives_by_class.get(vehicle_class, [])
        if len(rows) < MINIMUM_POSITIVE_REPEATS:
            reasons.append(f"positive_repeat_count:{vehicle_class}")
        if len({row.get("source_sha256") for row in rows}) != len(rows):
            reasons.append(f"duplicate_positive_source:{vehicle_class}")
        missing_faults = set(EXPECTED_FAULT_STATUS) - set(faults_by_class.get(vehicle_class, {}))
        if missing_faults:
            reasons.append(f"fault_coverage:{vehicle_class}:{sorted(missing_faults)!r}")
    if set(positives_by_class) - expected_classes:
        reasons.append("unexpected_positive_vehicle_class")
    if set(faults_by_class) - expected_classes:
        reasons.append("unexpected_fault_vehicle_class")

    statistics: dict[str, dict[str, object]] = {}
    for vehicle_class in sorted(expected_classes):
        rows = positives_by_class.get(vehicle_class, [])
        if not rows:
            continue
        metrics = {
            "maximum_horizontal_displacement_m": [
                float(row["maximum_horizontal_displacement_m"]) for row in rows
            ],
            "elapsed_duration_s": [float(row["elapsed_duration_s"]) for row in rows],
            "position_sample_count": [float(row["position_sample_count"]) for row in rows],
            "target_update_rate_hz": [float(row["target_update_rate_hz"]) for row in rows],
        }
        statistics[vehicle_class] = {
            "repeat_count": len(rows),
            "all_positive_runs_passed": all(row.get("validation_passed") is True for row in rows),
            "metrics": {
                name: {
                    "minimum": round(min(values), 6),
                    "p50": _percentile(values, 0.50),
                    "p95": _percentile(values, 0.95),
                    "maximum": round(max(values), 6),
                }
                for name, values in metrics.items()
            },
        }

    shared_checks = {
        "vehicle_classes_complete": set(positives_by_class) == expected_classes,
        "minimum_positive_repeats": all(
            len(positives_by_class.get(name, [])) >= MINIMUM_POSITIVE_REPEATS
            for name in expected_classes
        ),
        "positive_sources_unique": all(
            len({row.get("source_sha256") for row in positives_by_class.get(name, [])})
            == len(positives_by_class.get(name, []))
            for name in expected_classes
        ),
        "positive_runs_passed": all(row.get("validation_passed") is True for row in positive_rows),
        "fault_coverage_complete": all(
            set(faults_by_class.get(name, {})) == set(EXPECTED_FAULT_STATUS)
            for name in expected_classes
        ),
        "faults_fail_closed": all(row.get("validation_passed") is True for row in fault_rows),
        "shared_control_path": all(
            row.get("control_path") == EXPECTED_CONTROL_PATH for row in positive_rows + fault_rows
        ),
        "reported_position_only": all(
            row.get("position_source") == "MAVLink GLOBAL_POSITION_INT"
            for row in positive_rows + fault_rows
        ),
    }
    document: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "product": "Multi-Detect",
        "release_label": release_identity["release_label"],
        "release_identity": release_identity,
        "minimum_positive_repeats": MINIMUM_POSITIVE_REPEATS,
        "expected_faults": EXPECTED_FAULT_STATUS,
        "source_tools": _source_tools(root),
        "positive_runs": sorted(
            positive_rows,
            key=lambda row: (
                str(row.get("vehicle_class")),
                str(row.get("source_path")),
            ),
        ),
        "fault_runs": sorted(
            fault_rows, key=lambda row: (str(row.get("vehicle_class")), str(row.get("fault")))
        ),
        "statistics": statistics,
        "shared_checks": shared_checks,
        "truth_free": True,
        "hardware_acceptance": "not_claimed",
        "hil_acceptance": "not_claimed",
        "real_acceptance": "not_claimed",
        "gate_passed": not reasons and all(shared_checks.values()),
        "reasons": reasons,
    }
    document["campaign_sha256"] = _canonical_sha256(
        {key: value for key, value in document.items() if key != "campaign_sha256"}
    )
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positive", action="append", type=Path, required=True)
    parser.add_argument("--fault", action="append", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--release-parity", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        document = build_campaign(
            args.positive,
            args.fault,
            repository_root=args.repository_root,
            release_parity_path=args.release_parity,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        document = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "gate_passed": False,
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    output = args.out.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0 if document.get("gate_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
