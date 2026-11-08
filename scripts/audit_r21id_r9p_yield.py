#!/usr/bin/env python3
"""CPU-only yield audit for generated r21id identity pilots.

The audit reads the collected episode artifacts and the generated scenario
truth/teacher-intent sidecars.  It never writes below ``artifacts/``.  Output
contains the round-state status, observation-frame counts, teacher labels,
    truth-geometry projections for the paired confuser, optional identity/SFT
    rows, and a conservative 48-world yield estimate based on independent RGB
    paths.  The input artifacts are read-only; output paths are caller-owned.
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


def _world_id(value: str, prefix: str = "r9p") -> str:
    match = re.search(rf"gen_r21id_{re.escape(prefix)}_w\d{{4}}", value)
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
    qualified_records: dict[str, dict[str, Any]] = {}
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
                    qualified_records.setdefault(key, {
                        "path": key,
                        "view": view,
                        "step_index": step.get("step_index"),
                        "side": side,
                        "short_side_px": geom["short_side_px"],
                        "area_px": geom["area_px"],
                        "visible_fraction": geom["visible_fraction"],
                    })
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
        "confuser": {"target_id": confuser.get("target_id") if confuser else None, "min_ground_distance_m": round(min_ground, 3) if math.isfinite(min_ground) else None, "min_slant_distance_m": round(min_slant, 3) if math.isfinite(min_slant) else None, "nearest_step": nearest_step, "standoff_m": standoff, "max_detection_range_m": detection, "within_standoff": bool(math.isfinite(min_slant) and min_slant <= standoff + 2.0), "within_detection_range": bool(math.isfinite(min_slant) and min_slant <= detection), "planned_view_ground_offsets_m": trajectory, "projected_frame_count_by_view": dict(confuser_projected), "qualified_geometry_frame_count_by_view": dict(confuser_qualified), "unique_projected_rgb": len(confuser_paths), "unique_qualified_rgb": len(confuser_qualified_paths), "qualified_geometry_frames": sorted(qualified_records.values(), key=lambda row: row["path"]), "projected_short_side_min_px": round(min(confuser_short_sides), 3) if confuser_short_sides else None, "projected_short_side_max_px": round(max(confuser_short_sides), 3) if confuser_short_sides else None, "last_projected_by_view": projected},
    }


def _identity_rows(path: Path | None, pair: str | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if pair and row.get("confusable_pair_type") != pair:
            continue
        if row.get("label_type") not in {"confusable_positive", "confusable_negative"}:
            continue
        image = row.get("image_path")
        if not isinstance(image, str):
            continue
        rows.append({
            "path": Path(image.replace("\\", "/")).resolve().as_posix(),
            "label_type": row.get("label_type"),
            "decision": (row.get("assistant_target") or {}).get("decision"),
            "world": row.get("world"),
        })
    return rows


def _attach_identity_metrics(report: dict[str, Any], *, sft_path: Path | None, pair: str | None) -> None:
    rows = _identity_rows(sft_path, pair)
    by_path: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_path[row["path"]].add(str(row["label_type"]))
    for world in report["worlds"]:
        teacher_target = report.get("audit_inputs", {}).get("teacher_target", "target")
        complete = [item for item in report["pilot"]["complete_reports"] if item["world_id"] == world["world_id"] and (item["smoke"].get("passed") if teacher_target == "target" else item["yield"]["confuser"].get("within_detection_range"))]
        eligible_by_view: dict[str, set[str]] = defaultdict(set)
        for item in complete:
            for frame in item["yield"]["confuser"].get("qualified_geometry_frames", []):
                eligible_by_view[str(frame["view"])].add(str(frame["path"]))
        identity_by_view = {view: set() for view in REQUIRED_VIEWS}
        reject_by_view = {view: set() for view in REQUIRED_VIEWS}
        for path, labels in by_path.items():
            for view, eligible in eligible_by_view.items():
                if path not in eligible:
                    continue
                if "confusable_positive" in labels:
                    identity_by_view[view].add(path)
                if "confusable_negative" in labels:
                    reject_by_view[view].add(path)
        world["identity_reached_by_view"] = {view: len(identity_by_view[view]) for view in REQUIRED_VIEWS}
        world["usable_rejection_by_view"] = {view: len(reject_by_view[view]) for view in REQUIRED_VIEWS}
        world["identity_reached_unique_rgb"] = sum(len(value) for value in identity_by_view.values())
        world["usable_rejection_unique_rgb"] = sum(len(value) for value in reject_by_view.values())
        world["identity_source_rows"] = sum(1 for row in rows if row["world"] == world["world_id"] and row["label_type"] == "confusable_positive")
        world["rejection_source_rows"] = sum(1 for row in rows if row["world"] == world["world_id"] and row["label_type"] == "confusable_negative")
        world["identity_evidence"] = "builder SFT confusable_positive rows intersected with CPU geometry-qualified RGB" if sft_path else "未提供 SFT builder 输出"


def audit(*, episodes_root: Path, round_state_path: Path, index_path: Path, scenarios: Path, sdf_root: Path, world_prefix: str = "r9p", direction_field: str | None = None, teacher_target: str = "target", sft_path: Path | None = None, identity_pair: str | None = None, proposal_worlds: int = 48, episodes_per_world: int = 2) -> dict[str, Any]:
    round_state = _json(round_state_path)
    index = _json(index_path)
    by_world = {row["world_id"]: row for row in index.get("worlds", [])}
    complete = []
    logical_failed = []
    for key, entry in sorted((round_state.get("episodes") or {}).items()):
        world = _world_id(key, world_prefix)
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
            failed_artifacts.append({"world_id": _world_id(str(path), world_prefix), "path": str(path.parent), "failure": episode.get("failure"), "failure_group": _failure_group(episode.get("failure")), "step_count": episode.get("step_count")})
    worlds = []
    for world, spec in sorted(by_world.items()):
        truth = _json(scenarios / f"{world}.truth.json")
        intent = _json(scenarios / f"{world}.teacher_intent.json")
        target = _target(truth)
        confuser = _target(truth, confuser=True)
        rows = [row for row in complete if row["world_id"] == world]
        if teacher_target == "confuser":
            # r9q intentionally records the paired mailbox route while the
            # task text still names the car.  The r9p target-route smoke check
            # therefore cannot be used as the qualification predicate.
            qualified = [row for row in rows if row["yield"]["confuser"].get("within_detection_range")]
        else:
            qualified = [row for row in rows if row["smoke"].get("passed")]
        yield_rows = [row["yield"] for row in qualified]
        views = {view: sum(int(row["confuser"]["qualified_geometry_frame_count_by_view"].get(view, 0)) for row in yield_rows) for view in REQUIRED_VIEWS}
        unique = sum(int(row["confuser"]["unique_qualified_rgb"]) for row in yield_rows)
        direction = spec.get(direction_field or "r9p_direction")
        predicted = (spec.get("view_geometry", {}).get("confuser_observation") or {}).get("per_view", [])
        worlds.append({"world_id": world, "direction": direction, "target_category": spec.get("target_combo", {}).get("category"), "paired_confusers": spec.get("paired_confusers", []), "object_count": len(truth.get("targets", [])), "target_id": target.get("target_id") if target else None, "target_pose": target.get("pose") if target else None, "target_size_m": target.get("size_m") if target else None, "confuser_id": confuser.get("target_id") if confuser else None, "confuser_pose": confuser.get("pose") if confuser else None, "confuser_size_m": confuser.get("size_m") if confuser else None, "designed_target_ground_distance_m": spec.get("view_geometry", {}).get("ground_distance_m"), "target_short_side_px_estimate": spec.get("view_geometry", {}).get("target_short_side_px_estimate"), "confuser_short_side_px_design": [row.get("minimum_short_side_px") for row in predicted], "confuser_qualifying_frames_per_view_design": (spec.get("view_geometry", {}).get("confuser_observation") or {}).get("qualifying_frames_per_view", {}), "teacher_intent": {"target_observation": intent.get("observation", {}), "confuser_observation": intent.get("confuser_observation", {})}, "complete_count": len(rows), "qualified_count": len(qualified), "confuser_unique_qualified_rgb": unique, "confuser_qualified_by_view": views, "episodes": yield_rows, "logical_failed_count": sum(1 for row in logical_failed if row["world_id"] == world), "failed_artifact_count": sum(1 for row in failed_artifacts if row["world_id"] == world), "failure_groups": dict(Counter(row["failure_group"] for row in failed_artifacts if row["world_id"] == world))})
    by_direction = defaultdict(list)
    for row in worlds:
        by_direction[row["direction"]].append(row)
    proposal = {}
    for direction, rows in by_direction.items():
        episode_yields = [ep["confuser"]["unique_qualified_rgb"] for row in rows for ep in row["episodes"]]
        per_episode = sum(episode_yields) / len(episode_yields) if episode_yields else 0.0
        per_world = sum(row["confuser_unique_qualified_rgb"] for row in rows) / len(rows) if rows else 0.0
        default_direction_worlds = {"car->mailbox": 30, "suv->postbox": 18}
        world_count = proposal_worlds if len(by_direction) == 1 else default_direction_worlds.get(direction, 0)
        episode_count = world_count * episodes_per_world
        proposal[direction] = {"pilot_worlds": len(rows), "pilot_complete_episodes": len(episode_yields), "pilot_unique_rgb": sum(episode_yields), "pilot_mean_rgb_per_episode": round(per_episode, 3), "pilot_mean_rgb_per_world": round(per_world, 3), "proposal_worlds": world_count, "proposal_episodes": episode_count, "proposal_total_worlds": 48, "proposal_total_episodes": 96, "projected_unique_rgb": round(per_episode * episode_count, 1), "gate_total": 96, "gate_per_view": 8, "pilot_view_counts": {view: sum(row["confuser_qualified_by_view"].get(view, 0) for row in rows) for view in REQUIRED_VIEWS}, "projected_view_counts": {view: round(sum(row["confuser_qualified_by_view"].get(view, 0) for row in rows) / max(1, len(episode_yields)) * episode_count, 1) for view in REQUIRED_VIEWS}}
        proposal[direction].update({"pilot_identity_reached": sum(row.get("identity_reached_unique_rgb", 0) for row in rows), "pilot_rejections": sum(row.get("usable_rejection_unique_rgb", 0) for row in rows), "pilot_identity_by_view": {view: sum(row.get("identity_reached_by_view", {}).get(view, 0) for row in rows) for view in REQUIRED_VIEWS}, "pilot_rejections_by_view": {view: sum(row.get("usable_rejection_by_view", {}).get(view, 0) for row in rows) for view in REQUIRED_VIEWS}, "projected_identity_reached": round(sum(row.get("identity_reached_unique_rgb", 0) for row in rows) / max(1, len(episode_yields)) * episode_count, 1), "projected_rejections": round(sum(row.get("usable_rejection_unique_rgb", 0) for row in rows) / max(1, len(episode_yields)) * episode_count, 1), "projected_identity_by_view": {view: round(sum(row.get("identity_reached_by_view", {}).get(view, 0) for row in rows) / max(1, len(episode_yields)) * episode_count, 1) for view in REQUIRED_VIEWS}, "projected_rejections_by_view": {view: round(sum(row.get("usable_rejection_by_view", {}).get(view, 0) for row in rows) / max(1, len(episode_yields)) * episode_count, 1) for view in REQUIRED_VIEWS}})
    report = {"schema_version": 2, "generated_on": __import__("datetime").date.today().isoformat(), "pilot": {"logical_entries": len(round_state.get("episodes", {})), "complete": len(complete), "logical_failed": logical_failed, "failed_artifacts": failed_artifacts, "complete_reports": complete}, "worlds": worlds, "proposal_48_worlds": proposal, "audit_inputs": {"world_prefix": world_prefix, "direction_field": direction_field or "r9p_direction", "teacher_target": teacher_target, "sft_path": str(sft_path) if sft_path else None, "identity_pair": identity_pair, "proposal_worlds": proposal_worlds, "episodes_per_world": episodes_per_world}}
    _attach_identity_metrics(report, sft_path=sft_path, pair=identity_pair)
    # Metrics are attached after the world pass; recompute direction projections
    # so identity/rejection counts use the same deduped RGB gate as geometry.
    for direction, row in report["proposal_48_worlds"].items():
        matching = [world for world in worlds if world["direction"] == direction]
        episodes = sum(world["qualified_count"] for world in matching)
        count = row["proposal_episodes"]
        row["pilot_identity_reached"] = sum(world.get("identity_reached_unique_rgb", 0) for world in matching)
        row["pilot_rejections"] = sum(world.get("usable_rejection_unique_rgb", 0) for world in matching)
        row["pilot_identity_by_view"] = {view: sum(world.get("identity_reached_by_view", {}).get(view, 0) for world in matching) for view in REQUIRED_VIEWS}
        row["pilot_rejections_by_view"] = {view: sum(world.get("usable_rejection_by_view", {}).get(view, 0) for world in matching) for view in REQUIRED_VIEWS}
        row["projected_identity_reached"] = round(row["pilot_identity_reached"] / max(1, episodes) * count, 1)
        row["projected_rejections"] = round(row["pilot_rejections"] / max(1, episodes) * count, 1)
        row["projected_identity_by_view"] = {view: round(row["pilot_identity_by_view"][view] / max(1, episodes) * count, 1) for view in REQUIRED_VIEWS}
        row["projected_rejections_by_view"] = {view: round(row["pilot_rejections_by_view"][view] / max(1, episodes) * count, 1) for view in REQUIRED_VIEWS}
    return report


def render(report: dict[str, Any]) -> str:
    inputs = report.get("audit_inputs", {})
    lines = ["# r21id 产出审计", "", f"生成日期：{report['generated_on']}。只读采集 artifacts、round-state、truth、teacher intent 和已有 identity builder 输出。", "", "## 世界与架次", "", "| world | 方向 | objects | complete/合格 | 失败逻辑/失败 artifact | 目标距离 m | 预测短边 px（四视角） | 几何合格 RGB（四视角） | identity 触达（四视角） | car→mailbox 可用拒绝（四视角） |", "|---|---|---:|---:|---:|---|---|---|---|---|"]
    for row in report["worlds"]:
        geometry = "/".join(str(row["confuser_qualified_by_view"].get(view, 0)) for view in REQUIRED_VIEWS)
        predicted = "/".join(f"{float(value):g}" for value in row.get("confuser_short_side_px_design", [])) or "—"
        identity = "/".join(str(row.get("identity_reached_by_view", {}).get(view, 0)) for view in REQUIRED_VIEWS)
        rejects = "/".join(str(row.get("usable_rejection_by_view", {}).get(view, 0)) for view in REQUIRED_VIEWS)
        lines.append(f"| {row['world_id']} | {row['direction']} | {row['object_count']} | {row['complete_count']}/{row['qualified_count']} | {row['logical_failed_count']}/{row['failed_artifact_count']} | {row['designed_target_ground_distance_m']} | {predicted} | {geometry} | {identity} | {rejects} |")
    lines += ["", "## Episode 状态与失败类", "", "| logical episode | world | 状态 | attempts | 失败类 |", "|---|---|---|---:|---|"]
    for row in report["pilot"]["logical_failed"]:
        lines.append(f"| {row['key']} | {row['world_id']} | failed | {row.get('attempts') or '—'} | logical entry failed |")
    complete_keys = {row["key"]: row for row in report["pilot"]["complete_reports"]}
    for key, row in sorted(complete_keys.items()):
        lines.append(f"| {key} | {row['world_id']} | complete | — | — |")
    for row in report["pilot"]["failed_artifacts"]:
        lines.append(f"| artifact {Path(row['path']).parent.name} | {row['world_id']} | failed artifact | — | {row['failure_group']} |")
    lines += ["", "## 48-world 推算", "", "| 方向 | pilot 几何合格 | pilot identity 触达 | pilot 可用拒绝 | 推算几何 | 推算 identity | 推算可用拒绝 | 推算拒绝 view | gate |", "|---|---:|---:|---:|---:|---:|---:|---|---|"]
    for direction, row in sorted(report["proposal_48_worlds"].items()):
        xv = "/".join(str(row["projected_rejections_by_view"].get(view, 0)) for view in REQUIRED_VIEWS)
        lines.append(f"| {direction} | {row['pilot_unique_rgb']} | {row.get('pilot_identity_reached', 0)} | {row.get('pilot_rejections', 0)} | {row['projected_unique_rgb']} | {row.get('projected_identity_reached', 0)} | {row.get('projected_rejections', 0)} | {xv} | ≥96 total、每视角 ≥8 |")
    lines += ["", "## 失败 artifact", "", "| world | 原因 | steps |", "|---|---|---:|"]
    for row in report["pilot"]["failed_artifacts"]:
        lines.append(f"| {row['world_id']} | {row['failure_group']} | {row['step_count']} |")
    lines += ["", "## 口径", "", "- 几何合格 RGB 是 truth geometry 投影后短边 ≥8 px、面积 ≥4 px²、visible_fraction ≥0.3 的独立 RGB 路径；预测短边来自 index 的 confuser per-view 预测，计划每 view 采 8 帧。", "- identity 触达是已有 builder SFT 中 `confusable_positive` 的独立 RGB，并与本次 CPU 几何合格帧取交集；可用拒绝是 `confusable_negative` 的独立 RGB，并同样取交集。它们不是新的 GPU 推理结果。", "- 48-world 推算按合格逻辑 episode 的每 episode 平均值外推到 `proposal_worlds × episodes_per_world`，会保留 builder 的样本上限和路线偏差，不能替代新采集。", "- 失败类来自 episode artifact；同一 logical episode 可以最终 complete，但仍保留早期 failed attempt 的失败类。", "", f"已有 identity builder 输出：`{inputs.get('sft_path') or '未提供'}`。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes-root", type=Path, default=Path("artifacts/r21id-r9pa/episodes"))
    parser.add_argument("--round-state", type=Path, default=Path("artifacts/r21id-r9pa/pipeline/round-state.json"))
    parser.add_argument("--index", type=Path, default=Path("configs/scenarios/generated-r21id-r9p/index.json"))
    parser.add_argument("--scenarios", type=Path, default=Path("configs/scenarios/generated-r21id-r9p"))
    parser.add_argument("--sdf-root", type=Path, default=Path("simulation/gazebo/worlds"))
    parser.add_argument("--world-prefix", default="r9p")
    parser.add_argument("--direction-field", default=None)
    parser.add_argument("--teacher-target", choices=("target", "confuser"), default="target")
    parser.add_argument("--sft-jsonl", type=Path, default=None)
    parser.add_argument("--identity-pair", default=None)
    parser.add_argument("--proposal-worlds", type=int, default=48)
    parser.add_argument("--episodes-per-world", type=int, default=2)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(episodes_root=args.episodes_root, round_state_path=args.round_state, index_path=args.index, scenarios=args.scenarios, sdf_root=args.sdf_root, world_prefix=args.world_prefix, direction_field=args.direction_field, teacher_target=args.teacher_target, sft_path=args.sft_jsonl, identity_pair=args.identity_pair, proposal_worlds=args.proposal_worlds, episodes_per_world=args.episodes_per_world)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(render(report), encoding="utf-8")
    print(json.dumps({"output_json": str(args.output_json), "output_md": str(args.output_md)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
