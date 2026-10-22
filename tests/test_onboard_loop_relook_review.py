"""Contradiction relook regressions found in review (driven through next_action)."""

from __future__ import annotations

import math

from multidetect.autonomy.onboard_loop import Detection
from test_onboard_loop import _box, _contradiction_loop, _start_contradiction


def _fly(action, pose, yaw):
    if action.kind == "turn":
        return pose, yaw + action.yaw_delta_deg
    if action.kind == "translate":
        rad = math.radians(yaw)
        return (
            pose[0] + action.forward_m * math.cos(rad) - action.right_m * math.sin(rad),
            pose[1] + action.forward_m * math.sin(rad) + action.right_m * math.cos(rad),
            pose[2] - action.up_m,
        ), yaw
    return pose, yaw


def _drive_relook(loop, track, pose, yaw, max_steps=60):
    """Step next_action with the focus visible until the relook reaches ``collect``."""
    for _ in range(max_steps):
        loop.step_index += 1
        action, info = loop.next_action(
            [track], pose_ned=pose, yaw_deg=yaw, ground_point=lambda _u, _v: (0.0, 0.0)
        )
        assert info.get("second_look", {}).get("kind") == "contradiction_relook", info
        pose, yaw = _fly(action, pose, yaw)
        loop._last_pose = pose
        if loop._revisit is not None and loop._revisit["phase"] == "collect":
            return pose, yaw
    raise AssertionError(f"relook never reached collect: {loop._revisit}")


def test_contradiction_relook_advances_through_repeated_next_action_calls() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    pose, yaw = (0.0, -30.0, -20.0), 0.0
    pose, yaw = _drive_relook(loop, track, pose, yaw)
    look = loop._revisit
    assert math.dist(pose[:2], look["goal"]) <= loop.config.min_translate_m + 1e-6
    # Collect holds until the slow model answers from this new bearing.
    loop.step_index += 1
    action, info = loop.next_action(
        [track], pose_ned=pose, yaw_deg=yaw, ground_point=lambda _u, _v: (0.0, 0.0)
    )
    assert action.kind == "hold" and info["second_look"]["phase"] == "collect"


def test_contradiction_relook_times_out_when_the_vehicle_never_reaches_the_goal() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    pose = (0.0, -30.0, -20.0)
    for _ in range(2 * loop.config.max_approach_steps + 2):
        loop.step_index += 1
        loop.next_action([track], pose_ned=pose, yaw_deg=0.0, ground_point=lambda _u, _v: (0.0, 0.0))
        if not track.contradiction_active:
            break
    assert not track.contradiction_active
    assert loop._revisit is None or loop._revisit.get("kind") != "contradiction_relook"
