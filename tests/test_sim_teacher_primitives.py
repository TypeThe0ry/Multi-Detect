from __future__ import annotations

import json
import math

import pytest

from multidetect.sim_teacher.primitives import (
    ActionPrimitive,
    CompletionCriterion,
    body_to_ned,
    primitive_to_ned,
)


def test_primitive_json_round_trip() -> None:
    actions = [
        ActionPrimitive.translate(3.5, -2.0, 1.25),
        ActionPrimitive.turn(-45.0),
        ActionPrimitive.hold(1.5),
        ActionPrimitive.stop(),
    ]
    for action in actions:
        encoded = json.dumps(action.to_dict())
        decoded = ActionPrimitive.from_dict(json.loads(encoded))
        assert decoded == action


def test_body_to_ned_cardinal_and_yaw() -> None:
    assert body_to_ned(1.0, 0.0, 2.0, 0.0) == pytest.approx((1.0, 0.0, -2.0))
    assert body_to_ned(1.0, 0.0, 0.0, 90.0) == pytest.approx((0.0, 1.0, 0.0), abs=1e-8)
    assert body_to_ned(0.0, 1.0, 0.0, 90.0) == pytest.approx((-1.0, 0.0, 0.0), abs=1e-8)
    action = ActionPrimitive.translate(2.0, 1.0, 0.5)
    n, e, d, yaw_delta = primitive_to_ned(action, 30.0)
    assert (n, e, d) == pytest.approx(body_to_ned(2.0, 1.0, 0.5, 30.0))
    assert yaw_delta == 0.0


def test_completion_criterion_success_pending_timeout() -> None:
    criterion = CompletionCriterion(
        position_tolerance_m=0.25,
        yaw_tolerance_deg=5.0,
        speed_tolerance_mps=0.2,
        timeout_s=2.0,
    )
    assert (
        criterion(position_error_m=0.1, yaw_error_deg=2.0, speed_mps=0.1, elapsed_s=1.0)
        == "success"
    )
    assert (
        criterion(position_error_m=0.3, yaw_error_deg=2.0, speed_mps=0.1, elapsed_s=1.0)
        == "pending"
    )
    assert (
        criterion(position_error_m=0.1, yaw_error_deg=2.0, speed_mps=0.1, elapsed_s=2.0)
        == "timeout"
    )
    # Yaw comparison is angular magnitude; near-zero and wrap-around are caller-normalized.
    assert math.isfinite(criterion.yaw_tolerance_deg)


def test_invalid_primitive_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        ActionPrimitive("bogus")
    with pytest.raises(ValueError):
        ActionPrimitive.hold(-1.0)
    with pytest.raises(ValueError):
        ActionPrimitive.turn(361.0)
