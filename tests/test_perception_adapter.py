from __future__ import annotations

import hashlib
import math
from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.autonomy import OnboardFramePerceptionAdapter, OnboardTaskAction
from multidetect.autonomy.perception_adapter import FrameCameraPose
from multidetect.domain import BoundingBox, Detection
from multidetect.multimodal_ranging import AircraftPose, CameraCalibration


class _Detector:
    def __init__(self, detections: tuple[Detection, ...]) -> None:
        self.detections = detections

    def detect(self, _image: object) -> tuple[Detection, ...]:
        return self.detections


def _depth(width: int = 4, height: int = 4) -> SimpleNamespace:
    payload = np.full((height, width), 12.5, dtype="<f4").tobytes()
    return SimpleNamespace(
        width=width,
        height=height,
        encoding="FLOAT32_M",
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        minimum_depth_m=0.4,
        maximum_depth_m=800.0,
    )


def _depth_values(values: np.ndarray, **overrides: object) -> SimpleNamespace:
    payload = np.asarray(values, dtype="<f4").tobytes()
    data: dict[str, object] = {
        "width": int(values.shape[1]),
        "height": int(values.shape[0]),
        "stride_bytes": int(values.shape[1] * 4),
        "encoding": "FLOAT32_M",
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "minimum_depth_m": 0.4,
        "maximum_depth_m": 800.0,
        "distance_semantics": "ray_slant_m",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_adapter_binds_real_detection_color_depth_and_requires_match_confirmation() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:, :, 2] = 255  # BGR red
    detector = _Detector((Detection("car", 0.92, BoundingBox(0.0, 0.0, 1.0, 1.0)),))
    adapter = OnboardFramePerceptionAdapter(
        detector, target_class="vehicle", target_color="red", fx_px=100.0, cx_px=2.0
    )
    result = adapter.observe(
        observation_id="frame-red",
        observed_at_s=1.0,
        image_bgr=image,
        source_frame_sha256="a" * 64,
        depth_frame=_depth(),
    )

    assert result.observation.target_visible is True
    assert result.observation.target_class == "vehicle"
    assert result.observation.target_color == "red"
    assert result.observation.target_class_match is True
    assert result.observation.target_color_match is True
    assert result.observation.task_phase is OnboardTaskAction.SEARCH
    assert result.distance_m == 12.5
    assert result.observation.target_distance_m == 12.5
    assert result.depth_payload_sha256 == _depth().payload_sha256
    assert result.observation.source_frame_sha256 == "a" * 64
    confirmed = adapter.observe(
        observation_id="frame-red-confirmed",
        observed_at_s=2.0,
        image_bgr=image,
        source_frame_sha256="c" * 64,
        depth_frame=_depth(),
    )
    assert confirmed.observation.task_phase is OnboardTaskAction.MATCH_TARGET
    recorded = adapter.observe(
        observation_id="frame-red-record",
        observed_at_s=3.0,
        image_bgr=image,
        source_frame_sha256="d" * 64,
        depth_frame=_depth(),
    )
    assert recorded.observation.task_phase is OnboardTaskAction.RECORD_LOCATION


def test_adapter_rejects_green_vehicle_for_red_prompt_and_keeps_search_phase() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:, :, 1] = 255  # BGR green
    result = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.88, BoundingBox(0.0, 0.0, 1.0, 1.0)),)),
        target_color="red",
    ).observe(
        observation_id="frame-green",
        observed_at_s=2.0,
        image_bgr=image,
        source_frame_sha256="b" * 64,
    )

    assert result.observation.target_visible is True
    assert result.color_label == "green"
    assert result.observation.target_color_match is False
    assert result.observation.task_phase is OnboardTaskAction.SEARCH


def test_adapter_neutral_target_does_not_require_colour_identity() -> None:
    image = np.full((4, 4, 3), 96, dtype=np.uint8)
    detector = _Detector((Detection("car", 0.91, BoundingBox(0.0, 0.0, 1.0, 1.0)),))
    adapter = OnboardFramePerceptionAdapter(
        detector,
        target_class="vehicle",
        target_color=None,
        fx_px=100.0,
        cx_px=2.0,
    )

    first = adapter.observe(
        observation_id="neutral-target-search",
        observed_at_s=1.0,
        image_bgr=image,
        source_frame_sha256="1" * 64,
        depth_frame=_depth(),
    )
    second = adapter.observe(
        observation_id="neutral-target-match",
        observed_at_s=2.0,
        image_bgr=image,
        source_frame_sha256="2" * 64,
        depth_frame=_depth(),
    )
    third = adapter.observe(
        observation_id="neutral-target-record",
        observed_at_s=3.0,
        image_bgr=image,
        source_frame_sha256="3" * 64,
        depth_frame=_depth(),
    )

    assert first.observation.target_class_match is True
    assert first.observation.target_color_match is None
    assert first.observation.target_match_score == pytest.approx(0.91)
    assert first.observation.task_phase is OnboardTaskAction.SEARCH
    assert second.observation.task_phase is OnboardTaskAction.MATCH_TARGET
    assert third.observation.task_phase is OnboardTaskAction.RECORD_LOCATION


def test_adapter_color_classifier_uses_saturated_object_pixels_over_box_background() -> None:
    image = np.full((10, 10, 3), 80, dtype=np.uint8)
    image[3:7, 3:7, 2] = 240  # red object on a neutral background
    adapter = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.9, BoundingBox(0.0, 0.0, 1.0, 1.0)),)),
        target_color="red",
    )
    result = adapter.observe(
        observation_id="frame-red-on-background",
        observed_at_s=2.5,
        image_bgr=image,
        source_frame_sha256="1" * 64,
    )

    assert result.color_label == "red"
    assert result.observation.target_color_match is True
    assert result.observation.task_phase is OnboardTaskAction.SEARCH
    confirmed = adapter.observe(
        observation_id="frame-red-on-background-confirmed",
        observed_at_s=3.5,
        image_bgr=image,
        source_frame_sha256="2" * 64,
    )
    assert confirmed.observation.task_phase is OnboardTaskAction.MATCH_TARGET


def test_adapter_without_target_class_detection_fails_closed_to_search() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    result = OnboardFramePerceptionAdapter(
        _Detector((Detection("person", 0.99, BoundingBox(0.0, 0.0, 1.0, 1.0)),)),
        target_class="vehicle",
        target_color="red",
    ).observe(
        observation_id="frame-no-car",
        observed_at_s=3.0,
        image_bgr=image,
    )

    assert result.observation.target_visible is False
    assert result.observation.task_phase is OnboardTaskAction.SEARCH
    assert result.detector_count == 1
    assert result.detector_label is None


def test_adapter_selects_nearest_matching_colour_not_highest_confidence() -> None:
    image = np.zeros((4, 8, 3), dtype=np.uint8)
    image[:, :4, 2] = 255  # left red car
    image[:, 4:, 2] = 255  # right red car
    detector = _Detector(
        (
            Detection("car", 0.99, BoundingBox(0.0, 0.0, 0.5, 1.0)),
            Detection("car", 0.60, BoundingBox(0.5, 0.0, 1.0, 1.0)),
        )
    )
    depth = _depth_values(np.array([[30.0] * 4 + [5.0] * 4] * 4))
    result = OnboardFramePerceptionAdapter(
        detector, target_color="red", fx_px=100.0, cx_px=4.0
    ).observe(
        observation_id="nearest-red",
        observed_at_s=1.0,
        image_bgr=image,
        depth_frame=depth,
    )

    assert result.nearest_observed is True
    assert result.distance_m == 5.0
    assert result.detector_confidence == 0.60
    assert result.candidate_distances_m == (5.0, 30.0)


def test_adapter_does_not_confirm_a_different_red_bbox_as_same_target() -> None:
    image = np.zeros((4, 8, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    detector = _Detector((Detection("car", 0.9, BoundingBox(0.0, 0.0, 0.4, 1.0)),))
    adapter = OnboardFramePerceptionAdapter(detector, target_color="red")
    first = adapter.observe(observation_id="red-a", observed_at_s=1.0, image_bgr=image)
    detector.detections = (Detection("car", 0.9, BoundingBox(0.6, 0.0, 1.0, 1.0)),)
    second = adapter.observe(observation_id="red-b", observed_at_s=1.1, image_bgr=image)

    assert first.observation.task_phase is OnboardTaskAction.SEARCH
    assert second.observation.task_phase is OnboardTaskAction.SEARCH


def test_adapter_converts_optical_axis_depth_to_ray_slant() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    depth = _depth_values(
        np.full((4, 4), 5.0),
        distance_semantics="optical_axis_z_m",
        fx_px=2.0,
        fy_px=2.0,
        cx_px=2.0,
        cy_px=2.0,
    )
    result = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.9, BoundingBox(0.75, 0.0, 1.0, 1.0)),)),
        target_color="red",
    ).observe(
        observation_id="optical-axis-depth",
        observed_at_s=1.0,
        image_bgr=image,
        depth_frame=depth,
    )

    assert result.distance_m is not None
    assert result.distance_m > 5.0


def test_adapter_does_not_project_heading_only_bearing_into_ned() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    result = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.9, BoundingBox(0.0, 0.0, 1.0, 1.0)),)),
        target_color="red",
        fx_px=100.0,
        cx_px=2.0,
    ).observe(
        observation_id="heading-projection",
        observed_at_s=1.0,
        image_bgr=image,
        depth_frame=_depth(),
        aircraft_heading_deg=90.0,
    )

    assert result.observation.target_north_offset_m is None
    assert result.observation.target_east_offset_m is None
    assert result.observation.target_geometry_frame == "camera_relative"


def test_adapter_relabels_calibrated_projection_as_ned_bearing() -> None:
    image = np.zeros((4, 8, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    frame_hash = "1" * 64
    result = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.9, BoundingBox(0.75, 0.0, 1.0, 1.0)),)),
        target_color="red",
    ).observe(
        observation_id="calibrated-right",
        observed_at_s=1.0,
        image_bgr=image,
        source_frame_sha256=frame_hash,
        depth_frame=_depth_values(np.full((4, 8), 12.5)),
        camera_pose=FrameCameraPose(
            calibration=CameraCalibration(
                calibration_id="test-camera-wide",
                width_px=8,
                height_px=4,
                fx_px=100.0,
                fy_px=100.0,
                cx_px=4.0,
                cy_px=2.0,
            ),
            aircraft_pose=AircraftPose(
                captured_at_s=1.0,
                roll_deg=0.0,
                pitch_deg=0.0,
                heading_deg=0.0,
            ),
            source_frame_sha256=frame_hash,
        ),
    )

    assert result.observation.target_geometry_frame == "ned"
    assert result.observation.target_north_offset_m is not None
    assert result.observation.target_east_offset_m is not None
    assert result.observation.target_bearing_deg == pytest.approx(
        math.degrees(
            math.atan2(
                result.observation.target_east_offset_m,
                result.observation.target_north_offset_m,
            )
        )
    )


def _camera_pose(frame_hash: str, *, heading_deg: float = 0.0) -> FrameCameraPose:
    return FrameCameraPose(
        calibration=CameraCalibration(
            calibration_id="test-camera",
            width_px=4,
            height_px=4,
            fx_px=100.0,
            fy_px=100.0,
            cx_px=2.0,
            cy_px=2.0,
        ),
        aircraft_pose=AircraftPose(
            captured_at_s=1.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            heading_deg=heading_deg,
        ),
        source_frame_sha256=frame_hash,
    )


def test_adapter_projects_depth_with_full_calibrated_pose_and_frame_binding() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    frame_hash = "e" * 64
    result = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.9, BoundingBox(0.0, 0.0, 1.0, 1.0)),)),
        target_color="red",
    ).observe(
        observation_id="full-pose",
        observed_at_s=1.0,
        image_bgr=image,
        source_frame_sha256=frame_hash,
        depth_frame=_depth(),
        camera_pose=_camera_pose(frame_hash, heading_deg=90.0),
    )

    assert result.observation.target_north_offset_m == pytest.approx(0.0, abs=1e-6)
    assert result.observation.target_east_offset_m == pytest.approx(12.5, abs=1e-6)


def test_adapter_rejects_pose_from_different_frame() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    adapter = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.9, BoundingBox(0.0, 0.0, 1.0, 1.0)),)),
        target_color="red",
    )

    with pytest.raises(ValueError, match="source hash"):
        adapter.observe(
            observation_id="wrong-pose",
            observed_at_s=1.0,
            image_bgr=image,
            source_frame_sha256="a" * 64,
            depth_frame=_depth(),
            camera_pose=_camera_pose("b" * 64),
        )


def test_adapter_low_detector_confidence_stays_search_and_cannot_geolocate() -> None:
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    frame_hash = "f" * 64
    result = OnboardFramePerceptionAdapter(
        _Detector((Detection("car", 0.10, BoundingBox(0.0, 0.0, 1.0, 1.0)),)),
        target_color="red",
    ).observe(
        observation_id="low-confidence",
        observed_at_s=1.0,
        image_bgr=image,
        source_frame_sha256=frame_hash,
        depth_frame=_depth(),
        camera_pose=_camera_pose(frame_hash),
    )

    assert result.observation.task_phase is OnboardTaskAction.SEARCH
    assert result.observation.target_match_score == 0.0
    assert result.observation.target_north_offset_m is None
    assert result.observation.target_east_offset_m is None


def test_adapter_matching_colour_with_unknown_range_is_nearest_known_only() -> None:
    image = np.zeros((4, 8, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    depth = _depth_values(np.array([[0.0] * 4 + [5.0] * 4] * 4))
    frame_hash = "d" * 64
    result = OnboardFramePerceptionAdapter(
        _Detector(
            (
                Detection("car", 0.99, BoundingBox(0.0, 0.0, 0.5, 1.0)),
                Detection("car", 0.60, BoundingBox(0.5, 0.0, 1.0, 1.0)),
            )
        ),
        target_color="red",
    ).observe(
        observation_id="nearest-known-only",
        observed_at_s=1.0,
        image_bgr=image,
        source_frame_sha256=frame_hash,
        depth_frame=depth,
        camera_pose=FrameCameraPose(
            calibration=CameraCalibration(
                calibration_id="test-camera-wide",
                width_px=8,
                height_px=4,
                fx_px=100.0,
                fy_px=100.0,
                cx_px=4.0,
                cy_px=2.0,
            ),
            aircraft_pose=AircraftPose(
                captured_at_s=1.0,
                roll_deg=0.0,
                pitch_deg=0.0,
                heading_deg=0.0,
            ),
            source_frame_sha256=frame_hash,
        ),
    )

    assert result.distance_m == 5.0
    assert result.nearest_observed is False
    assert result.observation.target_north_offset_m is None
    assert result.observation.target_east_offset_m is None
