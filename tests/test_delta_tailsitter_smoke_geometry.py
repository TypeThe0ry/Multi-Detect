import math

import pytest

from multidetect.simulator.delta_tailsitter_smoke import tailsitter_flight_angles_deg


def _axis_quaternion(axis, angle_deg):
    angle = math.radians(angle_deg) / 2
    scale = math.sin(angle)
    return (math.cos(angle), *(component * scale for component in axis))


def _multiply(left, right):
    w1, x1, y1, z1 = left
    w2, x2, y2, z2 = right
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def test_tailsitter_flight_angles_use_body_axes_at_euler_singularity():
    level_fw = _axis_quaternion((0, 1, 0), -90)
    pitch, bank = tailsitter_flight_angles_deg(level_fw)
    assert pitch == pytest.approx(0, abs=1e-8)
    assert bank == pytest.approx(0, abs=1e-8)

    banked_fw = _multiply(level_fw, _axis_quaternion((0, 0, 1), -30))
    pitch, bank = tailsitter_flight_angles_deg(banked_fw)
    assert pitch == pytest.approx(0, abs=1e-8)
    assert bank == pytest.approx(30, abs=1e-8)


@pytest.mark.parametrize(
    ("body_pitch_deg", "expected_pitch_deg"),
    [(-80, 10), (-105, -15)],
)
def test_tailsitter_nose_climb_angle_is_signed(body_pitch_deg, expected_pitch_deg):
    q = _axis_quaternion((0, 1, 0), body_pitch_deg)
    assert tailsitter_flight_angles_deg(q)[0] == pytest.approx(expected_pitch_deg, abs=1e-8)


@pytest.mark.parametrize("quaternion", [(0, 0, 0, 0), (math.nan, 0, 0, 1), (1, 0, 0)])
def test_tailsitter_flight_angles_reject_invalid_quaternions(quaternion):
    with pytest.raises(ValueError):
        tailsitter_flight_angles_deg(quaternion)
