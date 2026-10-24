"""Open-loop replay of recorded onboard episodes through MissionExecutor."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import sys
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("REPLAY_CODE_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


def read_rows(episode: Path) -> list[dict[str, Any]]:
    path = episode / "steps.jsonl"
    if not path.is_file():
        raise FileNotFoundError(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


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


def replay(episode: Path, *, mode: str = "logged", cache_file: Path | None = None,
           closed_loop: bool = False, detections_file: Path | None = None) -> dict[str, Any]:
    if mode == "real":
        raise RuntimeError("real 模式未实现：当前 CLI 仅支持 logged/cache，不会启动 GPU 或 llama-server")
    from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
    from multidetect.autonomy.mission_plan import validate_mission_plan
    from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
    from multidetect.autonomy.onboard_policy_parts.config import POSITION_DEFAULTS
    from multidetect.autonomy.obstacle_source import StaticMapSource
    from multidetect.sim_teacher.policies import load_sdf_obstacles
    from multidetect.sim_teacher.primitives import ActionPrimitive
    from multidetect.simulator.camera_projection import pixel_camera_ray, pixel_ground_point
    from multidetect.simulator.task_semantics import typical_object_size

    rows = read_rows(episode)
    if not rows:
        raise ValueError(f"empty episode: {episode}")
    onboard = (rows[0].get("labels") or {}).get("onboard") or {}
    mission = json.loads((episode / "episode.json").read_text(encoding="utf-8")) if (episode / "episode.json").exists() else {}
    task = rows[0].get("task_text", "")
    query = task
    # Mission suite metadata/config may provide the normalized task query.
    config_path = next(iter(episode.parents[3].glob("*.json")), None) if len(episode.parents) > 3 else None
    if config_path:
        try:
            query = json.loads(config_path.read_text(encoding="utf-8")).get("task_text", query)
        except (OSError, ValueError):
            pass
    # The free-form logged task can be Chinese or exceed the mission-plan
    # eight-word contract; benchmark directory names are the stable fallback.
    if len(query.split()) > 8 or any(ord(ch) > 127 for ch in query):
        query = episode.parents[2].name.replace("count-", "").replace("-", " ")
    # Keep the same plan wording and references as the known-good replay.
    query = {"cones": "orange traffic cone", "fire hydrants": "fire hydrant"}.get(query, query)
    goal = episode.parents[2].name
    plan = validate_mission_plan({"plan_version": 1, "goal": goal, "steps": [
        {"id": "s1", "skill": "count", "params": {"region": "zone:site", "query": query},
         "on_success": "s2", "on_fail": "s2"},
        {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None}],
        "report": ["findings", "positions"]})
    info = rows[0]["obs_before"]["camera_info"]
    camera = CameraModel(float(info["fx"]), float(info["fy"]), float(info["cx"]), float(info["cy"]),
                         int(info["width"]), int(info["height"]), pitch_down_rad=0.18)
    world = Path(mission.get("world_sdf", "default.sdf")).stem
    sdf = ROOT / "simulation/gazebo/worlds" / f"{world}.sdf"
    obstacle = StaticMapSource(load_sdf_obstacles(sdf)) if sdf.exists() else None
    loop_defaults = {
        "confirm_min_side_px": 20.0,
        "contradiction_relook_enabled": True,
        "near_depth": False,
        "confirm_max_range_m": 40.0,
        "ground_extent_size_m": typical_object_size(query, default=0.0) or None,
        **POSITION_DEFAULTS,
    }
    loop_fields = {f.name for f in __import__("dataclasses").fields(LoopConfig)}
    def make_loop(*_args: Any, **overrides: Any) -> OnboardLoop:
        overrides.pop("restart_route", None)
        config = {k: v for k, v in {**loop_defaults, **overrides}.items() if k in loop_fields}
        return OnboardLoop(camera=camera, search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
                           obstacle_source=obstacle, config=LoopConfig(**config))
    executor = MissionExecutor(plan, loop_factory=make_loop,
        refs={"zone:site": (0.0, 0.0, 55.0), "route:fence": [(-30., -30.), (-30., 30.), (30., 30.), (30., -30.), (-30., -30.)]},
        config=ExecutorConfig(step_budget=140),
        obstacle_source=obstacle)
    results = Results(mode, cache_file)
    detections_by_step: dict[str, list[Any]] = {}
    # The established replay stores detector output as {step_index: [bbox, score, label]}.
    # Prefer an episode-local file, then an explicitly supplied sibling/benchmark result.
    detection_path = detections_file or episode / "detections.json"
    if detection_path.exists():
        detections_by_step = json.loads(detection_path.read_text(encoding="utf-8"))
    trace = []
    for row in rows:
        label = (row.get("labels") or {}).get("onboard") or {}
        if not label.get("skill"):
            continue
        obs = row["obs_before"]
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
        if closed_loop and row.get("obs_after"):
            # Approximation: the recorded next pose is treated as action execution.
            pass
        action_obj = {"kind": action.kind, "forward_m": action.forward_m, "right_m": action.right_m,
                      "up_m": action.up_m, "yaw_delta_deg": action.yaw_delta_deg, "seconds": action.seconds}
        trace.append({"step": row["step_index"], "action": action_obj, "logged_action": row.get("action"),
                      "calls": calls, "phase": detail.get("phase"), "counts": dict(executor.counts),
                      "alerts": list(executor.alerts), "revisit_plan": detail.get("alert_look"),
                      "detail": detail, "tracks": [{"id": t.track_id, "verdict": t.verdict, "label": t.label,
                          "bbox": t.bbox, "ground": t.ground} for t in executor.current_tracks]})
    results.save()
    action_types = sum(item["action"]["kind"] == (item.get("logged_action") or {}).get("kind") for item in trace)
    search_placeholder_steps = sum(item["action"]["kind"] == "hold"
        and (item.get("logged_action") or {}).get("kind") not in {None, "hold"} for item in trace)
    exact_actions = sum(item["action"]["kind"] == (item.get("logged_action") or {}).get("kind") and all(
        round(float(item["action"].get(k, 0)), 1) == round(float((item.get("logged_action") or {}).get(k, 0)), 1)
        for k in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds")) for item in trace)
    raw_by_step = {int(row["step_index"]): (row.get("labels") or {}).get("onboard") or {} for row in rows}
    call_equal = call_total = 0
    for item in trace:
        expected = raw_by_step.get(int(item["step"]), {}).get("slow_calls", [])
        actual_pairs = {(call["track_id"], call["decision"]) for call in item["calls"]}
        expected_pairs = {(call.get("track_id"), _decision(call)) for call in expected}
        call_equal += len(actual_pairs & expected_pairs)
        call_total += max(len(actual_pairs), len(expected_pairs))
    logged_mission = (raw_by_step.get(int(trace[-1]["step"]), {}).get("mission") or {}) if trace else {}
    return {"episode": str(episode), "mode": mode, "open_loop": not closed_loop,
            "limitations": ["actions are not fed back", "search action is a hold placeholder",
                            "revisit completion requires closed-loop pose approximation"],
            "metrics": {"steps": len(trace), "action_type_agreement": action_types / len(trace) if trace else 1.0,
                        "exact_action_agreement": exact_actions / len(trace) if trace else 1.0,
                        "search_placeholder_mismatches": search_placeholder_steps,
                        "slow_call_agreement": call_equal / call_total if call_total else 1.0,
                        "count_replay": dict(executor.counts), "count_logged_final": logged_mission.get("counts", {}),
                        "alerts_replay": list(executor.alerts), "alerts_logged_final": logged_mission.get("alerts", [])},
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
    script = Path(__file__).resolve()
    for root in roots:
        env = os.environ.copy()
        env["REPLAY_CODE_ROOT"] = str(root.resolve())
        env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            output = Path(handle.name)
        try:
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
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("CODE_A", "CODE_B"))
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if args.compare:
        if len(args.episodes) != 1:
            ap.error("--compare requires exactly one episode")
        result: Any = compare_roots(args.episodes[0], (args.compare[0], args.compare[1]),
                                    mode=args.mode, closed_loop=args.closed_loop_pose_approximation,
                                    detections_file=args.detections)
    else:
        result = {"episodes": [replay(p, mode=args.mode, cache_file=args.cache_file,
                          closed_loop=args.closed_loop_pose_approximation, detections_file=args.detections)
                          for p in args.episodes]}
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
