"""Offline evaluation of continuous SIM video against the onboard estimators."""
from __future__ import annotations

import json, math, time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .attitude_camera_motion import AttitudeCameraMotionEstimator
from .camera_defaults import DEFAULT_CAMERA_INFO
from .domain import BoundingBox, VehicleTelemetry
from .multimodal_ranging import AircraftPose, CameraCalibration, MultiModalRangingEngine, TargetImageObservation, load_camera_calibration
from .rgb_slam_range import RgbSlamRangeEstimator
from .simulator.camera_projection import project_target_bbox
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState
from .visual_inertial_range import VisualInertialRangeEstimator


def _stats(values: list[float]) -> dict[str, Any]:
    values = sorted(x for x in values if math.isfinite(x))
    if not values: return {"count": 0, "mean_m": None, "p95_m": None}
    return {"count": len(values), "mean_m": sum(values) / len(values), "p95_m": values[max(0, math.ceil(.95 * len(values)) - 1)]}


def _telemetry(row: dict[str, Any], t: float) -> VehicleTelemetry:
    p = row.get("pose") or {}; a = row.get("attitude") or {}; v = row.get("velocity") or [0, 0, 0]
    n, e, d = (float(x) for x in (p.get("local_ned_m") or [0, 0, 0])[:3])
    return VehicleTelemetry(altitude_agl_m=max(.1, -d), roll_deg=float(a.get("roll_deg", 0)), pitch_deg=float(a.get("pitch_deg", 0)), ground_speed_mps=math.hypot(float(v[0]), float(v[1])), in_allowed_zone=None, geofence_healthy=True, position_healthy=True, link_healthy=True, flight_mode_allows_deploy=None, release_zone_clear=None, heading_deg=float(a.get("yaw_deg", 0)) % 360, attitude_observed_at_s=t, velocity_north_mps=float(v[0]), velocity_east_mps=float(v[1]), velocity_observed_at_s=t, local_north_m=n, local_east_m=e, local_down_m=d, local_position_observed_at_s=t, airspeed_mps=math.nan, airspeed_observed_at_s=math.nan)


def _track(tid: str, box: tuple[float, float, float, float], width: int, height: int, t: float, count: int) -> UnifiedTrackSnapshot:
    b = BoundingBox(box[0] / width, box[1] / height, box[2] / width, box[3] / height)
    return UnifiedTrackSnapshot(track_id=tid, state=UnifiedTrackState.TRACKING, label="target", bbox=b, predicted_bbox=b, first_seen_at_s=max(0, t - count / 10), last_seen_at_s=t, state_changed_at_s=t, observation_count=count, missed_frame_count=0, confidence=1, tracking_quality=1, velocity_x_s=0, velocity_y_s=0, appearance_sample_count=0, last_appearance_distance=None, reid_confirmed=True, locked=False, primary=True, actionable=True)


def _triangulate(samples: list[tuple[tuple[float, float, float], tuple[float, float, float]]]) -> tuple[float, float, float] | None:
    if len(samples) < 2: return None
    matrix = [[0.0] * 3 for _ in range(3)]; rhs = [0.0] * 3
    for pos, ray in samples:
        projection = [[float(i == j) - ray[i] * ray[j] for j in range(3)] for i in range(3)]
        for i in range(3):
            rhs[i] += sum(projection[i][j] * pos[j] for j in range(3))
            for j in range(3): matrix[i][j] += projection[i][j]
    augmented = [matrix[i] + [rhs[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda i: abs(augmented[i][col]))
        if abs(augmented[pivot][col]) < 1e-8: return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]; augmented[col] = [x / scale for x in augmented[col]]
        for i in range(3):
            if i != col:
                factor = augmented[i][col]; augmented[i] = [x - factor * y for x, y in zip(augmented[i], augmented[col])]
    return tuple(augmented[i][3] for i in range(3))


def evaluate_video_episode(episode: str | Path, truth: dict[str, Any], *, calibration_path: str | Path, depth_model: str | Path | None = None) -> dict[str, Any]:
    episode = Path(episode); cal = load_camera_calibration(Path(calibration_path)); targets = {str(x["target_id"]): x for x in truth.get("targets", [])}
    manifest = episode / "video" / "manifest.jsonl"
    rows = [json.loads(x) for x in manifest.read_text(encoding="utf-8").splitlines() if x.strip() and json.loads(x).get("frame_index") is not None]
    rgb, vi, motion = RgbSlamRangeEstimator(), VisualInertialRangeEstimator(), AttitudeCameraMotionEstimator(cal); ray_history: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    values: dict[str, list[float]] = defaultdict(list); rejects = Counter(); buckets: dict[str, list[float]] = defaultdict(list); timings: dict[str, list[float]] = defaultdict(list); depth_values: list[float] = []; depth_times: list[float] = []; depth_status = "disabled"
    depth_estimator = None
    if depth_model is not None:
        try:
            from .metric_depth import MetricDepthConfig, MetricDepthEstimator
            depth_estimator = MetricDepthEstimator(MetricDepthConfig(model_path=Path(depth_model), providers=("CPUExecutionProvider",), minimum_valid_pixels=16))
            depth_status = "ready"
        except Exception as exc:
            depth_status = f"unavailable:{type(exc).__name__}"
    for index, row in enumerate(rows):
        width = int((row.get("camera_info") or {}).get("width", DEFAULT_CAMERA_INFO["width"])); height = int((row.get("camera_info") or {}).get("height", DEFAULT_CAMERA_INFO["height"]))
        if not targets: rejects["episode_missing_truth"] += 1; continue
        pose = row.get("pose") or {}
        visible = []
        for candidate in targets.values():
            candidate_box = project_target_bbox(target_ned=(candidate["pose"]["y_north_m"], candidate["pose"]["x_east_m"], candidate["pose"]["z_up_m"]), pose={"pose": pose, "attitude": row.get("attitude", {})}, camera_info={**DEFAULT_CAMERA_INFO, **(row.get("camera_info") or {})}, image_size=(width, height), target_size_m=tuple(candidate.get("size_m", (3, 1.8, 1.5))))
            if candidate_box is not None: visible.append((candidate, candidate_box))
        target, box = (visible[0] if visible else (None, None))
        if box is None: rejects["target_not_visible"] += 1; continue
        t = float(row["sensor_stamp_ns"]) / 1e9; telemetry = _telemetry(row, t); track = _track(str(target["target_id"]), box, width, height, t, index + 1); cm = motion.update(telemetry, captured_at_s=t)
        if cm is None: rejects["camera_motion_unavailable"] += 1
        cam = tuple(float(x) for x in pose.get("local_ned_m", [0, 0, 0])); truth_pos = (float(target["pose"]["y_north_m"]), float(target["pose"]["x_east_m"]), -float(target["pose"].get("z_up_m", 0)))
        center_x, center_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        aircraft_pose = AircraftPose(t, float(row.get("attitude", {}).get("roll_deg", 0)), float(row.get("attitude", {}).get("pitch_deg", 0)), float(row.get("attitude", {}).get("yaw_deg", 0)) % 360)
        ray = MultiModalRangingEngine.target_ray_ned(calibration=cal, pose=aircraft_pose, target=TargetImageObservation(str(target["target_id"]), f"video-{index}", t, center_x / width, center_y / height))
        ray_history.append((cam, ray)); ray_history = ray_history[-8:]
        stage = str(row.get("flight_stage", "action")); distance = math.dist(cam, truth_pos); bucket_suffix = "/" + stage + "/" + ("<10m" if distance < 10 else "10-20m" if distance < 20 else "20-40m" if distance < 40 else ">=40m")
        if abs(ray[2]) > 1e-8 and -cam[2] / ray[2] > 0:
            ground = tuple(cam[i] + ray[i] * (-cam[2] / ray[2]) for i in range(3)); values["flat_ground"].append(math.dist(ground, truth_pos)); buckets["flat_ground" + bucket_suffix].append(math.dist(ground, truth_pos))
        tri = _triangulate(ray_history)
        if tri is not None:
            values["step_triangulation"].append(math.dist(tri, truth_pos)); buckets["step_triangulation" + bucket_suffix].append(math.dist(tri, truth_pos))
        for name, estimator, kwargs in (("rgb_slam", rgb, {"camera_motion": cm}), ("visual_inertial", vi, {})):
            started = time.perf_counter()
            try: result = estimator.observe(track=track, telemetry=telemetry, calibration=cal, frame_id=f"video-{index}", captured_at_s=t, **kwargs)
            except Exception as exc: result = None; rejects[f"{name}_exception_{type(exc).__name__}"] += 1
            timings[name].append((time.perf_counter() - started) * 1000)
            if result is None: rejects[f"{name}_rejected"] += 1; continue
            error = abs(result.slant_range_m - math.dist(cam, truth_pos)); values[name].append(error); buckets[f"{name}{bucket_suffix}"].append(error)
        if depth_estimator is not None and index % 10 == 0:
            try:
                import cv2
                from .domain import BoundingBox
                rgb_path = episode / "video" / str(row["rgb_path"])
                image = cv2.imread(str(rgb_path)); bbox = BoundingBox(box[0] / width, box[1] / height, box[2] / width, box[3] / height)
                started = time.perf_counter(); depth = depth_estimator.estimate(image_bgr=image, target_id=str(target["target_id"]), bbox=bbox, frame_id=f"video-{index}", captured_at_s=t, calibration=cal); depth_times.append((time.perf_counter() - started) * 1000)
                depth_values.append(abs(depth.slant_range_m - math.dist(cam, truth_pos)))
            except Exception as exc:
                rejects[f"metric_depth_{type(exc).__name__}"] += 1
    methods = {name: {"error": _stats(vals), "valid_output_rate": len(vals) / max(1, len(rows)), "cpu_ms": _stats(timings[name])} for name, vals in values.items()}
    return {"schema_version": 1, "episode": str(episode), "frames": len(rows), "methods": methods, "depth": {"status": depth_status, "sampled_frames": (len(rows) + 9) // 10 if depth_estimator is not None else 0, "valid_frames": len(depth_values), "error": _stats(depth_values), "cpu_ms": _stats(depth_times)}, "reject_reasons": dict(rejects), "buckets": {k: _stats(v) for k, v in buckets.items()}, "notes": ["camera motion uses attitude-derived homography; no truth pose is passed to either estimator", "video rows are sampled at their recorded simulation timestamps", "metric depth is invoked on every tenth video frame when --depth-model is supplied"]}


__all__ = ["evaluate_video_episode"]
