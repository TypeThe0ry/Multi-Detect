#!/usr/bin/env python3
"""广域固定翼与 VTOL 长航程入口，支持纯 CPU 规划。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import ContextSnapshot, GeoPoint, ResolvedTaskSpec, TaskGeometry, TaskType, TaskConstraints, PlanAction  # noqa: E402
from multidetect.autonomy.motion_intent import MotionCommand  # noqa: E402
from multidetect.autonomy.serialization import canonical_sha256, require_real  # noqa: E402
from multidetect.camera_mount import _mat_mul, _mat_transpose, _mat_vec  # noqa: E402
from multidetect.mission_planning import compile_planned_task_mission_from_runtime  # noqa: E402
from multidetect.px4_mission import (  # noqa: E402
    FixedWingMissionGeometry, Px4FixedWingSafetyPolicy, Px4MissionSafetyPolicy, VtolMissionGeometry,
)
from multidetect.sim_teacher.executor import ExecutionResult  # noqa: E402
from multidetect.sim_teacher.gimbal_recording import X1_FW_PLANNING_MARGIN_DEG  # noqa: E402
from multidetect.sim_teacher.horizon_stabilization import C11_LIMITS, rotation_deg, solve_joint_target  # noqa: E402
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402
from multidetect.sim_teacher.target_tracking import (  # noqa: E402
    WorldTarget, c11_optical_axis_reachable, level_target_rotation, track_cli_argv, world_target_direction,
)
from multidetect.simulation.turn_envelope import coordinated_turn_envelope  # noqa: E402
from multidetect.terrain_dem import load_local_dem_grid  # noqa: E402
from scripts.realworld.x1_fw_loiter import waypoint_turn  # noqa: E402
from scripts.realworld.leased_flight import SITL_PYTHON  # noqa: E402
from scripts.realworld import x1_scenario_class as scenario_policy  # noqa: E402
from scripts.sitl_lock import SitlLock, sitl_lock_context  # noqa: E402

AIRFRAME = "rc_cessna_mono_cam"
DELTA = "delta_tailsitter_gimbal_cam"
AIRFRAME_PROFILES = {
    AIRFRAME: ("px4_fixed_wing_gazebo_v1", "rc_cessna_runway", "fixed-wing"),
    DELTA: ("px4_delta_tailsitter_gimbal_gazebo_v1", "delta_tailsitter_gimbal", "vtol"),
}
DEFAULT_PROFILE = "rw_farm_iowa_w6km__clear_noon"
PROFILES = (DEFAULT_PROFILE, "rw_city_zurich_w6km__clear_noon")
SCENARIO_ROOT = ROOT / "configs/scenarios/realworld"
DEFAULT_SCENARIO = SCENARIO_ROOT / DELTA / f"{DEFAULT_PROFILE}.scenario.json"
DEFAULT_OUTPUT_ROOT = ROOT / ".tmp/rtf-speedup-run/collect"
MIN_WIDE_EXTENT_M = 2000.0
MIN_2KM_DISTANCE_M = 1500.0
MIN_6KM_DISTANCE_M = float(scenario_policy.WIDE_DISTANCE_M)
DEFAULT_MIN_RTF = 0.9
DELTA_CRUISE_AGL_M = 85.0
MAX_CRUISE_AGL_M = 120.0
# Lowest supported FW_R_LIM in docs/reference/px4-pinned-fw-vtol-params.json.
DELTA_FW_BANK_LIMIT_DEG = 35.0
DELTA_FW_PITCH_RANGE_DEG = (-10.0, 15.0)
DELTA_LONG_RANGE_GIMBAL_PITCH_DEG = -56.0
FW_OFFBOARD_MODE = (6, 0)
AUTO_LOITER_MODE = (4, 3)
HEARTBEAT_MAX_AGE_S = 30.0  # Allow a slow lockstep simulation.


def minimum_distance_for_extent(extent_m: float) -> float:
    if not math.isfinite(extent_m):
        raise ValueError("long-range extent_m must be finite")
    return MIN_6KM_DISTANCE_M if extent_m >= 6000.0 else MIN_2KM_DISTANCE_M


@contextmanager
def _preview_slot(slot: int):
    previous = os.environ.get("MULTIDETECT_SITL_SLOT")
    os.environ["MULTIDETECT_SITL_SLOT"] = str(slot)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MULTIDETECT_SITL_SLOT", None)
        else:
            os.environ["MULTIDETECT_SITL_SLOT"] = previous


def sidecar_path(scenario: Path) -> Path:
    return scenario.with_name(scenario.name.removesuffix(".scenario.json") + ".long_range.json")


def scenario_id(scenario: Path) -> str:
    return scenario.name.removesuffix(".scenario.json")


def output_name(scenario: Path, minimum_rtf: float = DEFAULT_MIN_RTF) -> str:
    """Stable output stem for a profile run and its measured RTF gate."""
    return f"{scenario_id(scenario)}__long-range__rtf{int(round(minimum_rtf * 100)):02d}"


def default_output_path(scenario: Path, minimum_rtf: float = DEFAULT_MIN_RTF) -> Path:
    root = DEFAULT_OUTPUT_ROOT / DELTA if scenario.parent.name == DELTA else DEFAULT_OUTPUT_ROOT
    return root / output_name(scenario, minimum_rtf)


def _scenario_document(scenario: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        document = json.loads(scenario.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"scenario manifest not found: {scenario}") from exc
    from scripts import collect_sim_teacher_episodes as collector

    runtime_path, _ = collector._scenario_channels(scenario)
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(runtime, dict):
        raise ValueError("scenario and runtime channels must be JSON objects")
    return document, runtime


def resolve_profile(profile: str | None, scenario: Path | None, airframe: str | None = None) -> Path:
    """Resolve the named wide-area profile without falling back to generated worlds."""
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"unknown long-range profile: {profile}")
    if scenario is None:
        return (SCENARIO_ROOT / (airframe or DELTA) / f"{profile or DEFAULT_PROFILE}.scenario.json").resolve()
    if profile is not None and scenario_id(scenario) != profile:
        raise ValueError(f"profile {profile} requires {profile}.scenario.json")
    return scenario.resolve()


def _validate_profile(
    scenario: Path,
    sidecar: Mapping[str, Any],
    manifest: Mapping[str, Any],
    runtime: Mapping[str, Any],
    *,
    minimum_rtf: float | None,
) -> tuple[str, float]:
    scenario_name = str(manifest.get("scenario_id", ""))
    runtime_id = str(runtime.get("scenario_id", ""))
    if scenario_name != runtime_id or scenario_name != scenario_id(scenario):
        raise ValueError("scenario manifest/runtime identity does not match the scenario filename")
    if not scenario_name.startswith("rw_") or not any(
        profile in scenario_name for profile in ("w2km", "w6km")
    ):
        raise ValueError("long-range profile requires an rw_* 2 km or 6 km real-terrain scenario")
    vehicle = runtime.get("vehicle")
    if not isinstance(vehicle, Mapping):
        raise ValueError("long-range profile requires a runtime vehicle")
    airframe = vehicle.get("model")
    if airframe not in AIRFRAME_PROFILES or vehicle.get("airframe_profile_id") != AIRFRAME_PROFILES[airframe][0]:
        raise ValueError("long-range runtime vehicle does not match a supported airframe profile")
    world = runtime.get("world")
    if not isinstance(world, Mapping) or any(
        not str(world.get(key, "")).startswith("builtin://")
        or scenario_name not in str(world.get(key, ""))
        for key in ("world_asset", "map_asset", "dem_asset")
    ):
        raise ValueError("long-range profile requires scenario-owned rw_* world/map/DEM assets")
    declared_scenario = sidecar.get("scenario_id", scenario_name)
    if declared_scenario != scenario_name:
        raise ValueError(f"long-range sidecar scenario_id {declared_scenario!r} does not match {scenario_name!r}")
    declared_profile = sidecar.get("profile", scenario_name)
    if declared_profile != scenario_name:
        raise ValueError(f"long-range sidecar profile {declared_profile!r} does not match {scenario_name!r}")
    declared_airframe = sidecar.get("airframe", AIRFRAME)
    if declared_airframe != airframe:
        raise ValueError("long-range sidecar airframe must match the runtime vehicle")
    if sidecar.get("kind", "fixed-wing") != AIRFRAME_PROFILES[airframe][2]:
        raise ValueError("long-range sidecar kind must match the runtime vehicle class")
    extent = float(sidecar.get("extent_m", 0.0))
    expected_extent = 2000.0 if "w2km" in scenario_name else 6000.0
    if not math.isfinite(extent) or not math.isclose(extent, expected_extent, abs_tol=1e-6):
        raise ValueError(f"long-range sidecar extent_m must be {expected_extent:g} for {scenario_name}")
    declared_rtf = float(sidecar.get("minimum_rtf", DEFAULT_MIN_RTF))
    requested_rtf = declared_rtf if minimum_rtf is None else float(minimum_rtf)
    if not math.isfinite(requested_rtf) or requested_rtf < DEFAULT_MIN_RTF:
        raise ValueError(f"long-range RTF requirement must be at least {DEFAULT_MIN_RTF:g}")
    if not math.isfinite(declared_rtf) or declared_rtf < DEFAULT_MIN_RTF:
        raise ValueError(f"long-range sidecar RTF requirement must be at least {DEFAULT_MIN_RTF:g}")
    declared_output = sidecar.get("output_stem")
    if declared_output is not None and declared_output != output_name(scenario, declared_rtf):
        raise ValueError("long-range sidecar output_stem does not match the scenario/profile RTF gate")
    expected_task_id = f"{scenario_name}-long-range"
    task = sidecar.get("task")
    if not isinstance(task, Mapping) or task.get("task_id") != expected_task_id:
        raise ValueError(f"long-range sidecar task_id must be {expected_task_id}")
    return airframe, max(requested_rtf, declared_rtf)


def offset_ned(home: GeoPoint, point: GeoPoint) -> tuple[float, float, float]:
    radius = 6_378_137.0
    return (math.radians(point.latitude_deg - home.latitude_deg) * radius,
            math.radians(point.longitude_deg - home.longitude_deg) * radius * math.cos(math.radians(home.latitude_deg)),
            -float(point.altitude_m or 0.0))


def _dem_path(runtime: Mapping[str, Any]) -> Path:
    root = Path(os.environ.get("MULTIDETECT_RTF_TERRAIN_ROOT") or ROOT / "simulation/terrain")
    return root / runtime["world"]["dem_asset"].removeprefix("builtin://terrain/")


def _terrain_agl(dem, ned: tuple[float, float, float]) -> float:
    north, east, down = ned
    return dem.origin_elevation_m - dem.sample_local(north, east).elevation_m - down


def _delta_fw_turn_envelope(geometry, profile, bank_limit_deg, gimbal_pitch_deg):
    if (type(bank_limit_deg) not in (int, float) or not math.isfinite(bank_limit_deg)
            or bank_limit_deg != DELTA_FW_BANK_LIMIT_DEG or bank_limit_deg > profile["max_bank_deg"]):
        raise ValueError("Delta long-range FW_R_LIM must be 35 degrees")

    # Delta camera is attached to MC FRD, B_mc = B_fw Ry(-90°). With a
    # yaw pivot to the wing's forward azimuth, the ZXY joint solution is
    # (yaw, roll, pitch) = (FW bank, 0, 90° + camera pitch - FW pitch).
    pitch_low, pitch_high = DELTA_FW_PITCH_RANGE_DEG
    witness_pitch = (90 + gimbal_pitch_deg - pitch_high, 90 + gimbal_pitch_deg - pitch_low)
    joint_pitch_low, joint_pitch_high = profile["camera"]["gimbal"]["angle_range_deg"]["pitch"]
    # The Gazebo quaternion command carrier also requires |joint pitch| < 90°.
    if (witness_pitch[0] < max(joint_pitch_low, -89.9)
            or witness_pitch[1] > min(joint_pitch_high, 89.9)):
        raise ValueError("long-range gimbal_pitch_deg must cover the Delta FW pitch envelope")

    # Size the circle for maximum profile airspeed plus its wind allowance.
    # At constant airspeed and wind smaller than airspeed, the greatest
    # acceleration required for a ground circle occurs directly downwind.
    speed = profile["max_airspeed_mps"] + profile["endurance"]["wind_margin_mps"]
    envelope = coordinated_turn_envelope(
        true_airspeed_mps=speed, radius_m=geometry.loiter_radius_m, bank_limit_deg=bank_limit_deg
    )
    if not envelope["within_configured_bank_limit"]:
        raise ValueError("Delta long-range loiter radius is too small for its FW bank and speed envelope")
    return {
        "px4_fw_bank_limit_deg": bank_limit_deg,
        "guided_ground_speed_mps": profile["cruise_airspeed_mps"],
        "guided_loiter_radius_m": geometry.loiter_radius_m,
        "turn_design_speed_mps": speed,
        "required_bank_at_radius_deg": envelope["required_bank_deg"],
        "minimum_turn_radius_m": envelope["minimum_radius_at_bank_limit_m"],
        "assumed_fw_pitch_range_deg": DELTA_FW_PITCH_RANGE_DEG,
        "camera_world_pitch_deg": gimbal_pitch_deg,
        "gimbal_witness": {"yaw_reference": "wing_forward_azimuth", "joint_roll_deg": 0.0,
                           "joint_pitch_range_deg": witness_pitch},
    }


def _route_preflight(data, target_count):
    """Parse the Delta guided-route assumptions and attached offline evidence."""
    if "route_preflight" not in data:
        return None
    group = data["route_preflight"]
    if not isinstance(group, dict):
        raise ValueError("route_preflight must be an object")

    def positive(value, name):
        number = require_real(value, f"route_preflight.{name}")
        if number <= 0:
            raise ValueError(f"route_preflight.{name} must be positive")
        return number

    capture = group.get("fly_through_radius_m")
    if isinstance(capture, list):
        if len(capture) != target_count:
            raise ValueError("route_preflight.fly_through_radius_m must have one entry per guided waypoint")
        capture = [positive(radius, "fly_through_radius_m") if radius is not None else None
                   for radius in capture]
    else:
        capture = positive(capture, "fly_through_radius_m")
    result = {
        **group,
        "fly_through_radius_m": capture,
        "turn_radius_m": positive(group.get("turn_radius_m"), "turn_radius_m"),
        "buffer_m": positive(group.get("buffer_m"), "buffer_m"),
    }
    if "metrics" in group and not isinstance(group["metrics"], dict):
        raise ValueError("route_preflight.metrics must be an object")
    # Preserve supplied audit results verbatim. Reject NaN and infinities, which
    # Python's JSON reader accepts but other consumers cannot parse.
    json.dumps(result, allow_nan=False)
    return result


def verify_geometry_inputs(preflight, model_root):
    """Bind the audited collision, canopy and raw inputs to the resolved model set."""
    model_root = Path(model_root).resolve(strict=True)
    inputs = preflight.get("geometry_inputs")
    if model_root.name != "models" or not isinstance(inputs, dict) or not inputs:
        raise ValueError("X1 route requires the audited asset directory's models root")
    asset = model_root.parent
    for relative, expected in inputs.items():
        path = (asset / relative).resolve(strict=True)
        if Path(relative).is_absolute() or not path.is_relative_to(asset) or not path.is_file():
            raise ValueError(f"invalid X1 geometry input path: {relative}")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"X1 geometry input SHA-256 mismatch: {path}")


def _verify_route_stack(scenario, preflight, *, plan_only):
    if "schema_version" not in preflight and plan_only:
        return  # Historical Zürich audit can still be reviewed offline.
    expected = preflight.get("assets")
    if (type(preflight.get("schema_version")) is not int or preflight["schema_version"] != 1
            or not isinstance(expected, dict) or set(expected) != {"world_sha256", "map_sha256", "dem_sha256"}
            or not all(isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest)
                       for digest in expected.values())
            or preflight.get("metrics", {}).get("passed") is not True):
        raise ValueError("X1 fly-through route requires passed, hash-bound geometry evidence")
    from multidetect.runtime import RuntimeMode
    from multidetect.simulation import load_runtime_scenario
    from multidetect.simulator.stack import resolve_simulator_assets
    from scripts import collect_sim_teacher_episodes as collector

    runtime_path, _ = collector._scenario_channels(scenario)
    runtime = load_runtime_scenario(scenario, mode=RuntimeMode.SIM)
    assets = resolve_simulator_assets(runtime, repository_root=ROOT)
    if collector.scenario_world_path(runtime_path) != assets.world.path:
        raise ValueError("X1 collector and SIM stack did not resolve the same world")
    for field in ("world", "map", "dem"):
        if getattr(assets, field).sha256 != expected[f"{field}_sha256"]:
            raise ValueError(f"X1 preflight {field} SHA-256 mismatch")
    model_root = os.environ.get("MULTIDETECT_SIM_FUEL_RESOLVED_ROOT")
    if not model_root or os.environ.get("MULTIDETECT_RTF_MODEL_ROOT"):
        raise ValueError("X1 fly-through requires its audited resolved models root without a variant model override")
    verify_geometry_inputs(preflight, model_root)


def _verify_audited_route(data, geometry, targets, preflight):
    metrics = preflight.get("metrics", {})
    points = metrics.get("waypoints_enu_m")
    radii = metrics.get("radii_m")
    mission_waypoints = data["mission_compile_runtime"]["mission_geometry"]["waypoints"]
    if (metrics.get("passed") is not True or not isinstance(points, list) or len(points) != len(targets) + 2
            or not isinstance(radii, list) or preflight["turn_radius_m"] not in radii
            or preflight["buffer_m"] != metrics.get("buffer_m")
            or preflight["fly_through_radius_m"] != metrics.get("capture_radius_m")
            or geometry.acceptance_radius_m != preflight["fly_through_radius_m"]
            or data["task"]["geometry"]["centerline"] != mission_waypoints):
        raise ValueError("X1 mission does not match the audited route settings")
    actual = [(0, 0, geometry.takeoff_altitude_m), *((e, n, -d) for n, e, d in targets),
              (0, 0, -targets[-1][2])]
    for expected, point in zip(points, actual, strict=True):
        if (not isinstance(expected, list) or len(expected) != 3
                or math.dist([require_real(value, "audited ENU point") for value in expected], point) > 1e-5):
            raise ValueError("X1 mission does not match the audited route waypoints")


def _audit_delta_route(geometry, targets, data, dem, minimum, *, route_preflight=None):
    start = (0.0, 0.0, -geometry.takeoff_altitude_m)
    capture = route_preflight["fly_through_radius_m"] if route_preflight else None
    capture_radii = capture if isinstance(capture, list) else [capture] * len(targets)
    metrics = route_preflight.get("metrics", {}) if route_preflight else {}
    revision = metrics.get("flight_margin_revision")
    bounded_pass = revision in scenario_policy.FW_PASS_REVISIONS
    if revision == scenario_policy.SAMPLED_FLIGHT_MARGIN_REVISION and data["scenario_id"] not in {
            scenario_policy.ALPINE_WORLD, scenario_policy.AMSTERDAM_WORLD}:
        raise ValueError("sampled Offboard passage requires a surveyed Amsterdam or Grindelwald route")
    if bounded_pass:
        if any(radius is None for radius in capture_radii):
            raise ValueError("bounded Offboard passage requires an Offboard command at every waypoint")
        departure = max(math.hypot(*point[:2]) for point in targets) - scenario_policy.fw_capture_witness_m(revision)
    else:
        departure = max(
            math.hypot(*point[:2]) - (geometry.loiter_radius_m if radius is None else radius)
            for point, radius in zip(targets, capture_radii, strict=True)
        )
    if departure < minimum + 5.0:
        raise ValueError("VTOL route requires an arrival margin beyond the distance gate")
    if not 250.0 <= math.hypot(*targets[-1][:2]) <= 650.0:
        raise ValueError("VTOL back-transition point must be 250–650 m from home")
    if math.hypot(*offset_ned(geometry.home, geometry.landing)[:2]) > 1.0:
        raise ValueError("VTOL landing must be at home")
    boundary = data["boundary"]
    revised = revision in (*scenario_policy.FW_PASS_REVISIONS, scenario_policy.LEGACY_FLIGHT_MARGIN_REVISION)
    limits = (40.0, MAX_CRUISE_AGL_M)
    transition_min = None
    if revised and data["scenario_id"] == scenario_policy.ALPINE_WORLD:
        limits = scenario_policy.ALPINE_PLANNED_AGL_M
        transition_min = scenario_policy.ALPINE_TRANSITION_MIN_AGL_M
        if start[2] != targets[0][2]:
            raise ValueError("Grindelwald forward transition requires level first targets")
    if revision == scenario_policy.FLIGHT_MARGIN_REVISION or (
            revised and data["scenario_id"] == scenario_policy.AMSTERDAM_WORLD):
        mission_radius = data["mission_compile_runtime"]["safety_policy"]["maximum_mission_radius_m"]
        square_half = min(-boundary["north_min_m"], boundary["north_max_m"],
                          -boundary["east_min_m"], boundary["east_max_m"])
        points_ne = [start[:2], *(point[:2] for point in targets)]
        if (revision == scenario_policy.FLIGHT_MARGIN_REVISION
                and data.get("scenario_class", scenario_policy.WIDE_AREA) == scenario_policy.WIDE_AREA
                and not scenario_policy.has_wide_course_margin(points_ne)):
            raise ValueError("FW course change exceeds 22.5 degrees")
        runout = scenario_policy.fw_runout(points_ne, mission_radius, square_half, revision=revision)
        if not scenario_policy.has_runout_margin(runout):
            raise ValueError("FW runout exceeds the audited mission-radius or square margin")
    buffer_m = route_preflight["buffer_m"] if route_preflight else geometry.loiter_radius_m
    if revision == scenario_policy.FLIGHT_MARGIN_REVISION and buffer_m < scenario_policy.FW_PASS_BUFFER_M:
        raise ValueError("VTOL route buffer does not enclose the audited FW handoff")
    edge_margin_m = buffer_m + 20.0 if route_preflight else 100.0
    for point in targets:
        if not (boundary["north_min_m"] + edge_margin_m <= point[0] <= boundary["north_max_m"] - edge_margin_m
                and boundary["east_min_m"] + edge_margin_m <= point[1] <= boundary["east_max_m"] - edge_margin_m):
            raise ValueError(f"VTOL waypoints must leave {edge_margin_m:g} m of world-edge margin")
    # Check the straight route and lateral buffer. Allow the full range
    # between neighboring commanded altitudes, since guided PX4 need not interpolate.
    agl = []
    for leg_index, (left, right) in enumerate(zip((start, *targets), (*targets, (0.0, 0.0, targets[-1][2])))):
        dn, de = right[0] - left[0], right[1] - left[1]
        length = math.hypot(dn, de)
        if length < 1.0:
            raise ValueError("VTOL route contains coincident waypoints")
        count = math.ceil(length / 25)
        first_sample = len(agl)
        for i in range(count + 1):
            for lateral in (-buffer_m, 0.0, buffer_m):
                north = left[0] + dn * i / count - de / length * lateral
                east = left[1] + de * i / count + dn / length * lateral
                agl.extend(_terrain_agl(dem, (north, east, down)) for down in (left[2], right[2]))
        if leg_index == 0 and transition_min is not None and min(agl[first_sample:]) < transition_min:
            raise ValueError("Grindelwald first leg lacks forward transition AGL reserve")
    if min(agl) < limits[0] or max(agl) > limits[1]:
        raise ValueError(f"VTOL DEM route envelope {min(agl):.1f}–{max(agl):.1f} m AGL "
                         f"is outside [{limits[0]:g}, {limits[1]:g}]")
    return [min(agl), max(agl)]


def load_mission(scenario: Path, *, minimum_rtf: float | None = None,
                 dem_path: Path | None = None) -> dict[str, Any]:
    manifest, runtime_input = _scenario_document(scenario)
    data = json.loads(sidecar_path(scenario).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("long_range sidecar requires schema_version=1")
    airframe, required_rtf = _validate_profile(
        scenario, data, manifest, runtime_input, minimum_rtf=minimum_rtf
    )
    runtime = data["mission_compile_runtime"]
    profile_id, profile_name, vehicle_class = AIRFRAME_PROFILES[airframe]
    geometry_type = VtolMissionGeometry if airframe == DELTA else FixedWingMissionGeometry
    geometry = geometry_type.from_runtime_mapping(runtime)
    task = ResolvedTaskSpec.from_mapping(data["task"])
    context = ContextSnapshot.from_mapping(data["context"])
    if context.airframe_profile_id != profile_id or runtime["vehicle_class"] != vehicle_class:
        raise ValueError("mission compile context does not match the runtime vehicle")
    profile = json.loads((ROOT / "configs/airframes" / f"{profile_name}.json").read_text())
    compiled = compile_planned_task_mission_from_runtime(
        task, context, vehicle_class, runtime=runtime,
        airframe_profile_hash=canonical_sha256(profile), mission_id=f"{scenario_id(scenario)}-long-range")
    points = geometry.waypoints if airframe == DELTA else (*geometry.waypoints, geometry.landing_approach)
    targets = [offset_ned(geometry.home, point) for point in points]
    if airframe != DELTA and "route_preflight" in data:
        raise ValueError("route_preflight requires the Delta guided VTOL route")
    route_preflight = _route_preflight(data, len(targets))
    if route_preflight is not None and "schema_version" in route_preflight:
        _verify_audited_route(data, geometry, targets, route_preflight)
    maximum = max(math.hypot(p[0], p[1]) for p in targets)
    extent = float(data["extent_m"])
    scenario_class = data.get("scenario_class", scenario_policy.WIDE_AREA)
    class_threshold = scenario_policy.verify_route_policy(data, manifest["scenario_id"], scenario_class)
    short = scenario_class == scenario_policy.MOUNTAIN_SHORT
    if short and (airframe != DELTA or extent != 6000 or not route_preflight
                  or route_preflight.get("schema_version") != 1
                  or (route_preflight.get("metrics", {}).get("candidate"),
                      route_preflight.get("metrics", {}).get("flight_margin_revision")) not in {
                      (scenario_policy.ALPINE_LEGACY_CANDIDATE, None),
                      (scenario_policy.ALPINE_FIX1_CANDIDATE, scenario_policy.LEGACY_FLIGHT_MARGIN_REVISION),
                      *((scenario_policy.ALPINE_CANDIDATE, revision) for revision in scenario_policy.FW_PASS_REVISIONS)}
                  or math.dist((geometry.home.latitude_deg, geometry.home.longitude_deg),
                               scenario_policy.ALPINE_ORIGIN) > 1e-8):
        raise ValueError("mountain_short requires the audited Grindelwald X1 valley route")
    required_distance = class_threshold if short else minimum_distance_for_extent(extent)
    minimum = float(data.get("minimum_max_distance_m", required_distance))
    if (not math.isfinite(minimum) or minimum < required_distance or maximum < minimum
            or (short and data.get("minimum_max_distance_m") != required_distance)):
        raise ValueError(
            f"long-range mission must reach at least {required_distance:g} m from spawn"
        )
    if not math.isfinite(extent) or extent < MIN_WIDE_EXTENT_M:
        raise ValueError(f"long-range extent_m must be at least {MIN_WIDE_EXTENT_M:g}")
    terrain = agl_envelope = None
    gimbal_pitch_deg = fw_turn_envelope = None
    if airframe == DELTA:
        gimbal_config = profile["camera"]["gimbal"]
        gimbal_pitch_deg = data.get("gimbal_pitch_deg", DELTA_LONG_RANGE_GIMBAL_PITCH_DEG)
        lower, upper = gimbal_config["angle_range_deg"]["pitch"]
        if (type(gimbal_pitch_deg) not in (int, float) or not math.isfinite(gimbal_pitch_deg)
                or not lower <= gimbal_pitch_deg <= upper):
            raise ValueError("long-range gimbal_pitch_deg must be finite and within profile pitch limits")
        sim_params = dict(data.get("sim_params", {}))
        sim_params.setdefault("FW_R_LIM", DELTA_FW_BANK_LIMIT_DEG)
        fw_turn_envelope = _delta_fw_turn_envelope(geometry, profile, sim_params["FW_R_LIM"], gimbal_pitch_deg)
        data = {**data, "sim_params": sim_params, "gimbal_pitch_deg": gimbal_pitch_deg}
        if extent != 6000 or not 40 <= geometry.takeoff_altitude_m <= 90:
            raise ValueError("VTOL requires a 6 km world and a 40–90 m vertical climb")
        if data.get("capture_interval_sim_s") != 6.0:
            raise ValueError("VTOL requires a 6 simulation-second capture interval")
        spawn = runtime_input["vehicle"]["spawn_pose"]
        if math.hypot(float(spawn["x_east_m"]), float(spawn["y_north_m"])) > 1.0:
            raise ValueError("VTOL spawn must be at the central home pad")
        terrain = load_local_dem_grid(dem_path or _dem_path(runtime_input))
        origin = GeoPoint(terrain.origin_latitude_deg, terrain.origin_longitude_deg)
        if math.hypot(*offset_ned(geometry.home, origin)[:2]) > 1.0:
            raise ValueError("VTOL mission home does not match the DEM origin")
        agl_envelope = _audit_delta_route(geometry, targets, data, terrain, minimum,
                                          route_preflight=route_preflight)
    return {"source": data, "geometry": geometry, "compiled": compiled, "airframe": airframe,
            "gimbal_pitch_deg": gimbal_pitch_deg, "fw_turn_envelope": fw_turn_envelope,
            "terrain": terrain, "planned_agl_envelope_m": agl_envelope,
            "targets": targets, "route_preflight": route_preflight, "minimum_max_distance_m": minimum,
            "scenario_class": scenario_class,
            "planned_max_distance_m": maximum, "minimum_rtf": required_rtf,
            "profile": str(manifest["scenario_id"]), "seed": runtime_input.get("seed", 91000)}


def write_mission_sidecar(scenario: Path, extent_m: float = 6000.0) -> Path:
    """以独立场景的地图原点派生航线，保留中央已整平出生点。"""
    destination = sidecar_path(scenario)
    if destination.exists():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and "route_preflight" in existing:
            raise ValueError(f"refusing to overwrite route_preflight sidecar: {destination}")
    from scripts import collect_sim_teacher_episodes as collector
    runtime_path, _ = collector._scenario_channels(scenario)
    scenario_runtime = json.loads(runtime_path.read_text())
    dem = load_local_dem_grid(_dem_path(scenario_runtime))
    home = GeoPoint(dem.origin_latitude_deg, dem.origin_longitude_deg)
    airframe = scenario_runtime["vehicle"]["model"]
    profile_id, _, vehicle_class = AIRFRAME_PROFILES[airframe]
    is_vtol = airframe == DELTA
    if not math.isfinite(extent_m) or extent_m < MIN_WIDE_EXTENT_M:
        raise ValueError(f"long-range route requires extent_m>={MIN_WIDE_EXTENT_M:g}")
    half = extent_m / 2.0
    def point(east, north, altitude=100.0):
        return GeoPoint(home.latitude_deg + math.degrees(north / 6378137.0),
                        home.longitude_deg + math.degrees(east / (6378137.0 * math.cos(math.radians(home.latitude_deg)))),
                        altitude, "relative_home")
    route_offsets = (
        (1000, 0), (2300, 1300), (2800, 2400), (2850, 2850),
        (2400, 2800), (1300, 2300), (0, 1200), (-800, 500), (-800, 0),
    )
    if is_vtol:
        # North-west avoids the steep eastern side of the Zurich DEM. Turn
        # back in two legs, retaining margin from the square boundary.
        route_offsets = ((-850, 850), (-2200, 2450), (-2890, 2890),
                         (-2710, 2250), (-1900, 1510), (-900, 740), (-360, 300))
    route_radius = max(math.hypot(e, n) for e, n in route_offsets)
    required_distance = minimum_distance_for_extent(extent_m)
    scale = max(extent_m / 6000.0, required_distance / route_radius)
    waypoints = tuple(point(e * scale, n * scale, (
        dem.sample_local(n * scale, e * scale).elevation_m - dem.origin_elevation_m + DELTA_CRUISE_AGL_M
        if is_vtol else 100.0)) for e, n in route_offsets)
    planned_max_distance = max(math.hypot(e * scale, n * scale) for e, n in route_offsets)
    geofence_radius = max(half * math.sqrt(2), planned_max_distance + 100.0)
    limits = dict(geofence_center=home, geofence_radius_m=geofence_radius,
                  maximum_mission_radius_m=geofence_radius - 50.0,
                  allowed_airframe_profile_ids=(profile_id,))
    if is_vtol:
        geometry = VtolMissionGeometry(
            home=home, waypoints=waypoints, takeoff_altitude_m=65.0, loiter_time_s=1.0,
            loiter_radius_m=80.0, acceptance_radius_m=25.0, landing=point(0, 0, 0))
        policy = Px4MissionSafetyPolicy(**limits, maximum_relative_altitude_m=150.0)
    else:
        geometry = FixedWingMissionGeometry(
            home=home, waypoints=waypoints,
            landing_approach=point(-max(400.0 * scale, 200.0), 0, 40),
            landing=point(0, 0, 0), takeoff_altitude_m=40.0, acceptance_radius_m=25.0,
            takeoff_heading_deg=90.0, landing_heading_deg=90.0)
        policy = Px4FixedWingSafetyPolicy(**limits, cruise_airspeed_mps=15.0, minimum_airspeed_mps=10.0)
    centerline = (home, *waypoints, home)
    task = ResolvedTaskSpec(
        task_id=f"{scenario_runtime['scenario_id']}-long-range", task_type=TaskType.SURVEY,
        geometry=TaskGeometry(kind="map_feature", resolved_feature_id="long-range-circuit",
            direction="clockwise", length_m=sum(math.dist(offset_ned(home, a)[:2], offset_ned(home, b)[:2]) for a, b in zip(centerline, centerline[1:])), centerline=centerline,
            resolution_confidence=1.0, buffer_m=0.0), target_classes=("terrain",),
        on_detection=(), constraints=TaskConstraints(30.0, 3600.0, True),
        completion_action=PlanAction.RETURN)
    context = ContextSnapshot(snapshot_id="offline-long-range-feasibility", captured_at_s=0.0,
        captured_at_utc="2026-11-15T00:00:00Z", aircraft_position=home, home_position=home,
        airframe_profile_id=profile_id, battery_remaining_pct=100.0,
        estimated_flight_time_s=3600.0, ground_speed_mps=15.0, airspeed_mps=15.0,
        wind_north_mps=0.0, wind_east_mps=0.0, gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0, geofence_valid=True, camera_available=True,
        perception_available=True, map_cache_available=True, dem_available=True)
    scenario_id = str(scenario_runtime["scenario_id"])
    body = {"schema_version": 1, "scenario_id": scenario_id, "profile": scenario_id,
        "airframe": airframe, "minimum_rtf": DEFAULT_MIN_RTF,
        "output_stem": output_name(scenario, DEFAULT_MIN_RTF),
        "extent_m": extent_m, "minimum_max_distance_m": required_distance,
        "max_wall_s": 14400.0, "mission_compile_runtime": {"schema_version": 1,
            "vehicle_class": vehicle_class, **geometry.to_mapping(), "safety_policy": policy.to_mapping()},
        "task": task.to_mapping(), "context": context.to_mapping(),
        "context_scope": "offline assumed healthy SIM inputs; not observed telemetry",
        "sim_params": {"GF_MAX_HOR_DIST": geofence_radius, "GF_MAX_VER_DIST": 180,
                       "GF_ACTION": 1, **({} if is_vtol else {"BAT_N_CELLS": 3})},
        "boundary": {"east_min_m": -half, "east_max_m": half,
                     "north_min_m": -half, "north_max_m": half}}
    if is_vtol:
        body.update(kind="vtol", capture_interval_sim_s=6.0,
                    gimbal_pitch_deg=DELTA_LONG_RANGE_GIMBAL_PITCH_DEG)
        body["sim_params"]["FW_R_LIM"] = DELTA_FW_BANK_LIMIT_DEG
    destination.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    load_mission(scenario)  # Compiler validates route, turns, approach and energy before delivery.
    return destination


def collection_plan(scenario: Path, out: Path, slot: int, mission: dict[str, Any]):
    from scripts import collect_sim_teacher_episodes as collector
    runtime, truth = collector._scenario_channels(scenario)
    airframe = mission["airframe"]
    is_vtol = airframe == DELTA
    collector._validate_runtime(runtime, airframe)
    geometry = mission["geometry"]
    seconds = float(mission["source"].get("max_wall_s", 14400.0))
    overlay_root = os.environ.get("MULTIDETECT_RTF_OVERLAY_ROOT")
    if is_vtol and not overlay_root:
        # Also bind the wrapper over PX4_GZ_MODELS for launchers that load
        # model.sdf directly instead of resolving it via GZ_SIM_RESOURCE_PATH.
        overlay_root = str(ROOT / "simulation/gazebo/models-wide-area")
    return collector.CollectionPlan(
        str(scenario), str(runtime), str(truth), "explore", 1,
        len(mission["targets"]) + (3 if is_vtol else 1), mission.get("seed", 91000), str(out), airframe=airframe,
        smoke_flight=not is_vtol, container_prefix=os.environ.get("MULTIDETECT_LONG_RANGE_CONTAINER_PREFIX", f"md-rw-long-slot-{slot}"),
        control_port=14560 + 10 * slot, telemetry_port=14561 + 10 * slot,
        timeout_s=seconds,
        max_episode_s=seconds / collector.DELTA_TAILSITTER_WALL_CEILING_FACTOR if is_vtol else seconds,
        takeoff_altitude_m=geometry.takeoff_altitude_m,
        takeoff_method="vtol" if is_vtol else None,
        video_hz=1.0 / mission["source"]["capture_interval_sim_s"] if is_vtol else 0.0,
        video_use_sim_time=is_vtol,
        extra_sim_params=tuple((str(k), str(v)) for k, v in mission["source"].get("sim_params", {}).items()),
        task_text="VTOL wide-area terrain collection" if is_vtol else None,
        rtf_overlay_root=overlay_root or None)


GIMBAL_EVAL_HOLD_SIM_S = 60.0
GIMBAL_EVAL_MIN_TRACK_FRAMES = 3


def gimbal_eval_target(mission):
    """A fixed virtual SIM point, 100 m north of home and 5 m above its DEM."""
    if mission["airframe"] != DELTA or mission["scenario_class"] != scenario_policy.WIDE_AREA:
        raise ValueError("gimbal evaluation requires an X1 wide_area route")
    dem = mission["terrain"]
    hover_alt = dem.origin_elevation_m - mission["targets"][-1][2]
    target_alt = dem.sample_local(100.0, 0.0).elevation_m + 5.0
    if not 10 <= hover_alt - target_alt <= 150:
        raise ValueError("gimbal evaluation point is outside the terminal hover elevation envelope")
    home = mission["geometry"].home
    return WorldTarget(home.latitude_deg + math.degrees(100.0 / 6378137.0),
                       home.longitude_deg, target_alt)


X1_FW_SWITCH_LIMIT_DEG = 30.


def _fw_los_angle(a, b):
    # Chord length is well conditioned for nearly identical unit directions.
    return math.degrees(2 * math.asin(min(1., math.dist(a, b) / 2)))


def _fw_ground_offsets(length, radius):
    for along in (-1., -.5, -.25, 0., .25, .5, .75, 1., 1.25):
        for across in (0., -.125, .125, -.25, .25, -.5, .5, -1., 1.):
            yield along * length, across * length
    # Preserve a physical search scale when a short turn leg adjoins a long
    # straight. All candidates are anchored to this leg, including these rings.
    for scale in (.5, 1., 2., 4., 8.):
        for bearing in range(0, 360, 15):
            radians = math.radians(bearing)
            along = scale * radius * math.cos(radians)
            across = scale * radius * math.sin(radians)
            yield .5 * length + along, across
            yield along, across


def _fw_static_quality(point, poses, margin, inset):
    """Return the main planner's quality key, or None if any sampled pose misses."""
    zero = dict.fromkeys(C11_LIMITS, 0.)
    rays = [(world_target_direction(pos, point), attitudes) for pos, attitudes in poses]
    worst = -1.
    for ray, attitudes in rays:
        for body in attitudes:
            body_ray = _mat_vec(body, ray)
            if not c11_optical_axis_reachable(body_ray, margin_deg=margin):
                return None
            worst = max(worst, -body_ray[2])
    levels = [(level_target_rotation(ray), attitudes) for ray, attitudes in rays]
    level_count = sum(solve_joint_target(_mat_mul(body, level), zero, inset)[2]
                      for level, attitudes in levels for body in attitudes)
    return -level_count, worst


def gimbal_eval_fw_targets(mission, *, diagnostics=None):
    """每段独立选 DEM 点，先满足直线及转弯姿态余量，再最小化相邻视线夹角。"""
    dem = mission["terrain"]
    geometry = mission["geometry"]
    home = geometry.home
    earth = 6_378_137.0  # inverse of offset_ned
    east_radius = earth * math.cos(math.radians(home.latitude_deg))
    bank = mission["source"]["sim_params"]["FW_R_LIM"]
    margin = X1_FW_PLANNING_MARGIN_DEG
    inset = {axis: (lo + margin, hi - margin) for axis, (lo, hi) in C11_LIMITS.items()}

    def geo(north, east, down):
        return WorldTarget(home.latitude_deg + math.degrees(north / earth),
                           (home.longitude_deg + math.degrees(east / east_radius) + 180) % 360 - 180,
                           dem.origin_elevation_m - down)

    waypoints = [(0., 0., -geometry.takeoff_altitude_m), *mission["targets"]]
    legs = list(zip(waypoints[:-1], waypoints[1:], strict=True))
    if not legs:
        return ()
    headings, lengths = [], []
    for index, (start, end) in enumerate(legs):
        dn, de = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dn, de)
        if length == 0:
            raise ValueError(f"X1 FW eval leg {index} has no horizontal direction")
        lengths.append(length)
        headings.append(math.degrees(math.atan2(de, dn)))
    radius = getattr(geometry, "loiter_radius_m", 80.)
    preflight = mission.get("route_preflight") or {}
    capture = preflight.get("fly_through_radius_m")
    plane_pass = preflight.get("metrics", {}).get("flight_margin_revision") in scenario_policy.FW_PASS_REVISIONS
    captures = capture if isinstance(capture, list) else [capture] * len(legs)
    turns = [waypoint_turn(end, headings[i], headings[min(i + 1, len(legs) - 1)], radius, cap,
                           plane_pass=plane_pass, short_approach=lengths[i] < radius)
             for i, ((_, end), cap) in enumerate(zip(legs, captures, strict=True))]
    result = []
    used = set()
    for index, (start, end) in enumerate(legs):
        dn, de, dd = (b - a for a, b in zip(start, end, strict=True))
        length, heading = lengths[index], headings[index]
        bodies = tuple(_mat_transpose(rotation_deg(dict(roll=roll, pitch=pitch, yaw=heading)))
                       for pitch in (-90., -83., -65.) for roll in (0., -bank, bank))
        poses = [(geo(start[0] + f*dn/8, start[1] + f*de/8, start[2] + f*dd/8), bodies)
                 for f in range(9)]
        entry = turns[index-1].departing if index else ()
        poses.extend((geo(*pos), attitudes) for pos, attitudes in (*entry, *turns[index].arriving))
        switch = geo(*turns[index-1].switch_ned) if index else None
        previous_ray = world_target_direction(switch, result[-1]) if index else None
        lowest_alt = min(pos.alt_m for pos, _ in poses)
        candidates = []
        seen = set()
        for along, across in _fw_ground_offsets(length, radius):
            north = start[0] + (along * dn - across * de) / length
            east = start[1] + (along * de + across * dn) / length
            # Ignore coincident grid points even when roundoff differs between legs.
            location = round(north, 2), round(east, 2)
            if location in used or location in seen:
                continue
            seen.add(location)
            point = geo(north, east, 0.)
            dem_ne = dem.local_from_wgs84(point.lat_deg, point.lon_deg)
            if not dem.contains_local(*dem_ne):
                continue
            point = replace(point, alt_m=dem.sample_local(*dem_ne).elevation_m)
            if point.alt_m < lowest_alt:
                angle = _fw_los_angle(previous_ray, world_target_direction(switch, point)) if index else 0.
                candidates.append((angle, point, location, math.hypot(along / length - .5, across / length)))

        # Check in slew order. Once a reachable point is found, only ties can
        # improve its static quality. Never relax reachability to meet the cap.
        best = None
        for angle, point, location, distance in sorted(candidates, key=lambda item: item[0]):
            if best is not None and angle > best[0][0]:
                break
            quality = _fw_static_quality(point, poses, margin, inset)
            if quality is not None:
                key = angle, *quality, distance
                if best is None or key < best[0]:
                    best = key, point, location
        if best is None:
            raise ValueError(f"X1 FW eval leg {index} has no fresh terrain target with {margin:g} deg "
                             "C11 margin through cruise and waypoint turn")
        key, point, location = best
        result.append(point)
        used.add(location)
        slew = key[0] if index else None
        if index and slew > X1_FW_SWITCH_LIMIT_DEG:
            logging.getLogger(__name__).warning("X1 FW leg %d: best attainable switch %.2f exceeds %.1f deg",
                                                index, slew, X1_FW_SWITCH_LIMIT_DEG)
        if diagnostics is not None:
            diagnostics.append({"leg": index, "switch_los_deg": slew,
                                "switch_ned_m": turns[index-1].switch_ned if index else None,
                                "over_limit": bool(index and slew > X1_FW_SWITCH_LIMIT_DEG)})
    return tuple(result)


def gimbal_eval_plan(plan, mission, *, fw_legs=False, fw_diagnostics=None):
    """保留悬停目标；仅在 FW 验收开启时另外预选每段的地面点。"""
    hover = gimbal_eval_target(mission)
    return replace(plan, steps_per_episode=plan.steps_per_episode + 1,
                   track_targets=(hover,),
                   gimbal_eval_track_step=len(mission["targets"]) + 2,
                   gimbal_eval_fw_legs=len(mission["targets"]) if fw_legs else 0,
                   gimbal_eval_fw_targets=gimbal_eval_fw_targets(mission, diagnostics=fw_diagnostics) if fw_legs else ())


class LongRangePolicy:
    def __init__(self, targets: list[tuple[float, float, float]], radius: float, *, vtol: bool = False,
                 gimbal_pitch_deg: float = -45.0, gimbal_mode: str = "lock",
                 gimbal_yaw_mode: str = "follow", fw_speed_mps: float | None = None,
                 track_targets: tuple[WorldTarget, ...] = (),
                 gimbal_eval: bool = False,
                 fw_eval_switch_angles_deg: tuple[float | None, ...] | None = None,
                 fly_through_radius_m: float | list[float | None] | None = None) -> None:
        radii = (fly_through_radius_m if isinstance(fly_through_radius_m, list)
                 else [fly_through_radius_m] * len(targets))
        self.targets = iter(zip(targets, radii, strict=True))
        # Declarative route contract: the first N actions are FW repositions; no later FW transition.
        self.fw_gimbal_survey_legs = len(fw_eval_switch_angles_deg or ())
        self._next_switch_angles = None
        if fw_eval_switch_angles_deg is not None:
            if not vtol or not gimbal_eval or len(fw_eval_switch_angles_deg) != len(targets):
                raise ValueError("FW eval needs a planned switch angle for each leg")
            # Planner diagnostics describe entry to each leg; B runs on the outgoing leg.
            self._next_switch_angles = iter((*fw_eval_switch_angles_deg[1:], None))
            self._previous_ne = (0., 0.)
        self.radius = radius
        self.vtol = vtol
        self.fw_speed_mps = fw_speed_mps
        self.gimbal_mode, self.gimbal_yaw_mode = gimbal_mode, gimbal_yaw_mode
        self.track_targets = tuple(track_targets)
        if self.gimbal_mode == "track" and not self.track_targets:
            logging.getLogger(__name__).warning("track mode has no target; falling back to lock")
            self.gimbal_mode = "lock"
        self.cruise_gimbal_target = (
            {"pitch": float(gimbal_pitch_deg), "yaw": 0.0, "roll": 0.0} if vtol else None
        )
        self.return_actions = iter((
            MotionCommand("transition", mode="MC", reason="back_transition"),
            MotionCommand("mc_reposition", mode="MC", target_ned=(0.0, 0.0, targets[-1][2]),
                          speed_mps=8.0, reason="hover_return_to_home"),
            *((MotionCommand("mc_hold", mode="MC", target_ned=(0.0, 0.0, targets[-1][2]),
                             duration_s=GIMBAL_EVAL_HOLD_SIM_S, reason="gimbal_eval_tracking_hover"),)
              if gimbal_eval else ()),
        ) if vtol else ())

    def next_action(self):
        waypoint = next(self.targets, None)
        if waypoint is None:
            return next(self.return_actions, None) or ActionPrimitive.stop()
        target, capture_radius = waypoint
        metadata = {"long_range_home_relative": True}
        if self._next_switch_angles is not None:
            angle = next(self._next_switch_angles)
            if capture_radius is not None and angle is not None:
                metadata.update(long_range_leg_length_m=math.dist(self._previous_ne, target[:2]),
                                fw_next_switch_angle_deg=angle)
            self._previous_ne = target[:2]
        return MotionCommand("reposition", target_ned=target, radius_m=self.radius, speed_mps=self.fw_speed_mps,
                             action=ActionPrimitive.hold(1.0),
                             mode="FW" if self.vtol else None,
                             metadata=metadata,
                             fly_through_radius_m=capture_radius)

    def close(self) -> None:
        return None


def bind_target(command: MotionCommand, home_ned: tuple[float, float, float]) -> MotionCommand:
    if command.target_ned is None:
        raise ValueError("long-range reposition requires a target")
    return replace(command, target_ned=tuple(a + b for a, b in zip(command.target_ned, home_ned, strict=True)))


def _check_offboard_mode(collector, reader, streamer, *, wall=time.monotonic):
    if streamer is None:
        raise RuntimeError("FW fly-through requires an active Offboard path stream")
    streamer.raise_if_failed()
    state = reader.state
    received = getattr(state, "received_monotonic_ns", {}).get("HEARTBEAT", -1)
    mode = collector._field(state.get("HEARTBEAT"), "custom_mode", None)
    if mode is None or received < 0 or wall() - received / 1e9 > HEARTBEAT_MAX_AGE_S:
        raise RuntimeError("long-range Offboard guidance lost its PX4 heartbeat")
    if collector._px4_mode_pair(int(mode)) != FW_OFFBOARD_MODE:
        raise RuntimeError("PX4 left Offboard mode during long-range guidance")


def _fw_plane_passage(previous, current, tangent, target_ne):
    """Classify the first fresh on/after-plane sample of a commanded leg."""
    if previous is None or previous[1] >= 0:
        return {"passage_failure": "no_pre_plane_sample"}
    last_stamp, last_along, last_cross = previous
    stamp, along, cross = current
    fraction = -last_along / (along - last_along)
    at_plane = last_cross + fraction * (cross - last_cross)
    gap = stamp - last_stamp
    step = math.hypot(along - last_along, cross - last_cross)
    # The positive cross-track normal is (-east, north). A displacement
    # with a nonnegative radial component cannot reduce departure distance.
    tn, te = tangent
    n, e = target_ne
    outward = at_plane * (tn * e - te * n) >= 0
    limit = scenario_policy.FW_PASS_OUTWARD_M if outward else scenario_policy.FW_PASS_CROSS_M
    evidence = {"pre_plane_sim_stamp_ns": last_stamp,
                "pre_plane_along_m": last_along, "pre_plane_cross_track_m": last_cross,
                "crossing_cross_track_m": at_plane,
                "crossing_sim_stamp_ns": last_stamp + round(fraction * gap),
                "sample_gap_s": gap / 1e9, "sample_step_m": step,
                "crossing_cross_track_limit_m": limit}
    # Tolerate only arithmetic rounding at the metric boundaries.
    if not 0 < gap <= scenario_policy.FW_PASS_MAX_GAP_NS:
        evidence["passage_failure"] = "sample_gap"
    elif step > scenario_policy.FW_PASS_MAX_STEP_M + 1e-6:
        evidence["passage_failure"] = "sample_step"
    elif abs(cross) > scenario_policy.FW_PASS_OUTWARD_M + 1e-6:
        evidence["passage_failure"] = "post_plane_cross_track"
    elif abs(at_plane) > limit + 1e-6:
        evidence["passage_failure"] = "plane_cross_track"
    return evidence


def wait_for_leg(command, reader, *, collector, mission, cruise_mps,
                 offboard_streamer=None, fw_pass_tangent=None, on_passage=None,
                 wall=time.monotonic, sleep=time.sleep) -> dict[str, Any]:
    """按 PX4 仿真时间等待；另设墙钟与时钟停滞上限，实时检查任务围栏。"""
    _, home_ned = collector._home_reference(reader.state)
    command = bind_target(command, home_ned)
    revision = (mission.get("route_preflight") or {}).get("metrics", {}).get("flight_margin_revision")
    plane_pass = fw_pass_tangent is not None and revision != scenario_policy.SAMPLED_FLIGHT_MARGIN_REVISION
    target_relative = tuple(target - home for target, home in zip(command.target_ned, home_ned))

    def pose_state():
        # The reader is updated by another thread. Read the position message
        # once so offsets and sim_stamp_ns always describe the same pose.
        state = reader.state
        return {"LOCAL_POSITION_NED": state.get("LOCAL_POSITION_NED")} if plane_pass else state

    initial_state = pose_state()
    start = collector._state_position(initial_state)
    leg_n, leg_e = command.target_ned[0] - start[0], command.target_ned[1] - start[1]
    leg = math.hypot(leg_n, leg_e)
    sim_budget = leg / max(cruise_mps, 1.0) * 2.0 + 120.0
    wall_budget = min(float(mission["source"].get("max_wall_s", 14400.0)), sim_budget * 20.0 + 300.0)
    started = progress_wall = wall()
    stamp = collector._pose_sim_stamp_ns(initial_state)
    if stamp is None:
        raise RuntimeError("long-range flight requires a PX4 simulation clock")
    first_stamp = previous_stamp = stamp
    policy = mission["source"]["mission_compile_runtime"]["safety_policy"]
    center = GeoPoint.from_mapping(policy["geofence_center"])
    home = mission["geometry"].home
    center_n, center_e, _ = offset_ned(home, center)
    angle = 0.0
    previous_yaw = None
    previous_pose = None
    previous_pose_stamp = None
    previous_sample = None
    along = cross = None

    def reject(reason, **measurements):
        # Batch cleanup drops failed episodes, while flight.stdout.log survives.
        collector._progress(
            "long_range_guard", reason=reason, sim_stamp_ns=stamp,
            position_home_ned_m=relative, previous_position_home_ned_m=previous_pose,
            previous_sim_stamp_ns=previous_pose_stamp, target_home_ned_m=target_relative,
            distance_from_home_m=math.hypot(*relative[:2]),
            fly_through_radius_m=command.fly_through_radius_m,
            **({"along_past_target_m": along, "cross_track_m": cross} if fw_pass_tangent is not None else {}),
            **measurements,
        )
        raise RuntimeError(reason)

    # Retain the initial coherent sample even if the reader publishes again
    # before the first iteration.
    snapshot = initial_state
    while wall() - started < wall_budget:
        reader.raise_if_failed()
        if command.fly_through_radius_m is not None:
            _check_offboard_mode(collector, reader, offboard_streamer, wall=wall)
        current_state = snapshot if plane_pass and snapshot is not None else pose_state()
        snapshot = None
        stamp = collector._pose_sim_stamp_ns(current_state)
        if stamp is None or stamp < previous_stamp:
            raise RuntimeError("PX4 simulation clock missing or reset during long-range leg")
        if stamp > previous_stamp:
            progress_wall, previous_stamp = wall(), stamp
        if wall() - progress_wall > 60.0:
            raise TimeoutError("PX4 simulation clock stalled for 60 wall seconds")
        if (stamp - first_stamp) / 1e9 >= sim_budget:
            break
        pose = collector._state_position(current_state)
        relative = tuple(value - home for value, home in zip(pose, home_ned))
        dn, de = command.target_ned[0] - pose[0], command.target_ned[1] - pose[1]
        if fw_pass_tangent is not None:
            along, cross = scenario_policy.fw_target_offsets(fw_pass_tangent, (-dn, -de))
        boundary = mission["source"].get("boundary", {})
        if boundary and not (boundary["north_min_m"] <= relative[0] <= boundary["north_max_m"]
                             and boundary["east_min_m"] <= relative[1] <= boundary["east_max_m"]):
            reject("long-range telemetry crossed the square world boundary")
        if math.hypot(relative[0] - center_n, relative[1] - center_e) > float(policy["geofence_radius_m"]):
            reject("long-range telemetry crossed the configured geofence", radius_limit_m=policy["geofence_radius_m"])
        if math.hypot(*relative[:2]) > float(policy["maximum_mission_radius_m"]):
            reject("long-range telemetry exceeded the configured mission radius",
                   radius_limit_m=policy["maximum_mission_radius_m"])
        distance = math.hypot(dn, de)
        yaw = collector._state_yaw(reader.state)
        if previous_yaw is not None:
            angle += abs((yaw - previous_yaw + 180) % 360 - 180)
        previous_yaw = yaw
        if mission.get("terrain") is not None:
            if collector._flight_state_snapshot(reader.state)["vtol_state"] != 4:
                raise RuntimeError("VTOL left fixed-wing mode during long-range cruise")
            agl = _terrain_agl(mission["terrain"], relative)
            if not 30.0 <= agl <= MAX_CRUISE_AGL_M:
                reject(f"fixed-wing cruise left the 30–120 m DEM AGL envelope ({agl:.1f} m)",
                       agl_m=agl, dem_relative_home_m=-relative[2] - agl)
        passage = {}
        if plane_pass:
            # No early handoff and no extrapolation from a sample that starts
            # past the plane. A long gap can skip an entire short turn leg;
            # never credit that leg based on the preceding command's path.
            arrived = False
            if previous_sample is None or stamp > previous_sample[0]:
                sample = (stamp, along, cross)
                if along >= 0:
                    passage = _fw_plane_passage(previous_sample, sample, fw_pass_tangent, target_relative[:2])
                    if "passage_failure" in passage:
                        reject("fixed-wing cruise missed the bounded Offboard waypoint passage", **passage)
                    arrived = True
                previous_sample = sample
        elif fw_pass_tangent is not None:
            if along > scenario_policy.FW_PASS_ALONG_M:
                reject("fixed-wing cruise missed the bounded Offboard waypoint passage")
            arrived = along >= 0 and abs(cross) <= scenario_policy.FW_PASS_CROSS_M
        else:
            arrived = collector._fixed_wing_reposition_complete(
                dn=dn, de=de, leg_n=leg_n, leg_e=leg_e, distance_m=distance,
                loiter_radius_m=float(command.radius_m or 80.0), yaw_deg=yaw,
                centre_n=command.target_ned[0], centre_e=command.target_ned[1], angle_deg=angle,
                fly_through_radius_m=command.fly_through_radius_m)
        if arrived:
            if fw_pass_tangent is not None:
                collector._progress("long_range_waypoint_pass", sim_stamp_ns=stamp,
                                    position_home_ned_m=relative, along_past_target_m=along, cross_track_m=cross,
                                    **passage)
                # Sampled/radius-only arrivals need not have observed the plane crossing.
                if on_passage is not None and plane_pass:
                    on_passage({
                        **passage, "sim_stamp_ns": stamp,
                        "position_home_ned_m": relative,
                        "along_past_target_m": along, "cross_track_m": cross,
                    })
            return {"waited": "arrived", "distance_m": distance, "leg_m": leg,
                    "elapsed_s": wall() - started, "sim_elapsed_s": (stamp - first_stamp) / 1e9,
                    "sim_budget_s": sim_budget, "wall_budget_s": wall_budget,
                    **({"guidance": "offboard_path", "fly_through_radius_m": command.fly_through_radius_m}
                       if command.fly_through_radius_m is not None else {})}
        previous_pose, previous_pose_stamp = relative, stamp
        sleep(0.1)
    raise TimeoutError(f"long-range waypoint not reached within {sim_budget:.1f} sim seconds / {wall_budget:.1f} wall seconds")


@contextmanager
def collector_overrides(collector, mission):
    """覆盖仅存于独立 runner 进程，退出后恢复原 collector。"""
    original_policy = collector._SmokeFlightPolicy
    original_wait = collector._wait_motion_command
    original_executor = collector.FixedWingVtolExecutor
    geometry = mission["geometry"]
    offboard_streamer = None
    offboard_active = False
    fw_pass_tangent = None
    bounded_pass = (mission.get("route_preflight") or {}).get("metrics", {}).get(
        "flight_margin_revision") in scenario_policy.FW_PASS_REVISIONS

    class LongRangeExecutor(original_executor):
        has_fw_end_plane = bounded_pass

        def __init__(self, *args, **kwargs):
            nonlocal offboard_streamer
            super().__init__(*args, **kwargs)
            if mission.get("route_preflight") is not None:
                offboard_streamer = collector.SetpointStreamer(self.vehicle)
            self.offboard_streamer = offboard_streamer
            self.previous_fw_target = None

        def arm_and_takeoff(self, altitude_m=None):
            home_geo, home_ned = self._geo_reference()
            live = GeoPoint(float(home_geo[0]), float(home_geo[1]))
            if math.hypot(*offset_ned(geometry.home, live)[:2]) > 5.0:
                raise RuntimeError("live PX4 home differs from long-range scenario home by >5 m")
            # Takeoff's geographic conversion takes local-up, not home-relative up.
            absolute_up = float(altitude_m if altitude_m is not None else self.altitude_m) - home_ned[2]
            return super().arm_and_takeoff(absolute_up)

        def execute(self, action):
            nonlocal offboard_active
            if not isinstance(action, MotionCommand) or action.kind not in {"reposition", "mc_reposition", "mc_hold"}:
                if offboard_active and isinstance(action, MotionCommand) and action.kind == "transition":
                    _check_offboard_mode(collector, self.telemetry_reader, offboard_streamer)
                return super().execute(action)
            action = bind_target(action, self._geo_reference()[1])
            if action.fly_through_radius_m is not None:
                if offboard_streamer is None:
                    raise RuntimeError("FW fly-through requires route_preflight")
                return self._execute_fw_path(action)
            requested_ns = time.monotonic_ns()
            result = super().execute(action)
            if offboard_active and result.status == "success":
                mode = collector._wait_px4_mode(self.telemetry_reader, AUTO_LOITER_MODE,
                                                timeout_s=30.0, after_ns=requested_ns)
                if not mode["confirmed"]:
                    raise RuntimeError("PX4 did not confirm AUTO.LOITER after legacy reposition")
                offboard_streamer.stop()
                offboard_active = False
            if action.kind == "reposition" and result.status == "success":
                self.previous_fw_target = action.target_ned
            return result

        def _execute_fw_path(self, action):
            nonlocal offboard_active, fw_pass_tangent
            reader = self.telemetry_reader
            if collector._flight_state_snapshot(reader.state)["vtol_state"] != 4:
                raise RuntimeError("long-range Offboard path requires confirmed VTOL fixed-wing mode")
            started = self._clock()
            start = self.previous_fw_target or self._position()
            target = action.target_ned
            dn, de = target[0] - start[0], target[1] - start[1]
            length = math.hypot(dn, de)
            if length < 1.0:
                raise RuntimeError("long-range Offboard path requires a nonzero horizontal leg")
            # FW AUTO_PATH normalizes this vector to a tangent and uses PX4's
            # cruising airspeed. The line continues beyond the capture disk.
            tangent = (dn / length, de / length)
            if offboard_active:
                _check_offboard_mode(collector, reader, offboard_streamer)
            offboard_streamer.set_path_target(target, tangent)
            if not offboard_active:
                requested_ns = time.monotonic_ns()
                offboard_streamer.start()
                collector._wait_for_offboard(self.vehicle, reader, offboard_streamer, after_ns=requested_ns)
                _check_offboard_mode(collector, reader, offboard_streamer)
                offboard_active = True
            fw_pass_tangent = tangent if bounded_pass else None
            self.previous_fw_target = self._commanded = target
            return ExecutionResult("success", self._clock() - started,
                                   ack={"offboard_path": {"target_ned_m": target, "tangent_ne": tangent}})

    def wait(command, reader, cruise_mps, *, on_passage=None):
        if command.kind == "reposition":
            return wait_for_leg(command, reader, collector=collector, mission=mission, cruise_mps=cruise_mps,
                                offboard_streamer=offboard_streamer if offboard_active else None,
                                fw_pass_tangent=fw_pass_tangent if command.fly_through_radius_m is not None else None,
                                **({"on_passage": on_passage} if on_passage is not None else {}))
        if command.kind in {"mc_reposition", "mc_hold"}:
            command = bind_target(command, collector._home_reference(reader.state)[1])
        result = original_wait(command, reader, cruise_mps=cruise_mps)
        if offboard_active and command.kind == "transition" and command.mode == "MC" and result["waited"] == "transitioned":
            _check_offboard_mode(collector, reader, offboard_streamer)
            # Once MC is confirmed, stop supplying the wing's forward tangent
            # while the collector records the transition and schedules hover return.
            offboard_streamer.set_target(*collector._state_position(reader.state), collector._state_yaw(reader.state))
        if command.kind in {"mc_reposition", "mc_hold"} and result["waited"] != "settled":
            raise RuntimeError(f"VTOL hover return did not settle over home: {result['waited']}")
        return result

    collector._SmokeFlightPolicy = lambda **_kwargs: LongRangePolicy(mission["targets"], 80.0)
    collector.FixedWingVtolExecutor = LongRangeExecutor
    collector._wait_motion_command = wait
    try:
        yield
    finally:
        collector._SmokeFlightPolicy = original_policy
        collector._wait_motion_command = original_wait
        collector.FixedWingVtolExecutor = original_executor
        if offboard_streamer is not None:
            offboard_streamer.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slot", type=int, choices=(5, 6, 7))
    parser.add_argument("scenario", type=Path, nargs="?")
    parser.add_argument("out", type=Path, nargs="?")
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--airframe", choices=AIRFRAME_PROFILES,
                        help="select a named profile's airframe or verify an explicit scenario")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--gimbal-mode", choices=("lock", "follow", "track"), default="lock")
    parser.add_argument("--gimbal-bridge-stabilize", action="store_true",
                        help="send C11 pose targets to the horizon-enabled PX4 image")
    parser.add_argument("--gimbal-eval", action="store_true",
                        help="add a diagnostic target-tracking hover after the X1 MC return")
    parser.add_argument("--gimbal-eval-fw", action="store_true",
                        help="with --gimbal-eval: also score tracking of planned per-leg FW ground points")
    parser.add_argument("--image", help="local PX4/Gazebo runtime image")
    parser.add_argument("--track-target", type=WorldTarget.parse, action="append", default=[],
                        metavar="LAT,LON,ALT_M", help="world target, altitude AMSL metres (repeatable)")
    parser.add_argument("--gimbal-yaw-mode", choices=("lock", "follow"), default="follow")
    parser.add_argument("--horizon-video-hz", type=float,
                        help="override X1 video sampling rate in simulation seconds")
    parser.add_argument("--write-mission", action="store_true", help="为已有独立 scenario 生成可复现长航程 sidecar")
    parser.add_argument("--extent-m", type=float, default=6000.0)
    parser.add_argument("--min-rtf", type=float, default=None)
    args = parser.parse_args(track_cli_argv(sys.argv[1:] if argv is None else argv))
    if args.gimbal_eval and (not args.gimbal_bridge_stabilize or args.gimbal_mode != "lock"
                            or args.track_target or args.horizon_video_hz is not None):
        parser.error("--gimbal-eval requires bridge lock mode and the standard X1 target and capture rate")
    if args.gimbal_eval_fw and not args.gimbal_eval:
        parser.error("--gimbal-eval-fw requires --gimbal-eval")
    if args.gimbal_mode == "track" and not args.track_target:
        logging.getLogger(__name__).warning("track mode has no target; falling back to lock")
        args.gimbal_mode = "lock"
    if int(os.environ.get("MULTIDETECT_SITL_SLOTS", "8")) < 8:
        parser.error("long-range validation requires MULTIDETECT_SITL_SLOTS>=8")
    try:
        scenario = resolve_profile(args.profile, args.scenario, args.airframe)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    minimum_rtf = DEFAULT_MIN_RTF if args.min_rtf is None else args.min_rtf
    if not math.isfinite(minimum_rtf) or minimum_rtf < DEFAULT_MIN_RTF:
        parser.error(f"--min-rtf must be at least {DEFAULT_MIN_RTF:g}")
    out = (
        args.out.resolve()
        if args.out is not None
        else default_output_path(scenario, minimum_rtf).resolve()
    )
    try:
        if args.write_mission:
            if args.scenario is None:
                parser.error("--write-mission requires an explicit scenario")
            if args.airframe and _scenario_document(scenario)[1]["vehicle"]["model"] != args.airframe:
                parser.error("--airframe does not match the scenario runtime model")
            write_mission_sidecar(scenario, args.extent_m)
            return 0
        mission = load_mission(scenario, minimum_rtf=minimum_rtf)
        if mission["route_preflight"] is not None:
            _verify_route_stack(scenario, mission["route_preflight"], plan_only=args.plan_only)
        if args.airframe and args.airframe != mission["airframe"]:
            parser.error("--airframe does not match the scenario runtime model")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    plan = collection_plan(scenario, out, args.slot, mission)
    if args.image is not None:
        plan = replace(plan, image=args.image)
    is_vtol = mission["airframe"] == DELTA
    if args.gimbal_bridge_stabilize and not is_vtol:
        parser.error("--gimbal-bridge-stabilize requires the Delta C11 gimbal")
    if args.gimbal_mode == "track" and not is_vtol:
        parser.error("--gimbal-mode track requires the Delta C11 gimbal")
    if is_vtol:
        plan = replace(plan, gimbal_mode=args.gimbal_mode, track_targets=tuple(args.track_target),
                       gimbal_bridge_stabilize=args.gimbal_bridge_stabilize)
    fw_diagnostics = []
    if args.gimbal_eval:
        try:
            plan = gimbal_eval_plan(plan, mission, fw_legs=args.gimbal_eval_fw, fw_diagnostics=fw_diagnostics)
        except ValueError as exc:
            parser.error(str(exc))
    if args.horizon_video_hz is not None:
        if not is_vtol or not math.isfinite(args.horizon_video_hz) or args.horizon_video_hz <= 0:
            parser.error("--horizon-video-hz requires X1 and a finite positive rate")
        plan = replace(plan, video_hz=args.horizon_video_hz)
    from scripts import collect_sim_teacher_episodes as collector
    if args.plan_only:
        # A network-qualified command preview is opt-in via a known gateway;
        # a plain CPU plan never asks Docker to discover the bridge address.
        preview = bool(os.environ.get("MULTIDETECT_SIM_LISTEN_HOST", "").strip())
        with _preview_slot(args.slot):
            collector_plan = plan.to_mapping(include_docker_command=preview)
        print(json.dumps({"schema_version": 1, "mode": "cpu_plan_only", "flight_started": False,
                          "profile": mission["profile"], "airframe": mission["airframe"],
                          "scenario_class": mission["scenario_class"],
                          "minimum_rtf": mission["minimum_rtf"],
                          "minimum_max_distance_m": mission["minimum_max_distance_m"],
                          "output": str(out),
                          "planned_max_distance_m": mission["planned_max_distance_m"],
                          "planned_agl_envelope_m": mission["planned_agl_envelope_m"],
                          "fw_waypoints_home_ned_m": mission["targets"] if is_vtol else None,
                          "fw_turn_envelope": mission["fw_turn_envelope"],
                          **({"route_preflight": mission["route_preflight"],
                              "fw_guidance": "offboard_path"} if mission["route_preflight"] else {}),
                          "gimbal_stabilization": ({"mode": args.gimbal_mode,
                                                     "source": "bridge" if args.gimbal_bridge_stabilize else "host",
                                                     "yaw_mode": args.gimbal_yaw_mode,
                                                     "track_targets": [asdict(point) for point in plan.track_targets],
                                                     "target_pitch_deg": mission["gimbal_pitch_deg"]}
                                                    if is_vtol else None),
                          "compiled_mission_uploaded": False,
                          "compiled_mission": mission["compiled"].to_mapping(),
                          **({"gimbal_eval": {"track_step": plan.gimbal_eval_track_step,
                                              "hover_sim_s": GIMBAL_EVAL_HOLD_SIM_S,
                                              "fw_legs": plan.gimbal_eval_fw_legs,
                                              **({"switch_limit_deg": X1_FW_SWITCH_LIMIT_DEG,
                                                  "fw_switches": fw_diagnostics} if args.gimbal_eval_fw else {})}}
                             if args.gimbal_eval else {}),
                          "collector_plan": collector_plan},
                         ensure_ascii=False, indent=2))
        return 0
    if Path(sys.executable).resolve() != Path(SITL_PYTHON).resolve():
        parser.error(f"live flight must use {SITL_PYTHON}")
    out.mkdir(parents=True, exist_ok=False)
    (out / "compiled-mission.json").write_text(json.dumps(mission["compiled"].to_mapping(), indent=2) + "\n")
    with sitl_lock_context(SitlLock(label="rw-long-range", slots=8, slot=args.slot, retry_s=20.0)):
        with collector_overrides(collector, mission):
            if is_vtol:
                policy = LongRangePolicy(
                    mission["targets"], mission["geometry"].loiter_radius_m, vtol=True,
                    fw_speed_mps=mission["fw_turn_envelope"]["guided_ground_speed_mps"],
                    gimbal_pitch_deg=mission["gimbal_pitch_deg"],
                    gimbal_mode=args.gimbal_mode, gimbal_yaw_mode=args.gimbal_yaw_mode,
                    track_targets=plan.track_targets, gimbal_eval=args.gimbal_eval,
                    fw_eval_switch_angles_deg=(tuple(row["switch_los_deg"] for row in fw_diagnostics)
                                               if plan.gimbal_eval_fw_legs else None),
                    fly_through_radius_m=(mission["route_preflight"] or {}).get("fly_through_radius_m"),
                )
                episode = collector.run_episode(plan, Path(plan.truth), 0,
                                                policy_override=policy, land_after_route=True)
            else:
                episode = collector.run_episode(plan, Path(plan.truth), 0)
    from scripts.smoke_airframe_flight import _jsonl, _mav_value
    rows = _jsonl(episode / "mavlink.jsonl") or _jsonl(episode / "mavlink.jsonl.gz")
    positions = [(float(_mav_value(row, "x")), float(_mav_value(row, "y"))) for row in rows
                 if str(row.get("message_type", "")).upper() == "LOCAL_POSITION_NED"]
    maximum = max((math.dist(positions[0], p) for p in positions), default=0.0)
    metadata = json.loads((episode / "episode.json").read_text())
    steps = _jsonl(episode / "steps.jsonl")
    route_ok = len(steps) >= len(mission["targets"]) and all(
        row.get("outcome", {}).get("status") == "success" for row in steps)
    capture_ok = True
    if is_vtol:
        route_ok = route_ok and len(steps) == plan.steps_per_episode and any(
            row.get("message_type") == "collector_landing_complete"
            and row.get("flight_state", {}).get("landed_state") == 1
            and row.get("flight_state", {}).get("vtol_state") == 3 for row in rows)
        video = metadata.get("video") or {}
        capture_ok = (video.get("frame_count", 0) >= 2 and video.get("truncated") is False
                      and (metadata.get("gimbal_check") or {}).get("passed") is True)
    passed = (metadata.get("status") == "complete" and route_ok and capture_ok
              and maximum >= mission["minimum_max_distance_m"])
    result = {"status": "pass" if passed else "fail", "episode": str(episode),
              "airframe": mission["airframe"],
              "scenario_class": mission["scenario_class"],
              "max_distance_from_spawn_m": maximum, "route_completed": route_ok,
              "minimum_max_distance_m": mission["minimum_max_distance_m"],
              "minimum_rtf": mission["minimum_rtf"],
              "rtf_status": "pending_stats_probe",
              "execution": ("vertical climb, FW transition and guided waypoints, MC transition, hover return, vertical land"
                            if is_vtol else "guided reposition waypoints; collector RTL/LAND teardown"),
              "compiled_mission_uploaded": False}
    if is_vtol:
        result.update(capture_ok=capture_ok, cruise_video=metadata.get("video"),
                      gimbal_check=metadata.get("gimbal_check"), fw_turn_envelope=mission["fw_turn_envelope"])
    if is_vtol and args.gimbal_mode == "track":
        from scripts.realworld.horizon_report import summarize

        try:
            track_acceptance = summarize(episode, expected_mode="track")["track_acceptance"]
        except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            track_acceptance = {"passed": False, "errors": ["track_data_missing"], "error": str(exc)}
        result["track_acceptance"] = track_acceptance
        passed = passed and track_acceptance["passed"]
        result["status"] = "pass" if passed else "fail"
    (out / "long-range-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
