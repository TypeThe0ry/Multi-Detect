"""Build a truth-free continuous 3-D OnboardAI trajectory staging set.

This is the boundary between a persisted Gazebo camera/depth capture and a
reviewed training dataset.  It deliberately does *not* manufacture expert
labels.  Every frame is rebuilt through ``ContextSnapshot`` and
``OnboardRuntimeEvent`` so the exact 40-feature contract used by the host SIM
and the Jetson path is exercised.  A staging set remains ``training_ready``
false until an independent reviewer binds labels, planner receipts and (when
required) a reopenable raw replay recording.

The input is the actual Gazebo RGB wire manifest and its depth companion.  A
raw replay manifest is optional for plumbing captures, but when it is given
frame identity and order are checked.  Omitting it is recorded explicitly and
can never be promoted to a training or release artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Import the already sealed Gazebo sensor checks.  Keeping one provenance
# implementation prevents a staging path from accepting hand-written pixels.
from build_onboard_rendered_3d_dataset import (  # noqa: E402
    _depth_median_for_bbox,
    _load_mainline_bindings,
    _read_depth_values,
    _read_frame,
    _sha256,
    _verify_gazebo_provenance,
)
from build_onboard_rendered_3d_dataset import (  # noqa: E402
    _load_depth_manifest as _load_depth_manifest_base,
)

from multidetect.autonomy import (  # noqa: E402
    ONBOARD_MODEL_ACTION_ORDER,
    ONBOARD_MODEL_FEATURE_NAMES,
    ONBOARD_MODEL_TASK_ACTION_ORDER,
    ContextSnapshot,
    GeoPoint,
    OnboardEventKind,
    OnboardModelInput,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)
from multidetect.backends import RawSensorReplayRecording  # noqa: E402
from multidetect.multimodal_ranging import (  # noqa: E402
    AircraftPose,
    CameraCalibration,
    TargetImageObservation,
    _camera_offset_ned,
    _target_rays,
)
from multidetect.perception_factory import COCO80_CLASS_NAMES  # noqa: E402
from multidetect.vision import OnnxRawYoloConfig, OnnxRawYoloDetector  # noqa: E402

_STAGING_CONTRACT = "multidetect.onboard-trajectory-staging-v1"
_DEFAULT_BASE_UTC = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_depth_manifest(path: Path | None) -> dict[tuple[int, str], dict[str, Any]]:
    """Load depth companions without allowing duplicate frame replacement."""

    if path is None:
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    frames = document.get("frames") if isinstance(document, dict) else None
    if not isinstance(frames, list):
        raise ValueError("depth manifest frames are missing")
    identities: set[tuple[int, str]] = set()
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("depth manifest frame must be an object")
        sequence = frame.get("sequence")
        frame_id = frame.get("frame_id")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not isinstance(frame_id, str)
        ):
            raise ValueError("depth manifest frame identity is invalid")
        identity = (sequence, frame_id)
        if identity in identities:
            raise ValueError("depth manifest contains duplicate frame identity")
        identities.add(identity)
    return _load_depth_manifest_base(path)


def _require_hash(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _finite(value: object, default: float, *, label: str, missing: set[str]) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
    missing.add(label)
    return float(default)


def _bool(value: object, default: bool, *, label: str, missing: set[str]) -> bool:
    if type(value) is bool:
        return value
    missing.add(label)
    return default


def _vehicle_class(scenario_id: str) -> str:
    value = scenario_id.lower()
    if "fixed_wing" in value or "fixed-wing" in value:
        return "fixed-wing"
    if "vtol" in value:
        return "vtol"
    return "multirotor"


def _reject_simulator_truth(value: object, *, path: str = "root") -> None:
    """Reject truth channels instead of merely declining to copy them."""

    prohibited = {
        "truth",
        "ground_truth",
        "target_world_position",
        "target_model_id",
        "nearest_object_truth",
        "world_pose",
        "object_pose",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if key in prohibited:
                raise ValueError(f"Gazebo staging input contains simulator truth at {path}.{key}")
            _reject_simulator_truth(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_simulator_truth(child, path=f"{path}[{index}]")


def _detector(path: Path | None, confidence: float) -> Any:
    if path is None:
        return None
    if not path.is_file():
        raise FileNotFoundError(path)
    return OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=path,
            class_names=tuple(COCO80_CLASS_NAMES),
            input_width=640,
            input_height=640,
            confidence_threshold=confidence,
            providers=("CPUExecutionProvider",),
            model_version=f"trajectory-staging-detector:{path.name}",
        )
    )


def _telemetry_context(
    *,
    frame: dict[str, Any],
    episode_id: str,
    home: GeoPoint,
    airframe_profile_id: str,
    airframe_profile_sha256: str,
    captured_at_utc: datetime,
    defaults: dict[str, float],
    missing: set[str],
) -> ContextSnapshot:
    provenance = frame.get("provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("frame_telemetry"), dict):
        raise ValueError(f"{episode_id} frame telemetry is missing")
    telemetry = provenance["frame_telemetry"]
    latitude = _finite(
        telemetry.get("latitude_deg"), math.nan, label="latitude_deg", missing=missing
    )
    longitude = _finite(
        telemetry.get("longitude_deg"), math.nan, label="longitude_deg", missing=missing
    )
    if not math.isfinite(latitude) or not -90.0 <= latitude <= 90.0:
        raise ValueError(f"{episode_id} frame has no valid latitude")
    if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
        raise ValueError(f"{episode_id} frame has no valid longitude")
    captured_at_s = _finite(
        provenance.get("captured_at_s"), math.nan, label="captured_at_s", missing=missing
    )
    if not math.isfinite(captured_at_s) or captured_at_s < 0.0:
        raise ValueError(f"{episode_id} frame timestamp is invalid")
    altitude = _finite(
        telemetry.get("altitude_agl_m"), 0.0, label="altitude_agl_m", missing=missing
    )
    north = _finite(
        telemetry.get("velocity_north_mps"), 0.0, label="velocity_north_mps", missing=missing
    )
    east = _finite(
        telemetry.get("velocity_east_mps"), 0.0, label="velocity_east_mps", missing=missing
    )
    ground_speed = _finite(
        telemetry.get("ground_speed_mps"),
        math.hypot(north, east),
        label="ground_speed_mps",
        missing=missing,
    )
    airspeed = _finite(
        telemetry.get("airspeed_mps"), ground_speed, label="airspeed_mps", missing=missing
    )
    battery = _finite(
        telemetry.get("battery_remaining_pct"),
        defaults["battery_remaining_pct"],
        label="battery_remaining_pct",
        missing=missing,
    )
    flight_time = _finite(
        telemetry.get("estimated_flight_time_s"),
        defaults["estimated_flight_time_s"],
        label="estimated_flight_time_s",
        missing=missing,
    )
    wind_north = _finite(
        telemetry.get("wind_north_mps"),
        defaults["wind_north_mps"],
        label="wind_north_mps",
        missing=missing,
    )
    wind_east = _finite(
        telemetry.get("wind_east_mps"),
        defaults["wind_east_mps"],
        label="wind_east_mps",
        missing=missing,
    )
    gps_fix = telemetry.get("gps_fix_type")
    if isinstance(gps_fix, bool) or not isinstance(gps_fix, int) or gps_fix < 0:
        missing.add("gps_fix_type")
        gps_fix = 0
    gps_accuracy = _finite(
        telemetry.get("gps_horizontal_accuracy_m"),
        100.0,
        label="gps_horizontal_accuracy_m",
        missing=missing,
    )
    if gps_accuracy < 0.0:
        raise ValueError(f"{episode_id} GPS horizontal accuracy is negative")
    aircraft = GeoPoint(latitude, longitude, max(0.0, altitude), "AGL")
    return ContextSnapshot(
        snapshot_id=f"{episode_id}:{frame.get('frame_id')}",
        captured_at_s=captured_at_s,
        captured_at_utc=captured_at_utc.isoformat().replace("+00:00", "Z"),
        aircraft_position=aircraft,
        home_position=home,
        airframe_profile_id=airframe_profile_id,
        airframe_profile_hash=airframe_profile_sha256,
        battery_remaining_pct=max(0.0, min(100.0, battery)),
        estimated_flight_time_s=max(0.0, flight_time),
        ground_speed_mps=max(0.0, ground_speed),
        airspeed_mps=max(0.0, airspeed),
        wind_north_mps=wind_north,
        wind_east_mps=wind_east,
        gps_fix_type=gps_fix,
        gps_horizontal_accuracy_m=gps_accuracy,
        geofence_valid=_bool(
            telemetry.get("geofence_healthy"), False, label="geofence_healthy", missing=missing
        ),
        camera_available=True,
        perception_available=True,
        map_cache_available=_bool(
            telemetry.get("map_cache_available"), True, label="map_cache_available", missing=missing
        ),
        dem_available=True,
    )


def _event_from_frame(
    *,
    image: np.ndarray,
    depth_values: np.ndarray | None,
    frame_id: str,
    captured_at_s: float,
    detector: Any,
    telemetry: dict[str, Any] | None = None,
    task_phase: OnboardTaskAction,
    depth_frame: dict[str, Any] | None = None,
    camera_calibration: CameraCalibration | None = None,
    aircraft_pose: AircraftPose | None = None,
    target_match_confirmed: bool = False,
) -> OnboardRuntimeEvent:
    # Health events are derived only from the frame-bound PX4 telemetry.  They
    # are intentionally evaluated before the detector so a valid-looking RGB
    # frame cannot hide a navigation fault.  This is the same event contract
    # consumed by the live OnboardAI loop; no Gazebo truth or target pose is
    # consulted here.
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    raw_fix = telemetry.get("gps_fix_type")
    raw_accuracy = telemetry.get("gps_horizontal_accuracy_m")
    if (
        isinstance(raw_fix, (int, float))
        and not isinstance(raw_fix, bool)
        and float(raw_fix) < 3.0
    ) or (
        isinstance(raw_accuracy, (int, float))
        and not isinstance(raw_accuracy, bool)
        and math.isfinite(float(raw_accuracy))
        and float(raw_accuracy) > 5.0
    ):
        return OnboardRuntimeEvent(
            event_id=f"{frame_id}:gps-degraded",
            kind=OnboardEventKind.GPS_DEGRADED,
            observed_at_s=captured_at_s,
            severity=1,
            detail="frame-bound PX4 telemetry reports degraded GNSS quality",
            task_phase=task_phase,
        )
    raw_battery = telemetry.get("battery_remaining_pct")
    if (
        isinstance(raw_battery, (int, float))
        and not isinstance(raw_battery, bool)
        and math.isfinite(float(raw_battery))
        and float(raw_battery) <= 20.0
    ):
        return OnboardRuntimeEvent(
            event_id=f"{frame_id}:battery-low",
            kind=OnboardEventKind.BATTERY_LOW,
            observed_at_s=captured_at_s,
            severity=1,
            detail="frame-bound PX4 telemetry reports low remaining battery",
            task_phase=task_phase,
        )
    raw_obstacle = telemetry.get("obstacle_risk")
    raw_obstacle_distance = telemetry.get("obstacle_distance_m")
    obstacle_risk = raw_obstacle is True or (
        isinstance(raw_obstacle_distance, (int, float))
        and not isinstance(raw_obstacle_distance, bool)
        and math.isfinite(float(raw_obstacle_distance))
        and float(raw_obstacle_distance) < 8.0
    )
    if obstacle_risk:
        return OnboardRuntimeEvent(
            event_id=f"{frame_id}:obstacle-risk",
            kind=OnboardEventKind.OBSTACLE_RISK,
            observed_at_s=captured_at_s,
            severity=1,
            detail="frame-bound obstacle telemetry reports a navigation hazard",
            obstacle_distance_m=(
                float(raw_obstacle_distance)
                if isinstance(raw_obstacle_distance, (int, float))
                and not isinstance(raw_obstacle_distance, bool)
                else None
            ),
            task_phase=task_phase,
        )
    selected = None
    if detector is not None:
        detections = tuple(detector.detect(image))
        selected = max(detections, key=lambda item: item.confidence, default=None)
    if selected is None:
        return OnboardRuntimeEvent(
            event_id=f"{frame_id}:target-lost",
            kind=OnboardEventKind.TARGET_LOST,
            observed_at_s=captured_at_s,
            detail="no target observation from the shared detector boundary",
            task_phase=task_phase,
        )
    center_x, center_y = selected.bbox.center
    distance_m = _depth_median_for_bbox(
        depth_values,
        bbox=selected.bbox.rounded(),
        depth_frame=depth_frame,
        camera_calibration=camera_calibration,
    )
    bearing_deg = None
    geometry_frame = "camera_relative"
    north_offset_m = None
    east_offset_m = None
    # Match the live Jetson geometry gate.  Staging has no Prompt resolver or
    # tracker confirmation, so a detector candidate must stay camera-relative
    # instead of becoming a synthetic NED/GPS target.
    projection_gate = bool(target_match_confirmed) and (
        distance_m is not None
        and camera_calibration is not None
        and aircraft_pose is not None
    )
    if projection_gate:
        target = TargetImageObservation(
            target_id=frame_id,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            center_x=float(center_x),
            center_y=float(center_y),
        )
        _body_ray, ray_ned = _target_rays(camera_calibration, aircraft_pose, target)
        camera_offset = _camera_offset_ned(camera_calibration, aircraft_pose)
        north_offset_m = camera_offset[0] + distance_m * ray_ned[0]
        east_offset_m = camera_offset[1] + distance_m * ray_ned[1]
        horizontal_distance_m = math.hypot(north_offset_m, east_offset_m)
        if horizontal_distance_m > 1.0e-6:
            geometry_frame = "ned"
            bearing_deg = math.degrees(math.atan2(east_offset_m, north_offset_m))
            distance_m = horizontal_distance_m
        else:
            north_offset_m = None
            east_offset_m = None
    elif distance_m is not None and camera_calibration is None:
        # Keep a valid metric range for the shared perception contract, but do
        # not manufacture a bearing or NED offset from image pixels.  The live
        # adapter uses the same camera_relative/fail-closed boundary.
        pass
    return OnboardRuntimeEvent(
        event_id=f"{frame_id}:target-observed",
        kind=OnboardEventKind.TARGET_MOVED,
        observed_at_s=captured_at_s,
        detail="target observation from the shared detector boundary",
        target_bearing_deg=bearing_deg,
        target_geometry_frame=geometry_frame,
        target_distance_m=distance_m,
        target_confidence=float(selected.confidence),
        target_class=str(selected.label),
        # Prompt matching is deliberately not inferred from a class/color toy
        # rule.  The reviewed task resolver supplies these fields later.
        target_match_score=None,
        target_class_match=None,
        target_color_match=None,
        target_north_offset_m=north_offset_m,
        target_east_offset_m=east_offset_m,
        task_phase=task_phase,
    )


def _camera_calibration_from_manifest(document: dict[str, Any]) -> CameraCalibration | None:
    """Load the hash-bound CameraInfo calibration persisted beside RGB frames."""

    raw = document.get("camera_calibration")
    if not isinstance(raw, dict):
        return None
    values = raw.get("calibration")
    if not isinstance(values, dict):
        return None
    try:
        return CameraCalibration(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError("RGB manifest camera_calibration is invalid") from exc


def _aircraft_pose_from_telemetry(
    telemetry: dict[str, Any], *, captured_at_s: float
) -> AircraftPose | None:
    required = ("roll_deg", "pitch_deg", "heading_deg")
    if any(not isinstance(telemetry.get(field), (int, float)) for field in required):
        return None
    try:
        return AircraftPose(
            captured_at_s=captured_at_s,
            roll_deg=float(telemetry["roll_deg"]),
            pitch_deg=float(telemetry["pitch_deg"]),
            heading_deg=float(telemetry["heading_deg"]) % 360.0,
            roll_sigma_deg=float(telemetry.get("attitude_roll_sigma_deg", 0.3)),
            pitch_sigma_deg=float(telemetry.get("attitude_pitch_sigma_deg", 0.3)),
            heading_sigma_deg=float(telemetry.get("attitude_heading_sigma_deg", 1.0)),
            uncertainty_source=str(
                telemetry.get("attitude_uncertainty_source", "gazebo_trajectory_fallback")
            ),
        )
    except (TypeError, ValueError):
        return None


def _validate_depth_frame_contract(
    frame: dict[str, Any],
    *,
    rgb_width: int,
    rgb_height: int,
    camera_calibration: CameraCalibration | None,
) -> None:
    """Require the persisted Gazebo depth semantics before staging a label."""

    if frame.get("encoding") != "FLOAT32_M":
        raise ValueError("3-D episode depth encoding must be FLOAT32_M")
    if frame.get("coordinate_space") != "rgb_pixel_aligned":
        raise ValueError("3-D episode depth must be registered to RGB pixels")
    if frame.get("registration_mode") != "registered_to_rgb":
        raise ValueError("3-D episode depth registration mode is invalid")
    if frame.get("distance_semantics") != "optical_axis_z_m":
        raise ValueError("Gazebo 3-D episode depth must use optical_axis_z_m")
    for key in ("minimum_depth_m", "maximum_depth_m"):
        value = frame.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"3-D episode depth {key} is missing")
    for key in ("width", "height", "stride_bytes"):
        value = frame.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"3-D episode depth {key} is invalid")
    if frame["width"] != rgb_width or frame["height"] != rgb_height:
        raise ValueError("depth geometry does not match the RGB frame")
    if frame["stride_bytes"] != frame["width"] * 4:
        raise ValueError("FLOAT32 depth stride does not match its width")
    for key in ("fx_px", "fy_px", "cx_px", "cy_px"):
        value = frame.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"optical-axis depth {key} is missing")
        if camera_calibration is not None:
            expected = getattr(camera_calibration, key)
            if not math.isclose(float(value), float(expected), rel_tol=0.0, abs_tol=1.0e-6):
                raise ValueError(f"depth {key} does not match RGB CameraInfo")


def _split_for(index: int, explicit: str | None) -> str:
    if explicit is not None:
        if explicit not in {"train", "evaluation", "test"}:
            raise ValueError("split must be train, evaluation, or test")
        return explicit
    # A deterministic episode-level split.  Frames never participate in the
    # split independently, so temporal neighbors cannot leak across partitions.
    return (
        "train",
        "train",
        "train",
        "train",
        "train",
        "train",
        "train",
        "train",
        "evaluation",
        "test",
    )[index % 10]


def _recording_binding(
    path: Path | None,
    *,
    frames: list[dict[str, Any]],
    scenario_manifest_hash: str,
) -> tuple[str, str, bool, datetime | None]:
    if path is None:
        return "unbound-gazebo-capture", "0" * 64, False, None
    recording = RawSensorReplayRecording.from_manifest(path)
    if (
        recording.scenario_manifest_hash is not None
        and recording.scenario_manifest_hash != scenario_manifest_hash
    ):
        raise ValueError("raw replay recording scenario hash does not match RGB manifest")
    if len(recording.frames) != len(frames):
        raise ValueError("raw replay and RGB frame counts differ")
    for index, (raw_frame, rgb_frame) in enumerate(zip(recording.frames, frames, strict=True)):
        if str(rgb_frame.get("frame_id")) != raw_frame.frame_id:
            raise ValueError(f"raw replay frame identity mismatch at sequence {index}")
        rgb_time = rgb_frame.get("provenance", {}).get("captured_at_s")
        if not isinstance(rgb_time, (int, float)) or not math.isfinite(float(rgb_time)):
            raise ValueError("RGB frame timestamp is invalid")
    return recording.recording_id, recording.recording_hash, True, recording.started_at_utc


def build_staging_dataset(
    manifests: list[Path],
    *,
    depth_manifests: list[Path],
    recording_manifests: list[Path | None],
    airframe_profile_id: str,
    airframe_profile_sha256: str,
    task_contract_sha256: str,
    seed: int,
    split: str | None,
    detector_path: Path | None,
    detector_confidence: float,
    mainline_manifest: Path | None = None,
    task_phase: OnboardTaskAction = OnboardTaskAction.SEARCH,
    defaults: dict[str, float] | None = None,
    base_utc: datetime = _DEFAULT_BASE_UTC,
) -> dict[str, object]:
    if not manifests:
        raise ValueError("at least one RGB manifest is required")
    if len(depth_manifests) != len(manifests):
        raise ValueError("one depth manifest is required for every RGB manifest")
    if len(recording_manifests) != len(manifests):
        raise ValueError("recording manifest list must match RGB manifest count")
    _require_hash(airframe_profile_sha256, "airframe_profile_sha256")
    _require_hash(task_contract_sha256, "task_contract_sha256")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if base_utc.tzinfo is None or base_utc.utcoffset() is None:
        raise ValueError("base_utc must be timezone-aware")
    detector = _detector(detector_path, detector_confidence)
    mainline_bindings = (
        None if mainline_manifest is None else _load_mainline_bindings(mainline_manifest)
    )
    defaults = {
        "battery_remaining_pct": 100.0,
        "estimated_flight_time_s": 0.0,
        "wind_north_mps": 0.0,
        "wind_east_mps": 0.0,
        **(defaults or {}),
    }
    source_manifests: list[dict[str, object]] = []
    episodes: list[dict[str, object]] = []
    missing_context_fields: set[str] = set()
    for index, (rgb_path_raw, depth_path_raw, recording_path_raw) in enumerate(
        zip(manifests, depth_manifests, recording_manifests, strict=True)
    ):
        rgb_path = rgb_path_raw.resolve(strict=True)
        depth_path = depth_path_raw.resolve(strict=True)
        rgb_document = json.loads(rgb_path.read_text(encoding="utf-8"))
        if (
            not isinstance(rgb_document, dict)
            or rgb_document.get("schema") != "multidetect.gazebo-bgr24-frame-set"
        ):
            raise ValueError(f"unsupported RGB manifest: {rgb_path}")
        _reject_simulator_truth(rgb_document)
        _verify_gazebo_provenance(
            rgb_document,
            rgb_path,
            mainline_bindings=mainline_bindings,
        )
        depth_index = _load_depth_manifest(depth_path)
        frame_list = rgb_document.get("frames")
        if (
            not isinstance(frame_list, list)
            or len(frame_list) < 2
            or any(not isinstance(item, dict) for item in frame_list)
        ):
            raise ValueError("each 3-D episode requires at least two ordered RGB frames")
        scenario_id = str(rgb_document.get("scenario_id", "unknown"))
        scenario_hash = _require_hash(
            rgb_document.get("scenario_manifest_hash"), "scenario_manifest_hash"
        )
        recording_path = (
            None if recording_path_raw is None else recording_path_raw.resolve(strict=True)
        )
        recording_id, recording_hash, recording_bound, recording_started = _recording_binding(
            recording_path, frames=frame_list, scenario_manifest_hash=scenario_hash
        )
        camera_calibration = _camera_calibration_from_manifest(rgb_document)
        episode_id = f"gazebo-3d-{scenario_id}-{index:03d}"
        first_provenance = frame_list[0].get("provenance")
        if not isinstance(first_provenance, dict):
            raise ValueError("first RGB frame provenance is missing")
        first_telemetry = first_provenance.get("frame_telemetry")
        if not isinstance(first_telemetry, dict):
            raise ValueError("first RGB frame telemetry is missing")
        first_lat = first_telemetry.get("latitude_deg")
        first_lon = first_telemetry.get("longitude_deg")
        if not isinstance(first_lat, (int, float)) or not isinstance(first_lon, (int, float)):
            raise ValueError("episode home GPS is missing")
        home = GeoPoint(float(first_lat), float(first_lon))
        recording_base = recording_started or base_utc
        episode_rows: list[dict[str, object]] = []
        previous_time = -math.inf
        frame_ids: set[str] = set()
        for sequence, frame in enumerate(frame_list):
            frame_id = frame.get("frame_id")
            if not isinstance(frame_id, str) or not frame_id.strip() or frame_id in frame_ids:
                raise ValueError("episode frame IDs must be unique and non-empty")
            frame_ids.add(frame_id)
            provenance = frame.get("provenance")
            if not isinstance(provenance, dict):
                raise ValueError("episode frame provenance is missing")
            telemetry = provenance.get("frame_telemetry")
            if not isinstance(telemetry, dict):
                raise ValueError("episode frame telemetry is missing")
            captured_at_s = provenance.get("captured_at_s")
            if not isinstance(captured_at_s, (int, float)) or not math.isfinite(
                float(captured_at_s)
            ):
                raise ValueError("episode frame timestamp is invalid")
            captured_at_s = float(captured_at_s)
            if captured_at_s <= previous_time:
                raise ValueError("episode frame timestamps must be strictly increasing")
            previous_time = captured_at_s
            image = _read_frame(rgb_document, rgb_path.parent, frame)
            if camera_calibration is not None and (
                camera_calibration.width_px != image.shape[1]
                or camera_calibration.height_px != image.shape[0]
            ):
                raise ValueError("CameraInfo dimensions do not match the RGB frame")
            depth_frame = depth_index.get((int(frame.get("sequence", -1)), frame_id))
            if depth_frame is None:
                raise ValueError(f"3-D episode frame has no depth companion: {frame_id}")
            _validate_depth_frame_contract(
                depth_frame,
                rgb_width=image.shape[1],
                rgb_height=image.shape[0],
                camera_calibration=camera_calibration,
            )
            depth_values = _read_depth_values(
                depth_path.parent,
                depth_frame,
                width=int(provenance["width"]),
                height=int(provenance["height"]),
            )
            context = _telemetry_context(
                frame=frame,
                episode_id=episode_id,
                home=home,
                airframe_profile_id=airframe_profile_id,
                airframe_profile_sha256=airframe_profile_sha256,
                captured_at_utc=recording_base
                + timedelta(
                    seconds=captured_at_s - float(frame_list[0]["provenance"]["captured_at_s"])
                ),
                defaults=defaults,
                missing=missing_context_fields,
            )
            event = _event_from_frame(
                image=image,
                depth_values=depth_values,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                detector=detector,
                telemetry=telemetry,
                task_phase=task_phase,
                depth_frame=depth_frame,
                camera_calibration=camera_calibration,
                aircraft_pose=_aircraft_pose_from_telemetry(
                    telemetry,
                    captured_at_s=captured_at_s,
                ),
            )
            model_input = OnboardModelInput(context, event)
            episode_rows.append(
                {
                    "sequence": sequence,
                    "transition_id": f"{episode_id}:transition:{sequence:06d}",
                    "frame_id": frame_id,
                    "next_frame_id": None
                    if sequence + 1 == len(frame_list)
                    else str(frame_list[sequence + 1]["frame_id"]),
                    "observed_at_s": captured_at_s,
                    "observation": {"context": context.to_mapping(), "event": event.to_mapping()},
                    "features": list(model_input.feature_vector()),
                    "proposed_task_label": None,
                    "proposed_flight_label": None,
                    "proposed_regression": None,
                    "event_kind": event.kind.value,
                    "task_phase": task_phase.value,
                    "source": {
                        "recording_id": recording_id,
                        "recording_hash": recording_hash,
                        "recording_bound": recording_bound,
                        "frame_id": frame_id,
                        "captured_at_s": captured_at_s,
                        "episode_group_sha256": "pending",
                        "rgb_payload_sha256": _require_hash(
                            frame.get("sha256"), "RGB payload sha256"
                        ),
                        "depth_payload_sha256": _require_hash(
                            depth_frame.get("sha256"), "Depth payload sha256"
                        ),
                        "rgb_manifest_sha256": _sha256(rgb_path),
                        "depth_manifest_sha256": _sha256(depth_path),
                        "scenario_manifest_sha256": scenario_hash,
                        "context_sha256": context.canonical_hash,
                        "event_sha256": event.canonical_hash,
                    },
                    "label_status": "pending_independent_review",
                    "planner_revalidated": False,
                    "truth_free": True,
                    "policy_applied_to_actuator": False,
                }
            )
        episode_group_hash = _sha256_bytes(
            "|".join(
                (
                    recording_hash,
                    scenario_hash,
                    airframe_profile_sha256,
                    str(seed + index),
                    task_contract_sha256,
                )
            ).encode("utf-8")
        )
        for row in episode_rows:
            row["source"]["episode_group_sha256"] = episode_group_hash
        source: dict[str, object] = {
            "path": rgb_path.as_posix(),
            "sha256": _sha256(rgb_path),
            "scenario_manifest_sha256": scenario_hash,
            "pixel_source": "actual_gazebo_camera_sensor_output",
            "depth_manifest": {"path": depth_path.as_posix(), "sha256": _sha256(depth_path)},
        }
        if recording_path is not None:
            source["recording_manifest"] = {
                "path": recording_path.as_posix(),
                "sha256": _sha256(recording_path),
                "recording_id": recording_id,
                "recording_hash": recording_hash,
                "frame_count": len(frame_list),
            }
        source_manifests.append(source)
        episodes.append(
            {
                "episode_id": episode_id,
                "scene_id": scenario_id,
                "scenario_manifest_sha256": scenario_hash,
                "airframe_profile_id": airframe_profile_id,
                "airframe_profile_sha256": airframe_profile_sha256,
                "recording_id": recording_id,
                "recording_hash": recording_hash,
                "recording_bound": recording_bound,
                "scenario_seed": seed + index,
                "task_contract_sha256": task_contract_sha256,
                "episode_group_sha256": episode_group_hash,
                "seed": seed + index,
                "split": _split_for(index, split),
                "outcome": "incomplete_capture",
                "transitions": episode_rows,
            }
        )
    return {
        "schema": "multidetect.onboard-trajectory-staging",
        "schema_version": 1,
        "contract": _STAGING_CONTRACT,
        "feature_names": list(ONBOARD_MODEL_FEATURE_NAMES),
        "task_action_order": [item.value for item in ONBOARD_MODEL_TASK_ACTION_ORDER],
        "flight_action_order": [item.value for item in ONBOARD_MODEL_ACTION_ORDER],
        "label_source": "pending_independent_review_and_planner_revalidation",
        "feature_provenance": "recomputed_from_context_event_v1",
        "training_ready": False,
        "review_required": True,
        "truth_free": True,
        "truth_injected": False,
        "policy_applied_to_actuator": False,
        "split_strategy": "episode_scene_airframe_seed",
        "materialized_sources": False,
        "source_manifests": source_manifests,
        "episodes": episodes,
        "missing_context_fields": sorted(missing_context_fields),
        "recording_bound": all(bool(item.get("recording_bound")) for item in episodes),
        "external_gazebo_runtime": True,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "event_coverage": {
            "contract": "multidetect.onboard-3d-event-coverage-v1",
            "event_kind_counts": dict(
                sorted(
                    Counter(
                        transition.get("event_kind")
                        for episode in episodes
                        if isinstance(episode, dict)
                        for transition in (episode.get("transitions") or [])
                        if isinstance(transition, dict)
                        and isinstance(transition.get("event_kind"), str)
                    ).items()
                )
            ),
            "truth_free": True,
            "training_ready": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rgb-manifest", action="append", type=Path, required=True)
    parser.add_argument("--depth-manifest", action="append", type=Path, required=True)
    parser.add_argument("--recording-manifest", action="append", type=Path, default=[])
    parser.add_argument("--airframe-profile-id", required=True)
    parser.add_argument("--airframe-profile-sha256", required=True)
    parser.add_argument("--task-contract-sha256", required=True)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--split", choices=("train", "evaluation", "test"))
    parser.add_argument("--detector", type=Path)
    parser.add_argument("--detector-confidence", type=float, default=0.25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mainline-manifest",
        type=Path,
        default=ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
        help="hash-pinned repository Gazebo mainline required for trajectory staging",
    )
    args = parser.parse_args(argv)
    if len(args.recording_manifest) not in {0, len(args.rgb_manifest)}:
        raise ValueError("recording manifests must be omitted or match RGB manifest count")
    recordings: list[Path | None] = (
        list(args.recording_manifest)
        if args.recording_manifest
        else [None] * len(args.rgb_manifest)
    )
    if not 0.0 <= args.detector_confidence <= 1.0 or not math.isfinite(args.detector_confidence):
        raise ValueError("detector confidence must be in [0, 1]")
    document = build_staging_dataset(
        args.rgb_manifest,
        depth_manifests=args.depth_manifest,
        recording_manifests=recordings,
        airframe_profile_id=args.airframe_profile_id,
        airframe_profile_sha256=args.airframe_profile_sha256,
        task_contract_sha256=args.task_contract_sha256,
        seed=args.seed,
        split=args.split,
        detector_path=args.detector,
        detector_confidence=args.detector_confidence,
        mainline_manifest=args.mainline_manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "episodes": len(document["episodes"]),
                "transitions": sum(len(item["transitions"]) for item in document["episodes"]),
                "training_ready": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
