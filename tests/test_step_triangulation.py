"""Geometry tests use synthetic measured rays, never simulator truth admission."""

import math

import pytest

from multidetect.autonomy.step_triangulation import (
    PositionEstimate,
    RangeMeasurement,
    ViewObservation,
    fuse_position_estimates,
    make_ground_estimate,
    position_from_range,
    triangulate_views,
)


def view(camera, target=(20.0, 3.0, 0.0), step=0, timestamp=None):
    offset = tuple(target[i] - camera[i] for i in range(3))
    norm = math.hypot(*offset)
    return ViewObservation(
        camera,
        tuple(x / norm for x in offset),
        step,
        float(step if timestamp is None else timestamp),
    )


def test_intersection_and_offline_sigma_propagation():
    observations = [view((0.0, 0.0, -10.0)), view((0.0, 2.0, -10.0), step=1)]
    estimate = triangulate_views(observations, sigma_scale=2.0)
    assert estimate is not None
    assert estimate.position_ned == pytest.approx((20.0, 3.0, 0.0), abs=1e-10)
    assert estimate.source == "triangulation"
    assert estimate.view_count == 2
    assert estimate.baseline_m == 2
    expected = 2 * max(
        0.5,
        estimate.residual_m
        + 0.003
        * math.dist(estimate.position_ned, observations[-1].camera_ned)
        / max(math.sin(math.radians(estimate.ray_angle_deg)), 0.05),
    )
    assert estimate.sigma_m == pytest.approx(expected)
    assert estimate.timestamp_s == 1.0


def test_requires_distinct_steps_and_translation():
    assert triangulate_views([]) is None
    assert triangulate_views([view((0.0, 0.0, -10.0))]) is None
    assert triangulate_views([view((0.0, 0.0, -10.0)), view((0.0, 2.0, -10.0))]) is None
    assert triangulate_views([view((0.0, 0.0, -10.0)), view((0.0, 0.0, -10.0), step=1)]) is None
    assert triangulate_views([view((0.0, 0.0, -10.0)), view((0.0, 0.1, -10.0), step=1)]) is None


def test_minimum_angle_and_parallel_degeneracy():
    far = (10000.0, 0.0, 0.0)
    assert (
        triangulate_views([view((0.0, 0.0, -10.0), far), view((0.0, 2.0, -10.0), far, 1)]) is None
    )
    assert (
        triangulate_views(
            [ViewObservation((0.0, float(i), 0.0), (1.0, 0.0, 0.0), i, float(i)) for i in range(2)]
        )
        is None
    )


def test_behind_camera_and_large_residual_rejected():
    a, b = view((0.0, 0.0, -10.0)), view((0.0, 2.0, -10.0), step=1)
    backwards = ViewObservation(
        b.camera_ned, tuple(-x for x in b.direction_ned), b.step, b.timestamp_s
    )
    assert triangulate_views([a, backwards]) is None
    skew = [
        ViewObservation((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), 0, 0.0),
        ViewObservation((10.0, -10.0, 20.0), (0.0, 1.0, 0.0), 1, 1.0),
    ]
    assert triangulate_views(skew) is None
    assert triangulate_views(skew, max_residual_m=11) is not None


def test_latest_window_and_scale():
    observations = [view((0.0, float(i), -10.0), step=i) for i in range(12)]
    estimate = triangulate_views(observations, max_views=8, sigma_scale=3)
    base = triangulate_views(observations[-8:], sigma_scale=1)
    assert estimate is not None and base is not None
    assert estimate.view_count == 8
    assert estimate.baseline_m == 7
    assert estimate.position_ned == base.position_ned
    assert estimate.sigma_m == pytest.approx(3 * base.sigma_m)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_invalid_numbers_fail_closed(bad):
    a = view((0.0, 0.0, -10.0))
    b = ViewObservation((0.0, bad, -10.0), (1.0, 0.0, 0.0), 1, 1.0)
    assert triangulate_views([a, b]) is None
    assert triangulate_views([a, view((0.0, 2.0, -10.0), step=1)], sigma_scale=bad) is None
    assert (
        make_ground_estimate(
            (1.0, 2.0, bad),
            camera_ned=a.camera_ned,
            direction_ned=a.direction_ned,
            ground_height_sigma_m=1,
            timestamp_s=0,
        )
        is None
    )


def test_nonunit_and_overflow_fail_closed():
    assert (
        triangulate_views(
            [view((0.0, 0.0, -10.0)), ViewObservation((0.0, 2.0, -10.0), (2.0, 0.0, 0.0), 1, 1.0)]
        )
        is None
    )
    huge = [
        ViewObservation(
            (1e308, float(i), 1e308), (math.sqrt(0.5), 0.0, math.sqrt(0.5)), i, float(i)
        )
        for i in range(2)
    ]
    assert triangulate_views(huge) is None
    assert triangulate_views([ViewObservation((10**1000, 0, 0), (1, 0, 0), 0, 0.0)]) is None


@pytest.mark.parametrize(
    "options",
    [
        {"max_views": True},
        {"max_views": 1},
        {"sigma_scale": 0},
        {"min_baseline_m": -1},
        {"max_residual_m": math.inf},
    ],
)
def test_invalid_quality_configuration_fails_closed(options):
    assert (
        triangulate_views([view((0.0, 0.0, -10.0)), view((0.0, 2.0, -10.0), step=1)], **options)
        is None
    )


def test_ground_height_uncertainty_and_range_extension():
    direction = (math.sqrt(0.75), 0.0, 0.5)
    ground = make_ground_estimate(
        (10.0, 0.0, 0.0),
        camera_ned=(0.0, 0.0, -10.0),
        direction_ned=direction,
        ground_height_sigma_m=3,
        timestamp_s=4,
    )
    assert ground is not None and ground.sigma_m == 6
    direct = position_from_range(
        RangeMeasurement(20, 2, 5), camera_ned=(0.0, 0.0, -10.0), direction_ned=direction
    )
    assert direct is not None
    assert direct.position_ned == pytest.approx((math.sqrt(300), 0, 0))
    assert direct.source == "depth" and direct.sigma_m == 2 and direct.timestamp_s == 5
    assert (
        position_from_range(
            RangeMeasurement(-1, 2, 5), camera_ned=(0, 0, 0), direction_ned=direction
        )
        is None
    )
    assert (
        make_ground_estimate(
            (1, 2, 0),
            camera_ned=(0, 0, -10),
            direction_ned=(1, 0, 0),
            ground_height_sigma_m=1,
            timestamp_s=0,
        )
        is None
    )


def test_variance_weighting_and_missing_triangulation_exact_fallback():
    ground = PositionEstimate((10.0, 0.0, 0.0), 2.0, 1.0, "ground")
    tri = PositionEstimate(
        (12.0, 0.0, 0.0), 1.0, 1.0, "triangulation", view_count=4, baseline_m=3, ray_angle_deg=4
    )
    assert fuse_position_estimates(ground, None) is ground
    fused = fuse_position_estimates(ground, tri)
    assert fused is not None
    assert fused.source == "fused" and fused.consistent
    assert fused.position_ned == pytest.approx((11.6, 0, 0))
    assert fused.sigma_m == pytest.approx(math.sqrt(0.8))
    assert fused.view_count == 4 and fused.baseline_m == 3


def test_inconsistency_prefers_ground_anchor_and_marks():
    ground = PositionEstimate((0.0, 0.0, 0.0), 2.0, 5.0, "ground")
    tri = PositionEstimate((30.0, 0.0, 0.0), 1.0, 4.0, "triangulation", view_count=4)
    result = fuse_position_estimates(ground, tri)
    assert result is not None and result.source == "ground"
    assert result.position_ned == ground.position_ned
    assert result.sigma_m >= 30 and not result.consistent
    assert result.consistency_note and result.view_count == 4
    tie = fuse_position_estimates(
        ground, PositionEstimate(tri.position_ned, 3.0, 5.0, "triangulation")
    )
    assert tie is not None and tie.source == "ground"


def test_ground_anchor_gate_rejects_small_sigma_triangulation_drift():
    ground = PositionEstimate((10.0, 20.0, 0.0), 1.0, 1.0, "ground")
    tri = PositionEstimate((10.15, 20.0, 1.0), 0.2, 1.0, "triangulation", view_count=4)
    result = fuse_position_estimates(ground, tri, ground_anchor_max_error_m=0.1)
    assert result is not None
    assert result.source == "ground"
    assert result.position_ned == ground.position_ned
    assert result.consistency_note == "ground_anchor_disagreement:fallback_ground"


def test_fusion_handles_degenerate_inputs_and_tiny_variances():
    assert fuse_position_estimates(None) is None
    bad = PositionEstimate((math.nan, 0.0, 0.0), 1.0, 0.0, "ground")
    assert fuse_position_estimates(bad) is None
    tiny = PositionEstimate((1.0, 0.0, 0.0), 1e-250, 0.0, "ground")
    fused = fuse_position_estimates(tiny, tiny)
    assert fused is not None and math.isfinite(fused.sigma_m) and fused.sigma_m > 0
