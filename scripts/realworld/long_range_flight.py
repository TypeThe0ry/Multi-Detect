#!/usr/bin/env python3
"""独立固定翼长航程入口；CPU plan 不租锁、不启动 Docker 或 GPU。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import ContextSnapshot, GeoPoint, ResolvedTaskSpec, TaskGeometry, TaskType, TaskConstraints, PlanAction  # noqa: E402
from multidetect.autonomy.motion_intent import MotionCommand  # noqa: E402
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.mission_planning import compile_planned_task_mission_from_runtime  # noqa: E402
from multidetect.px4_mission import FixedWingMissionGeometry  # noqa: E402
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402
from scripts.realworld.leased_flight import SITL_PYTHON  # noqa: E402
from scripts.sitl_lock import SitlLock, sitl_lock_context  # noqa: E402

AIRFRAME = "rc_cessna_mono_cam"


def sidecar_path(scenario: Path) -> Path:
    return scenario.with_name(scenario.name.removesuffix(".scenario.json") + ".long_range.json")


def offset_ned(home: GeoPoint, point: GeoPoint) -> tuple[float, float, float]:
    radius = 6_378_137.0
    return (math.radians(point.latitude_deg - home.latitude_deg) * radius,
            math.radians(point.longitude_deg - home.longitude_deg) * radius * math.cos(math.radians(home.latitude_deg)),
            -float(point.altitude_m or 0.0))


def load_mission(scenario: Path) -> dict[str, Any]:
    data = json.loads(sidecar_path(scenario).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("long_range sidecar requires schema_version=1")
    runtime = data["mission_compile_runtime"]
    geometry = FixedWingMissionGeometry.from_runtime_mapping(runtime)
    task = ResolvedTaskSpec.from_mapping(data["task"])
    context = ContextSnapshot.from_mapping(data["context"])
    profile = json.loads((ROOT / "configs/airframes/rc_cessna_runway.json").read_text())
    compiled = compile_planned_task_mission_from_runtime(
        task, context, "fixed-wing", runtime=runtime,
        airframe_profile_hash=canonical_sha256(profile), mission_id=f"{scenario.stem}-long-range")
    targets = [offset_ned(geometry.home, point) for point in (*geometry.waypoints, geometry.landing_approach)]
    maximum = max(math.hypot(p[0], p[1]) for p in targets)
    minimum = float(data.get("minimum_max_distance_m", 4000.0))
    if minimum < 4000.0 or maximum < minimum:
        raise ValueError("long-range mission must reach at least 4000 m from spawn")
    extent = float(data["extent_m"])
    if not math.isfinite(extent) or extent < 6000:
        raise ValueError("long-range extent_m must be at least 6000")
    return {"source": data, "geometry": geometry, "compiled": compiled,
            "targets": targets, "minimum_max_distance_m": minimum,
            "planned_max_distance_m": maximum}


def write_mission_sidecar(scenario: Path, extent_m: float = 6000.0) -> Path:
    """以独立场景的地图原点派生航线，保留中央已整平出生点。"""
    from scripts import collect_sim_teacher_episodes as collector
    runtime_path, _ = collector._scenario_channels(scenario)
    scenario_runtime = json.loads(runtime_path.read_text())
    dem_name = scenario_runtime["world"]["dem_asset"].removeprefix("builtin://terrain/")
    dem = json.loads((ROOT / "simulation/terrain" / dem_name).read_text())
    origin = dem["origin"]
    home = GeoPoint(origin["latitude_deg"], origin["longitude_deg"])
    if extent_m < 6000.0:
        raise ValueError("4 km route requires extent_m>=6000")
    half = extent_m / 2.0
    def point(east, north, altitude=100.0):
        return GeoPoint(home.latitude_deg + math.degrees(north / 6378137.0),
                        home.longitude_deg + math.degrees(east / (6378137.0 * math.cos(math.radians(home.latitude_deg)))),
                        altitude, "relative_home")
    scale = extent_m / 6000.0
    waypoints = tuple(point(e * scale, n * scale) for e, n in (
        (1000, 0), (2300, 1300), (2800, 2400), (2850, 2850),
        (2400, 2800), (1300, 2300), (0, 1200), (-800, 500), (-800, 0)))
    from multidetect.px4_mission import Px4FixedWingSafetyPolicy
    geometry = FixedWingMissionGeometry(
        home=home, waypoints=waypoints, landing_approach=point(-400, 0, 40),
        landing=point(0, 0, 0), takeoff_altitude_m=40.0, acceptance_radius_m=25.0,
        takeoff_heading_deg=90.0, landing_heading_deg=90.0)
    policy = Px4FixedWingSafetyPolicy(
        geofence_center=home, geofence_radius_m=half * math.sqrt(2),
        maximum_mission_radius_m=half * math.sqrt(2) - 50,
        allowed_airframe_profile_ids=("px4_fixed_wing_gazebo_v1",),
        cruise_airspeed_mps=15.0, minimum_airspeed_mps=10.0)
    task = ResolvedTaskSpec(
        task_id=f"{scenario_runtime['scenario_id']}-long-range", task_type=TaskType.SURVEY,
        geometry=TaskGeometry(kind="map_feature", resolved_feature_id="long-range-circuit",
            direction="clockwise", length_m=sum(math.dist(offset_ned(home, a)[:2], offset_ned(home, b)[:2]) for a, b in zip((home, *waypoints, home), (waypoints[0], *waypoints[1:], home, home), strict=True)), centerline=(home, *waypoints, home),
            resolution_confidence=1.0, buffer_m=0.0), target_classes=("terrain",),
        on_detection=(), constraints=TaskConstraints(30.0, 3600.0, True),
        completion_action=PlanAction.RETURN)
    context = ContextSnapshot(snapshot_id="offline-long-range-feasibility", captured_at_s=0.0,
        captured_at_utc="2026-11-15T00:00:00Z", aircraft_position=home, home_position=home,
        airframe_profile_id="px4_fixed_wing_gazebo_v1", battery_remaining_pct=100.0,
        estimated_flight_time_s=3600.0, ground_speed_mps=15.0, airspeed_mps=15.0,
        wind_north_mps=0.0, wind_east_mps=0.0, gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0, geofence_valid=True, camera_available=True,
        perception_available=True, map_cache_available=True, dem_available=True)
    body = {"schema_version": 1, "extent_m": extent_m, "minimum_max_distance_m": 4000.0,
        "max_wall_s": 14400.0, "mission_compile_runtime": {"schema_version": 1,
            "vehicle_class": "fixed-wing", **geometry.to_mapping(), "safety_policy": policy.to_mapping()},
        "task": task.to_mapping(), "context": context.to_mapping(),
        "context_scope": "offline assumed healthy SIM inputs; not observed telemetry",
        "sim_params": {"GF_MAX_HOR_DIST": half * math.sqrt(2), "GF_MAX_VER_DIST": 180,
                       "GF_ACTION": 1, "BAT_N_CELLS": 3},
        "boundary": {"east_min_m": -half, "east_max_m": half,
                     "north_min_m": -half, "north_max_m": half}}
    destination = sidecar_path(scenario)
    destination.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    load_mission(scenario)  # Compiler validates route, turns, approach and energy before delivery.
    return destination


def collection_plan(scenario: Path, out: Path, slot: int, mission: dict[str, Any]):
    from scripts import collect_sim_teacher_episodes as collector
    runtime, truth = collector._scenario_channels(scenario)
    collector._validate_runtime(runtime, AIRFRAME)
    geometry = mission["geometry"]
    seconds = float(mission["source"].get("max_wall_s", 14400.0))
    return collector.CollectionPlan(
        str(scenario), str(runtime), str(truth), "explore", 1,
        len(mission["targets"]) + 1, 91000, str(out), airframe=AIRFRAME,
        smoke_flight=True, container_prefix=f"md-rw-long-slot-{slot}",
        control_port=14560 + 10 * slot, telemetry_port=14561 + 10 * slot,
        timeout_s=seconds, max_episode_s=seconds,
        takeoff_altitude_m=geometry.takeoff_altitude_m,
        extra_sim_params=tuple((str(k), str(v)) for k, v in mission["source"].get("sim_params", {}).items()))


class LongRangePolicy:
    def __init__(self, targets: list[tuple[float, float, float]], radius: float) -> None:
        self.targets = iter(targets)
        self.radius = radius

    def next_action(self):
        target = next(self.targets, None)
        if target is None:
            return ActionPrimitive.stop()
        return MotionCommand("reposition", target_ned=target, radius_m=self.radius,
                             action=ActionPrimitive.hold(1.0),
                             metadata={"long_range_home_relative": True})

    def close(self) -> None:
        return None


def bind_target(command: MotionCommand, home_ned: tuple[float, float, float]) -> MotionCommand:
    if command.target_ned is None:
        raise ValueError("long-range reposition requires a target")
    return replace(command, target_ned=tuple(a + b for a, b in zip(command.target_ned, home_ned, strict=True)))


def wait_for_leg(command, reader, *, collector, mission, cruise_mps,
                 wall=time.monotonic, sleep=time.sleep) -> dict[str, Any]:
    """按 PX4 仿真时间等待；另设墙钟与时钟停滞上限，实时检查任务围栏。"""
    _, home_ned = collector._home_reference(reader.state)
    command = bind_target(command, home_ned)
    start = collector._state_position(reader.state)
    leg_n, leg_e = command.target_ned[0] - start[0], command.target_ned[1] - start[1]
    leg = math.hypot(leg_n, leg_e)
    sim_budget = leg / max(cruise_mps, 1.0) * 2.0 + 120.0
    wall_budget = min(float(mission["source"].get("max_wall_s", 14400.0)), sim_budget * 20.0 + 300.0)
    started = progress_wall = wall()
    stamp = collector._pose_sim_stamp_ns(reader.state)
    if stamp is None:
        raise RuntimeError("long-range flight requires a PX4 simulation clock")
    first_stamp = previous_stamp = stamp
    policy = mission["source"]["mission_compile_runtime"]["safety_policy"]
    center = GeoPoint.from_mapping(policy["geofence_center"])
    home = mission["geometry"].home
    center_n, center_e, _ = offset_ned(home, center)
    angle = 0.0
    previous_yaw = None
    while wall() - started < wall_budget:
        reader.raise_if_failed()
        stamp = collector._pose_sim_stamp_ns(reader.state)
        if stamp is None or stamp < previous_stamp:
            raise RuntimeError("PX4 simulation clock missing or reset during long-range leg")
        if stamp > previous_stamp:
            progress_wall, previous_stamp = wall(), stamp
        if wall() - progress_wall > 60.0:
            raise TimeoutError("PX4 simulation clock stalled for 60 wall seconds")
        if (stamp - first_stamp) / 1e9 >= sim_budget:
            break
        pose = collector._state_position(reader.state)
        relative = (pose[0] - home_ned[0], pose[1] - home_ned[1])
        boundary = mission["source"].get("boundary", {})
        if boundary and not (boundary["north_min_m"] <= relative[0] <= boundary["north_max_m"]
                             and boundary["east_min_m"] <= relative[1] <= boundary["east_max_m"]):
            raise RuntimeError("long-range telemetry crossed the square world boundary")
        if math.hypot(relative[0] - center_n, relative[1] - center_e) > float(policy["geofence_radius_m"]):
            raise RuntimeError("long-range telemetry crossed the configured geofence")
        if math.hypot(*relative) > float(policy["maximum_mission_radius_m"]):
            raise RuntimeError("long-range telemetry exceeded the configured mission radius")
        dn, de = command.target_ned[0] - pose[0], command.target_ned[1] - pose[1]
        distance = math.hypot(dn, de)
        yaw = collector._state_yaw(reader.state)
        if previous_yaw is not None:
            angle += abs((yaw - previous_yaw + 180) % 360 - 180)
        previous_yaw = yaw
        if collector._fixed_wing_reposition_complete(
            dn=dn, de=de, leg_n=leg_n, leg_e=leg_e, distance_m=distance,
            loiter_radius_m=float(command.radius_m or 80.0), yaw_deg=yaw,
            centre_n=command.target_ned[0], centre_e=command.target_ned[1], angle_deg=angle):
            return {"waited": "arrived", "distance_m": distance, "leg_m": leg,
                    "elapsed_s": wall() - started, "sim_elapsed_s": (stamp - first_stamp) / 1e9,
                    "sim_budget_s": sim_budget, "wall_budget_s": wall_budget}
        sleep(0.1)
    raise TimeoutError(f"long-range waypoint not reached within {sim_budget:.1f} sim seconds / {wall_budget:.1f} wall seconds")


@contextmanager
def collector_overrides(collector, mission):
    """覆盖仅存于独立 runner 进程，退出后恢复原 collector。"""
    original_policy = collector._SmokeFlightPolicy
    original_wait = collector._wait_motion_command
    original_executor = collector.FixedWingVtolExecutor
    geometry = mission["geometry"]

    class LongRangeExecutor(original_executor):
        def arm_and_takeoff(self, altitude_m=None):
            home_geo, _ = self._geo_reference()
            live = GeoPoint(float(home_geo[0]), float(home_geo[1]))
            if math.hypot(*offset_ned(geometry.home, live)[:2]) > 5.0:
                raise RuntimeError("live PX4 home differs from long-range scenario home by >5 m")
            return super().arm_and_takeoff(altitude_m)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._long_targets = iter(mission["targets"])

        def execute(self, action):
            # collector unwraps MotionCommand.action before invoking the executor;
            # convert that hold placeholder into the next absolute route leg.
            if action.kind == "hold":
                target = next(self._long_targets, None)
                if target is not None:
                    current = self._position()
                    dn, de = target[0] - current[0], target[1] - current[1]
                    yaw = math.radians(self._yaw())
                    forward = dn * math.cos(yaw) + de * math.sin(yaw)
                    right = -dn * math.sin(yaw) + de * math.cos(yaw)
                    action = ActionPrimitive.translate(forward, right, 0.0)
            return super().execute(action)

    collector._SmokeFlightPolicy = lambda: LongRangePolicy(mission["targets"], 80.0)
    collector.FixedWingVtolExecutor = LongRangeExecutor
    collector._wait_motion_command = lambda command, reader, cruise_mps: wait_for_leg(
        command, reader, collector=collector, mission=mission, cruise_mps=cruise_mps)
    try:
        yield
    finally:
        collector._SmokeFlightPolicy = original_policy
        collector._wait_motion_command = original_wait
        collector.FixedWingVtolExecutor = original_executor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slot", type=int, choices=(5, 6, 7))
    parser.add_argument("scenario", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--write-mission", action="store_true", help="为已有独立 scenario 生成可复现长航程 sidecar")
    parser.add_argument("--extent-m", type=float, default=6000.0)
    args = parser.parse_args(argv)
    if int(os.environ.get("MULTIDETECT_SITL_SLOTS", "8")) < 8:
        parser.error("long-range validation requires MULTIDETECT_SITL_SLOTS>=8")
    scenario, out = args.scenario.resolve(), args.out.resolve()
    if args.write_mission:
        write_mission_sidecar(scenario, args.extent_m)
        return 0
    mission = load_mission(scenario)
    plan = collection_plan(scenario, out, args.slot, mission)
    from scripts import collect_sim_teacher_episodes as collector
    if args.plan_only:
        os.environ["MULTIDETECT_SITL_SLOT"] = str(args.slot)
        print(json.dumps({"schema_version": 1, "mode": "cpu_plan_only", "flight_started": False,
                          "planned_max_distance_m": mission["planned_max_distance_m"],
                          "compiled_mission": mission["compiled"].to_mapping(),
                          "collector_plan": plan.to_mapping()}, ensure_ascii=False, indent=2))
        return 0
    if Path(sys.executable).resolve() != Path(SITL_PYTHON).resolve():
        parser.error(f"live flight must use {SITL_PYTHON}")
    out.mkdir(parents=True, exist_ok=False)
    (out / "compiled-mission.json").write_text(json.dumps(mission["compiled"].to_mapping(), indent=2) + "\n")
    with sitl_lock_context(SitlLock(label="rw-long-range", slots=8, slot=args.slot, retry_s=20.0)):
        with collector_overrides(collector, mission):
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
    passed = metadata.get("status") == "complete" and route_ok and maximum >= mission["minimum_max_distance_m"]
    result = {"status": "pass" if passed else "fail", "episode": str(episode),
              "max_distance_from_spawn_m": maximum, "route_completed": route_ok,
              "minimum_max_distance_m": mission["minimum_max_distance_m"],
              "execution": "guided reposition waypoints; collector RTL/LAND teardown",
              "compiled_mission_uploaded": False}
    (out / "long-range-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
