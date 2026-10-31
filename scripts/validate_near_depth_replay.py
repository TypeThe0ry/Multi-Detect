"""Small, read-only B2 replay over an existing SIM episode.

This is an evaluation harness, not a replacement for the live policy.  It uses
recorded poses and projected truth boxes, runs the pinned metric-depth model on
CPU only for frames whose geometric range estimate is below the near-depth
gate, and compares geometric and fused position errors.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.near_depth import empirical_sigma  # noqa: E402
from multidetect.autonomy.step_triangulation import (  # noqa: E402
    RangeMeasurement,
    ViewObservation,
    fuse_position_estimates,
    position_from_range,
    triangulate_views,
)
from multidetect.domain import BoundingBox  # noqa: E402
from multidetect.metric_depth import MetricDepthConfig, MetricDepthEstimator  # noqa: E402
from multidetect.multimodal_ranging import CameraCalibration  # noqa: E402
from multidetect.simulator.camera_projection import (  # noqa: E402
    pixel_camera_ray,
    project_target_bbox,
)
from scripts.score_onboard_loop import truth_for_episode  # noqa: E402
DEFAULT_EPISODE = ROOT / "artifacts/datasets/onboard-loop/eval-v2-test-r3seg-descent/gen_v2_w0035/episodes/episode-0000-3be7faf9"
DEFAULT_MODEL = ROOT / "artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx"


def _error(estimate, truth: tuple[float, float, float], camera) -> tuple[float, float] | None:
    if estimate is None:
        return None
    return (
        math.dist(estimate.position_ned, truth),
        abs(math.dist(estimate.position_ned, camera) - math.dist(truth, camera)),
    )


def replay(episode: Path, model: Path, max_frames: int, sigma_scale: float) -> dict:
    _, truth = truth_for_episode(episode)
    if truth is None:
        raise ValueError(f"truth not found for {episode}")
    target = next(item for item in truth["targets"] if item.get("true_target"))
    truth_point = (
        float(target["pose"]["y_north_m"]),
        float(target["pose"]["x_east_m"]),
        -float(target["pose"].get("z_up_m", 0.0)),
    )
    rows = [
        json.loads(line)
        for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = rows[:max_frames]
    estimator = MetricDepthEstimator(
        MetricDepthConfig(
            model_path=model,
            providers=("CPUExecutionProvider",),
            intra_op_num_threads=2,
            inter_op_num_threads=2,
        )
    )
    counts: Counter[str] = Counter()
    geometry_errors: list[float] = []
    fused_errors: list[float] = []
    geometry_range_errors: list[float] = []
    fused_range_errors: list[float] = []
    views: list[ViewObservation] = []
    depth_rows: list[dict] = []
    try:
        for step, row in enumerate(rows):
            obs = row["obs_before"]
            camera_info = obs["camera_info"]
            camera = tuple(float(value) for value in obs["pose"]["local_ned_m"][:3])
            bbox = project_target_bbox(
                target_ned=(
                    target["pose"]["y_north_m"],
                    target["pose"]["x_east_m"],
                    target["pose"].get("z_up_m", 0.0),
                ),
                pose=obs,
                camera_info=camera_info,
                image_size=(int(camera_info["width"]), int(camera_info["height"])),
                target_size_m=tuple(target.get("size_m", (3, 1.8, 1.5))),
                target_yaw_rad=target["pose"].get("yaw_rad", 0.0),
                target_bottom_offset_m=target.get("bottom_offset_m"),
            )
            if bbox is None:
                counts["no_bbox"] += 1
                continue
            centre = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            ray_data = pixel_camera_ray(
                centre[0],
                centre[1],
                pose=obs,
                camera_info=camera_info,
                image_size=(int(camera_info["width"]), int(camera_info["height"])),
            )
            if ray_data is None:
                counts["no_ray"] += 1
                continue
            origin, direction = ray_data
            timestamp_s = float(obs.get("frame_sensor_stamp_ns", step * 100_000_000)) / 1e9
            views.append(ViewObservation(tuple(origin), tuple(direction), step, timestamp_s))
            geometry = triangulate_views(views[-8:], sigma_scale=sigma_scale)
            if geometry is not None:
                counts["geometry_estimate"] += 1
                result = _error(geometry, truth_point, camera)
                if result:
                    geometry_errors.append(result[0])
                    geometry_range_errors.append(result[1])
            # Use the current geometric result; before triangulation is valid,
            # intersect the centre ray with the recorded ground plane as the
            # same ground-derived range source used by OnboardLoop.
            current_range = None
            if geometry is not None:
                current_range = math.dist(tuple(origin), geometry.position_ned)
            elif direction[2] > 1e-6:
                ground_scale = -float(origin[2]) / direction[2]
                if ground_scale > 0:
                    current_range = ground_scale
            if current_range is None or current_range >= 10.0:
                counts["skip_range"] += 1
                continue
            image_path = episode / str(obs["rgb_path"]).replace("\\", "/")
            if not image_path.is_file():
                counts["skip_image"] += 1
                continue
            image_rgb = np.asarray(Image.open(image_path).convert("RGB"))
            calibration = CameraCalibration(
                calibration_id=str(camera_info.get("calibration_id", "sim")),
                width_px=int(camera_info["width"]),
                height_px=int(camera_info["height"]),
                fx_px=float(camera_info["fx"]),
                fy_px=float(camera_info["fy"]),
                cx_px=float(camera_info["cx"]),
                cy_px=float(camera_info["cy"]),
            )
            normalized = BoundingBox(
                bbox[0] / calibration.width_px,
                bbox[1] / calibration.height_px,
                bbox[2] / calibration.width_px,
                bbox[3] / calibration.height_px,
            )
            counts["depth_attempt"] += 1
            try:
                result = estimator.estimate(
                    image_bgr=image_rgb[:, :, ::-1].copy(),
                    target_id="truth-target",
                    bbox=normalized,
                    target_label="backpack",
                    frame_id=f"step-{step}",
                    captured_at_s=timestamp_s,
                    calibration=calibration,
                )
            except Exception as exc:
                counts[f"depth_error:{type(exc).__name__}"] += 1
                continue
            if not math.isfinite(result.slant_range_m) or result.slant_range_m <= 0:
                counts["depth_invalid"] += 1
                continue
            sigma = (
                max(float(result.sigma_m), empirical_sigma(float(result.slant_range_m)))
                * sigma_scale
            )
            depth_measurement = RangeMeasurement(
                float(result.slant_range_m), sigma, timestamp_s, "near_depth"
            )
            depth_position = position_from_range(
                depth_measurement, camera_ned=tuple(origin), direction_ned=tuple(direction)
            )
            fused = fuse_position_estimates(geometry, depth_position)
            if fused is None:
                counts["fused_none"] += 1
                continue
            counts["depth_used"] += 1
            d_error = _error(fused, truth_point, camera)
            if d_error:
                fused_errors.append(d_error[0])
                fused_range_errors.append(d_error[1])
            depth_rows.append(
                {
                    "step": step,
                    "current_range_m": current_range,
                    "depth_range_m": result.slant_range_m,
                    "depth_sigma_m": sigma,
                    "geometry_error_m": _error(geometry, truth_point, camera)[0]
                    if geometry
                    else None,
                    "fused_error_m": d_error[0] if d_error else None,
                }
            )
    finally:
        estimator.close()

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        "episode": str(episode),
        "frames_considered": len(rows),
        "target_id": target["target_id"],
        "counts": dict(counts),
        "geometry_position_error_mean_m": mean(geometry_errors),
        "fused_position_error_mean_m": mean(fused_errors),
        "geometry_slant_error_mean_m": mean(geometry_range_errors),
        "fused_slant_error_mean_m": mean(fused_range_errors),
        "delta_fused_minus_geometry_m": (mean(fused_errors) - mean(geometry_errors))
        if fused_errors and geometry_errors
        else None,
        "depth_rows": depth_rows,
        "note": "小样本诊断回放；目标框使用记录真值投影，不能替代端到端机载评估。",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", type=Path, default=DEFAULT_EPISODE)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--max-frames", type=int, default=48)
    parser.add_argument("--sigma-scale", type=float, default=1.0)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp/near_depth_replay.json")
    args = parser.parse_args()
    report = replay(args.episode, args.model, max(20, min(60, args.max_frames)), args.sigma_scale)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "depth_rows"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
