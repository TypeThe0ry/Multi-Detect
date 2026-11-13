import math

from multidetect.simulator.fixed_wing_reposition import reposition_ring_metrics


TARGET = (12.0, -7.0, -40.0)
RADIUS = 80.0


def _circle(*, duration_s: float, radius_m: float = RADIUS, rate_deg_s: float = 18.0):
    rows = []
    count = int(duration_s * 4) + 1
    for index in range(count):
        time_s = index / 4
        angle = math.radians(time_s * rate_deg_s)
        rows.append({
            "sim_time_s": time_s,
            "x": TARGET[0] + radius_m * math.cos(angle),
            "y": TARGET[1] + radius_m * math.sin(angle),
            "fw_active": True,
        })
    return rows


def test_stable_target_loiter_ring_passes():
    result = reposition_ring_metrics(
        _circle(duration_s=35.0), target_ned=TARGET, radius_m=RADIUS
    )

    assert result["fw_reposition_success"] is True
    assert result["fw_reposition_ring_error_p95_m"] < 1e-9
    assert result["fw_reposition_dwell_s"] >= 30.0
    assert result["fw_reposition_swept_deg"] >= 360.0


def test_ring_with_centre_offset_by_thirty_metres_fails():
    result = reposition_ring_metrics(
        _circle(duration_s=40.0, radius_m=RADIUS + 30.0),
        target_ned=TARGET,
        radius_m=RADIUS,
    )

    assert result["fw_reposition_success"] is False
    assert result["fw_reposition_dwell_s"] == 0.0


def test_single_target_flyby_does_not_count_as_ring_capture():
    rows = [
        {
            "sim_time_s": index / 4,
            "x": TARGET[0] + 15.0 * (index / 4 - 6.5),
            "y": TARGET[1],
            "fw_active": True,
        }
        for index in range(53)
    ]
    result = reposition_ring_metrics(rows, target_ned=TARGET, radius_m=RADIUS)

    assert result["fw_reposition_success"] is False
    assert result["fw_reposition_dwell_s"] < 30.0


def test_brief_ring_capture_does_not_count_even_after_a_full_sweep():
    result = reposition_ring_metrics(
        _circle(duration_s=10.0, rate_deg_s=45.0),
        target_ned=TARGET,
        radius_m=RADIUS,
    )

    assert result["fw_reposition_success"] is False
    assert result["fw_reposition_dwell_s"] < 30.0
    assert result["fw_reposition_swept_deg"] >= 360.0
