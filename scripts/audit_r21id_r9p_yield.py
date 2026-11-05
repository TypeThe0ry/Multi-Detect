#!/usr/bin/env python3
"""CPU-only yield audit for the r21id-r9p pilot.

The audit reads the collected episode artifacts and the generated scenario
truth/teacher-intent sidecars.  It never writes below ``artifacts/``.  Output
contains the round-state status, observation-frame counts, teacher labels,
truth-geometry projections for the paired confuser, and a conservative
world/proposal yield estimate based on independent RGB paths.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from PIL import Image

from multidetect.simulator.camera_projection import project_target_bbox, visible_fraction
from scripts.check_sim_teacher_smoke import evaluate_episode
from scripts.audit_r21id_r4_collection import REQUIRED_VIEWS
from multidetect.sim_teacher.policies import load_sdf_obstacles


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _world_id(value: str) -> str:
    match = re.search(r"gen_r21id_r9p_w\d{4}", value)
    return match.group(0) if match else "unknown"


def _ep_index(key: str) -> int | None:
    match = re.search(r":(\d+)$", key)
    return int(match.group(1)) if match else None


def _episode_dir(path: str | Path) -> Path:
    p = Path(path)
    return p.parent if p.name == "episode.json" else p


def _target(truth: dict[str, Any], *, target_id: str | None = None, confuser: bool = False) -> dict[str, Any] | None:
    for item in truth.get("targets", []):
        if target_id and item.get("target_id") == target_id:
            return item
    for item in truth.get("targets", []):
        if confuser and item.get("confuser"):
            return item
        if not confuser and item.get("true_target"):
            return item
    return None


def _rgb_path(episode_dir: Path, obs: dict[str, Any]) -> Path | None:
    value = obs.get("rgb_path")
    if not isinstance(value, str):
        return None
    path = episode_dir / Path(value.replace("\\", "/"))
    return path if path.is_file() else None


def _pose_distance(pose: dict[str, Any], target: dict[str, Any]) -> tuple[float, float]:
    pos = pose.get("local_ned_m") or (0.0, 0.0, 0.0)
    metric = target.get("pose") or {}
    dn = float(pos[0]) - float(metric.get("y_north_m", 0.0))
    de = float(pos[1]) - float(metric.get("x_east_m", 0.0))
    du = -float(pos[2]) - float(metric.get("z_up_m", 0.0))
    ground = math.hypot(dn, de)
    return ground, math.sqrt(dn * dn + de * de + du * du)


def _project_confuser(
    *, obs: dict[str, Any], confuser: dict[str, Any], image_size: tuple[int, int], obstacles: tuple[Any, ...]
) -> dict[str, Any]:
    metric = confuser.get("pose") or {}
    size = tuple(confuser.get("size_m") or confuser.get("measured_size_m") or (1.0, 1.0, 1.0))
    bottom = confuser.get("bottom_offset_m", confuser.get("mesh_bottom_offset_m"))
    target_ned = (
        float(metric.get("y_north_m", 0.0)),
        float(metric.get("x_east_m", 0.0)),
        float(metric.get("z_up_m", 0.0)),
    )
    box = project_target_bbox(
        target_ned=target_ned,
        pose={**(obs.get("pose") or {}), "attitude": obs.get("attitude") or {}},
        camera_info=obs.get("camera_info") or {},
        image_size=image_size,
        target_size_m=size,
        target_yaw_rad=float(metric.get("yaw_rad", 0.0)),
        target_bottom_offset_m=float(bottom) if bottom is not None else None,
    )
    visible = 0.0
    if box is not None:
        visible = visible_fraction(
            pose={**(obs.get("pose") or {}), "attitude": obs.get("attitude") or {}},
            target_ned=target_ned,
            target_size_m=size,
            target_yaw_rad=float(metric.get("yaw_rad", 0.0)),
            obstacles=obstacles,
            target_bottom_offset_m=float(bottom) if bottom is not None else None,
        )
    side = min(box[2] - box[0], box[3] - box[1]) if box is not None else 0.0
    area = max(0.0, (box[2] - box[0]) * (box[3] - box[1])) if box is not None else 0.0
    return {"bbox": [round(float(v), 3) for v in box] if box else None, "short_side_px": round(side, 3), "area_px": round(area, 3), "visible_fraction": round(float(visible), 4)}


def _failure_group(value: Any) -> str:
    text = str(value or "<missing>")
    if "simulation clock stalled" in text:
        return "PX4 仿真时钟停滞"
    if "wire stream ended early" in text:
        return "传感器 wire stream 提前结束"
    if "takeoff_reached" in text:
        return "等待起飞超时"
    if "pre-arm health" in text:
        return "PX4 起飞前健康检查"
    return text.split(":", 1)[0]


def _read_steps(episode_dir: Path) -> list[dict[str, Any]]:
    path = episode_dir / "steps.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _episode_report(episode_dir: Path, truth: dict[str, Any], intent: dict[str, Any], sdf_path: Path) -> dict[str, Any]:
    episode = _json(episode_dir / "episode.json")
    target = _target(truth)
    confuser = _target(truth, confuser=True)
    obstacles = tuple(load_sdf_obstacles(sdf_path)) if sdf_path.is_file() else ()
    steps = _read_steps(episode_dir)
    views = Counter()
    observe_views = Counter()
    target_in_fov = Counter()
    projected: dict[str, dict[str, Any]] = {}
    confuser_projected = Counter()
    confuser_qualified = Counter()
    confuser_short_sides: list[float] = []
    confuser_paths: set[str] = set()
    confuser_qualified_paths: set[str] = set()
    min_ground = math.inf
    min_slant = math.inf
    nearest_step = None
    path_length = 0.0
    previous = None
    for step in steps:
        labels = step.get("labels") or {}
        view = str(step.get("observation_view") or labels.get("observation_view") or "unknown")
        if view in REQUIRED_VIEWS:
            views[view] += 1
            if labels.get("phase") == "observe":
                observe_views[view] += 1
            if labels.get("target_in_fov") is True:
                target_in_fov[view] += 1
        obs = step.get("obs_before") or {}
        pose = obs.get("pose") or {}
        if previous is not None:
            a, b = previous, pose.get("local_ned_m") or (0.0, 0.0, 0.0)
            path_length += math.sqrt(sum((float(b[i]) - float(a[i])) ** 2 for i in range(3)))
        previous = pose.get("local_ned_m") or previous
        if confuser is None:
            continue
        ground, slant = _pose_distance(pose, confuser)
        if ground < min_ground:
            min_ground, min_slant, nearest_step = ground, slant, step.get("step_index")
        for side in ("before", "after"):
            side_obs = step.get("obs_" + side) or {}
            frame = _rgb_path(episode_dir, side_obs)
            if frame is None:
                continue
            with Image.open(frame) as image:
                image_size = image.size
            geom = _project_confuser(obs=side_obs, confuser=confuser, image_size=image_size, obstacles=obstacles)
            projected[view] = geom
            key = frame.resolve().as_posix()
            if geom["bbox"] is not None:
                confuser_projected[view] += 1
                confuser_short_sides.append(float(geom["short_side_px"]))
                confuser_paths.add(key)
                if geom["short_side_px"] >= 8.0 and geom["area_px"] >= 4.0 and geom["visible_fraction"] >= 0.3:
                    confuser_qualified[view] += 1
                    confuser_qualified_paths.add(key)
    conf_plan = intent.get("confuser_observation") or {}
    waypoints = conf_plan.get("views") or []
    plan_min = math.inf
    trajectory = []
    for row in waypoints:
        wp = row.get("observation_waypoint") or {}
        if confuser is None:
            continue
        dn = float(wp.get("y_north_m", 0.0)) - float((confuser.get("pose") or {}).get("y_north_m", 0.0))
        de = float(wp.get("x_east_m", 0.0)) - float((confuser.get("pose") or {}).get("x_east_m", 0.0))
        trajectory.append({"view": row.get("view"), "ground_distance_m": round(math.hypot(dn, de), 3)})
    # The recorded truth route is target-centred.  A confuser visit is counted
    # only when an actual camera pose enters the intended confuser standoff
    # (or, as a secondary reachability measure, the declared detection range).
    standoff = float(conf_plan.get("standoff_m", 0.0) or 0.0)
    detection = float(conf_plan.get("max_detection_range_m", math.inf))
    return {
        "episode_id": episode.get("episode_id"),
        "status": episode.get("status"),
        "task_text": episode.get("task_text"),
        "step_count": len(steps),
        "views": dict(views),
        "observe_views": dict(observe_views),
        "target_in_fov_views": dict(target_in_fov),
        "path_length_m": round(path_length, 3),
        "confuser": {"target_id": confuser.get("target_id") if confuser else None, "min_ground_distance_m": round(min_ground, 3) if math.isfinite(min_ground) else None, "min_slant_distance_m": round(min_slant, 3) if math.isfinite(min_slant) else None, "nearest_step": nearest_step, "standoff_m": standoff, "max_detection_range_m": detection, "within_standoff": bool(math.isfinite(min_slant) and min_slant <= standoff + 2.0), "within_detection_range": bool(math.isfinite(min_slant) and min_slant <= detection), "planned_view_ground_offsets_m": trajectory, "projected_frame_count_by_view": dict(confuser_projected), "qualified_geometry_frame_count_by_view": dict(confuser_qualified), "unique_projected_rgb": len(confuser_paths), "unique_qualified_rgb": len(confuser_qualified_paths), "projected_short_side_min_px": round(min(confuser_short_sides), 3) if confuser_short_sides else None, "projected_short_side_max_px": round(max(confuser_short_sides), 3) if confuser_short_sides else None, "last_projected_by_view": projected},
    }


def audit(*, episodes_root: Path, round_state_path: Path, index_path: Path, scenarios: Path, sdf_root: Path) -> dict[str, Any]:
    round_state = _json(round_state_path)
    index = _json(index_path)
    by_world = {row["world_id"]: row for row in index.get("worlds", [])}
    complete = []
    logical_failed = []
    for key, entry in sorted((round_state.get("episodes") or {}).items()):
        world = _world_id(key)
        if entry.get("status") == "complete" and entry.get("path"):
            ep_dir = _episode_dir(entry["path"])
            scenario = scenarios / f"{world}.teacher_intent.json"
            truth_path = scenarios / f"{world}.truth.json"
            smoke = evaluate_episode(ep_dir, intent_path=scenario, truth_path=truth_path, minimum_frames=8)
            complete.append({"key": key, "world_id": world, "path": str(ep_dir), "smoke": smoke, "yield": _episode_report(ep_dir, _json(truth_path), _json(scenario), sdf_root / f"{world}.sdf")})
        elif entry.get("status") == "failed":
            logical_failed.append({"key": key, "world_id": world, "attempts": entry.get("attempts_total", entry.get("attempts"))})
    failed_artifacts = []
    for path in sorted(episodes_root.rglob("episode.json")):
        episode = _json(path)
        if episode.get("status") == "failed":
            failed_artifacts.append({"world_id": _world_id(str(path)), "path": str(path.parent), "failure": episode.get("failure"), "failure_group": _failure_group(episode.get("failure")), "step_count": episode.get("step_count")})
    worlds = []
    for world, spec in sorted(by_world.items()):
        truth = _json(scenarios / f"{world}.truth.json")
        intent = _json(scenarios / f"{world}.teacher_intent.json")
        target = _target(truth)
        confuser = _target(truth, confuser=True)
        rows = [row for row in complete if row["world_id"] == world]
        qualified = [row for row in rows if row["smoke"].get("passed")]
        yield_rows = [row["yield"] for row in qualified]
        views = {view: sum(int(row["confuser"]["qualified_geometry_frame_count_by_view"].get(view, 0)) for row in yield_rows) for view in REQUIRED_VIEWS}
        unique = sum(int(row["confuser"]["unique_qualified_rgb"]) for row in yield_rows)
        worlds.append({"world_id": world, "direction": spec.get("r9p_direction"), "target_category": spec.get("target_combo", {}).get("category"), "paired_confusers": spec.get("paired_confusers", []), "object_count": len(truth.get("targets", [])), "target_id": target.get("target_id") if target else None, "target_pose": target.get("pose") if target else None, "target_size_m": target.get("size_m") if target else None, "confuser_id": confuser.get("target_id") if confuser else None, "confuser_pose": confuser.get("pose") if confuser else None, "confuser_size_m": confuser.get("size_m") if confuser else None, "designed_target_ground_distance_m": spec.get("view_geometry", {}).get("ground_distance_m"), "target_short_side_px_estimate": spec.get("view_geometry", {}).get("target_short_side_px_estimate"), "confuser_short_side_px_design": (spec.get("view_geometry", {}).get("confuser_observation") or {}).get("projected_width_px"), "teacher_intent": {"target_observation": intent.get("observation", {}), "confuser_observation": intent.get("confuser_observation", {})}, "complete_count": len(rows), "qualified_count": len(qualified), "confuser_unique_qualified_rgb": unique, "confuser_qualified_by_view": views, "episodes": yield_rows, "logical_failed_count": sum(1 for row in logical_failed if row["world_id"] == world), "failed_artifact_count": sum(1 for row in failed_artifacts if row["world_id"] == world), "failure_groups": dict(Counter(row["failure_group"] for row in failed_artifacts if row["world_id"] == world))})
    # The r8 proposal uses 30 car->mailbox and 18 suv->postbox worlds,
    # two episodes per world (48 worlds / 96 episodes total).
    proposal_worlds = {"car->mailbox": 30, "suv->postbox": 18}
    by_direction = defaultdict(list)
    for row in worlds:
        by_direction[row["direction"]].append(row)
    proposal = {}
    for direction, rows in by_direction.items():
        episode_yields = [ep["confuser"]["unique_qualified_rgb"] for row in rows for ep in row["episodes"]]
        per_episode = sum(episode_yields) / len(episode_yields) if episode_yields else 0.0
        per_world = sum(row["confuser_unique_qualified_rgb"] for row in rows) / len(rows) if rows else 0.0
        world_count = proposal_worlds.get(direction, 0)
        episode_count = world_count * 2
        proposal[direction] = {"pilot_worlds": len(rows), "pilot_complete_episodes": len(episode_yields), "pilot_unique_rgb": sum(episode_yields), "pilot_mean_rgb_per_episode": round(per_episode, 3), "pilot_mean_rgb_per_world": round(per_world, 3), "proposal_worlds": world_count, "proposal_episodes": episode_count, "proposal_total_worlds": 48, "proposal_total_episodes": 96, "projected_unique_rgb": round(per_episode * episode_count, 1), "gate_total": 96, "gate_per_view": 8, "pilot_view_counts": {view: sum(row["confuser_qualified_by_view"].get(view, 0) for row in rows) for view in REQUIRED_VIEWS}, "projected_view_counts": {view: round(sum(row["confuser_qualified_by_view"].get(view, 0) for row in rows) / max(1, len(episode_yields)) * episode_count, 1) for view in REQUIRED_VIEWS}}
    return {"schema_version": 1, "generated_on": __import__("datetime").date.today().isoformat(), "pilot": {"logical_entries": len(round_state.get("episodes", {})), "complete": len(complete), "logical_failed": logical_failed, "failed_artifacts": failed_artifacts, "complete_reports": complete}, "worlds": worlds, "proposal_48_worlds": proposal}


def render(report: dict[str, Any]) -> str:
    lines = ["# r21id-r9p CPU 产出审计", "", f"生成日期：{report['generated_on']}。只读 r9pa artifacts、round-state、truth 与 teacher intent。", "", "## 世界与架次", "", "| world | 方向 | objects | complete/合格 | 失败逻辑/失败 artifact | 目标距离 m | confuser 尺寸 m | confuser 合格 RGB（front/oblique/edge/recovery） |", "|---|---|---:|---:|---:|---:|---|---|"]
    for row in report["worlds"]:
        v = "/".join(str(row["confuser_qualified_by_view"].get(view, 0)) for view in REQUIRED_VIEWS)
        lines.append(f"| {row['world_id']} | {row['direction']} | {row['object_count']} | {row['complete_count']}/{row['qualified_count']} | {row['logical_failed_count']}/{row['failed_artifact_count']} | {row['designed_target_ground_distance_m']} | {row['confuser_size_m']} | {v} |")
    lines += ["", "## 48-world 推算", "", "| 方向 | pilot 合格 RGB | 平均/episode | 推算 48 episode | pilot view | 推算 view | gate |", "|---|---:|---:|---:|---|---|---|"]
    for direction, row in sorted(report["proposal_48_worlds"].items()):
        pv = "/".join(str(row["pilot_view_counts"].get(view, 0)) for view in REQUIRED_VIEWS)
        xv = "/".join(str(row["projected_view_counts"].get(view, 0)) for view in REQUIRED_VIEWS)
        lines.append(f"| {direction} | {row['pilot_unique_rgb']} | {row['pilot_mean_rgb_per_episode']} | {row['projected_unique_rgb']} | {pv} | {xv} | ≥96 total、每视角 ≥8 |")
    lines += ["", "## 失败 artifact", "", "| world | 原因 | steps |", "|---|---|---:|"]
    for row in report["pilot"]["failed_artifacts"]:
        lines.append(f"| {row['world_id']} | {row['failure_group']} | {row['step_count']} |")
    lines += ["", "## 口径", "", "- confuser 合格 RGB 是 truth geometry 投影后短边 ≥8 px、面积 ≥4 px²、visible_fraction ≥0.3 的独立 RGB 路径；它是 CPU 几何上限/代理，不等同于 YOLOE proposal 命中。", "- 实际 confuser visit 同时记录每个 complete episode 到 confuser 的最近 ground/slant 距离，并与 teacher intent 的 standoff/detection range 比较。", "- 失败相关性用对象数、目标/干扰物 mesh 尺寸、设计目标距离和实际路径长度对照；PX4 clock/wire/takeoff failures are infrastructure-level signals unless a repeated world-specific pattern is visible。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes-root", type=Path, default=Path("artifacts/r21id-r9pa/episodes"))
    parser.add_argument("--round-state", type=Path, default=Path("artifacts/r21id-r9pa/pipeline/round-state.json"))
    parser.add_argument("--index", type=Path, default=Path("configs/scenarios/generated-r21id-r9p/index.json"))
    parser.add_argument("--scenarios", type=Path, default=Path("configs/scenarios/generated-r21id-r9p"))
    parser.add_argument("--sdf-root", type=Path, default=Path("simulation/gazebo/worlds"))
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(episodes_root=args.episodes_root, round_state_path=args.round_state, index_path=args.index, scenarios=args.scenarios, sdf_root=args.sdf_root)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(render(report), encoding="utf-8")
    print(json.dumps({"output_json": str(args.output_json), "output_md": str(args.output_md)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
