from __future__ import annotations

import pytest

from multidetect.adapters.fire_smoke_legacy import (
    adapt_darknet_detection,
    adapt_yolov5_detections,
)
from multidetect.config import MissionConfig, MissionType, PayloadSpec, PlatformMode
from multidetect.domain import BoundingBox, Detection, SensorKind
from multidetect.perception import fuse_rgb_thermal
from multidetect.rgb_fire_corroboration import (
    IndependentRgbFireCorroborationConfig,
    IndependentRgbFireCorroborator,
)
from multidetect.tracking import FrameOrderError, IoUMultiObjectTracker

_DEFAULT_FLAME_BOX = BoundingBox(0.2, 0.2, 0.4, 0.4)


def _mission_config(
    *,
    minimum_track_observations: int = 3,
    minimum_track_time_seconds: float = 2.0,
    maximum_track_gap_seconds: float = 1.0,
    require_independent_rgb_corroboration: bool = False,
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
        require_independent_rgb_corroboration=require_independent_rgb_corroboration,
        require_thermal_corroboration=require_thermal_corroboration,
    )


def _flame(
    bbox: BoundingBox = _DEFAULT_FLAME_BOX,
    *,
    confidence: float = 0.9,
) -> Detection:
    return Detection("flame", confidence, bbox)


def _corroborated_flame() -> Detection:
    corroborator = IndependentRgbFireCorroborator(
        IndependentRgbFireCorroborationConfig(
            evidence_qualified=True,
            primary_artifact_sha256="1" * 64,
            verifier_artifact_sha256="2" * 64,
        )
    )
    primary = Detection(
        "flame",
        0.9,
        _DEFAULT_FLAME_BOX,
        model_version="primary-fire-v1",
    )
    verifier = Detection(
        "flame",
        0.88,
        _DEFAULT_FLAME_BOX,
        model_version="verifier-fire-v1",
    )
    return corroborator.corroborate((primary,), (verifier,)).detections[0]


@pytest.mark.parametrize(
    "values",
    [
        (float("nan"), 0.1, 0.2, 0.3),
        (0.1, float("inf"), 0.2, 0.3),
    ],
)
def test_bounding_box_rejects_nonfinite_coordinates(values) -> None:
    with pytest.raises(ValueError, match="finite"):
        BoundingBox(*values)


def test_detection_rejects_nonfinite_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        Detection("flame", float("nan"), _DEFAULT_FLAME_BOX)


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


@pytest.mark.parametrize("invalid_size", [True, 0, -1, 1.5])
def test_frame_id_history_size_must_be_a_positive_integer(invalid_size) -> None:
    with pytest.raises(ValueError, match="frame_id_history_size"):
        IoUMultiObjectTracker(
            _mission_config(),
            frame_id_history_size=invalid_size,
        )


def test_recent_frame_id_duplicate_detection_has_bounded_memory() -> None:
    tracker = IoUMultiObjectTracker(_mission_config(), frame_id_history_size=2)

    tracker.update_detections(frame_id="frame-1", captured_at_s=1.0, detections=(_flame(),))
    tracker.update_detections(frame_id="frame-2", captured_at_s=2.0, detections=(_flame(),))
    tracker.update_detections(frame_id="frame-3", captured_at_s=3.0, detections=(_flame(),))

    assert tracker.remembered_frame_id_count == 2
    with pytest.raises(FrameOrderError, match="duplicate"):
        tracker.update_detections(frame_id="frame-2", captured_at_s=4.0, detections=(_flame(),))


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


def test_latest_observation_must_retain_independent_rgb_corroboration() -> None:
    tracker = IoUMultiObjectTracker(_mission_config(require_independent_rgb_corroboration=True))
    corroborated = _corroborated_flame()
    tracker.update_detections(frame_id="frame-1", captured_at_s=0.0, detections=(corroborated,))
    tracker.update_detections(frame_id="frame-2", captured_at_s=1.0, detections=(corroborated,))
    (confirmed,) = tracker.update_detections(
        frame_id="frame-3", captured_at_s=2.0, detections=(corroborated,)
    )

    (lost_corroboration,) = tracker.update_detections(
        frame_id="frame-4", captured_at_s=3.0, detections=(_flame(),)
    )

    assert confirmed.confirmed is True
    assert confirmed.independent_rgb_corroborated is True
    assert lost_corroboration.independent_rgb_corroborated is False
    assert lost_corroboration.confirmed is False


def test_boolean_only_independent_rgb_claim_never_confirms_track() -> None:
    tracker = IoUMultiObjectTracker(_mission_config(require_independent_rgb_corroboration=True))
    forged = Detection(
        "flame",
        0.9,
        _DEFAULT_FLAME_BOX,
        metadata={"independent_rgb_corroborated": True},
    )

    tracker.update_detections(frame_id="frame-1", captured_at_s=0.0, detections=(forged,))
    tracker.update_detections(frame_id="frame-2", captured_at_s=1.0, detections=(forged,))
    (track,) = tracker.update_detections(
        frame_id="frame-3", captured_at_s=2.0, detections=(forged,)
    )

    assert track.independent_rgb_corroborated is False
    assert track.confirmed is False
