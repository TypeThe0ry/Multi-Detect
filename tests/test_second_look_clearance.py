from __future__ import annotations

from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive


def _loop(segment_clear) -> OnboardLoop:
    return OnboardLoop(
        camera=CameraModel(
            fx=540.0, fy=540.0, cx=640.0, cy=480.0, width=1280, height=960
        ),
        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
        segment_clear=segment_clear,
        config=LoopConfig(second_look_after_steps=1),
    )


def _rejection(ground: tuple[float, float]) -> dict:
    return {
        "ground": ground,
        "views": [(0.0, 0.0)],
        "revisited": False,
        "confidence": None,
    }


def test_blocked_second_look_remains_eligible_after_clearance_changes() -> None:
    clear = False
    loop = _loop(lambda pose, n, e: clear)
    rejection = _rejection((40.0, 0.0))
    loop._rejections.append(rejection)

    assert loop._plan_second_look((0.0, 0.0, -20.0)) is None
    assert rejection["revisited"] is False
    assert loop._revisits_done == 0

    clear = True
    planned = loop._plan_second_look((0.0, 20.0, -20.0))

    assert planned is not None
    assert planned["centre"] == rejection["ground"]
    assert planned["goal"][0] > rejection["ground"][0]
    assert rejection["revisited"] is True
    assert loop._plan_second_look((0.0, 20.0, -20.0)) is None


def test_blocked_nearer_rejection_is_not_consumed_by_another_second_look() -> None:
    loop = _loop(lambda pose, n, e: e > 0.0)
    blocked = _rejection((40.0, 0.0))
    reachable = _rejection((80.0, 40.0))
    loop._rejections.extend([blocked, reachable])

    planned = loop._plan_second_look((0.0, 0.0, -20.0))

    assert planned is not None
    assert planned["centre"] == reachable["ground"]
    assert blocked["revisited"] is False
    assert reachable["revisited"] is True


def test_second_look_checks_clearance_before_marking_rejection_revisited() -> None:
    checked = []
    rejection = _rejection((40.0, 0.0))

    def segment_clear(pose, n, e):
        checked.append(rejection["revisited"])
        return True

    loop = _loop(segment_clear)
    loop._rejections.append(rejection)

    assert loop._plan_second_look((0.0, 0.0, -20.0)) is not None
    assert checked == [False]
    assert rejection["revisited"] is True


def test_second_look_releases_rejection_when_execution_leg_becomes_blocked() -> None:
    clear = True
    loop = _loop(lambda pose, n, e: clear)
    rejection = _rejection((40.0, 0.0))
    loop._rejections.append(rejection)
    loop._revisit = loop._plan_second_look((0.0, 0.0, -20.0))
    assert rejection["revisited"] is True

    clear = False
    action = loop._second_look_action((0.0, 0.0, -20.0), 0.0, {})

    assert action.kind == "hold"
    assert loop._revisit is None
    assert rejection["revisited"] is False
    assert loop._revisits_done == 0
    clear = True
    assert loop._plan_second_look((0.0, 20.0, -20.0)) is not None


def test_second_look_releases_rejection_when_far_side_travel_times_out() -> None:
    loop = _loop(lambda pose, n, e: True)
    rejection = _rejection((40.0, 0.0))
    loop._rejections.append(rejection)
    loop._revisit = loop._plan_second_look((0.0, 0.0, -20.0))
    loop._revisit["steps"] = 2 * loop.config.max_approach_steps

    assert loop._second_look_action((0.0, 0.0, -20.0), 0.0, {}) is None
    assert loop._revisit is None
    assert rejection["revisited"] is False
    assert loop._revisits_done == 0
