"""Build and load an offline Gazebo capture route for the shared OnboardAI.

The mainline manifest is allowed to use the known model placement to design a
repeatable camera search route.  The resulting route is capture infrastructure
only: the target pose is never placed in the runtime observation, detector
input, model feature vector, or training label.  Vehicle-specific runners must
opt in explicitly; the current external acceptance runner supports multirotors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "configs" / "scenarios" / "gazebo-3d-mainline-v1.manifest.json"
DEFAULT_OUTPUT = ROOT / "configs" / "scenarios" / "gazebo-3d-capture-plan-v1.json"
SCHEMA = "multidetect.gazebo-3d-capture-plan-v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value: object, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"capture plan {field} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"capture plan {field} must be finite")
    return result


def _pose_enu(pose: object, *, scenario_id: str) -> dict[str, float]:
    if not isinstance(pose, str):
        raise ValueError(f"{scenario_id}: target_model.pose must be an ENU pose string")
    values = pose.split()
    if len(values) != 6:
        raise ValueError(f"{scenario_id}: target_model.pose must contain six values")
    return {
        "east_m": _finite(values[0], field=f"{scenario_id}.target_pose.east_m"),
        "north_m": _finite(values[1], field=f"{scenario_id}.target_pose.north_m"),
        "up_m": _finite(values[2], field=f"{scenario_id}.target_pose.up_m"),
        "yaw_rad": _finite(values[5], field=f"{scenario_id}.target_pose.yaw_rad"),
    }


def _multirotor_route() -> tuple[dict[str, float], ...]:
    """Return a bounded scene-envelope lawn-mower route.

    ENU is x=east/y=north in the SDF.  The route is stored as NED-facing
    offsets for the existing Mission geometry adapter.  The envelope is
    derived from the repository-owned urban inspection corridor, not from the
    target include pose; the target therefore cannot become a hidden runtime
    waypoint or a training shortcut.
    """

    # The existing disposable multirotor runner has a reviewed 12 m maximum
    # relative-altitude policy.  Keep the collection route inside that policy;
    # do not widen safety limits just to make a capture plan look convenient.
    altitude = 12.0
    return (
        {"north_m": -80.0, "east_m": -30.0, "altitude_m": altitude},
        {"north_m": -80.0, "east_m": 280.0, "altitude_m": altitude},
        {"north_m": 110.0, "east_m": 280.0, "altitude_m": altitude},
        {"north_m": 110.0, "east_m": -30.0, "altitude_m": altitude},
        {"north_m": 15.0, "east_m": 125.0, "altitude_m": altitude},
    )


def build_capture_plan(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("scenarios"), list):
        raise ValueError("Gazebo mainline manifest must contain a scenarios array")
    scenarios: list[dict[str, Any]] = []
    for raw in document["scenarios"]:
        if not isinstance(raw, dict):
            raise ValueError("Gazebo mainline scenarios must be objects")
        scenario_id = str(raw.get("scenario_id", "")).strip()
        vehicle = raw.get("vehicle")
        target = raw.get("target_model")
        if not scenario_id or not isinstance(vehicle, dict) or not isinstance(target, dict):
            raise ValueError(f"invalid capture-plan scenario: {scenario_id or '<missing>'}")
        vehicle_class = str(vehicle.get("class", "")).strip()
        target_pose = _pose_enu(target.get("pose"), scenario_id=scenario_id)
        supported = vehicle_class == "multirotor"
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "vehicle_class": vehicle_class,
                "world_frame": str(raw.get("world_frame_orientation", "ENU")),
                "scenario_manifest": str(raw.get("scenario_manifest", {}).get("path", "")),
                "camera_calibration": str(raw.get("camera_calibration", "")),
                "target_model_id": str(target.get("uri", "")),
                "target_pose_enu_m": target_pose,
                "capture_route": list(_multirotor_route()) if supported else [],
                "status": "ready_to_execute" if supported else "blocked_unsupported_vehicle_runner",
                "truth_free_runtime_input": True,
                "target_pose_used_for_runtime_model_input": False,
                "target_pose_used_for_capture_route": False,
            }
        )
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "manifest_path": manifest_path.relative_to(ROOT).as_posix()
        if manifest_path.is_relative_to(ROOT)
        else manifest_path.as_posix(),
        "manifest_sha256": _sha256(manifest_path),
        "purpose": (
            "offline initial-mission capture route only; route is derived from "
            "the scene envelope and target pose is not a runtime observation or model feature"
        ),
        "enu_convention": "Gazebo world ENU: +X east, +Y north, +Z up",
        "mission_convention": (
            "route offsets are north/east metres relative to live PX4 home; "
            "the reviewed scene spawn must remain at ENU 0,0"
        ),
        "scenarios": scenarios,
        "external_gazebo_runtime": "not_executed",
        "candidate_promoted": False,
        "final_release_allowed": False,
    }


def load_capture_plan(
    path: Path,
    *,
    scenario_id: str,
    vehicle_class: str = "multirotor",
) -> dict[str, Any]:
    path = path.expanduser().resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != SCHEMA or document.get("schema_version") != 1:
        raise ValueError("capture plan schema mismatch")
    if document.get("external_gazebo_runtime") != "not_executed":
        raise ValueError("capture plan cannot claim external runtime execution")
    entries = [
        item for item in document.get("scenarios", ()) if item.get("scenario_id") == scenario_id
    ]
    if len(entries) != 1:
        raise ValueError(f"capture plan must contain exactly one scenario: {scenario_id}")
    entry = entries[0]
    if entry.get("vehicle_class") != vehicle_class:
        raise ValueError(f"capture plan vehicle mismatch: expected {vehicle_class}")
    if entry.get("target_pose_used_for_capture_route") is not False:
        raise ValueError("capture plan route must not be derived from target pose")
    if entry.get("status") != "ready_to_execute":
        raise ValueError(f"capture plan is not executable for {scenario_id}")
    route = entry.get("capture_route")
    if not isinstance(route, list) or len(route) < 3:
        raise ValueError(f"capture plan route is too short: {scenario_id}")
    for index, waypoint in enumerate(route):
        if not isinstance(waypoint, dict):
            raise ValueError(f"capture plan waypoint {index} is invalid")
        for field in ("north_m", "east_m", "altitude_m"):
            _finite(waypoint.get(field), field=f"{scenario_id}.route[{index}].{field}")
    return {
        "path": path.as_posix(),
        "sha256": _sha256(path),
        "scenario_id": scenario_id,
        "vehicle_class": vehicle_class,
        "route": tuple(dict(item) for item in route),
        "target_pose_excluded_from_runtime": True,
        "target_pose_excluded_from_model_input": True,
        "route_is_initial_mission_geometry_only": True,
        "manifest_sha256": document.get("manifest_sha256"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    document = build_capture_plan(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": str(args.output.resolve()), "sha256": _sha256(args.output)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
