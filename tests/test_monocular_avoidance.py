from __future__ import annotations

from math import hypot

import pytest

from multidetect.monocular_avoidance import (
    CollisionRiskState,
    MonocularAvoidanceConfig,
    MonocularCollisionRiskEvaluator,
    OpenCVSparseFlowAvoidance,
    SparseFlowSample,
)


def _radial_samples(
    *, width: int, height: int, ttc_s: float | None
) -> tuple[SparseFlowSample, ...]:
    center_x = width / 2.0
    center_y = height / 2.0
    interval_s = 0.05
    samples = []
    for x in range(40, width, 40):
        for y in range(40, height, 40):
            radius_x = x - center_x
            radius_y = y - center_y
            radius = hypot(radius_x, radius_y)
            if radius < 40:
                continue
            if ttc_s is None:
                dx = dy = 0.0
            else:
                radial_speed = radius / ttc_s
                dx = radius_x / radius * radial_speed * interval_s
                dy = radius_y / radius * radial_speed * interval_s
            samples.append(SparseFlowSample(float(x), float(y), dx, dy))
    return tuple(samples)


def _evaluate(ttc_s: float | None):
    width, height = 640, 360
    return MonocularCollisionRiskEvaluator().evaluate(
        frame_id="frame-2",
        width=width,
        height=height,
        captured_at_s=10.0,
        produced_at_s=10.05,
        frame_interval_s=0.05,
        samples=_radial_samples(width=width, height=height, ttc_s=ttc_s),
        rotation_compensated=True,
    )


def test_static_rotation_compensated_scene_is_clear_and_advisory_only() -> None:
    assessment = _evaluate(None)

    assert assessment.state is CollisionRiskState.CLEAR
    assert assessment.rotation_compensated is True
    assert assessment.advisory_only is True
    assert all(zone.state is CollisionRiskState.CLEAR for zone in assessment.zones)


def test_radial_expansion_crosses_caution_and_avoid_ttc_thresholds() -> None:
    caution = _evaluate(2.25)
    avoid = _evaluate(1.0)

    assert caution.state is CollisionRiskState.CAUTION
    assert avoid.state is CollisionRiskState.AVOID
    assert min(zone.ttc_s for zone in avoid.zones if zone.ttc_s is not None) <= 1.01


def test_stale_uncompensated_or_feature_poor_flow_fails_closed() -> None:
    evaluator = MonocularCollisionRiskEvaluator()
    common = {
        "frame_id": "frame-2",
        "width": 640,
        "height": 360,
        "captured_at_s": 10.0,
        "frame_interval_s": 0.05,
        "samples": _radial_samples(width=640, height=360, ttc_s=None),
    }

    stale = evaluator.evaluate(**common, produced_at_s=10.5, rotation_compensated=True)
    uncompensated = evaluator.evaluate(**common, produced_at_s=10.05, rotation_compensated=False)
    sparse = evaluator.evaluate(
        **{**common, "samples": common["samples"][:4]},
        produced_at_s=10.05,
        rotation_compensated=True,
    )

    assert stale.state is CollisionRiskState.INVALID and stale.reason == "STALE_FRAME"
    assert uncompensated.state is CollisionRiskState.INVALID
    assert sparse.state is CollisionRiskState.INVALID and sparse.reason == "INSUFFICIENT_FEATURES"


def test_opencv_frontend_starts_invalid_and_never_claims_control_authority() -> None:
    import numpy as np

    tracker = OpenCVSparseFlowAvoidance(
        MonocularAvoidanceConfig(minimum_feature_count=8, minimum_zone_feature_count=1)
    )
    image = np.zeros((180, 320, 3), dtype=np.uint8)

    assessment = tracker.update(
        image,
        frame_id="warmup",
        captured_at_s=1.0,
        produced_at_s=1.01,
    )

    assert assessment.state is CollisionRiskState.INVALID
    assert assessment.reason == "WARMUP"
    assert assessment.advisory_only is True


def test_opencv_frontend_exposes_normalized_camera_motion_for_target_compensation() -> None:
    import cv2
    import numpy as np

    tracker = OpenCVSparseFlowAvoidance(
        MonocularAvoidanceConfig(
            minimum_feature_count=8,
            minimum_zone_feature_count=1,
            analysis_width=320,
        )
    )
    rng = np.random.default_rng(42)
    previous = rng.integers(0, 256, size=(180, 320, 3), dtype=np.uint8)
    current = cv2.warpAffine(
        previous,
        np.asarray([[1.0, 0.0, 8.0], [0.0, 1.0, 0.0]], dtype=np.float32),
        (320, 180),
    )
    tracker.update(previous, frame_id="frame-1", captured_at_s=1.0, produced_at_s=1.01)
    assessment = tracker.update(
        current,
        frame_id="frame-2",
        captured_at_s=1.05,
        produced_at_s=1.06,
    )

    assert assessment.rotation_compensated is True
    assert assessment.camera_motion_dx is not None
    assert 0.015 <= assessment.camera_motion_dx <= 0.035
    assert assessment.camera_motion_dy is not None
    assert abs(assessment.camera_motion_dy) <= 0.015
    assert assessment.camera_motion_scale is not None
    assert 0.95 <= assessment.camera_motion_scale <= 1.05
    assert assessment.camera_motion_confidence is not None
    assert assessment.camera_motion_confidence >= 0.5


def test_opencv_frontend_exposes_roll_and_aspect_for_target_compensation() -> None:
    import cv2
    import numpy as np

    width, height = 320, 180
    tracker = OpenCVSparseFlowAvoidance(
        MonocularAvoidanceConfig(
            minimum_feature_count=8,
            minimum_zone_feature_count=1,
            analysis_width=320,
        )
    )
    previous = np.random.default_rng(121).integers(
        0,
        256,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    transform = cv2.getRotationMatrix2D((width * 0.5, height * 0.5), 11.0, 1.04)
    transform[:, 2] += np.asarray((5.0, -3.0))
    current = cv2.warpAffine(
        previous,
        transform,
        (width, height),
        borderMode=cv2.BORDER_REFLECT,
    )
    tracker.update(previous, frame_id="roll-1", captured_at_s=1.0, produced_at_s=1.01)
    assessment = tracker.update(
        current,
        frame_id="roll-2",
        captured_at_s=1.05,
        produced_at_s=1.06,
    )

    assert assessment.rotation_compensated is True
    assert assessment.camera_motion_rotation_deg is not None
    assert abs(assessment.camera_motion_rotation_deg) == pytest.approx(11.0, abs=1.2)
    assert assessment.camera_motion_aspect_ratio == pytest.approx(width / height)
    assert assessment.camera_motion_affine is not None
