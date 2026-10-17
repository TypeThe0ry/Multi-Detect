import math

from scripts.evaluate_onboard_ranging_reuse import iou, stats, triangulate


def test_stats_empty_is_explicit():
    result = stats([])
    assert result["count"] == 0
    assert result["p95_m"] is None


def test_stats_and_iou_are_deterministic():
    result = stats([1, 2, 3, 4])
    assert result["count"] == 4
    assert result["median_m"] == 2.5
    assert math.isclose(result["rmse_m"], math.sqrt(7.5))
    assert iou([0, 0, 2, 2], [1, 1, 3, 3]) == 1 / 7


def test_triangulate_reconstructs_point_from_three_rays():
    point = (4.0, 1.5, 2.0)
    cameras = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    samples = []
    for camera in cameras:
        ray = tuple(point[i] - camera[i] for i in range(3))
        norm = math.sqrt(sum(value * value for value in ray))
        samples.append((camera, tuple(value / norm for value in ray)))
    estimate = triangulate(samples)
    assert estimate is not None
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(estimate, point, strict=False))


def test_triangulate_rejects_degenerate_single_direction():
    assert triangulate([((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))] * 3) is None
