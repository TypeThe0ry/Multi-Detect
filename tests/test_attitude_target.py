import math

import pytest

from multidetect.simulation.attitude_target import decode_attitude_target


def decode(q, mask=7, tolerance=1e-5):
    return decode_attitude_target(dict(q=q, type_mask=mask), quaternion_norm_tolerance=tolerance)


@pytest.mark.parametrize("roll", [-170, -50, 0, 50, 170])
def test_known_roll_and_sign_equivalence(roll):
    half = math.radians(roll) / 2
    q = [math.cos(half), math.sin(half), 0, 0]
    assert decode(q)["roll_deg"] == pytest.approx(roll)
    assert decode([-v for v in q])["roll_deg"] == pytest.approx(roll)


def test_mask_ignored_attitude_does_not_require_quaternion():
    assert decode(None, 128) == dict(attitude_used=False, reason="attitude_ignored_by_mask")
    assert decode([1, 0, 0, 0], 7)["attitude_used"]  # ignored rates don't invalidate attitude


def test_normalization_is_bounded_and_disclosed():
    result = decode([1.000001, 0, 0, 0])
    assert result["numerical_normalization_applied"]
    assert result["original_quaternion_norm"] == 1.000001
    with pytest.raises(ValueError):
        decode([2, 0, 0, 0])


@pytest.mark.parametrize(
    "q", [[0, 0, 0, 0], [math.nan, 0, 0, 0], [True, 0, 0, 0], [1, 0, 0], None, [1e308] * 4]
)
def test_invalid_quaternion(q):
    with pytest.raises(ValueError):
        decode(q)


@pytest.mark.parametrize("mask", [True, -1, 256, None])
def test_invalid_mask(mask):
    with pytest.raises(ValueError):
        decode([1, 0, 0, 0], mask)


@pytest.mark.parametrize("tolerance", [-1, 1, True, math.inf, math.nan])
def test_invalid_tolerance(tolerance):
    with pytest.raises(ValueError):
        decode([1, 0, 0, 0], tolerance=tolerance)


def test_pitch_singularity_does_not_invent_roll():
    result = decode([math.sqrt(0.5), 0, math.sqrt(0.5), 0])
    assert result["euler_roll_yaw_ambiguous"]
    assert result["roll_deg"] is None and result["yaw_deg"] is None
    assert result["pitch_deg"] == pytest.approx(90)
