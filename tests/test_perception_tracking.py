from __future__ import annotations

import pytest

from multidetect.adapters.fire_smoke_legacy import (
    adapt_darknet_detection,
    adapt_yolov5_detections,
)
from multidetect.config import MissionConfig, MissionType, PayloadSpec, PlatformMode
from multidetect.domain import BoundingBox, Detection, SensorKind
from multidetect.perception import fuse_rgb_thermal
from multidetect.tracking import FrameOrderError, IoUMultiObjectTracker

_DEFAULT_FLAME_BOX = BoundingBox(0.2, 0.2, 0.4, 0.4)


def _mission_config(
    *,
    minimum_track_observations: int = 3,
    minimum_track_time_seconds: float = 2.0,
    maximum_track_gap_seconds: float = 1.0,
    require_thermal_corroboration: bool = False,
) -> MissionConfig:
    return MissionConfig(
        mission_id="perception-test",
        mission_type=MissionType.FIRE_SUPPRESSION,
        platform_mode=PlatformMode.MULTI_DEPLOYMENT,
        payloads=(PayloadSpec("slot-1", "fire_suppression_agent"),),
        target_classes=("flame",),
        minimum_confidence=0.8,
        minimum_track_time_seconds=minimum_track_time_seconds,
        minimum_track_observations=minimum_track_observations,
        maximum_track_gap_seconds=maximum_track_gap_seconds,
        require_thermal_corroboration=require_thermal_corroboration,
    )


def _flame(
    bbox: BoundingBox = _DEFAULT_FLAME_BOX,
    *,
    confidence: float = 0.9,
) -> Detection:
    return Detection("flame", confidence, bbox)


def test_darknet_center_box_is_normalized_and_fire_is_aliased() -> None:
    detection = adapt_darknet_detection(
        (b"fire", "90", (100, 50, 40, 20)),
        image_width=200,
        image_height=100,
    )

    assert detection.label == "flame"
    assert detection.confidence == pytest.approx(0.9)
    assert detection.bbox.rounded() == (0.4, 0.4, 0.6, 0.6)


def test_yolov5_xyxy_rows_are_normalized() -> None:
    (detection,) = adapt_yolov5_detections(
        [[20, 10, 100, 60, 0.85, 0]],
        image_width=200,
        image_height=100,
    )

    assert detection.label == "flame"
    assert detection.bbox.rounded() == (0.1, 0.1, 0.5, 0.6)


def test_rgb_thermal_iou_fusion_records_corroboration() -> None:
    rgb = _flame()
    thermal = Detection(
        "hotspot",
        0.88,
        BoundingBox(0.22, 0.22, 0.42, 0.42),
        sensor=SensorKind.THERMAL,
        model_version="thermal-v1",
    )
    far_thermal = Detection(
        "hotspot",
        0.99,
        BoundingBox(0.7, 0.7, 0.9, 0.9),
        sensor=SensorKind.THERMAL,
    )

    (fused,) = fuse_rgb_thermal((rgb,), (thermal, far_thermal), iou_threshold=0.3)

    assert fused.sensor is SensorKind.FUSED
    assert fused.confidence == rgb.confidence
    assert fused.metadata["thermal_corroborated"] is True
    assert fused.metadata["thermal_label"] == "hotspot"
    assert fused.metadata["thermal_iou"] == pytest.approx(rgb.bbox.iou(thermal.bbox))


def test_single_frame_never_confirms_track() -> None:
    tracker = IoUMultiObjectTracker(_mission_config())

    (track,) = tracker.update_detections(
        frame_id="frame-1",
        captured_at_s=0.0,
        detections=(_flame(),),
    )

    assert track.observation_count == 1
    assert track.confirmed is False


def test_continuous_observations_confirm_track() -> None:
    tracker = IoUMultiObjectTracker(_mission_config())

    tracker.update_detections(frame_id="frame-1", captured_at_s=0.0, detections=(_flame(),))
    tracker.update_detections(frame_id="frame-2", captured_at_s=1.0, detections=(_flame(),))
    (track,) = tracker.update_detections(
        frame_id="frame-3", captured_at_s=2.0, detections=(_flame(),)
    )

    assert track.track_id == "track-000001"
    assert track.observation_count == 3
    assert track.consecutive_observations == 3
    assert track.duration_s == pytest.approx(2.0)
    assert track.confirmed is True


def test_duplicate_and_out_of_order_frames_are_rejected() -> None:
    tracker = IoUMultiObjectTracker(_mission_config())
    tracker.update_detections(frame_id="frame-1", captured_at_s=1.0, detections=(_flame(),))

    with pytest.raises(FrameOrderError, match="duplicate"):
        tracker.update_detections(frame_id="frame-1", captured_at_s=2.0, detections=(_flame(),))
    with pytest.raises(FrameOrderError, match="strictly increasing"):
        tracker.update_detections(frame_id="frame-2", captured_at_s=0.5, detections=(_flame(),))


def test_gap_beyond_limit_rebuilds_track_identity() -> None:
    tracker = IoUMultiObjectTracker(_mission_config(maximum_track_gap_seconds=0.5))
    (first,) = tracker.update_detections(
        frame_id="frame-1", captured_at_s=0.0, detections=(_flame(),)
    )
    (rebuilt,) = tracker.update_detections(
        frame_id="frame-2", captured_at_s=1.0, detections=(_flame(),)
    )

    assert rebuilt.track_id != first.track_id
    assert rebuilt.observation_count == 1
    assert rebuilt.confirmed is False


def test_latest_observation_must_retain_thermal_corroboration() -> None:
    tracker = IoUMultiObjectTracker(_mission_config(require_thermal_corroboration=True))
    corroborated = Detection(
        "flame",
        0.9,
        _DEFAULT_FLAME_BOX,
        sensor=SensorKind.FUSED,
        metadata={"thermal_corroborated": True},
    )
    tracker.update_detections(frame_id="frame-1", captured_at_s=0.0, detections=(corroborated,))
    tracker.update_detections(frame_id="frame-2", captured_at_s=1.0, detections=(corroborated,))
    (confirmed,) = tracker.update_detections(
        frame_id="frame-3", captured_at_s=2.0, detections=(corroborated,)
    )

    (lost_thermal,) = tracker.update_detections(
        frame_id="frame-4", captured_at_s=3.0, detections=(_flame(),)
    )

    assert confirmed.confirmed is True
    assert lost_thermal.thermal_corroborated is False
    assert lost_thermal.confirmed is False
