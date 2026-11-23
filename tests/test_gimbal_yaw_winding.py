"""Python reference of GZGimbal.cpp nearestTurnWithin (gz-gimbal-horizon-stabilization.patch).

Mirrors the C++ helper so the winding rule can be checked without a PX4 build.
"""

import math

TURN = 2.0 * math.pi


def nearest_turn(angle, previous):
    return previous + math.remainder(angle - previous, TURN)


def nearest_turn_within(angle, previous, lo, hi):
    y = nearest_turn(angle, previous)
    if hi - lo >= TURN:
        while y > hi:
            y -= TURN
        while y < lo:
            y += TURN
    return y


LO, HI = math.radians(-360.0), math.radians(360.0)


def test_full_turn_accumulated_selects_in_range_winding():
    target = math.radians(-103.0)
    assert nearest_turn(target, -TURN) < LO  # legacy: -463 deg, clipped to the stop
    assert math.isclose(nearest_turn_within(target, -TURN, LO, HI), target, abs_tol=1e-6)


def test_positive_stop_symmetric():
    target = math.radians(103.0)
    assert math.isclose(nearest_turn_within(target, TURN, LO, HI), target, abs_tol=1e-6)


def test_previous_inside_range_is_unchanged():
    previous = math.radians(-200.0)
    target = math.radians(-170.0)
    assert nearest_turn_within(target, previous, LO, HI) == nearest_turn(target, previous)
    previous = math.radians(10.0)
    target = math.radians(350.0)
    assert math.isclose(nearest_turn_within(target, previous, LO, HI), math.radians(-10.0), abs_tol=1e-6)


def test_narrow_range_keeps_legacy_behavior():
    lo, hi = math.radians(-90.0), math.radians(90.0)
    previous = math.radians(80.0)
    target = math.radians(-100.0)
    assert nearest_turn_within(target, previous, lo, hi) == nearest_turn(target, previous)
