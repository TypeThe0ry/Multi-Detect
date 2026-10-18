"""纯 CPU 回放：比较旧评估交会与机载纯函数，并校准 sigma（不加载模型）。

默认重放旧报告的排序前 30 个 episode；真值只用于图像关联和事后评分，
不参与射线、窗口、质量门或位置计算。分层抽样只属于旧报告的深度部分，
这里重放该报告全部 1,274 个已关联目标帧，无须执行任何深度推理。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.step_triangulation import (  # noqa: E402
    ViewObservation,
    triangulate_views,
)
from multidetect.simulator.camera_projection import (  # noqa: E402
    pixel_camera_ray,
    project_target_bbox,
)
from scripts import evaluate_onboard_ranging_reuse as reference  # noqa: E402
from scripts.score_onboard_loop import truth_for_episode  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _segmentation_boxes(
    path: Path | None, truth: dict, caches: list[Path], counts: Counter
) -> dict:
    """Read existing cache or parse recorded labels; never modify the dataset/cache."""
    if path is None or not path.is_file():
        return {}
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24] + ".json"
    for directory in caches:
        candidate = directory / key
        try:
            if candidate.is_file() and candidate.stat().st_mtime >= path.stat().st_mtime:
                result = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(result, dict):
                    counts["cache_hit"] += 1
                    return result
        except (OSError, ValueError):
            continue
    # This reads Gazebo's stored segmentation PNG, not a segmentation model.
    from multidetect.sim_teacher.segmentation import boxes_from_segmentation, load_labels_map

    counts["segmentation_png_read"] += 1
    return {
        str(key): list(value["box"])
        for key, value in boxes_from_segmentation(
            load_labels_map(path), truth, min_area_px=4, min_box_px=2
        ).items()
    }


def _coverage(rows: list[tuple[float, float, float]], scale: float) -> dict:
    total = len(rows)
    return {
        "sigma_scale": scale,
        "sample_count": total,
        "slant_95_covered": sum(slant <= 1.96 * sigma * scale for slant, _, sigma in rows),
        "slant_95_coverage": sum(slant <= 1.96 * sigma * scale for slant, _, sigma in rows)
        / max(1, total),
        "horizontal_ne_95_covered": sum(ne <= 1.96 * sigma * scale for _, ne, sigma in rows),
        "horizontal_ne_95_coverage": sum(ne <= 1.96 * sigma * scale for _, ne, sigma in rows)
        / max(1, total),
    }


def replay(args: argparse.Namespace) -> dict:
    episodes = sorted({path.parent for path in args.root.rglob("episode.json")})[
        : args.max_episodes
    ]
    if not episodes:
        raise ValueError(f"no episode.json found under {args.root}")
    counts: Counter = Counter()
    association: Counter = Counter()
    errors: list[tuple[float, float, float]] = []
    distinct_step_errors: list[tuple[float, float, float]] = []
    runtime_camera_errors: list[tuple[float, float, float]] = []
    point_differences: list[float] = []
    sigma_differences: list[float] = []
    geometry_differences: list[float] = []
    manifest = []
    caches = args.segmentation_cache or [ROOT / "tmp/ranging-eval/cache"]
    for episode in episodes:
        _, truth = truth_for_episode(episode, args.scenarios)
        if truth is None:
            counts["missing_truth_episode"] += 1
            continue
        steps_path = episode / "steps.jsonl"
        lines = steps_path.read_text(encoding="utf-8").splitlines()
        manifest.append(
            {
                "episode": episode.relative_to(args.root).as_posix(),
                "steps_sha256": _sha256(steps_path),
            }
        )
        targets = {str(target["target_id"]): target for target in truth.get("targets", [])}
        history: dict[str, list[ViewObservation]] = defaultdict(list)
        distinct_history: dict[str, dict[int, ViewObservation]] = defaultdict(dict)
        logged_track_history: dict[str, dict[int, ViewObservation]] = defaultdict(dict)
        for step, line in enumerate(lines):
            if not line.strip():
                continue
            row = json.loads(line)
            observation = row.get("obs_before") or {}
            tracks = ((row.get("labels") or {}).get("onboard") or {}).get("tracks") or []
            if not tracks:
                continue
            segmentation = observation.get("segmentation_path")
            path = episode / str(segmentation).replace("\\", "/") if segmentation else None
            boxes = _segmentation_boxes(path, truth, caches, counts)
            boxes = {key: value for key, value in boxes.items() if key in targets}
            source = "segmentation" if boxes else "projected_truth_bbox"
            if not boxes:
                # Preserve the reference evaluator exactly. In the reproduced
                # 30-episode report all accepted rows actually use segmentation.
                for target_id, target in targets.items():
                    pose = target["pose"]
                    box = project_target_bbox(
                        target_ned=(pose["y_north_m"], pose["x_east_m"], pose["z_up_m"]),
                        pose=observation,
                        camera_info=observation["camera_info"],
                        image_size=(1280, 960),
                        target_size_m=tuple(target.get("size_m", (3, 1.8, 1.5))),
                        target_yaw_rad=pose.get("yaw_rad", 0),
                        target_bottom_offset_m=target.get("bottom_offset_m"),
                    )
                    if box is not None:
                        boxes[target_id] = list(box)
            camera = tuple(float(value) for value in observation["pose"]["local_ned_m"][:3])
            timestamp = reference.frame_time(observation, step * 0.1)
            for track in tracks:
                box = track.get("bbox")
                match = max(
                    ((reference.iou(box, candidate), key) for key, candidate in boxes.items()),
                    default=(0, None),
                )
                if match[1] is None or match[0] < 0.1:
                    counts["unassociated_track"] += 1
                    continue
                target_id = match[1]
                pose = targets[target_id]["pose"]
                truth_point = (
                    float(pose["y_north_m"]),
                    float(pose["x_east_m"]),
                    -float(pose.get("z_up_m", 0)),
                )
                counts["samples"] += 1
                association[source] += 1
                direction = reference.ray(observation, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                views = history[target_id]
                # The legacy evaluator keys history by truth target, so two
                # tracks can contribute two views of that target in one step.
                # Unique observation ordinals reproduce its identical ray
                # inputs; a second pass below applies real step deduplication.
                views.append(ViewObservation(camera, direction, counts["samples"], timestamp))
                history[target_id] = views = views[-8:]
                step_views = distinct_history[target_id]
                if step in step_views:
                    counts["duplicate_target_observation_same_step"] += 1
                step_views.pop(step, None)
                step_views[step] = ViewObservation(camera, direction, step, timestamp)
                for old_step in list(step_views):
                    if not step - 8 < old_step <= step:
                        del step_views[old_step]
                distinct_estimate = triangulate_views(
                    step_views.values(), sigma_scale=args.sigma_scale
                )
                if distinct_estimate is not None:
                    distinct_step_errors.append(
                        (
                            abs(
                                math.dist(distinct_estimate.position_ned, camera)
                                - math.dist(truth_point, camera)
                            ),
                            math.hypot(
                                distinct_estimate.position_ned[0] - truth_point[0],
                                distinct_estimate.position_ned[1] - truth_point[1],
                            ),
                            distinct_estimate.sigma_m / args.sigma_scale,
                        )
                    )
                # Diagnostic using the newly shared ground-projection camera
                # centre/ray and the recorded track ID (not truth ID). It still
                # is not a full OnboardLoop replay: no new association or
                # camera-dependent control action is executed here.
                camera_info = observation["camera_info"]
                camera_ray = pixel_camera_ray(
                    (box[0] + box[2]) / 2,
                    (box[1] + box[3]) / 2,
                    pose=observation,
                    camera_info=camera_info,
                    image_size=(int(camera_info["width"]), int(camera_info["height"])),
                )
                if camera_ray is not None and track.get("id") is not None:
                    actual_camera, actual_direction = camera_ray
                    logged_views = logged_track_history[str(track["id"])]
                    logged_views.pop(step, None)
                    logged_views[step] = ViewObservation(
                        actual_camera, actual_direction, step, timestamp
                    )
                    for old_step in list(logged_views):
                        if not step - 8 < old_step <= step:
                            del logged_views[old_step]
                    actual_estimate = triangulate_views(
                        logged_views.values(), sigma_scale=args.sigma_scale
                    )
                    if actual_estimate is not None:
                        runtime_camera_errors.append(
                            (
                                abs(
                                    math.dist(actual_estimate.position_ned, actual_camera)
                                    - math.dist(truth_point, actual_camera)
                                ),
                                math.hypot(
                                    actual_estimate.position_ned[0] - truth_point[0],
                                    actual_estimate.position_ned[1] - truth_point[1],
                                ),
                                actual_estimate.sigma_m / args.sigma_scale,
                            )
                        )
                pairs = [(view.camera_ned, view.direction_ned) for view in views]
                point = reference.triangulate(pairs)
                quality = reference.triangulation_quality(pairs, point)
                accepted = (
                    quality is not None
                    and quality[0] >= 0.5
                    and quality[1] >= 0.5
                    and quality[2] <= 5
                    and quality[3]
                )
                estimate = triangulate_views(views, sigma_scale=args.sigma_scale)
                if bool(estimate is not None) != bool(accepted):
                    counts["quality_gate_mismatch"] += 1
                if not accepted:
                    counts["reference_rejected"] += 1
                    continue
                counts["reference_accepted"] += 1
                errors.append(
                    (
                        abs(math.dist(point, camera) - math.dist(truth_point, camera)),
                        math.hypot(point[0] - truth_point[0], point[1] - truth_point[1]),
                        quality[4],
                    )
                )
                if estimate is not None:
                    point_differences.append(math.dist(estimate.position_ned, point))
                    sigma_differences.append(abs(estimate.sigma_m - quality[4] * args.sigma_scale))
                    geometry_differences.append(
                        max(
                            abs(estimate.baseline_m - quality[0]),
                            abs(estimate.ray_angle_deg - quality[1]),
                            abs(estimate.residual_m - quality[2]),
                        )
                    )
    candidates = [_coverage(errors, scale) for scale in (1.0, 1.5, 2.0, 2.5, 3.0)]
    selected = _coverage(errors, args.sigma_scale)
    minimum_scale = (
        sorted(error / (1.96 * sigma) for error, _, sigma in errors)[
            math.ceil(0.93 * len(errors)) - 1
        ]
        if errors
        else None
    )
    parity_passed = (
        bool(errors)
        and not counts["quality_gate_mismatch"]
        and max(point_differences, default=math.inf) <= 1e-8
        and max(sigma_differences, default=math.inf) <= 1e-8
        and max(geometry_differences, default=math.inf) <= 1e-8
    )
    return {
        "schema_version": 1,
        "execution": {"cpu_only": True, "model_loaded": False, "dataset_read_only": True},
        "dataset_root": str(args.root.resolve()),
        "selection": (
            "sorted episode paths, first max_episodes; same complete geometry sample as old "
            "evaluator (depth-only stratification is not used)"
        ),
        "selected_episode_count": len(episodes),
        "used_episode_count": len(manifest),
        "sample_count": counts["samples"],
        "association": dict(association),
        "counts": dict(counts),
        "reference": {
            "valid_output_rate": len(errors) / max(1, counts["samples"]),
            "horizontal_error": reference.stats([row[1] for row in errors]),
            "slant_error": reference.stats([row[0] for row in errors]),
        },
        "same_input_parity": {
            "passed": parity_passed,
            "quality_gate_mismatches": counts["quality_gate_mismatch"],
            "max_position_difference_m": max(point_differences, default=None),
            "max_scaled_sigma_difference_m": max(sigma_differences, default=None),
            "max_geometry_difference": max(geometry_differences, default=None),
            "tolerance": 1e-8,
            "step_numbering": (
                "unique observation ordinal preserves all legacy ray inputs; the old evaluator "
                "can put multiple tracks for one truth target into one recorded step"
            ),
            "camera_origin": (
                "legacy body local-NED origin and legacy calibrated ray, unchanged only to verify"
                " same-input pure-function parity; runtime uses the actual camera mount centre "
                "and shared ground-projector camera model"
            ),
        },
        "distinct_recorded_step_replay": {
            "description": (
                "legacy camera geometry and truth-target association retained; actual recorded "
                "step numbers, one latest target observation per step, window current_step - 8 < "
                "view.step <= current_step; differs from legacy history for duplicate tracks and "
                "gaps"
            ),
            "sample_count": len(distinct_step_errors),
            "valid_output_rate": len(distinct_step_errors) / max(1, counts["samples"]),
            "horizontal_error": reference.stats([row[1] for row in distinct_step_errors]),
            "slant_error": reference.stats([row[0] for row in distinct_step_errors]),
            "unscaled_coverage": _coverage(distinct_step_errors, 1.0),
            "selected_coverage": _coverage(distinct_step_errors, args.sigma_scale),
            "candidate_coverage": [
                _coverage(distinct_step_errors, scale) for scale in (1.5, 2.0, 2.5, 3.0)
            ],
        },
        "runtime_camera_logged_track_diagnostic": {
            "description": (
                "shared pixel_camera_ray actual camera centre plus centre ray; per-recorded-track"
                " ID history, latest 8 action steps. Only offline-associated rows are replayed; "
                "ground reidentification/association, fusion and closed-loop actions are not "
                "replayed. Truth associates/scores recorded targets but does not change ray "
                "values or geometry gates."
            ),
            "sample_count": len(runtime_camera_errors),
            "valid_output_rate": len(runtime_camera_errors) / max(1, counts["samples"]),
            "horizontal_error": reference.stats([row[1] for row in runtime_camera_errors]),
            "slant_error": reference.stats([row[0] for row in runtime_camera_errors]),
            "unscaled_coverage": _coverage(runtime_camera_errors, 1.0),
            "selected_coverage": _coverage(runtime_camera_errors, args.sigma_scale),
            "candidate_coverage": [
                _coverage(runtime_camera_errors, scale) for scale in (1.5, 2.0, 2.5, 3.0)
            ],
        },
        "sigma_calibration": {
            "target_slant_95_coverage": 0.93,
            "minimum_empirical_scale": minimum_scale,
            "candidates": candidates,
            "selected": selected,
            "coverage_target_passed": selected["slant_95_coverage"] >= 0.93,
            "limitation": (
                "same recorded data used for scale calibration and coverage reporting; not an "
                "independent held-out or real-flight validation; NE check is radial horizontal "
                "error <= 1.96 sigma, not a calibrated 2-D Gaussian confidence ellipse"
            ),
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): _sha256(path)
            for path in (
                ROOT / "scripts/evaluate_onboard_ranging_reuse.py",
                ROOT / "scripts/validate_step_triangulation_replay.py",
                ROOT / "src/multidetect/autonomy/step_triangulation.py",
                ROOT / "src/multidetect/simulator/camera_projection.py",
                ROOT / "simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json",
            )
        },
        "episodes": manifest,
    }


def replay_fusion_gates(
    roots: list[Path],
    *,
    loop_configs: dict[str, dict],
    ground_offsets_m: list[float],
    scenarios: Path | None = None,
) -> dict:
    """Re-run ``OnboardLoop._update_position`` on recorded closed-loop tracks.

    Recorded track IDs, boxes, poses and stamps are replayed unchanged; only
    the ground model (``FlatGround(offset)``, i.e. ground believed higher than
    truth) and the loop configuration vary. Association and flight actions are
    not re-run, so a biased ground cannot change which frames were recorded.
    Truth only selects/scores the true target (segmentation IoU >= 0.1).
    """
    from multidetect.autonomy.ground_model import FlatGround
    from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop, Track
    from multidetect.sim_teacher.primitives import ActionPrimitive
    from multidetect.simulator.camera_projection import pixel_ground_point
    from scripts.score_onboard_loop import (
        _iou,
        _row_sim_time,
        _true_box,
        _true_target,
        target_pose_at_time,
    )

    episodes = sorted({meta.parent for root in roots for meta in root.rglob("episode.json")})
    rows_by_episode = {}
    for episode in episodes:
        _, truth = truth_for_episode(episode, scenarios)
        steps_path = episode / "steps.jsonl"
        if truth is None or not steps_path.is_file():
            continue
        rows = [
            json.loads(line)
            for line in steps_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        boxes = [
            _true_box(episode, row["obs_before"], truth, _row_sim_time(row))[0] for row in rows
        ]
        rows_by_episode[episode] = (rows, _true_target(truth), boxes)

    def summary(values: list[float]) -> dict:
        result = reference.stats(values)
        return {key: result[key] for key in ("count", "median_m", "p95_m", "max_m")}

    def stamp_s(observation: dict) -> float | None:
        # Same precedence as scripts/onboard_policy.py::_observation_timestamp_s.
        for key, units in (
            ("pose_time_boot_ms", 1e3),
            ("pose_sim_stamp_ns", 1e9),
            ("frame_sensor_stamp_ns", 1e9),
        ):
            value = observation.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return value / units
        return None

    report = {}
    for offset in ground_offsets_m:
        ground_model = FlatGround(offset)
        for name, options in loop_configs.items():
            config = LoopConfig(step_triangulation=True, **options)
            frame_errors, ground_errors, confirm_errors, confirm_ground_errors = [], [], [], []
            sources: Counter = Counter()
            by_source: dict[str, list[float]] = defaultdict(list)
            ground_by_source: dict[str, list[float]] = defaultdict(list)
            confirm_sources: Counter = Counter()
            notes: Counter = Counter()
            frame_rows, confirm_rows = [], []
            for episode, (rows, target, true_boxes) in rows_by_episode.items():
                info = rows[0]["obs_before"]["camera_info"]
                size = (int(info["width"]), int(info["height"]))
                loop = OnboardLoop(
                    CameraModel(info["fx"], info["fy"], info["cx"], info["cy"], *size),
                    lambda p, y: ActionPrimitive.hold(1),
                    segment_clear=lambda *args: True,
                    config=config,
                )
                tracks: dict[str, Track] = {}
                confirmed_seen = False
                for row, true_box in zip(rows, true_boxes, strict=True):
                    onboard = (row.get("labels") or {}).get("onboard") or {}
                    observation = row["obs_before"]
                    pose = tuple(float(x) for x in observation["pose"]["local_ned_m"][:3])
                    yaw = float(observation["attitude"]["yaw_deg"])
                    stamp = stamp_s(observation)
                    loop.step_index = int(row.get("step_index", loop.step_index))
                    kwargs = dict(pose=observation, camera_info=info, image_size=size)

                    def camera_ray(u, v, kwargs=kwargs):
                        return pixel_camera_ray(u, v, **kwargs)

                    confirmed = onboard.get("confirmed")
                    is_confirm = (
                        not confirmed_seen
                        and isinstance(confirmed, dict)
                        and onboard.get("state_before") != "returning"
                    )
                    confirmed_id = confirmed.get("track_id") if is_confirm else None
                    recorded = list(onboard.get("tracks") or [])
                    if is_confirm and not any(t.get("id") == confirmed_id for t in recorded):
                        recorded.append({"id": confirmed_id, "bbox": confirmed["bbox"]})
                    for item in recorded:
                        # The confirmation record keeps the unrounded box.
                        raw = confirmed["bbox"] if item.get("id") == confirmed_id else item["bbox"]
                        box = tuple(float(x) for x in raw)
                        track = tracks.setdefault(
                            str(item["id"]),
                            Track(str(item["id"]), box, loop.step_index, loop.step_index),
                        )
                        track.bbox = box
                        ground = pixel_ground_point(
                            (box[0] + box[2]) / 2, box[3], **kwargs, ground_model=ground_model
                        )
                        track.ground = ground
                        loop._update_position(
                            track, ground, pose, yaw, camera_ray, stamp, allow_level_ray=False
                        )
                        position = loop.target_position(track)
                        if position is None or true_box is None or _iou(box, true_box) < 0.1:
                            continue
                        truth_pose = target_pose_at_time(target, _row_sim_time(row))
                        truth_ne = (float(truth_pose["y_north_m"]), float(truth_pose["x_east_m"]))
                        error = math.dist(position, truth_ne)
                        ground_error = math.dist(ground, truth_ne) if ground else math.inf
                        estimate = track.position_estimate
                        source = estimate.source if estimate else "ground_ne_only"
                        note = estimate.consistency_note if estimate else None
                        frame_errors.append(error)
                        ground_errors.append(ground_error)
                        sources[source] += 1
                        by_source[source].append(error)
                        ground_by_source[source].append(ground_error)
                        notes[str(note)] += 1
                        frame_rows.append(
                            [
                                episode.relative_to(episode.parents[3]).as_posix(),
                                loop.step_index,
                                str(item["id"]),
                                round(error, 3),
                                round(ground_error, 3),
                                source,
                            ]
                        )
                        if item.get("id") == confirmed_id:
                            confirm_errors.append(error)
                            confirm_ground_errors.append(ground_error)
                            confirm_sources[source] += 1
                            confirm_rows.append(
                                {
                                    "episode": episode.relative_to(episode.parents[3]).as_posix(),
                                    "step": loop.step_index,
                                    "error_m": round(error, 3),
                                    "ground_error_m": round(ground_error, 3),
                                    "source": source,
                                    "note": note,
                                    "recorded_position_ne": confirmed.get("position_estimate_ne"),
                                    "replayed_position_ne": [round(x, 4) for x in position],
                                }
                            )
                    confirmed_seen = confirmed_seen or is_confirm
            report[f"ground_up_{offset:g}m/{name}"] = {
                "ground_up_m": offset,
                "loop_config": options,
                "true_target_frames": {
                    "reported": summary(frame_errors),
                    "ground_intersection": summary(ground_errors),
                    "sources": dict(sources),
                    "reported_by_source": {key: summary(by_source[key]) for key in by_source},
                    "ground_intersection_by_source": {
                        key: summary(ground_by_source[key]) for key in ground_by_source
                    },
                    "notes": dict(notes),
                    "rows": frame_rows,
                    "row_fields": [
                        "episode",
                        "step",
                        "track",
                        "error_m",
                        "ground_error_m",
                        "source",
                    ],
                },
                "true_target_confirmations": {
                    "reported": summary(confirm_errors),
                    "ground_intersection": summary(confirm_ground_errors),
                    "sources": dict(confirm_sources),
                    "rows": confirm_rows,
                },
            }
    return {
        "schema_version": 1,
        "execution": {"cpu_only": True, "model_loaded": False, "dataset_read_only": True},
        "roots": [str(root) for root in roots],
        "episode_count": len(rows_by_episode),
        "semantics": (
            "recorded track boxes/IDs/poses replayed through OnboardLoop._update_position; "
            "FlatGround(ground_up_m) believes the ground higher than truth; association and "
            "actions are not re-run; horizontal N/E error to the true target, frames whose "
            "track box has segmentation IoU >= 0.1 with it"
        ),
        "conditions": report,
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--fusion-gate-replay" in argv:
        parser = argparse.ArgumentParser(description=replay_fusion_gates.__doc__)
        parser.add_argument("--fusion-gate-replay", action="store_true")
        parser.add_argument("roots", type=Path, nargs="+")
        parser.add_argument("--scenarios", type=Path)
        parser.add_argument("--ground-offset-m", type=float, action="append")
        parser.add_argument(
            "--loop-configs",
            type=json.loads,
            default={"default": {}},
            help='JSON {"name": {LoopConfig options}}; step_triangulation is always on',
        )
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(argv)
        report = replay_fusion_gates(
            args.roots,
            loop_configs=args.loop_configs,
            ground_offsets_m=args.ground_offset_m or [0.0, 2.0, 5.0],
            scenarios=args.scenarios,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"output": str(args.output), "episodes": report["episode_count"]}))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--max-episodes", type=int, default=30)
    parser.add_argument("--sigma-scale", type=float, default=2.5)
    parser.add_argument("--scenarios", type=Path)
    parser.add_argument("--segmentation-cache", type=Path, action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.max_episodes < 1 or not math.isfinite(args.sigma_scale) or args.sigma_scale < 1:
        parser.error("max-episodes must be positive; sigma-scale must be finite and >= 1")
    report = replay(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "sample_count": report["sample_count"],
                "parity": report["same_input_parity"],
                "coverage": report["sigma_calibration"]["selected"],
            },
            ensure_ascii=False,
        )
    )
    return (
        0
        if (
            report["same_input_parity"]["passed"]
            and report["sigma_calibration"]["coverage_target_passed"]
            and report["distinct_recorded_step_replay"]["selected_coverage"]["slant_95_coverage"]
            >= 0.93
            and report["runtime_camera_logged_track_diagnostic"]["selected_coverage"][
                "slant_95_coverage"
            ]
            >= 0.93
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
