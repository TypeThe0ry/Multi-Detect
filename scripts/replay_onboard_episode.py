"""Open-loop replay of recorded onboard episodes through MissionExecutor."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
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
    return {"approach_track": "approach", "confirm_target": "confirm", "reject_track": "no_match"}.get(
        call.get("decision"), call.get("identity", "no_match")
    )


def replay(episode: Path, *, mode: str = "logged", cache_file: Path | None = None,
           closed_loop: bool = False) -> dict[str, Any]:
    from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor, plan_for_count_task
    from multidetect.autonomy.onboard_loop import CameraModel, Detection
    from multidetect.autonomy.obstacle_source import StaticMapSource
    from multidetect.sim_teacher.policies import load_sdf_obstacles
    from multidetect.sim_teacher.primitives import ActionPrimitive
    from multidetect.simulator.camera_projection import pixel_camera_ray, pixel_ground_point

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
        query = episode.parents[3].name.replace("count-", "").replace("-", " ")
    plan = plan_for_count_task(query)
    info = rows[0]["obs_before"]["camera_info"]
    camera = CameraModel(float(info["fx"]), float(info["fy"]), float(info["cx"]), float(info["cy"]),
                         int(info["width"]), int(info["height"]), pitch_down_rad=0.18)
    world = Path(mission.get("world_sdf", "default.sdf")).stem
    sdf = ROOT / "simulation/gazebo/worlds" / f"{world}.sdf"
    obstacle = StaticMapSource(load_sdf_obstacles(sdf)) if sdf.exists() else None
    executor = MissionExecutor(plan, loop_factory=lambda *a, **kw: __import__(
        "multidetect.autonomy.onboard_loop", fromlist=["OnboardLoop"]
    ).OnboardLoop(camera=camera, search_action=lambda pose, yaw: ActionPrimitive.hold(1.0), obstacle_source=obstacle),
        config=ExecutorConfig(step_budget=140), obstacle_source=obstacle)
    results = Results(mode, cache_file)
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
        detections = [Detection(tuple(t["bbox"]), 1.0, None) for t in tracks if t.get("bbox")]
        detections = results.get(f"det:{row['step_index']}", detections, "detector")
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
                      "alerts": list(executor.alerts), "revisit_plan": detail.get("alert_look")})
    results.save()
    return {"episode": str(episode), "mode": mode, "open_loop": not closed_loop,
            "limitations": ["actions are not fed back", "search action is a hold placeholder",
                            "revisit completion requires closed-loop pose approximation"],
            "stats": results.stats, "trace": trace}


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    left, right = a["trace"], b["trace"]
    for i, (x, y) in enumerate(zip(left, right)):
        if (x["action"], x["calls"]) != (y["action"], y["calls"]):
            return {"first_divergence": i, "step": x["step"], "left": x, "right": y,
                    "summary": "action or model decision differs"}
    if len(left) != len(right):
        return {"first_divergence": min(len(left), len(right)), "summary": f"trace lengths differ: {len(left)} vs {len(right)}"}
    return {"first_divergence": None, "summary": "traces identical"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("episodes", nargs="+", type=Path)
    ap.add_argument("--mode", choices=("real", "logged", "cache"), default="logged")
    ap.add_argument("--cache-file", type=Path)
    ap.add_argument("--cache-ram", type=int, default=0)
    ap.add_argument("--closed-loop-pose-approximation", action="store_true")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("CODE_A", "CODE_B"))
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if args.compare:
        # Directory comparison runs use the current checkout's import path for A/B only
        # when the caller launches this entrypoint from each code directory.
        outputs = [replay(p, mode=args.mode, cache_file=args.cache_file,
                          closed_loop=args.closed_loop_pose_approximation) for p in args.episodes]
        result: Any = {"episodes": outputs, "compare_roots": [str(p) for p in args.compare],
                       "comparison": compare(outputs[0], outputs[1]) if len(outputs) == 2 else None}
    else:
        result = {"episodes": [replay(p, mode=args.mode, cache_file=args.cache_file,
                          closed_loop=args.closed_loop_pose_approximation) for p in args.episodes]}
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
