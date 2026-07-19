from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from multidetect.domain import BoundingBox
from multidetect.short_term_tracking import (
    OpenCVShortTermTargetTracker,
    ShortTermTrackingConfig,
    ShortTermTrackingStatus,
)
from multidetect.unified_tracking import (
    CameraMotionEstimate,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackState,
)


def _textured_frame(
    *,
    left: int,
    top: int,
    size: int,
    seed: int = 42,
    width: int = 320,
    height: int = 180,
) -> tuple[np.ndarray, BoundingBox]:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    patch = np.random.default_rng(seed).integers(
        0,
        256,
        size=(size, size, 3),
        dtype=np.uint8,
    )
    frame[top : top + size, left : left + size] = patch
    return frame, BoundingBox(
        left / width,
        top / height,
        (left + size) / width,
        (top + size) / height,
    )


def _initial_tracks(bbox: BoundingBox):
    pool = UnifiedTargetPool()
    update = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(
            TargetObservation(
                label="vehicle",
                confidence=0.95,
                bbox=bbox,
            ),
        ),
    )
    return update.tracks


def test_local_forward_backward_flow_emits_prediction_hint_only() -> None:
    first, bbox = _textured_frame(left=80, top=60, size=64)
    second, _ = _textured_frame(left=92, top=60, size=64)
    tracker = OpenCVShortTermTargetTracker(ShortTermTrackingConfig(minimum_flow_points=6))

    warmup = tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))
    result = tracker.update_frame(second, captured_at_s=1.05)

    assert warmup.status is ShortTermTrackingStatus.WARMUP
    assert result.status is ShortTermTrackingStatus.OK
    assert result.optical_flow_hint_count == 1
    assert result.template_hint_count == 0
    assert len(result.hints) == 1
    hint = result.hints[0]
    assert hint.source == "optical_flow_fb"
    assert 0.025 <= hint.residual_dx <= 0.050
    assert abs(hint.residual_dy) <= 0.01
    assert hint.confidence >= 0.55
    assert result.metadata_only is True
    assert result.flight_control_enabled is False


def test_global_camera_motion_is_removed_from_local_flow_hint() -> None:
    first, bbox = _textured_frame(left=80, top=60, size=64)
    second, _ = _textured_frame(left=88, top=60, size=64)
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))

    result = tracker.update_frame(
        second,
        captured_at_s=1.05,
        camera_motion=CameraMotionEstimate(
            dx=8 / 320,
            dy=0.0,
            confidence=0.95,
        ),
    )

    assert result.optical_flow_hint_count == 1
    assert abs(result.hints[0].residual_dx) <= 0.01
    assert abs(result.hints[0].residual_dy) <= 0.01


def test_camera_motion_seeds_lk_with_compensated_feature_positions(monkeypatch) -> None:
    """Known camera motion should avoid making LK search from stale feature pixels."""

    import cv2

    first, bbox = _textured_frame(left=80, top=60, size=64)
    second, _ = _textured_frame(left=88, top=60, size=64)
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))
    original = cv2.calcOpticalFlowPyrLK
    seeded_calls = []

    def spy(previous_gray, gray, previous_points, initial_points, **kwargs):
        if initial_points is not None:
            seeded_calls.append(
                (
                    previous_points.copy(),
                    initial_points.copy(),
                    int(kwargs.get("flags", 0)),
                )
            )
        return original(previous_gray, gray, previous_points, initial_points, **kwargs)

    monkeypatch.setattr(cv2, "calcOpticalFlowPyrLK", spy)
    result = tracker.update_frame(
        second,
        captured_at_s=1.05,
        camera_motion=CameraMotionEstimate(dx=8 / 320, dy=0.0, confidence=0.95),
    )

    assert result.optical_flow_hint_count == 1
    assert len(seeded_calls) == 1
    previous_points, initial_points, flags = seeded_calls[0]
    assert flags & cv2.OPTFLOW_USE_INITIAL_FLOW
    seeded_dx_px = np.median(initial_points[:, 0, 0] - previous_points[:, 0, 0])
    seeded_dy_px = np.median(initial_points[:, 0, 1] - previous_points[:, 0, 1])
    assert seeded_dx_px == pytest.approx(8.0, abs=0.01)
    assert seeded_dy_px == pytest.approx(0.0, abs=0.01)


def test_background_flow_automatically_compensates_fixed_fire_target_during_camera_pan() -> None:
    import cv2

    width, height = 320, 180
    camera_dx_px = 8
    rng = np.random.default_rng(314)
    background = rng.integers(0, 160, size=(height, width, 3), dtype=np.uint8)
    patch = np.random.default_rng(2718).integers(
        80,
        256,
        size=(52, 52, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[64:116, 92:144] = patch
    second = cv2.warpAffine(
        background,
        np.float32([[1.0, 0.0, camera_dx_px], [0.0, 1.0, 0.0]]),
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    second[64:116, 92 + camera_dx_px : 144 + camera_dx_px] = patch
    bbox = BoundingBox(92 / width, 64 / height, 144 / width, 116 / height)
    pool = UnifiedTargetPool()
    tracks = pool.update(
        frame_id="fire-1",
        captured_at_s=1.0,
        observations=(TargetObservation("flame", 0.95, bbox),),
    ).tracks
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(tracks)

    result = tracker.update_frame(second, captured_at_s=1.05)

    assert result.camera_motion is not None
    assert result.camera_motion_source == "background_affine_flow"
    assert result.camera_motion_feature_count >= 20
    assert result.camera_motion.dx == pytest.approx(camera_dx_px / width, abs=0.008)
    assert result.camera_motion.confidence >= 0.55
    assert len(result.hints) == 1
    assert abs(result.hints[0].residual_dx) <= 0.01
    assert abs(result.hints[0].residual_dy) <= 0.01


def test_background_flow_preserves_real_target_motion_after_camera_compensation() -> None:
    import cv2

    width, height = 320, 180
    camera_dx_px = 7
    target_dx_px = 5
    background = np.random.default_rng(1618).integers(
        0,
        180,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(1414).integers(
        70,
        256,
        size=(56, 56, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[58:114, 82:138] = patch
    second = cv2.warpAffine(
        background,
        np.float32([[1.0, 0.0, camera_dx_px], [0.0, 1.0, 0.0]]),
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    destination_x = 82 + camera_dx_px + target_dx_px
    second[58:114, destination_x : destination_x + 56] = patch
    bbox = BoundingBox(82 / width, 58 / height, 138 / width, 114 / height)
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))

    result = tracker.update_frame(second, captured_at_s=1.05)

    assert result.camera_motion is not None
    assert len(result.hints) == 1
    assert result.hints[0].residual_dx == pytest.approx(target_dx_px / width, abs=0.010)
    assert abs(result.hints[0].residual_dy) <= 0.01


def test_phase_correlation_recovers_large_camera_pan_after_sparse_flow_loses_lock() -> None:
    import cv2

    width, height = 320, 180
    camera_dx_px = 72
    background = np.random.default_rng(314).integers(
        0,
        160,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(2718).integers(
        80,
        256,
        size=(44, 44, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[64:108, 92:136] = patch
    second = cv2.warpAffine(
        background,
        np.float32([[1.0, 0.0, camera_dx_px], [0.0, 1.0, 0.0]]),
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    second[64:108, 92 + camera_dx_px : 136 + camera_dx_px] = patch
    bbox = BoundingBox(92 / width, 64 / height, 136 / width, 108 / height)
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))

    result = tracker.update_frame(second, captured_at_s=1.05)

    assert result.status is ShortTermTrackingStatus.OK
    assert result.camera_motion is not None
    assert result.camera_motion_source == "background_phase_correlation"
    assert result.camera_motion.dx == pytest.approx(camera_dx_px / width, abs=0.012)
    assert result.camera_motion.confidence >= 0.50
    assert len(result.hints) == 1
    assert abs(result.hints[0].residual_dx) <= 0.015
    assert abs(result.hints[0].residual_dy) <= 0.015


def test_large_camera_pan_keeps_visual_track_box_continuous_without_detector_frame() -> None:
    import cv2

    width, height = 320, 180
    camera_dx_px = 72
    background = np.random.default_rng(991).integers(
        0,
        160,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(992).integers(
        80,
        256,
        size=(44, 44, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[64:108, 80:124] = patch
    second = cv2.warpAffine(
        background,
        np.float32([[1.0, 0.0, camera_dx_px], [0.0, 1.0, 0.0]]),
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    second[64:108, 80 + camera_dx_px : 124 + camera_dx_px] = patch
    bbox = BoundingBox(80 / width, 64 / height, 124 / width, 108 / height)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.15,
            reacquisition_timeout_s=0.50,
        )
    )
    initial = pool.update(
        frame_id="camera-pan-1",
        captured_at_s=1.0,
        observations=(TargetObservation("flame", 0.95, bbox),),
    )
    target_id = initial.tracks[0].track_id
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)

    short_term = tracker.update_frame(second, captured_at_s=1.05)
    update = pool.update(
        frame_id="camera-pan-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=short_term.camera_motion,
        motion_hints=short_term.hints,
        visual_confirmation_track_ids=(target_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == target_id)
    assert short_term.camera_motion_source == "background_phase_correlation"
    assert update.visual_confirmed_track_ids == (target_id,)
    assert tracked.state is UnifiedTrackState.TRACKING
    assert tracked.missed_frame_count == 0
    assert tracked.bbox.center[0] * width == pytest.approx(102 + camera_dx_px, abs=2.0)


def test_background_flow_compensates_roll_zoom_and_pan_for_off_axis_target() -> None:
    """Roll plus yaw/pitch-like pan/scale keeps a detector-gap target on its scene point."""

    import cv2

    width, height = 320, 180
    rotation_deg = 12.0
    camera_scale = 1.06
    translation_x_px = 6.0
    translation_y_px = -4.0
    background = np.random.default_rng(2207).integers(
        0,
        180,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(2208).integers(
        50,
        256,
        size=(42, 48, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[30:72, 42:90] = patch
    transform = cv2.getRotationMatrix2D((width * 0.5, height * 0.5), rotation_deg, camera_scale)
    transform[:, 2] += np.asarray((translation_x_px, translation_y_px))
    second = cv2.warpAffine(
        first,
        transform,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    bbox = BoundingBox(42 / width, 30 / height, 90 / width, 72 / height)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.15,
            reacquisition_timeout_s=0.50,
        )
    )
    initial = pool.update(
        frame_id="roll-zoom-1",
        captured_at_s=1.0,
        observations=(TargetObservation("flame", 0.95, bbox),),
    )
    target_id = initial.tracks[0].track_id
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)

    short_term = tracker.update_frame(second, captured_at_s=1.05)

    assert short_term.camera_motion is not None
    assert short_term.camera_motion_source == "background_affine_flow"
    assert short_term.camera_motion.scale == pytest.approx(camera_scale, abs=0.025)
    assert abs(short_term.camera_motion.rotation_deg) == pytest.approx(rotation_deg, abs=1.2)
    expected_center = short_term.camera_motion.transform_point(*bbox.center)
    update = pool.update(
        frame_id="roll-zoom-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=short_term.camera_motion,
        motion_hints=short_term.hints,
        visual_confirmation_track_ids=(target_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == target_id)
    assert update.visual_confirmed_track_ids == (target_id,)
    assert tracked.state is UnifiedTrackState.TRACKING
    assert tracked.bbox.center[0] * width == pytest.approx(expected_center[0] * width, abs=3.0)
    assert tracked.bbox.center[1] * height == pytest.approx(expected_center[1] * height, abs=3.0)


def test_background_affine_flow_compensates_yaw_pitch_like_shear_for_detector_gap() -> None:
    """A bounded non-uniform affine scene warp remains continuous without a detector box."""

    import cv2

    width, height = 320, 180
    background = np.random.default_rng(2307).integers(
        0,
        180,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(2308).integers(
        50,
        256,
        size=(42, 48, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[30:72, 42:90] = patch
    transform = np.asarray(
        ((1.04, 0.06, 5.0), (-0.03, 0.96, -3.0)),
        dtype=np.float32,
    )
    second = cv2.warpAffine(
        first,
        transform,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    bbox = BoundingBox(42 / width, 30 / height, 90 / width, 72 / height)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.15,
            reacquisition_timeout_s=0.50,
        )
    )
    initial = pool.update(
        frame_id="affine-gap-1",
        captured_at_s=1.0,
        observations=(TargetObservation("flame", 0.95, bbox),),
    )
    target_id = initial.tracks[0].track_id
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)

    short_term = tracker.update_frame(second, captured_at_s=1.05)

    assert short_term.camera_motion is not None
    assert short_term.camera_motion_source == "background_affine_flow"
    assert short_term.camera_motion.affine is not None
    expected_center = short_term.camera_motion.transform_point(*bbox.center)
    update = pool.update(
        frame_id="affine-gap-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=short_term.camera_motion,
        motion_hints=short_term.hints,
        visual_confirmation_track_ids=(target_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == target_id)
    assert update.visual_confirmed_track_ids == (target_id,)
    assert tracked.state is UnifiedTrackState.TRACKING
    assert tracked.bbox.center[0] * width == pytest.approx(expected_center[0] * width, abs=3.0)
    assert tracked.bbox.center[1] * height == pytest.approx(expected_center[1] * height, abs=3.0)


def test_background_homography_compensates_material_perspective_for_detector_gap() -> None:
    """Off-axis yaw/pitch perspective beats a stale external transform when available."""

    import cv2

    width, height = 320, 180
    background = np.random.default_rng(2407).integers(
        0,
        180,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(2408).integers(
        50,
        256,
        size=(42, 48, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[32:74, 38:86] = patch
    source_corners = np.float32(
        ((0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1))
    )
    destination_corners = np.float32(
        ((4, 5), (316, 1), (310, 177), (12, 174))
    )
    transform = cv2.getPerspectiveTransform(source_corners, destination_corners)
    second = cv2.warpPerspective(
        first,
        transform,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    bbox = BoundingBox(38 / width, 32 / height, 86 / width, 74 / height)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.15,
            reacquisition_timeout_s=0.50,
        )
    )
    initial = pool.update(
        frame_id="homography-gap-1",
        captured_at_s=1.0,
        observations=(TargetObservation("flame", 0.95, bbox),),
    )
    target_id = initial.tracks[0].track_id
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)

    short_term = tracker.update_frame(
        second,
        captured_at_s=1.05,
        camera_motion=CameraMotionEstimate(dx=-0.12, dy=0.0, confidence=0.95),
        prefer_background_motion=True,
    )

    assert short_term.camera_motion is not None
    assert short_term.camera_motion_source == "background_homography_flow"
    assert short_term.camera_motion.homography is not None
    assert short_term.camera_motion.dx != pytest.approx(-0.12, abs=0.03)
    assert len(short_term.hints) == 1
    expected_center = short_term.camera_motion.transform_point(*bbox.center)
    update = pool.update(
        frame_id="homography-gap-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=short_term.camera_motion,
        motion_hints=short_term.hints,
        visual_confirmation_track_ids=(target_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == target_id)
    assert update.visual_confirmed_track_ids == (target_id,)
    assert tracked.state is UnifiedTrackState.TRACKING
    assert tracked.bbox.center[0] * width == pytest.approx(expected_center[0] * width, abs=3.0)
    assert tracked.bbox.center[1] * height == pytest.approx(expected_center[1] * height, abs=3.0)


def test_expected_motion_applies_target_velocity_before_local_homography() -> None:
    """Perspective compensation must use the target's local Jacobian, not image-centre scale."""

    bbox = BoundingBox(0.16, 0.22, 0.28, 0.34)
    track = replace(
        _initial_tracks(bbox)[0],
        velocity_x_s=0.40,
        velocity_y_s=-0.10,
    )
    motion = CameraMotionEstimate(
        dx=0.0,
        dy=0.0,
        confidence=0.95,
        homography=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.18, -0.04, 1.0),
    )
    interval_s = 0.10
    center_x, center_y = bbox.center
    predicted_x = center_x + track.velocity_x_s * interval_s
    predicted_y = center_y + track.velocity_y_s * interval_s
    transformed_x, transformed_y = motion.transform_point(predicted_x, predicted_y)

    expected_dx, expected_dy, expected_scale = OpenCVShortTermTargetTracker._expected_motion(
        track,
        interval_s,
        motion,
    )

    assert expected_dx == pytest.approx(transformed_x - center_x)
    assert expected_dy == pytest.approx(transformed_y - center_y)
    assert expected_scale == pytest.approx(motion.local_scale_at(predicted_x, predicted_y))
    assert expected_scale != pytest.approx(motion.effective_scale, abs=0.01)


def test_template_correlation_is_conservative_fallback_when_flow_is_sparse() -> None:
    first, bbox = _textured_frame(left=100, top=70, size=28, seed=7)
    second, _ = _textured_frame(left=109, top=70, size=28, seed=7)
    tracker = OpenCVShortTermTargetTracker(
        ShortTermTrackingConfig(
            maximum_features_per_track=48,
            minimum_flow_points=40,
            flow_minimum_distance_px=12.0,
            minimum_box_size_px=12,
        )
    )
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))

    result = tracker.update_frame(second, captured_at_s=1.05)

    assert result.status is ShortTermTrackingStatus.OK
    assert result.optical_flow_hint_count == 0
    assert result.template_hint_count == 1
    assert result.hints[0].source == "template_correlation"
    assert 0.015 <= result.hints[0].residual_dx <= 0.045
    assert result.hints[0].confidence >= 0.55


def test_camera_warped_template_fallback_handles_roll_zoom_when_local_flow_is_sparse() -> None:
    """The template fallback should retain a target through camera rotation and zoom."""

    import cv2

    width, height = 320, 180
    background = np.random.default_rng(3401).integers(
        0,
        180,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(3402).integers(
        30,
        256,
        size=(42, 42, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[55:97, 110:152] = patch
    pixel_transform = cv2.getRotationMatrix2D((width * 0.5, height * 0.5), 14.0, 1.12)
    pixel_transform[:, 2] += np.asarray((5.0, -3.0))
    pixel_homography = np.vstack((pixel_transform, (0.0, 0.0, 1.0))).astype(np.float64)
    second = cv2.warpPerspective(
        first,
        pixel_homography,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    pixel_to_normalized = np.diag((1.0 / width, 1.0 / height, 1.0))
    normalized_to_pixel = np.diag((float(width), float(height), 1.0))
    normalized_homography = pixel_to_normalized @ pixel_homography @ normalized_to_pixel
    normalized_homography /= normalized_homography[2, 2]
    transformed_center = normalized_homography @ np.asarray((0.5, 0.5, 1.0))
    transformed_center /= transformed_center[2]
    motion = CameraMotionEstimate(
        dx=float(transformed_center[0] - 0.5),
        dy=float(transformed_center[1] - 0.5),
        scale=1.12,
        confidence=0.95,
        homography=tuple(float(value) for value in normalized_homography.ravel()),
    )
    bbox = BoundingBox(110 / width, 55 / height, 152 / width, 97 / height)
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    initial = pool.update(
        frame_id="camera-template-1",
        captured_at_s=1.0,
        observations=(TargetObservation("flame", 0.95, bbox),),
    )
    target_id = initial.tracks[0].track_id
    tracker = OpenCVShortTermTargetTracker(
        ShortTermTrackingConfig(
            maximum_features_per_track=8,
            minimum_flow_points=8,
            flow_minimum_distance_px=20.0,
            template_minimum_correlation=0.65,
            template_minimum_peak_margin=0.02,
        )
    )
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)

    short_term = tracker.update_frame(
        second,
        captured_at_s=1.05,
        camera_motion=motion,
    )

    assert short_term.optical_flow_hint_count == 0
    assert short_term.template_hint_count == 1
    assert short_term.hints[0].source == "camera_warped_template_correlation"
    assert abs(short_term.hints[0].residual_dx) <= 0.01
    assert abs(short_term.hints[0].residual_dy) <= 0.01
    expected_center = motion.transform_point(*bbox.center)
    update = pool.update(
        frame_id="camera-template-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=motion,
        motion_hints=short_term.hints,
        visual_confirmation_track_ids=(target_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == target_id)
    assert update.visual_confirmed_track_ids == (target_id,)
    assert tracked.state is UnifiedTrackState.TRACKING
    assert tracked.bbox.center[0] * width == pytest.approx(expected_center[0] * width, abs=3.0)
    assert tracked.bbox.center[1] * height == pytest.approx(expected_center[1] * height, abs=3.0)


def test_camera_warped_retained_template_recovers_after_occlusion_and_two_attitude_steps() -> None:
    """An older flame crop must survive a measured roll/zoom while it is hidden."""

    import cv2

    width, height = 320, 180
    background = np.random.default_rng(3401).integers(
        0,
        180,
        size=(height, width, 3),
        dtype=np.uint8,
    )
    patch = np.random.default_rng(3402).integers(
        30,
        256,
        size=(42, 42, 3),
        dtype=np.uint8,
    )
    first = background.copy()
    first[55:97, 110:152] = patch

    def _motion(angle_deg: float, scale: float, dx_px: float, dy_px: float):
        affine = cv2.getRotationMatrix2D((width * 0.5, height * 0.5), angle_deg, scale)
        affine[:, 2] += np.asarray((dx_px, dy_px))
        pixel_homography = np.vstack((affine, (0.0, 0.0, 1.0))).astype(np.float64)
        pixel_to_normalized = np.diag((1.0 / width, 1.0 / height, 1.0))
        normalized_to_pixel = np.diag((float(width), float(height), 1.0))
        normalized_homography = (
            pixel_to_normalized @ pixel_homography @ normalized_to_pixel
        )
        normalized_homography /= normalized_homography[2, 2]
        transformed_center = normalized_homography @ np.asarray((0.5, 0.5, 1.0))
        transformed_center /= transformed_center[2]
        return (
            pixel_homography,
            CameraMotionEstimate(
                dx=float(transformed_center[0] - 0.5),
                dy=float(transformed_center[1] - 0.5),
                scale=scale,
                confidence=0.95,
                homography=tuple(float(value) for value in normalized_homography.ravel()),
            ),
        )

    first_step, first_motion = _motion(7.0, 1.04, 3.0, -2.0)
    second_step, second_motion = _motion(7.0, 1.04, 3.0, -2.0)
    occluded = cv2.warpPerspective(
        background,
        first_step,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    reappeared = cv2.warpPerspective(
        first,
        second_step @ first_step,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    bbox = BoundingBox(110 / width, 55 / height, 152 / width, 97 / height)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.2,
            reacquisition_timeout_s=1.0,
        )
    )
    initial = pool.update(
        frame_id="retained-camera-1",
        captured_at_s=1.0,
        observations=(TargetObservation("flame", 0.95, bbox),),
    )
    target_id = initial.tracks[0].track_id
    tracker = OpenCVShortTermTargetTracker(
        ShortTermTrackingConfig(
            maximum_features_per_track=8,
            minimum_flow_points=8,
            flow_minimum_distance_px=20.0,
            template_minimum_correlation=0.65,
            template_minimum_peak_margin=0.02,
        )
    )
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)

    hidden = tracker.update_frame(
        occluded,
        captured_at_s=1.4,
        camera_motion=first_motion,
    )
    reacquiring = pool.update(
        frame_id="retained-camera-2",
        captured_at_s=1.4,
        observations=(),
        camera_motion=first_motion,
        motion_hints=hidden.hints,
        visual_confirmation_track_ids=tuple(hint.track_id for hint in hidden.hints),
    )
    assert reacquiring.tracks[0].state is UnifiedTrackState.REACQUIRING
    tracker.synchronize_tracks(reacquiring.tracks)

    recovered_hint = tracker.update_frame(
        reappeared,
        captured_at_s=1.45,
        camera_motion=second_motion,
    )

    assert recovered_hint.status is ShortTermTrackingStatus.OK
    assert recovered_hint.optical_flow_hint_count == 0
    assert recovered_hint.template_hint_count == 1
    assert recovered_hint.hints[0].source == "camera_warped_retained_template_correlation"
    assert abs(recovered_hint.hints[0].residual_dx) <= 0.01
    assert abs(recovered_hint.hints[0].residual_dy) <= 0.01
    recovered = pool.update(
        frame_id="retained-camera-3",
        captured_at_s=1.45,
        observations=(),
        camera_motion=second_motion,
        motion_hints=recovered_hint.hints,
        visual_confirmation_track_ids=(target_id,),
    )

    assert recovered.visual_confirmed_track_ids == (target_id,)
    assert recovered.tracks[0].state is UnifiedTrackState.RECOVERED


def _run_retained_template_reacquisition(*, maximum_template_age_s: float):
    first, bbox = _textured_frame(left=100, top=70, size=28, seed=71)
    occluded = np.zeros_like(first)
    reappeared, reappeared_bbox = _textured_frame(left=140, top=70, size=28, seed=71)
    pool = UnifiedTargetPool()
    initial = pool.update(
        frame_id="pool-1",
        captured_at_s=1.0,
        observations=(TargetObservation("vehicle", 0.95, bbox),),
    )
    tracker = OpenCVShortTermTargetTracker(
        ShortTermTrackingConfig(
            maximum_features_per_track=48,
            minimum_flow_points=40,
            flow_minimum_distance_px=20.0,
            search_expansion=2.5,
            reacquiring_search_multiplier=2.0,
            maximum_search_expansion=6.0,
            maximum_retained_template_age_s=maximum_template_age_s,
        )
    )
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)
    tracker.update_frame(occluded, captured_at_s=1.4)
    reacquiring = pool.update(
        frame_id="pool-2",
        captured_at_s=1.4,
        observations=(),
    )
    assert reacquiring.tracks[0].state is UnifiedTrackState.REACQUIRING
    tracker.synchronize_tracks(reacquiring.tracks)
    return (
        tracker.update_frame(reappeared, captured_at_s=1.45),
        pool,
        reappeared_bbox,
        initial.tracks[0].track_id,
    )


def test_reacquiring_track_uses_last_reliable_template_and_expanded_search() -> None:
    result, pool, reappeared_bbox, original_track_id = _run_retained_template_reacquisition(
        maximum_template_age_s=2.0
    )

    assert result.status is ShortTermTrackingStatus.OK
    assert result.optical_flow_hint_count == 0
    assert result.template_hint_count == 1
    assert result.hints[0].source == "retained_template_correlation"
    assert 0.10 <= result.hints[0].residual_dx <= 0.15

    recovered = pool.update(
        frame_id="pool-3",
        captured_at_s=1.45,
        observations=(TargetObservation("vehicle", 0.95, reappeared_bbox),),
        motion_hints=result.hints,
    )
    assert recovered.recovered_track_ids == (original_track_id,)
    assert recovered.created_track_ids == ()
    assert recovered.tracks[0].state is UnifiedTrackState.RECOVERED


def test_stale_retained_template_does_not_force_reacquisition() -> None:
    result, _pool, _bbox, _track_id = _run_retained_template_reacquisition(
        maximum_template_age_s=0.2
    )

    assert result.status is ShortTermTrackingStatus.DEGRADED
    assert result.hints == ()


def test_lost_track_is_not_blindly_followed_by_short_term_template() -> None:
    first, bbox = _textured_frame(left=80, top=60, size=64)
    second, _ = _textured_frame(left=90, top=60, size=64)
    lost_tracks = tuple(
        replace(track, state=UnifiedTrackState.LOST) for track in _initial_tracks(bbox)
    )
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(lost_tracks)

    result = tracker.update_frame(second, captured_at_s=1.05)

    assert result.status is ShortTermTrackingStatus.WARMUP
    assert result.reason == "NO_TRACKS"
    assert result.attempted_track_count == 0
    assert result.hints == ()


@pytest.mark.parametrize(
    "changes",
    (
        {"occluded_search_multiplier": 0.9},
        {"reacquiring_search_multiplier": float("nan")},
        {"maximum_search_expansion": 2.0},
        {"maximum_retained_template_age_s": 0.0},
    ),
)
def test_adaptive_reacquisition_config_is_strict(changes: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ShortTermTrackingConfig(**changes)


def test_frame_gap_invalidates_short_term_hint_instead_of_extrapolating() -> None:
    first, bbox = _textured_frame(left=80, top=60, size=64)
    second, _ = _textured_frame(left=92, top=60, size=64)
    tracker = OpenCVShortTermTargetTracker(ShortTermTrackingConfig(maximum_frame_interval_s=0.2))
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))

    result = tracker.update_frame(second, captured_at_s=1.5)

    assert result.status is ShortTermTrackingStatus.INVALID
    assert result.reason == "FRAME_GAP"
    assert result.hints == ()


def test_frame_stride_skips_expensive_flow_without_stale_prediction_hint() -> None:
    first, bbox = _textured_frame(left=80, top=60, size=64)
    second, _ = _textured_frame(left=88, top=60, size=64)
    third, _ = _textured_frame(left=96, top=60, size=64)
    tracker = OpenCVShortTermTargetTracker(ShortTermTrackingConfig(frame_stride=2))
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(_initial_tracks(bbox))
    processed = tracker.update_frame(second, captured_at_s=1.05)
    skipped = tracker.update_frame(third, captured_at_s=1.10)

    assert processed.status is ShortTermTrackingStatus.OK
    assert processed.hints
    assert skipped.status is ShortTermTrackingStatus.SKIPPED
    assert skipped.reason == "FRAME_STRIDE"
    assert skipped.hints == ()


def test_exclusive_lck_overrides_stride_and_samples_only_one_target() -> None:
    first, bbox = _textured_frame(left=80, top=60, size=64)
    second, _ = _textured_frame(left=88, top=60, size=64)
    tracks = _initial_tracks(bbox)
    other = replace(
        tracks[0],
        track_id="track-other",
        bbox=BoundingBox(0.65, 0.25, 0.85, 0.60),
    )
    tracker = OpenCVShortTermTargetTracker(ShortTermTrackingConfig(frame_stride=3))
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks((tracks[0], other))

    result = tracker.update_frame(
        second,
        captured_at_s=1.05,
        exclusive_track_id=tracks[0].track_id,
    )

    assert result.status is not ShortTermTrackingStatus.SKIPPED
    assert result.attempted_track_count == 1
    assert all(hint.track_id == tracks[0].track_id for hint in result.hints)


def test_ten_targets_share_one_target_and_one_background_flow_batch(monkeypatch) -> None:
    import cv2

    rng = np.random.default_rng(99)
    first = rng.integers(0, 256, size=(360, 640, 3), dtype=np.uint8)
    second = np.roll(first, shift=3, axis=1)
    pool = UnifiedTargetPool()
    observations = tuple(
        TargetObservation(
            label="vehicle",
            confidence=0.9,
            bbox=BoundingBox(
                0.04 + index * 0.09,
                0.25,
                0.11 + index * 0.09,
                0.45,
            ),
        )
        for index in range(10)
    )
    tracks = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=observations,
    ).tracks
    calls = 0
    original = cv2.calcOpticalFlowPyrLK

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cv2, "calcOpticalFlowPyrLK", counted)
    tracker = OpenCVShortTermTargetTracker()
    tracker.update_frame(first, captured_at_s=1.0)
    tracker.synchronize_tracks(tracks)
    result = tracker.update_frame(second, captured_at_s=1.05)

    assert calls == 4
    assert result.optical_flow_hint_count == 10


def test_two_manual_tracks_follow_opposite_motion_end_to_end() -> None:
    width, height, size = 400, 240, 52
    first_patch = np.random.default_rng(11).integers(0, 256, size=(size, size, 3), dtype=np.uint8)
    second_patch = np.random.default_rng(22).integers(0, 256, size=(size, size, 3), dtype=np.uint8)

    def moving_frame(step: int) -> np.ndarray:
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        first_x = 45 + 4 * step
        second_x = 295 - 3 * step
        frame[45 : 45 + size, first_x : first_x + size] = first_patch
        frame[135 : 135 + size, second_x : second_x + size] = second_patch
        return frame

    def bbox(left: int, top: int) -> BoundingBox:
        return BoundingBox(
            left / width,
            top / height,
            (left + size) / width,
            (top + size) / height,
        )

    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.08,
            reacquisition_timeout_s=0.35,
            lost_retention_s=2.0,
            locked_lost_retention_s=3.0,
        )
    )
    initial = pool.update(
        frame_id="manual-flow-0",
        captured_at_s=1.0,
        observations=(
            TargetObservation("manual", 0.90, bbox(45, 45)),
            TargetObservation("manual", 0.90, bbox(295, 135)),
        ),
    )
    track_ids = tuple(track.track_id for track in initial.tracks)
    tracker = OpenCVShortTermTargetTracker(ShortTermTrackingConfig(frame_stride=1))
    tracker.update_frame(moving_frame(0), captured_at_s=1.0)
    tracker.synchronize_tracks(initial.tracks)

    for step in range(1, 17):
        captured_at_s = 1.0 + step * 0.05
        short_term = tracker.update_frame(
            moving_frame(step),
            captured_at_s=captured_at_s,
        )
        assert {hint.track_id for hint in short_term.hints} == set(track_ids)
        update = pool.update(
            frame_id=f"manual-flow-{step}",
            captured_at_s=captured_at_s,
            observations=(),
            camera_motion=short_term.camera_motion,
            motion_hints=short_term.hints,
            visual_confirmation_track_ids=track_ids,
        )
        assert update.visual_confirmed_track_ids == track_ids
        tracker.synchronize_tracks(update.tracks)

    by_id = {track.track_id: track for track in update.tracks}
    expected_first_center_x = 45 + 4 * 16 + size / 2
    expected_second_center_x = 295 - 3 * 16 + size / 2
    assert by_id[track_ids[0]].bbox.center[0] * width == pytest.approx(
        expected_first_center_x, abs=2.0
    )
    assert by_id[track_ids[1]].bbox.center[0] * width == pytest.approx(
        expected_second_center_x, abs=2.0
    )
    assert by_id[track_ids[0]].velocity_x_s > 0.0
    assert by_id[track_ids[1]].velocity_x_s < 0.0
    assert all(track.state is UnifiedTrackState.TRACKING for track in by_id.values())
