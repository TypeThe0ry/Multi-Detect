#!/usr/bin/env python3
"""Plan and execute one envelope-checked natural-language X1 mission in SITL."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.airframe_profile import load_airframe_profile  # noqa: E402
from multidetect.autonomy.contracts import GeoPoint  # noqa: E402
from multidetect.autonomy.mission_package import (  # noqa: E402
    check_plan_against_package, effective_no_fly_radius, load_mission_package,
)
from multidetect.autonomy.mission_plan import PlanDecision  # noqa: E402
from multidetect.autonomy.mission_planner import compile_mission_plan, plan_json_schema  # noqa: E402
from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads  # noqa: E402
from multidetect.autonomy.x1_mission import AGL_MIN_M, X1PlanError, build_x1_mission_steps  # noqa: E402
from multidetect.autonomy.x1_perception_tools import (  # noqa: E402
    GimbalReading, PerceptionSources, RouteReading, ToolRunner,
)
from multidetect.autonomy.x1_plan import (  # noqa: E402
    X1_CAPABILITY, one_waypoint_primitives_input, x1_planner_context,
)
from multidetect.terrain_dem import LocalDemGrid, load_local_dem_grid  # noqa: E402
from multidetect.sim_teacher.x1_live_clearance import (  # noqa: E402
    MOTION_SAMPLE_MAX_AGE_MS, LiveContinueEvidence, clearance_scope,
)
from multidetect.sim_teacher.x1_live_evidence_builder import build_live_evidence  # noqa: E402
from multidetect.sim_teacher.x1_scenario import (  # noqa: E402
    SUPPORTED, CollectorScenario, load_schedule, schedule_sha256, wind_server_config,
)
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, llama_decisions  # noqa: E402
from scripts import generate_x1_assumed_sim_bounds as assumptions  # noqa: E402
from scripts.realworld import long_range_flight as long_range  # noqa: E402
from scripts.realworld import x1_site_evidence  # noqa: E402

AIRFRAME = "delta_tailsitter_gimbal_cam"
IMAGE = "multidetect/px4-sitl-gazebo:gz-rendering-830-r1-gimbal-horizon4"
PROFILE = ROOT / "configs/airframes/delta_tailsitter_gimbal.json"
SLOTS = (5, 6, 7)


def read_object(path: Path) -> dict:
    value = strict_json_loads(path.read_text(encoding="utf-8"), name=str(path))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _finite(value, counter):
    """Non-finite telemetry (e.g. PX4 airspeed NaN after sensor invalidation) is recorded as null, and counted."""
    if isinstance(value, float) and not math.isfinite(value):
        counter[0] += 1
        return None
    if isinstance(value, dict):
        return {key: _finite(item, counter) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item, counter) for item in value]
    return value


def write_result(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    counter = [0]
    value = _finite(value, counter)
    if counter[0]:
        value["nonfinite_values_nulled"] = counter[0]
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


@dataclass(frozen=True)
class Site:
    scenario: Path
    scenario_id: str
    world_sdf: Path
    dem_path: Path
    terrain: LocalDemGrid
    world_sha256: str
    home: GeoPoint
    source: dict


def load_site(world: str, scenario: Path) -> Site:
    from scripts import collect_sim_teacher_episodes as collector
    from multidetect.simulator.stack import parse_builtin_world_uri

    manifest = read_object(scenario)
    runtime_path, _ = collector._scenario_channels(scenario)
    runtime = collector._validate_runtime(runtime_path, AIRFRAME)
    scenario_id = manifest["scenario_id"]
    if (not re.fullmatch(r"rw_[a-z0-9_]+", world) or
            scenario_id.split("__", 1)[0] != world or runtime["scenario_id"] != scenario_id
            or runtime["vehicle"]["model"] != AIRFRAME):
        raise ValueError("scenario, runtime, world and X1 airframe must agree")
    world_sdf = collector.scenario_world_path(runtime_path)
    if world_sdf is None or not world_sdf.is_file():
        raise ValueError(f"scenario world SDF unavailable: {scenario_id}")
    parse_builtin_world_uri(runtime["world"]["world_asset"], repository_root=ROOT, scenario_id=scenario_id)
    source = read_object(long_range.sidecar_path(scenario))
    long_range._validate_profile(scenario, source, manifest, runtime, minimum_rtf=None)
    dem_path = long_range._dem_path(runtime).resolve()
    terrain = load_local_dem_grid(dem_path)
    if terrain.scenario_id != scenario_id:
        raise X1PlanError("SITE_DEM", "DEM belongs to a different scenario")
    home = GeoPoint.from_mapping(source["mission_compile_runtime"]["home_position"])
    with world_sdf.open("rb") as stream:
        world_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return Site(scenario, scenario_id, world_sdf, dem_path, terrain, world_sha256, home, source)


def recorded_backend(path: Path):
    return lambda _prompt, _schema: path.read_text(encoding="utf-8")


def checkpoint_backend(args):
    from scripts.eval_slow_system import LlamaServerRunner

    def post(prompt, schema):
        runner = LlamaServerRunner(
            args.checkpoint, None, executable=args.llama_server, port=args.planner_port,
            max_new_tokens=768, device=args.planner_device, cache_ram_mib=0,
            startup_timeout_s=args.planner_timeout_s, log_dir=args.output / "planner-logs",
        )
        try:
            response = runner._http_json(
                f"http://127.0.0.1:{runner.port}/v1/chat/completions",
                {"model": "local", "temperature": 0, "max_tokens": 768,
                 "messages": [{"role": "user", "content": prompt}],
                 "response_format": {"type": "json_schema", "json_schema": {
                     "name": "mission_plan", "strict": True, "schema": schema}}},
                timeout=args.planner_timeout_s,
            )
            return response["choices"][0]["message"].get("content", "")
        finally:
            runner.close()

    return post


def compile_plan(package, post, record):
    from scripts.slow_system_runtime import trained_key_order_schema

    capabilities = frozenset(package.document["capabilities"])
    if X1_CAPABILITY not in capabilities:
        raise ValueError("mission package must enable airframe:x1")
    context = x1_planner_context(package)
    schema = trained_key_order_schema(plan_json_schema(
        capabilities=capabilities, include_decision_contract=True))
    backend_error = None

    def tracked_post(prompt, schema):
        nonlocal backend_error
        try:
            return post(prompt, schema)
        except Exception as exc:
            backend_error = exc
            raise

    plan, report = compile_mission_plan(
        tracked_post, package.document["task_text"], refs=tuple(package.refs), schema=schema,
        capabilities=capabilities, airframe=AIRFRAME, include_decision_contract=True, attempts=1,
        plan_checker=lambda candidate: check_plan_against_package(candidate, package, capabilities=capabilities),
        x1_context=context,
    )
    if report["raw"]:
        record["raw_planner_output"] = report["raw"]
        try:
            record["plan_json"] = strict_json_loads(report["raw"], name="X1 planner output")
        except ValueError:
            pass
    if backend_error is not None:
        cause = getattr(backend_error, "reason", backend_error)
        code = "PLANNER_TIMEOUT" if isinstance(cause, TimeoutError) else "PLANNER_ERROR"
        raise X1PlanError(code, str(backend_error)) from backend_error
    if isinstance(plan, PlanDecision):
        raise X1PlanError(f"PLANNER_{plan.decision.upper()}", f"{plan.reason_code}: {plan.reason}")
    if plan is None:
        findings = report.get("package_violations") or report.get("intent_violations") or []
        record["validator"]["findings"] = findings
        raise X1PlanError(findings[0]["code"] if findings else "INVALID_PLAN", report["error"])
    return plan, capabilities


def prepare(package, plan, capabilities, site, *, output, slot):
    """Bind the validated mission to the site's DEM and the collector's SIM inputs."""
    view, adapted, radius = one_waypoint_primitives_input(plan, package, capabilities)
    profile = load_airframe_profile(PROFILE)
    home_ne = tuple(view.document["home"])
    if math.dist(home_ne, site.terrain.local_from_wgs84(site.home.latitude_deg, site.home.longitude_deg)) > 0.01:
        raise X1PlanError("SITE_HOME", "package home differs from the scenario home in the DEM frame")
    if view.document["x1_planning"]["world"] not in {site.scenario_id, site.scenario_id.split("__", 1)[0]}:
        raise X1PlanError("SITE_WORLD", "package world differs from the selected scenario")
    area = view.document["operating_area"]
    boundary = site.source["boundary"]
    site_policy = site.source["mission_compile_runtime"]["safety_policy"]
    center_ne = long_range.offset_ned(site.home, GeoPoint.from_mapping(site_policy["geofence_center"]))[:2]
    for axis, home in zip(("north", "east"), home_ne, strict=True):
        if (area[f"{axis}_min_m"] - home < boundary[f"{axis}_min_m"] or
                area[f"{axis}_max_m"] - home > boundary[f"{axis}_max_m"]):
            raise X1PlanError("SITE_FENCE", "package corridor exceeds the site boundary")
    if (radius > site_policy["maximum_mission_radius_m"] or
            radius + math.hypot(*center_ne) > site_policy["geofence_radius_m"]):
        raise X1PlanError("SITE_FENCE", "package radius exceeds the site geofence")
    steps = build_x1_mission_steps(view, plan=adapted, terrain=site.terrain, profile=profile, mission_radius_m=radius)
    height = steps[0].takeoff_height_m
    if not site_policy["minimum_relative_altitude_m"] <= height <= site_policy["maximum_relative_altitude_m"]:
        raise X1PlanError("SITE_ALTITUDE", "planned altitude exceeds the site relative altitude limits")
    safety = {"corridor_dem_ne": view.document["x1_planning"]["corridor"], "home_dem_ne_m": home_ne,
              "radius_limit_m": radius, "cruise_agl_limits_m": [40.0, 120.0],
              "no_fly_circles_dem_ne": view.no_fly_zones, "budget": view.document["budget"],
              "transition_height_margin_m": height - profile.transition.minimum_height_m}
    source = {
        "boundary": {axis + end: area[axis + end] - home
                     for axis, home in zip(("north", "east"), home_ne, strict=True)
                     for end in ("_min_m", "_max_m")},
        "mission_compile_runtime": {"safety_policy": {**site_policy, "maximum_mission_radius_m": radius}},
        "capture_interval_sim_s": site.source["capture_interval_sim_s"],
        "max_wall_s": site.source.get("max_wall_s", 14400.0),
    }
    cruise = steps[2].command
    observed_margins = {"sample_count": 0, "minimum_margins_m": {}}

    def observe_cruise(position):
        margins = cruise_margins(position, safety, site.terrain)
        observed_margins["sample_count"] += 1
        minima = observed_margins["minimum_margins_m"]
        for name, value in margins.items():
            if value is not None:
                minima[name] = min(minima.get(name, value), value)
        return cruise_guard(margins, safety)

    mission = {"airframe": AIRFRAME, "source": source, "targets": [cruise.target_ned],
               "geometry": SimpleNamespace(home=site.home, takeoff_altitude_m=height),
               "terrain": site.terrain, "x1_cruise_guard": observe_cruise, "observed_margins": observed_margins}
    with long_range._preview_slot(slot):
        (output / "flight").mkdir(parents=True, exist_ok=True)
        collector_plan = long_range.collection_plan(site.scenario, output / "flight", slot, mission)
    return replace(collector_plan, steps_per_episode=sum(step.command is not None for step in steps),
                   task_text=package.document["task_text"], image=IMAGE), steps, safety, mission


def cruise_margins(position, safety, terrain):
    n, e, down = position
    hn, he = safety["home_dem_ne_m"]
    area = safety["corridor_dem_ne"]
    point = hn + n, he + e
    agl = terrain.sample_local(hn, he).elevation_m - terrain.sample_local(*point).elevation_m - down
    return {
        "radius_m": safety["radius_limit_m"] - math.hypot(n, e),
        "corridor_m": min(point[0] - area["north_min_m"], area["north_max_m"] - point[0],
                          point[1] - area["east_min_m"], area["east_max_m"] - point[1]),
        "no_fly_m": min((math.dist(point, center) - effective_no_fly_radius(radius)
                         for center, radius in safety["no_fly_circles_dem_ne"]), default=None),
        "min_agl_m": agl - safety["cruise_agl_limits_m"][0],
        "max_agl_m": safety["cruise_agl_limits_m"][1] - agl,
    }


def cruise_guard(margins, safety):
    for name, margin in margins.items():
        if margin is not None and margin < 0:
            raise ValueError(f"X1 cruise exceeded {name}: margin {margin:.2f} m")
    return margins["min_agl_m"] + safety["cruise_agl_limits_m"][0]


class MissionPolicy:
    controls_vtol_transition = True

    def __init__(self, steps, pitch):
        self.commands = iter(step.command for step in steps if step.command is not None)
        self.cruise_gimbal_target = {"pitch": pitch, "yaw": 0.0, "roll": 0.0}
        self.gimbal_mode = "lock"
        self.gimbal_yaw_mode = "follow"
        self.track_targets = ()

    def next_action(self):
        return next(self.commands)


def forward_clearance(steps, dem):
    commands = tuple(step.command for step in steps if step.command is not None)

    def clearance(index, message):
        if message is None:
            return None
        start = tuple(getattr(message, axis, None) for axis in "xyz")
        if not all(type(value) in (float, int) and math.isfinite(value) for value in start):
            return None
        target = next((command.target_ned for command in commands[index:] if command.target_ned is not None), None)
        if target is None:
            return None
        distance = math.dist(start[:2], target[:2])
        if distance < 1.0:
            return None
        fraction = min(1.0, 80.0 / distance)
        # Forward clearance estimate along the active connector. Keep the
        # physical DEM and the nominal X1 route guard on the pristine grid.
        try:
            return min(long_range._terrain_agl(dem, tuple(a + (b - a) * fraction * i / 8
                        for a, b in zip(start, target, strict=True))) for i in range(1, 9))
        except ValueError:
            return None

    return clearance


def load_assumed_sim(path, *, package, site, collector_plan, faults, site_bounds, scope, base_digest):
    """Authorize the generated template only against a verified SITL run and its site inventory."""
    data = path.read_bytes()
    raw = strict_json_loads(data.decode("utf-8"), name=str(path))
    expected = assumptions.generate(mode=package.mode, world=site.scenario_id,
                                    image=collector_plan.image, profile_path=PROFILE)
    fault = faults[0].kind if len(faults) == 1 else None
    if (canonical_sha256(raw) != canonical_sha256(expected) or site.world_sha256 != assumptions.WORLD_SHA
            or site.terrain.source_sha256 != assumptions.DEM_SHA
            or base_digest != assumptions.BASE_SHA or scope not in assumptions.scopes_for(fault)
            or set(site_bounds or ()) != {"scope_sha256", "dem_sha256", "base_mission_sha256",
                                          "obstacle_coverage", "obstacles"}):
        raise ValueError("ASSUMED_SIM requires the exact Iowa route, fault schedule and verified site bounds")
    battery = raw["battery"]["value"]
    if battery["max_sample_age_ms"] != MOTION_SAMPLE_MAX_AGE_MS:
        raise ValueError("ASSUMED_SIM battery sample delay differs from the runtime gate")
    capacity = raw["battery_capacity_wh"]["value"]
    # PX4 native-mode parameters are set before arming and read back like the battery model.
    state = {"sha256": hashlib.sha256(data).hexdigest(), "image_id": assumptions.IMAGE_ID,
             "capacity_wh": capacity, "parameters": {"SIM_BAT_DRAIN": battery["nominal_drain_s"],
                                                      "SIM_BAT_MIN_PCT": battery["nominal_floor_pct"],
                                                      **raw["px4_native_parameters"]["value"]}}
    if "battery_capacity_wh" in package.document["budget"]:
        raise ValueError("ASSUMED_SIM requires the base mission without a battery capacity override")
    document = {**package.document, "budget": {**package.document["budget"], "battery_capacity_wh": capacity}}
    assumed_bounds = {**site_bounds, "dem_error_m": raw["dem_error_m"]["value"],
                      "landing_pad": raw["landing_pad"]["value"],
                      "battery": {"max_voltage_v": battery["max_voltage_v"],
                                  "consumption_error_wh": battery["consumption_error_wh"],
                                  "soc_error_pct": battery["fault_soc_error_pct" if fault == "battery_drop"
                                                           else "nominal_soc_error_pct"]},
                      "suffixes": {key: entry["value"] for key, entry in raw["suffixes"].items()},
                      "native": {section: {key: entry["value"] for key, entry in entries.items()}
                                 for section, entries in raw["native"].items()}}
    return load_mission_package(document, mode="sim"), assumed_bounds, state


def demo_perception(package, profile, dem, steps):
    """Publish only existing readbacks; the model worker cannot see the collector ports."""
    from scripts.collect_sim_teacher_episodes import _gimbal_readback

    current = SimpleNamespace(route=None, gimbal=None)
    home_ne = tuple(package.document["home"])
    home = dem.sample_local(*home_ne).elevation_m
    geometry = profile.camera.gimbal
    # Collector indices address the policy's command iterator, not lifecycle steps.
    route = tuple(step for step in steps if step.command is not None or step.primitive == "vtol_land")

    def publish(scenario, index, samples, hold, boot_ms, position_ok):
        global_row = samples.get("GLOBAL_POSITION_INT")
        stamp = global_row.get("time_boot_ms") if global_row else None
        live_position = (position_ok is True and not scenario.native_active and type(stamp) is int
                         and type(boot_ms) is int and 0 < stamp <= boot_ms <= stamp + 1_000)
        try:
            point = scenario._motion_position(global_row) if live_position else None
        except ValueError:
            point = None
        current.route = (RouteReading(global_row["receive_monotonic_ns"] // 1_000_000, route, index,
                                      (point[0] - home_ne[0], point[1] - home_ne[1], home - point[2]),
                                      (*home_ne, home), AGL_MIN_M)
                         if point is not None and 0 <= index < len(route) else None)
        current.gimbal = None
        sample = hold.session.latest_attitude if hold is not None else None
        if sample is None:
            return
        status, received_ns = sample
        stamp = status.get("time_boot_ms") if isinstance(status, dict) else getattr(status, "time_boot_ms", None)
        if (type(received_ns) is not int or type(boot_ms) is not int
                or type(stamp) is not int or not 0 < stamp <= boot_ms <= stamp + 1_000):
            return
        # Shared readback aligns vehicle pose to the gimbal device time. It does not poll or command.
        readback = _gimbal_readback(hold.reader, status, hold.target,
                                    yaw_correction_deg=hold.yaw_correction_deg,
                                    gazebo_body_joints=hold.gazebo_body_joints)
        current.gimbal = GimbalReading(received_ns // 1_000_000, readback, geometry.get("angle_range_deg"))

    return (ToolRunner(PerceptionSources(route=lambda: current.route, gimbal=lambda: current.gimbal,
                                         dem=dem, profile=profile)), publish)


def make_scenario(args, mission, steps, record):
    faults = mission.get("scenario_faults")
    cadence_s = getattr(args, "decision_cadence_s", 0)
    if not faults and not cadence_s:
        return None
    make_decision = motion_evidence = perception_tick = None
    if args.decision_port is not None:
        backend = llama_decisions(args.decision_port, args.sentence)
        profile = load_airframe_profile(PROFILE)
        package = mission["scenario_package"]
        motion_evidence = LiveContinueEvidence(package, profile, mission["terrain"], steps,
                                               scope=record["contingency_scenario"]["motion_scope_sha256"],
                                               assumed_sim=mission.get("assumed_sim"))

        extra = {"decision_cadence_s": cadence_s} if cadence_s else {}
        if getattr(args, "perception_tools", False):
            extra["tool_runner"], perception_tick = demo_perception(package, profile, mission["terrain"], steps)

        def make_decision(gimbal, emit, episode_id, peer, native=None):
            return DecisionTick(package, profile, backend, gimbal, emit,
                                run_id=episode_id, plan_digest=record["compiled_mission_sha256"],
                                revision=mission["scenario_revision"], peer=peer,
                                build_proof=motion_evidence.build_proof, native_port=native, **extra)

    return CollectorScenario(faults, clearance=forward_clearance(steps, mission["terrain"]),
                             make_decision=make_decision, terrain=mission["terrain"],
                             motion_evidence=motion_evidence, cadence_steps=steps if cadence_s else (),
                             perception_tick=perception_tick)


def _restore_env(name, previous):
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def fly(args, collector_plan, steps, mission, record):
    from scripts import collect_sim_teacher_episodes as collector
    from scripts.realworld import collection_guard, x1_gpu, x1_resume
    from scripts.realworld.x1_lock import LOCK, flight_lock
    from scripts.sitl_lock import SITL_LOCK_HELD_ENV, SitlLock, sitl_lock_context
    from scripts.run_simulator_docker import X1_WIND_AIRSPEED_ENV

    if Path(sys.executable).resolve() != Path(long_range.SITL_PYTHON).resolve():
        raise ValueError(f"live SITL must use {long_range.SITL_PYTHON}")
    if os.environ.get(SITL_LOCK_HELD_ENV) == "1":
        raise ValueError("run the X1 demo outside an inherited SITL lock")
    with flight_lock(LOCK, shared=True):
        image_id = x1_resume.image_check(IMAGE)
        assumed = mission.get("assumed_sim")
        if assumed is not None and image_id != assumed["image_id"]:
            raise ValueError("ASSUMED_SIM immutable SITL image mismatch")
        x1_resume.flight_gate((args.slot,))
        with sitl_lock_context(SitlLock(label="x1-autonomy-demo", slots=8, slot=args.slot, retry_s=20.0),
                               wait=False):
            selection = x1_gpu.select(args.output / "GPU-PREFLIGHT.json", skip_leased=True)
            gpu, uuid = selection["gpu_index"], selection["gpu_uuid"]
            lock_root = Path(os.environ.get("RTF_MATRIX_GPU_LOCK_ROOT") or x1_gpu.GPU_LOCK_ROOT)
            lock_root.mkdir(parents=True, exist_ok=True)
            with (lock_root / f"{uuid}.lock").open("ab") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                record["gpu_admission"] = collection_guard.x1_start(
                    args.output, gpu, collection_guard.environment_settings(), uuid)
                plan = replace(collector_plan, image=image_id, requested_image=IMAGE,
                               gpu_render=True, gpu_device=str(gpu), gpu_devices=(str(gpu),))
                with ExitStack() as stack:
                    scenario = make_scenario(args, mission, steps, record)
                    # Only this invocation's explicit flag can opt an X1 episode in.
                    plugin = getattr(args, "wind_airspeed_plugin", None)
                    stack.callback(_restore_env, X1_WIND_AIRSPEED_ENV,
                                   os.environ.pop(X1_WIND_AIRSPEED_ENV, None))
                    if scenario is not None and any(f.kind == "wind_step" for f in scenario.faults):
                        env_key = "MULTIDETECT_RTF_SERVER_CONFIG"
                        previous = os.environ.get(env_key)
                        source = Path(previous) if previous else ROOT / "simulation/gazebo/server-wide-area.config"
                        modified_config = args.output / "x1-wind-server.config"
                        wind_server_config(source, modified_config, wind_relative_airspeed=plugin is not None)
                        os.environ[env_key] = str(modified_config)
                        stack.callback(_restore_env, env_key, previous)
                        if plugin is not None:
                            os.environ[X1_WIND_AIRSPEED_ENV] = str(plugin)
                    stack.enter_context(long_range.collector_overrides(
                        collector, mission, x1_heading_deg=steps[1].required_mc_heading_deg))
                    record["flight"]["status"] = "started"
                    write_result(args.output / "result.json", record)
                    return collector.run_episode(plan, Path(plan.truth), 0, land_after_route=True,
                                                 policy_override=MissionPolicy(steps, args.gimbal_pitch_deg),
                                                 **({"x1_scenario": scenario} if scenario is not None else {}))


def finish_flight(episode, steps, record):
    from scripts.smoke_airframe_flight import _jsonl, _mav_value

    metadata = read_object(episode / "episode.json")
    mavlink = _jsonl(episode / "mavlink.jsonl") or _jsonl(episode / "mavlink.jsonl.gz")
    rows = _jsonl(episode / "steps.jsonl")
    names = [step for step in steps if step.command is not None]
    markers = [row for row in mavlink if row.get("message_type") in {
        "fixed_wing_takeoff_complete", "collector_landing_complete"}]
    observed = {}
    for step, row in zip(names, rows):
        motion = row.get("labels", {}).get("motion")
        waited = row.get("ack", {}).get("motion_wait", {})
        expected = {"reposition": "arrived", "transition": "transitioned", "mc_reposition": "settled"}
        observed[step.primitive] = (row.get("outcome", {}).get("status") == "success"
                                    and motion == step.command.to_dict()
                                    and waited.get("waited") == expected[step.command.kind]
                                    and (step.primitive != "fw_goto_waypoint" or bool(waited.get("x1_passage"))))
        record["markers"].append({"primitive": step.primitive, "plan_step_id": step.source_step_id,
                                  "motion_wait": waited, "confirmed": observed[step.primitive]})
    takeoff = any(r["message_type"] == "fixed_wing_takeoff_complete" for r in markers)
    landed = any(r["message_type"] == "collector_landing_complete"
                 and r.get("flight_state", {}).get("landed_state") == 1
                 and r.get("flight_state", {}).get("vtol_state") == 3 for r in markers)
    positions = [tuple(_mav_value(row, axis) for axis in "xyz") for row in mavlink
                 if str(row.get("message_type", "")).upper() == "LOCAL_POSITION_NED"]
    positions = [p for p in positions if all(type(v) in (int, float) and math.isfinite(v) for v in p)]
    peak = max((math.dist(positions[0][:2], p[:2]) for p in positions), default=None)
    record["safety"]["observed"].update({
        "local_position_sample_count": len(positions), "max_horizontal_displacement_from_first_sample_m": peak,
    })
    record["markers"].extend(markers)
    if "contingency_scenario" in record:
        scenario_rows = [row for row in mavlink if str(row.get("message_type", "")).startswith("scenario_")]
        record["contingency_scenario"]["markers"] = scenario_rows
    native = _native_outcome(mavlink, markers, positions, names[len(rows):], record)
    return_home = bool(observed.get("mc_return_to_home")) or bool(native and native["return_home"])
    record["flight"].update(episode=str(episode), takeoff=takeoff,
                            waypoints_reached=[steps[2].source_step_id] if observed.get("fw_goto_waypoint") else [],
                            return_home=return_home, landing=landed, steps=observed,
                            episode_status=metadata.get("status"), episode_failure=metadata.get("failure"))
    if native is not None:
        record["flight"]["native_handoff"] = native
    complete = takeoff and landed and metadata.get("status") == "complete" and all(observed.values())
    if native is not None:
        # The admitted native action replaced the remaining compiled steps; PX4 flew them.
        return complete and native["confirmed"] and native["landed_by_native"] and (
            native["action"] != "return" or native["return_home"])
    return complete and len(rows) == len(names)


def _native_outcome(mavlink, markers, positions, superseded, record):
    """Summarize a native takeover. None when the route was never superseded."""
    if not any(row.get("message_type") == "collector_route_superseded" for row in mavlink):
        return None
    handoffs = [row for row in mavlink if row.get("message_type") == "scenario_native_handoff"]
    last = handoffs[-1] if handoffs else {}
    landing = next((row for row in reversed(markers) if row["message_type"] == "collector_landing_complete"), {})
    pad_radius = None
    package_path = record.get("contingency_scenario", {}).get("live_evidence", {}).get("package_path")
    if package_path:
        pad = read_object(Path(package_path)).get("x1_live_evidence", {}).get("landing_pad") or {}
        pad_radius = pad.get("radius_m")
    offset = math.dist(positions[0][:2], positions[-1][:2]) if positions else None
    return {"action": last.get("action"), "status": last.get("status"), "reason": last.get("reason"),
            "confirmed": last.get("status") == "confirmed",
            "landed_by_native": str(landing.get("method", "")).startswith("native_"),
            "landing_offset_from_first_sample_m": offset, "landing_pad_radius_m": pad_radius,
            "return_home": (last.get("action") == "return" and offset is not None
                            and type(pad_radius) in (int, float) and offset <= pad_radius),
            "superseded_steps": [step.primitive for step in superseded], "handoffs": handoffs}


def run(args, *, post, launch=fly):
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    record = {"sentence": args.sentence, "world": args.world, "scenario": str(args.scenario),
              "plan_json": None, "mission_package_sha256": None, "validator": {"verdict": "pending"},
              "flight": {"status": "not_started", "takeoff": None, "waypoints_reached": [],
                         "return_home": None, "landing": None}, "safety": {}, "markers": []}
    result = args.output / "result.json"
    try:
        faults = ()
        cadence_s = getattr(args, "decision_cadence_s", 0)
        scenario_path = getattr(args, "contingency_scenario", None)
        if scenario_path is not None:
            faults = load_schedule(scenario_path, args.inject_fault)
        if scenario_path is not None or cadence_s:
            record["contingency_scenario"] = {
                "path": str(scenario_path) if scenario_path is not None else None,
                "sha256": schedule_sha256(scenario_path) if scenario_path is not None else None,
                "clock": "PX4 LOCAL_POSITION_NED.time_boot_ms at first route tick",
                "faults": [asdict(fault) for fault in faults], "decision_port": args.decision_port,
            }
            if cadence_s:
                record["contingency_scenario"]["decision_cadence_s"] = cadence_s
            if getattr(args, "perception_tools", False):
                record["contingency_scenario"]["perception_tools"] = True
        plugin = getattr(args, "wind_airspeed_plugin", None)
        if plugin is not None:
            if not any(fault.kind == "wind_step" for fault in faults):
                raise ValueError("--wind-airspeed-plugin requires a selected wind_step fault")
            if not plugin.is_file():
                raise ValueError(f"--wind-airspeed-plugin is not a file: {plugin}")
            record["contingency_scenario"]["wind_airspeed_plugin"] = str(plugin)
        document = read_object(args.mission_package)
        if "plan" in document:
            raise X1PlanError("PRECOMPILED_PLAN", "demo input must not contain a precomputed plan")
        generate_evidence = getattr(args, "generate_live_evidence", False)
        assumed_path = getattr(args, "assumed_sim_bounds", None)
        if assumed_path is not None and (not generate_evidence or not faults
                                         or not getattr(args, "live_evidence_bounds", None)):
            raise ValueError("ASSUMED_SIM requires generation, selected faults and verified site bounds")
        if assumed_path is not None and not args.plan_only and args.decision_port is None:
            raise ValueError("ASSUMED_SIM live runs require the Altair scenario decision backend")
        if generate_evidence and (not (faults or cadence_s) or "x1_live_evidence" in document):
            raise ValueError("--generate-live-evidence requires a decision scenario and a package without x1_live_evidence")
        package = load_mission_package({**document, "task_text": args.sentence}, mode="sim")
        record["mission_package_sha256"] = canonical_sha256(package.document)
        plan, caps = compile_plan(package, post, record)
        site = load_site(args.world, args.scenario)
        collector_plan, steps, safety, mission = prepare(package, plan, caps, site, output=args.output, slot=args.slot)
        record["prepared"] = {"collector": asdict(collector_plan), "steps": [asdict(step) for step in steps],
                              "world_sdf": str(site.world_sdf), "world_sha256": site.world_sha256,
                              "dem_path": str(site.dem_path), "dem_sha256": site.terrain.source_sha256,
                              "slot": args.slot, "image": IMAGE}
        if faults or cadence_s:
            record["contingency_scenario"]["motion_scope_sha256"] = clearance_scope(
                steps, faults, profile_sha256=hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
                world_sha256=site.world_sha256, image=IMAGE,
                wind_plugin_sha256=hashlib.sha256(plugin.read_bytes()).hexdigest() if plugin is not None else None,
            )
        if generate_evidence:
            base_digest = canonical_sha256({"package": package.document, "plan": asdict(plan)})
            bounds_path = getattr(args, "live_evidence_bounds", None)
            bounds = None
            if bounds_path is not None:
                if bounds_path.name.endswith(x1_site_evidence.BOUNDS_SUFFIX):
                    bounds = x1_site_evidence.verify_site_bounds(
                        bounds_path, world_path=site.world_sdf, world_sha256=site.world_sha256,
                        dem_sha256=site.terrain.source_sha256,
                        models_root=os.environ.get(x1_site_evidence.MODELS_ENV, "").strip(),
                        rtf_model_root=os.environ.get("MULTIDETECT_RTF_MODEL_ROOT", "").strip(),
                    )
                else:
                    bounds = read_object(bounds_path)
            provenance = {}
            if assumed_path is not None:
                if not bounds_path.name.endswith(x1_site_evidence.BOUNDS_SUFFIX):
                    raise ValueError("ASSUMED_SIM requires verified .site-bounds.json geometry")
                package, bounds, assumed = load_assumed_sim(
                    assumed_path, package=package, site=site, collector_plan=collector_plan, faults=faults,
                    site_bounds=bounds, scope=record["contingency_scenario"]["motion_scope_sha256"],
                    base_digest=base_digest,
                )
                collector_plan = replace(collector_plan, extra_sim_params=(
                    *collector_plan.extra_sim_params,
                    *((key, str(value)) for key, value in assumed["parameters"].items()),
                ))
                mission["assumed_sim"] = assumed
                provenance = {"basis": assumptions.BASIS, "assumed_bounds_sha256": assumed["sha256"]}
                record["contingency_scenario"]["live_evidence"] = {
                    **provenance, "assumed_bounds_path": str(assumed_path), "expected_image_id": assumed["image_id"],
                    "battery_parameters": assumed["parameters"],
                }
                record["prepared"]["collector"] = asdict(collector_plan)
            evidence, missing = build_live_evidence(
                package, terrain=site.terrain,
                scope_sha256=record["contingency_scenario"]["motion_scope_sha256"],
                base_mission_sha256=base_digest,
                surveyed_bounds=bounds,
            )
            if assumed_path is not None and missing:
                raise ValueError(f"incomplete ASSUMED_SIM evidence: {', '.join(missing)}")
            evidence.update(provenance)
            package = load_mission_package({**package.document, "x1_live_evidence": evidence}, mode="sim",
                                           allow_assumed_sim=assumed_path is not None)
            generated_path = args.output / "mission-package.json"
            write_result(generated_path, package.document)
            record["mission_package_sha256"] = canonical_sha256(package.document)
            record["contingency_scenario"].setdefault("live_evidence", {}).update({
                "package_path": str(generated_path), "base_mission_sha256": base_digest,
                "host_bounds_path": str(bounds_path) if bounds_path is not None else None,
                "present_fields": sorted(evidence), "missing_fields": missing,
            })
        record["safety"]["planned"] = safety
        record["validator"]["verdict"] = "accepted"
        record["markers"].append({"phase": "mission_prepared"})
        record["compiled_mission_sha256"] = canonical_sha256({"package": package.document, "plan": asdict(plan)})
        if args.plan_only:
            record["flight"]["status"] = "plan_only"
            return 0
        if faults or cadence_s:
            mission.update(scenario_faults=faults, scenario_package=package, scenario_revision=plan.plan_version)
        record["safety"]["observed"] = mission["observed_margins"]
        try:
            episode = launch(args, collector_plan, steps, mission, record)
        except (ValueError, OSError, RuntimeError):
            episodes = list((args.output / "flight").rglob("episode.json"))
            if len(episodes) == 1:
                try:
                    finish_flight(episodes[0].parent, steps, record)
                except (ValueError, OSError, RuntimeError) as evidence_error:
                    record["flight"]["evidence_error"] = str(evidence_error)
            raise
        passed = finish_flight(episode, steps, record)
        record["flight"]["status"] = "pass" if passed else "fail"
        return 0 if passed else 1
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        failure = {"code": getattr(exc, "code", type(exc).__name__), "reason": str(exc)}
        if record["validator"]["verdict"] == "accepted":
            record["flight"].update(status="error", **failure)
        else:
            record["validator"].update(verdict="rejected", **failure)
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        record["elapsed_wall_s"] = round(time.monotonic() - started, 3)
        write_result(result, record)
        print(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sentence", required=True)
    parser.add_argument("--world", required=True, help="site name, e.g. rw_farm_iowa_w6km")
    parser.add_argument("--scenario", required=True, type=lambda p: Path(p).resolve())
    parser.add_argument("--contingency-scenario", type=Path, help="opt in to X1 fault schedule JSON")
    parser.add_argument("--inject-fault", action="append", choices=sorted(SUPPORTED), default=[],
                        help="select a SITL-supported fault from a mixed replay schedule (repeatable)")
    parser.add_argument("--wind-airspeed-plugin", type=lambda p: Path(p).resolve(), metavar="SO",
                        help="replace stock Gazebo AirSpeed for a selected wind_step (default: off)")
    parser.add_argument("--decision-port", type=int, metavar="PORT",
                        help="local onboard Altair llama-server port for live contingency requests")
    parser.add_argument("--decision-cadence-s", type=float, default=0, metavar="SECONDS",
                        help="steady-cruise interval in host seconds, halved outside cruise (0 disables)")
    parser.add_argument("--mission-package", required=True, type=Path, help="site references and constraints, without a plan")
    parser.add_argument("--generate-live-evidence", action="store_true",
                        help="attach scenario-bound SIM evidence and save the generated package (default: off)")
    parser.add_argument("--assumed-sim-bounds", type=Path, help="Iowa SIM-only assumption template")
    parser.add_argument("--live-evidence-bounds", type=Path, metavar="JSON",
                        help="local host-verified hard bounds for the compiled mission")
    parser.add_argument("--output", required=True, type=lambda p: Path(p).resolve(), help="fresh result directory")
    planner = parser.add_mutually_exclusive_group(required=True)
    planner.add_argument("--recorded-output", type=Path, help="recorded raw planner output (plan-only)")
    planner.add_argument("--checkpoint", type=Path, help="merged, text-only planner GGUF")
    parser.add_argument("--llama-server", type=Path)
    parser.add_argument("--planner-device", default="cpu")
    parser.add_argument("--planner-port", type=int, default=9957)
    parser.add_argument("--planner-timeout-s", type=float, default=180.0)
    parser.add_argument("--slot", type=int, choices=SLOTS, default=7)
    parser.add_argument("--gimbal-pitch-deg", type=float, default=-56.0)
    parser.add_argument("--perception-tools", action="store_true", help="opt in to Altair read-only decision tools")
    parser.add_argument("--plan-only", "--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not math.isfinite(args.decision_cadence_s) or args.decision_cadence_s < 0:
        parser.error("--decision-cadence-s must be finite and nonnegative")
    if args.perception_tools and args.decision_port is None:
        parser.error("--perception-tools requires --decision-port")
    if args.decision_cadence_s and args.decision_port is None:
        parser.error("--decision-cadence-s requires --decision-port")
    if args.inject_fault and args.contingency_scenario is None:
        parser.error("--inject-fault requires --contingency-scenario")
    if args.decision_port is not None and not (args.contingency_scenario or args.decision_cadence_s):
        parser.error("--decision-port requires --contingency-scenario or --decision-cadence-s")
    if args.live_evidence_bounds is not None and not args.generate_live_evidence:
        parser.error("--live-evidence-bounds requires --generate-live-evidence")
    if args.decision_port is not None and not 1 <= args.decision_port <= 65535:
        parser.error("--decision-port must be between 1 and 65535")
    if args.decision_port is not None and args.decision_port == args.planner_port:
        parser.error("the standalone decision backend needs a port separate from --planner-port")
    if args.recorded_output and not args.plan_only:
        parser.error("recorded output requires --plan-only or --dry-run")
    if args.checkpoint and not (args.checkpoint.is_file() and args.llama_server and args.llama_server.is_file()):
        parser.error("checkpoint backend requires a local --checkpoint and --llama-server executable")
    if (not math.isfinite(args.planner_timeout_s) or args.planner_timeout_s <= 0
            or not math.isfinite(args.gimbal_pitch_deg) or not -90 <= args.gimbal_pitch_deg <= 0):
        parser.error("expected a positive timeout and gimbal pitch between -90 and 0 degrees")
    post = recorded_backend(args.recorded_output) if args.recorded_output else checkpoint_backend(args)
    try:
        return run(args, post=post)
    except FileExistsError as exc:
        parser.error(f"output directory already exists: {exc.filename}")


if __name__ == "__main__":
    raise SystemExit(main())
