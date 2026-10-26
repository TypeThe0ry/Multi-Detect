"""Open-loop replay of recorded onboard episodes through MissionExecutor."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
import sys
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("REPLAY_CODE_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from multidetect.provenance import build_provenance, replay_version_record  # noqa: E402


def read_rows(episode: Path) -> list[dict[str, Any]]:
    path = episode / "steps.jsonl"
    if not path.is_file():
        raise FileNotFoundError(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def recover_recorded_mission_plan(episode: Path, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Recover the recorded plan and package snapshot without inventing provenance.

    Collector versions differ: newer rows contain a complete plan, while older
    rows contain the planner's ``skills`` summary.  The latter is still the
    recorded decision and is expanded with deterministic task parameters.
    """
    rows = rows if rows is not None else read_rows(episode)
    onboard = ((rows[0].get("labels") or {}).get("onboard") or {}) if rows else {}
    recorded = None
    for row in rows:
        candidate = (((row.get("labels") or {}).get("onboard") or {}).get("mission") or {}).get("plan")
        if isinstance(candidate, dict) and (candidate.get("steps") or candidate.get("skills")):
            recorded = candidate
            break
    summary = _json_file(episode / "mission_summary.json")
    if recorded is None and isinstance(summary.get("steps"), list):
        recorded = {"steps": summary["steps"], "skills": summary.get("skills")}
    task = str((rows[0] if rows else {}).get("task_text") or _json_file(episode / "episode.json").get("task_text") or "")
    skills = recorded.get("skills") if isinstance(recorded, dict) else None
    if not isinstance(skills, list):
        skills = [s.get("skill") for s in (recorded or {}).get("steps", []) if isinstance(s, dict)]
    skills = [str(s) for s in skills if s]
    package_document: dict[str, Any] | None = None
    package_source = None
    episode_meta = _json_file(episode / "episode.json")
    # A package/config snapshot is preferred over the live repository config.
    for candidate in sorted((episode / "config").glob("**/*.json")) if (episode / "config").is_dir() else ():
        value = _json_file(candidate)
        if "operating_area" in value and ("plan" in value or "steps" in value):
            package_document, package_source = value, str(candidate)
            break
    if package_document is None:
        for candidate in (episode / "mission_package.json", episode / "package.json"):
            value = _json_file(candidate)
            if "operating_area" in value:
                package_document, package_source = value, str(candidate)
                break
    # Recover the suite/package context used by the collector.  The suite is
    # keyed by the mission directory immediately below its root.
    suite_config: dict[str, Any] = {}
    mission_id = episode.parents[3].name if len(episode.parents) > 3 else ""
    for suite_path in sorted((ROOT / "configs/onboard").glob("*.json")):
        suite = _json_file(suite_path)
        for entry in suite.get("missions", []):
            if isinstance(entry, dict) and entry.get("id") == mission_id:
                suite_config = {**suite.get("defaults", {}), **entry}
                break
        if suite_config:
            break
    package_setting = suite_config.get("mission_package")
    if package_document is None and isinstance(package_setting, dict):
        package_document, package_source = package_setting, "suite.mission_package"
    elif package_document is None and isinstance(package_setting, str):
        package_path = Path(package_setting)
        if not package_path.is_absolute(): package_path = ROOT / package_path
        package_document = _json_file(package_path)
        if package_document: package_source = str(package_path)
    # Preserve a complete recorded plan verbatim.  Summary-only rows receive
    # stable params from the task text and the suite's refs where available.
    complete_steps = (recorded.get("steps") if recorded else None)
    if complete_steps and all(isinstance(s, dict) and all(k in s for k in ("id", "skill", "params", "on_success", "on_fail")) for s in complete_steps):
        plan = {"plan_version": recorded.get("plan_version", 1), "goal": recorded.get("goal", task),
                "steps": complete_steps, "report": recorded.get("report", ["findings", "positions"])}
        source = "labels.onboard.mission.plan.steps" if recorded is not summary else "mission_summary.steps"
    elif skills:
        query = task
        lower = task.lower()
        if "backpack" in lower: query = "backpack"
        elif "person" in lower: query = "person"
        elif "hydrant" in lower: query = "fire hydrant"
        elif "cone" in lower: query = "orange traffic cone"
        steps = []
        for index, skill in enumerate(skills):
            params: dict[str, Any] = {}
            if skill in {"count", "monitor", "area_search"}:
                params = {"region": "zone:site", "query": query}
            if skill == "search": params = {"region": "zone:site", "query": query}
            if skill == "inspect": params = {"target_ref": "poi:target", "query": query, "views": 4, "standoff_m": 8.0}
            if skill == "monitor": params["duration_s"] = 300
            if skill == "follow": params["duration_s"] = 120
            if skill == "follow": params = {"track_ref": "track:target", "query": query, "duration_s": 120}
            if skill == "patrol": params = {"route_ref": "route:fence", "query": query, "loops": 1}
            steps.append({"id": f"s{index + 1}", "skill": skill, "params": params,
                          "on_success": f"s{index + 2}" if index + 1 < len(skills) else None,
                          "on_fail": f"s{index + 2}" if index + 1 < len(skills) else None})
        plan = {"plan_version": 1, "goal": task, "steps": steps, "report": ["findings", "positions"]}
        source = "labels.onboard.mission.plan.skills" if recorded else "mission_summary.skills"
    else:
        fallback_query = "target"
        if "backpack" in task.lower(): fallback_query = "backpack"
        elif "person" in task.lower(): fallback_query = "person"
        elif "hydrant" in task.lower(): fallback_query = "fire hydrant"
        elif "cone" in task.lower(): fallback_query = "orange traffic cone"
        plan = {"plan_version": 1, "goal": episode.parent.parent.name,
                "steps": [{"id": "s1", "skill": "count", "params": {"region": "zone:site", "query": fallback_query}, "on_success": "s2", "on_fail": "s2"},
                          {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None}],
                "report": ["findings", "positions"]}
        source = "fallback_count"
    return {"plan": plan, "plan_source": source, "skills": [s.get("skill") for s in plan["steps"]],
            "package_document": package_document or suite_config.get("mission_package"), "package_source": package_source,
            "suite_config": suite_config,
            "episode": episode_meta}


class Results:
    def __init__(self, mode: str, cache_file: Path | None):
        self.mode, self.cache_file = mode, cache_file
        self.cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file and cache_file.exists() else {}
        self.stats = {"detector_calls": 0, "slow_calls": 0, "cache_hits": 0}

    def get(self, key: str, logged: Any, kind: str) -> Any:
        if self.mode == "logged":
            return logged
        if key in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[key]
        if self.mode == "cache":
            value = logged
        else:
            raise RuntimeError("real model mode requires an installed provider; use --provider module:function")
        self.cache[key] = value
        return value

    def save(self) -> None:
        if self.cache_file:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _decision(call: dict[str, Any]) -> str:
    value = call.get("decision")
    if value in {"approach_track", "confirm_target", "reject_track"}:
        return value
    return {"match": "approach_track", "confirm": "confirm_target", "no_match": "reject_track"}.get(
        call.get("identity"), "reject_track"
    )


def first_non_search_divergence(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    for index, item in enumerate(trace):
        expected_kind = (item.get("logged_action") or {}).get("kind")
        if item.get("decision_class") != "search" and item["action"]["kind"] != expected_kind:
            return {"index": index, "step": item["step"], "decision_class": item["decision_class"]}
    return None


def replay(episode: Path, *, mode: str = "logged", cache_file: Path | None = None,
           closed_loop: bool = False, detections_file: Path | None = None,
           safety_exit: bool = True) -> dict[str, Any]:
    if mode == "real":
        raise RuntimeError("real 模式未实现：当前 CLI 仅支持 logged/cache，不会启动 GPU 或 llama-server")
    from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
    from multidetect.autonomy.motion_intent import AirframeMotionAdapter
    from multidetect.autonomy.mission_plan import validate_mission_plan
    from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
    from multidetect.autonomy.onboard_policy_parts.config import POSITION_DEFAULTS
    from multidetect.autonomy.obstacle_source import StaticMapSource
    from multidetect.sim_teacher.policies import load_sdf_obstacles
    from multidetect.sim_teacher.primitives import ActionPrimitive
    from multidetect.sim_teacher.policies import ExpertSearchPolicy
    from multidetect.simulator.camera_projection import pixel_camera_ray, pixel_ground_point
    from multidetect.simulator.task_semantics import typical_object_size

    rows = read_rows(episode)
    if not rows:
        raise ValueError(f"empty episode: {episode}")
    restored = recover_recorded_mission_plan(episode, rows)
    onboard = (rows[0].get("labels") or {}).get("onboard") or {}
    logged_actions = {int(row["step_index"]): row.get("action") or {} for row in rows}
    replay_step: int | None = None
    search_delegated = False
    search_delegate_mismatches: list[int] = []
    mission = restored["episode"]
    onboard_config = restored["suite_config"]
    query = str(onboard_config.get("task_text") or (rows[0].get("task_text", "")))
    # The free-form logged task can be Chinese or exceed the mission-plan
    # eight-word contract; benchmark directory names are the stable fallback.
    if len(query.split()) > 8 or any(ord(ch) > 127 for ch in query):
        query = episode.parents[2].name.replace("count-", "").replace("-", " ")
    # Keep the same plan wording and references as the known-good replay.
    query = {"cones": "orange traffic cone", "fire hydrants": "fire hydrant"}.get(query, query)
    goal = restored["plan"].get("goal") or episode.parent.parent.name
    plan = validate_mission_plan(restored["plan"], capabilities=frozenset({"payload"}))
    info = rows[0]["obs_before"]["camera_info"]
    camera = CameraModel(float(info["fx"]), float(info["fy"]), float(info["cx"]), float(info["cy"]),
                         int(info["width"]), int(info["height"]), pitch_down_rad=0.18)
    world_setting = mission.get("world_sdf", "default.sdf")
    world_path = Path(str(world_setting))
    sdf = world_path if world_path.is_absolute() else ROOT / "simulation/gazebo/worlds" / f"{world_path.stem}.sdf"
    obstacle = StaticMapSource(load_sdf_obstacles(sdf)) if sdf.exists() else None
    # Mirror OnboardPolicy's final arbiter construction.  Replay defaults to
    # the real safety exit; --disable-safety-exit is retained for A/B against
    # historical traces that intentionally bypassed guard_command().
    airframe_name = (
        onboard_config.get("airframe")
        or (mission.get("airframe") if isinstance(mission, dict) else None)
        or ((mission.get("airframe_profile") or {}).get("id") if isinstance(mission, dict) and isinstance(mission.get("airframe_profile"), dict) else None)
        or (onboard.get("airframe") if isinstance(onboard, dict) else None)
        or "x500_mono_cam"
    )
    adapter = None
    if safety_exit:
        from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig

        adapter = AirframeMotionAdapter(str(airframe_name), base_dir=ROOT)
        envelope_source: dict[str, Any] = {}
        for source in (onboard_config, mission, restored.get("package_document") or {}):
            if isinstance(source, dict):
                envelope_source.update(source)
                nested = source.get("safety_envelope")
                if isinstance(nested, dict):
                    envelope_source.update(nested)
                package = source.get("mission_package")
                if isinstance(package, dict):
                    envelope_source.update(package)
                    package_nested = package.get("safety_envelope")
                    if isinstance(package_nested, dict):
                        envelope_source.update(package_nested)
        # OnboardPolicy lets each non-multirotor profile widen the generic
        # multirotor step default; explicit safety config remains authoritative.
        if adapter.profile.category != "multirotor":
            envelope_source.setdefault("max_translation_m", adapter.profile.limits.max_step_distance_m)
            dynamics = adapter.safety.vehicle
            envelope_source.setdefault("vehicle", {
                "category": dynamics.category,
                "can_hover": dynamics.can_hover,
                "min_airspeed_mps": dynamics.min_airspeed_mps,
                "cruise_airspeed_mps": dynamics.cruise_airspeed_mps,
                "max_airspeed_mps": dynamics.max_airspeed_mps,
                "min_turn_radius_m": dynamics.min_turn_radius_m,
                "max_bank_deg": dynamics.max_bank_deg,
                "climb_rate_mps": dynamics.climb_rate_mps,
                "descent_rate_mps": dynamics.descent_rate_mps,
            })
        area = envelope_source.get("operating_area")
        if isinstance(area, dict):
            try:
                envelope_source.setdefault("operating_polygon", (
                    (float(area["north_min_m"]), float(area["east_min_m"])),
                    (float(area["north_max_m"]), float(area["east_min_m"])),
                    (float(area["north_max_m"]), float(area["east_max_m"])),
                    (float(area["north_min_m"]), float(area["east_max_m"])),
                ))
                envelope_source.setdefault("min_altitude_m", float(area["min_altitude_m"]))
                envelope_source.setdefault("max_altitude_m", float(area["max_altitude_m"]))
            except (KeyError, TypeError, ValueError):
                pass
        # Mission package formats may expose the area fields at the top level.
        envelope_source.setdefault("min_altitude_m", float(envelope_source.get("min_altitude_m", 0.0)))
        home_raw = envelope_source.get("home")
        # Recorded local-NED poses are already relative to the mission home.
        # When no signed package supplies an explicit home, keep the runtime's
        # conventional origin; anchoring to the first observed aircraft pose
        # turns every subsequent altitude/position check into a false offset.
        home = tuple(float(v) for v in home_raw) if isinstance(home_raw, (list, tuple)) else (0.0, 0.0, 0.0)
        if len(home) < 3:
            home = (*home, 0.0)
        safety = SafetyEnvelope(
            SafetyEnvelopeConfig.from_mapping(envelope_source),
            home_ned=home[:3],
            obstacle_source=obstacle,
        )
        adapter.safety = safety
    # Live onboard derives this target before constructing the loop.  Recreate
    # the same size-aware and obstacle-clearance floor for pose-rebound replay.
    takeoff_altitude = float(mission.get("takeoff_altitude_m", 0.0) or 0.0)
    if takeoff_altitude <= 0.0:
        takeoff_altitude = max(8.0, -float(rows[0]["obs_before"]["pose"]["local_ned_m"][2]))
    obstacles = tuple(load_sdf_obstacles(sdf)) if sdf.exists() else ()
    search_altitude = max(
        8.0,
        min(takeoff_altitude, (typical_object_size(query, default=0.0) or 0.0)
            * float(info["fx"]) / 14.0 * math.sin(0.18 + math.radians(20.0))),
    )
    if obstacles:
        search_altitude = max(search_altitude, min(takeoff_altitude,
            max(float(item.height_m) for item in obstacles) + 2.0))
    loop_defaults = {
        "confirm_min_side_px": 20.0,
        "contradiction_relook_enabled": True,
        "near_depth": False,
        "confirm_max_range_m": 40.0,
        "ground_extent_size_m": typical_object_size(query, default=0.0) or None,
        **POSITION_DEFAULTS,
    }
    if closed_loop:
        loop_defaults["search_altitude_m"] = search_altitude
    loop_fields = {f.name for f in __import__("dataclasses").fields(LoopConfig)}
    search_policy: ExpertSearchPolicy | None = None
    def logged_search_action(_pose: Any, _yaw: Any) -> ActionPrimitive:
        nonlocal search_delegated
        search_delegated = True
        action = logged_actions.get(replay_step or -1)
        if not isinstance(action, dict) or not action.get("kind"):
            return ActionPrimitive.hold(1.0)
        return ActionPrimitive.from_dict(action)

    def logged_search_outcome() -> str | None:
        if replay_step is None:
            return None
        mission_label = ((rows_by_step.get(replay_step, {}).get("labels") or {}).get("onboard") or {}).get("mission") or {}
        history = mission_label.get("history") or []
        return history[-1].get("reason") if history and isinstance(history[-1], dict) else None

    rows_by_step = {int(row["step_index"]): row for row in rows}
    def make_loop(*_args: Any, **overrides: Any) -> OnboardLoop:
        nonlocal search_policy
        overrides.pop("restart_route", None)
        config = {k: v for k, v in {**loop_defaults, **overrides}.items() if k in loop_fields}
        if closed_loop:
            search_policy = ExpertSearchPolicy(
                target_ned=(1.0e7, 1.0e7, 0.0), intrinsics=info,
                image_size=(camera.width, camera.height), altitude_m=takeoff_altitude,
                obstacles=obstacles, home_ned=(0.0, 0.0, 0.0), search_radius_m=55.0,
                restart_route=False,
            )
        else:
            search_policy = None
        return OnboardLoop(
            camera=camera,
            search_action=(lambda pose, yaw: search_policy.next_action(pose_ned=pose, yaw_deg=yaw))
                if closed_loop else logged_search_action,
            search_outcome=(lambda: search_policy.search_outcome if search_policy else None)
                if closed_loop else logged_search_outcome,
            obstacle_source=obstacle, config=LoopConfig(**config),
        )
    refs = dict(onboard_config.get("refs") or {})
    refs.setdefault("zone:site", (0.0, 0.0, 55.0))
    refs.setdefault("route:fence", [(-30., -30.), (-30., 30.), (30., 30.), (30., -30.), (-30., -30.)])
    executor = MissionExecutor(plan, loop_factory=make_loop,
        refs=refs,
        config=ExecutorConfig(step_budget=140),
        obstacle_source=obstacle, airframe_adapter=adapter,
        no_fly_zones=(envelope_source.get("no_fly_zones", ()) if safety_exit else ()))
    results = Results(mode, cache_file)
    detections_by_step: dict[str, list[Any]] = {}
    # The established replay stores detector output as {step_index: [bbox, score, label]}.
    # Prefer an episode-local file, then an explicitly supplied sibling/benchmark result.
    detection_path = detections_file or episode / "detections.json"
    if detection_path.exists():
        detections_by_step = json.loads(detection_path.read_text(encoding="utf-8"))
    trace = []
    execution_obs = rows[0]["obs_before"]
    for row_index, row in enumerate(rows):
        replay_step = int(row["step_index"])
        search_delegated = False
        label = (row.get("labels") or {}).get("onboard") or {}
        if not label.get("skill"):
            if closed_loop:
                execution_obs = row.get("obs_after") or (
                    rows[row_index + 1].get("obs_before", execution_obs)
                    if row_index + 1 < len(rows) else execution_obs
                )
            continue
        obs = execution_obs if closed_loop else row["obs_before"]
        pose = tuple(float(x) for x in obs["pose"]["local_ned_m"])
        yaw = float(obs["attitude"]["yaw_deg"])
        stamp = obs.get("frame_sensor_stamp_ns")
        stamp_s = float(stamp) / 1e9 if stamp is not None else None
        width, height = camera.width, camera.height
        def ground(u: float, v: float) -> Any:
            return pixel_ground_point(u, v, pose=obs, camera_info=obs["camera_info"], image_size=(width, height), camera_pitch_rad=0.18)
        def ray(u: float, v: float) -> Any:
            return pixel_camera_ray(u, v, pose=obs, camera_info=obs["camera_info"], image_size=(width, height), camera_pitch_rad=0.18)
        tracks = label.get("tracks", [])
        raw_detections = detections_by_step.get(str(row["step_index"]))
        if raw_detections is None:
            raw_detections = [[t["bbox"], 1.0, t.get("verdict", "candidate")] for t in tracks if t.get("bbox")]
        detections = [Detection(tuple(box), float(score), str(label_name))
                      for box, score, label_name in results.get(f"det:{row['step_index']}", raw_detections, "detector")]
        results.stats["detector_calls"] += 1
        needing = executor.observe(detections, pose_ned=pose, yaw_deg=yaw, ground_point=ground,
                                   camera_ray=ray, timestamp_s=stamp_s)
        raw_calls = {c.get("track_id"): c for c in label.get("slow_calls", [])}
        calls = []
        for track in needing[:3]:
            logged = raw_calls.get(track.track_id, {})
            key = hashlib.sha256(f"{row['step_index']}:{track.track_id}:{track.bbox}".encode()).hexdigest()
            verdict = results.get(key, _decision(logged), "slow")
            results.stats["slow_calls"] += 1
            executor.apply_verdict(track.track_id, verdict, logged.get("confidence"))
            calls.append({"track_id": track.track_id, "decision": verdict, "cache_hit": key in results.cache})
        action, detail = executor.next_action(pose_ned=pose, yaw_deg=yaw,
            ground_point=ground, frame_id=str(row.get("step_index")), timestamp_s=stamp_s)
        if closed_loop:
            after = row.get("obs_after")
            if after is not None:
                execution_obs = after
            else:
                next_index = row_index + 1
                if next_index < len(rows):
                    execution_obs = rows[next_index].get("obs_before", execution_obs)
        action_obj = {"kind": action.kind, "forward_m": action.forward_m, "right_m": action.right_m,
                      "up_m": action.up_m, "yaw_delta_deg": action.yaw_delta_deg, "seconds": action.seconds}
        detail_class = "search" if detail.get("state_before") in {"searching", "search"} and not detail.get("focus") else "non_search"
        logged_kind = (row.get("action") or {}).get("kind")
        logged_state = (((row.get("labels") or {}).get("onboard") or {}).get("state_before"))
        if search_delegated and (logged_state not in {"searching", "search"} or logged_kind not in {"translate", "turn", "hold"}):
            search_delegate_mismatches.append(int(row["step_index"]))
        if any(key in detail for key in ("count_rejection_revisit", "small_unjudged_revisit", "revisit", "alert_look")):
            detail_class = "revisit"
        safety_info = detail.get("safety") if isinstance(detail, dict) else None
        if safety_info:
            safety_info = dict(safety_info)
            safety_info["guarded_action_kind"] = action.kind
            safety_info["changed_to_hold_stop"] = bool(
                safety_info.get("rejected") and action.kind in {"hold", "stop"}
            )
        trace.append({"step": row["step_index"], "pose_ned": list(pose), "action": action_obj, "logged_action": row.get("action"),
                      "search_delegated": search_delegated,
                      "safety": safety_info or {"status": "bypassed", "rejected": False, "reasons": []},
                      "calls": calls, "phase": detail.get("phase"), "counts": dict(executor.counts),
                      "search_waypoint_index": search_policy._waypoint_index if search_policy else None,
                      "decision_class": detail_class,
                      "alerts": list(executor.alerts), "revisit_plan": detail.get("alert_look"),
                      "detail": detail, "tracks": [{"id": t.track_id, "verdict": t.verdict, "label": t.label,
                          "bbox": t.bbox, "ground": t.ground} for t in executor.current_tracks],
                      "diagnostics": {"state_before": detail.get("state_before"),
                          "focus": detail.get("focus"), "bearing_deg": detail.get("bearing_deg"),
                          "horizontal_range_m": detail.get("horizontal_range_m"),
                          "min_side_px": detail.get("min_side_px"),
                          "confirm_views": detail.get("confirm_views"),
                          "match_views": detail.get("match_views"),
                          "counted": detail.get("counted"),
                          "search_altitude_m": loop_defaults.get("search_altitude_m"),
                          "search_waypoint_index": search_policy._waypoint_index if search_policy else None,
                          "search_commanded_yaw_deg": search_policy._commanded_yaw_deg if search_policy else None,
                          "search_outcome": search_policy.search_outcome if search_policy else None}})
    results.save()
    first_divergence = first_non_search_divergence(trace)
    scored = trace if first_divergence is None else trace[:first_divergence["index"]]
    action_types = sum(item["action"]["kind"] == (item.get("logged_action") or {}).get("kind") for item in trace)
    search_placeholder_steps = sum(item["action"]["kind"] == "hold"
        and (item.get("logged_action") or {}).get("kind") not in {None, "hold"} for item in trace)
    exact_actions = sum(item["action"]["kind"] == (item.get("logged_action") or {}).get("kind") and all(
        round(float(item["action"].get(k, 0)), 1) == round(float((item.get("logged_action") or {}).get(k, 0)), 1)
        for k in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds")) for item in trace)
    # Discrete decision agreement is stable under pose rebound and motion
    # integration differences; continuous command agreement remains diagnostic.
    decision_actions = sum(
        item["action"]["kind"] == (item.get("logged_action") or {}).get("kind")
        for item in trace
    )
    decision_mismatches = []
    continuous_mismatches = []
    for item in trace:
        logged = item.get("logged_action") or {}
        same_kind = item["action"]["kind"] == logged.get("kind")
        if not same_kind:
            decision_mismatches.append(item["step"])
        elif not all(round(float(item["action"].get(k, 0)), 1) == round(float(logged.get(k, 0)), 1)
                     for k in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds")):
            continuous_mismatches.append(item["step"])
    scored_action_types = sum(item["action"]["kind"] == (item.get("logged_action") or {}).get("kind") for item in scored)
    raw_by_step = {int(row["step_index"]): (row.get("labels") or {}).get("onboard") or {} for row in rows}
    call_equal = call_total = 0
    for item in trace:
        expected = raw_by_step.get(int(item["step"]), {}).get("slow_calls", [])
        actual_pairs = {(call["track_id"], call["decision"]) for call in item["calls"]}
        expected_pairs = {(call.get("track_id"), _decision(call)) for call in expected}
        call_equal += len(actual_pairs & expected_pairs)
        call_total += max(len(actual_pairs), len(expected_pairs))
    logged_mission = (raw_by_step.get(int(trace[-1]["step"]), {}).get("mission") or {}) if trace else {}
    coverage_history = [item for item in executor.history if item.get("reason") == "coverage_complete"]
    skill_by_step = {step["id"]: step["skill"] for step in restored["plan"]["steps"]}
    by_task: dict[str, dict[str, int]] = {}
    for item in trace:
        key = str(skill_by_step.get(item.get("detail", {}).get("step") or item.get("detail", {}).get("step_id"), "unknown"))
        counts = by_task.setdefault(key, {"steps": 0, "exact_action_matches": 0})
        counts["steps"] += 1
        logged = item.get("logged_action") or {}
        counts["exact_action_matches"] += int(item["action"].get("kind") == logged.get("kind") and all(
            round(float(item["action"].get(k, 0)), 1) == round(float(logged.get(k, 0)), 1)
            for k in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds")))
    current_provenance = build_provenance(root=ROOT, command=sys.argv)
    return {"episode": str(episode), "mode": mode, "open_loop": not closed_loop,
            "provenance": replay_version_record((mission.get("provenance") or {}).get("code"), current_provenance.get("code")),
            "safety_exit": safety_exit, "plan_source": restored["plan_source"],
            "plan_skills": restored["skills"], "package_source": restored["package_source"],
            "limitations": ["半闭环只回灌日志执行后位姿，不模拟物理执行"],
            "metrics": {"steps": len(trace), "action_type_agreement": action_types / len(trace) if trace else 1.0,
                        "pre_divergence_steps": len(scored),
                        "pre_divergence_action_type_agreement": scored_action_types / len(scored) if scored else 1.0,
                        "diverged_at_step": first_divergence["step"] if first_divergence else None,
                        "divergence": first_divergence,
                        "exact_action_agreement": exact_actions / len(trace) if trace else 1.0,
                        "decision_agreement": decision_actions / len(trace) if trace else 1.0,
                        "decision_mismatch_steps": decision_mismatches,
                        "continuous_mismatch_steps": continuous_mismatches,
                        "search_delegate_mismatch_steps": search_delegate_mismatches,
                        "action_agreement_by_task": {key: {**value, "exact_action_agreement": value["exact_action_matches"] / value["steps"] if value["steps"] else 1.0} for key, value in by_task.items()},
                        "search_placeholder_mismatches": search_placeholder_steps,
                        "slow_call_agreement": call_equal / call_total if call_total else 1.0,
                        "count_replay": dict(executor.counts), "count_logged_final": logged_mission.get("counts", {}),
                        "coverage_complete_replay": bool(coverage_history),
                        "coverage_complete_logged": any(
                            item.get("reason") == "coverage_complete"
                            for item in (logged_mission.get("history") or [])
                        ),
                        "coverage_history": coverage_history,
                        "alerts_replay": list(executor.alerts), "alerts_logged_final": logged_mission.get("alerts", []),
                        "revisit_replay_steps": [x["step"] for x in trace if x.get("decision_class") == "revisit"],
                        "revisit_logged_steps": [r["step_index"] for r in rows if (r.get("labels") or {}).get("onboard", {}).get("revisit_plan") or (r.get("labels") or {}).get("onboard", {}).get("count_rejection_revisit")]},
            "stats": results.stats, "trace": trace}


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    left, right = a["trace"], b["trace"]
    for i, (x, y) in enumerate(zip(left, right)):
        fields = ("action", "calls", "counts", "alerts", "revisit_plan")
        changed = [field for field in fields if x.get(field) != y.get(field)]
        if changed:
            return {"first_divergence": i, "step": x["step"], "left": x, "right": y,
                    "changed_fields": changed, "summary": "changed: " + ", ".join(changed)}
    if len(left) != len(right):
        return {"first_divergence": min(len(left), len(right)), "summary": f"trace lengths differ: {len(left)} vs {len(right)}"}
    return {"first_divergence": None, "summary": "traces identical"}


def compare_roots(episode: Path, roots: tuple[Path, Path], *, mode: str, closed_loop: bool,
                  detections_file: Path | None) -> dict[str, Any]:
    """Run the same CLI in isolated subprocesses with each code root on PYTHONPATH."""
    outputs: list[dict[str, Any]] = []
    for root in roots:
        env = os.environ.copy()
        env["REPLAY_CODE_ROOT"] = str(root.resolve())
        env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            output = Path(handle.name)
        try:
            script = root.resolve() / "scripts" / "replay_onboard_episode.py"
            if not script.is_file():
                script = Path(__file__).resolve()
            cmd = [sys.executable, str(script), str(episode), "--mode", mode, "--output", str(output)]
            if closed_loop:
                cmd.append("--closed-loop-pose-approximation")
            if detections_file:
                cmd.extend(("--detections", str(detections_file.resolve())))
            completed = subprocess.run(cmd, env=env, cwd=str(root), text=True,
                                       capture_output=True, check=False)
            if completed.returncode:
                raise RuntimeError(f"compare child failed ({root}): {completed.stderr[-1000:]}")
            document = json.loads(output.read_text(encoding="utf-8"))
            outputs.append(document["episodes"][0])
        finally:
            output.unlink(missing_ok=True)
    return {"compare_roots": [str(p.resolve()) for p in roots],
            "comparison": compare(outputs[0], outputs[1]), "runs": outputs}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("episodes", nargs="+", type=Path)
    ap.add_argument("--mode", choices=("real", "logged", "cache"), default="logged")
    ap.add_argument("--cache-file", type=Path)
    ap.add_argument("--detections", type=Path, help="optional logged detector results: {step_index: [[bbox, score, label], ...]}")
    ap.add_argument("--cache-ram", type=int, default=0)
    ap.add_argument("--closed-loop-pose-approximation", action="store_true")
    ap.add_argument("--disable-safety-exit", action="store_true",
                    help="bypass AirframeMotionAdapter.guard_command (legacy replay)")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("CODE_A", "CODE_B"))
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if args.compare:
        if len(args.episodes) != 1:
            ap.error("--compare requires exactly one episode")
        result: Any = compare_roots(args.episodes[0], (args.compare[0], args.compare[1]),
                                    mode=args.mode, closed_loop=args.closed_loop_pose_approximation,
                                    detections_file=args.detections,
                                    safety_exit=not args.disable_safety_exit)
    else:
        result = {"episodes": [replay(p, mode=args.mode, cache_file=args.cache_file,
                          closed_loop=args.closed_loop_pose_approximation, detections_file=args.detections,
                          safety_exit=not args.disable_safety_exit)
                          for p in args.episodes]}
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
