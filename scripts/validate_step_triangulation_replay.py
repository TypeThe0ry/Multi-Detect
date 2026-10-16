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


def main(argv: list[str] | None = None) -> int:
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
