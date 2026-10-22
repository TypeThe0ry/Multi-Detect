"""Contradiction relook regressions found in review (driven through next_action)."""

from __future__ import annotations

import math

from test_onboard_loop import CAMERA, _box, _contradiction_loop, _start_contradiction

from multidetect.autonomy.onboard_loop import Detection, LoopConfig, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive


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
        loop.next_action(
            [track], pose_ned=pose, yaw_deg=0.0, ground_point=lambda _u, _v: (0.0, 0.0)
        )
        if not track.contradiction_active:
            break
    assert not track.contradiction_active
    assert loop._revisit is None or loop._revisit.get("kind") != "contradiction_relook"


_BEARINGS = [(30.0, 0.0, -20.0), (0.0, 30.0, -20.0), (-30.0, 0.0, -20.0)]


def _relook_votes(loop, track, verdicts) -> None:
    for pose, verdict in zip(_BEARINGS, verdicts, strict=False):
        loop._last_pose = pose
        loop.step_index += 1
        loop.apply_verdict(track.track_id, verdict)


def test_approach_answers_during_a_relook_are_never_rejects() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    rejects_before = track.identity_reject_matches
    _relook_votes(loop, track, ["approach_track", "approach_track", "confirm_target"])
    assert not track.contradiction_active
    assert track.verdict == "confirm" and track.confirm_steps
    assert track.identity_reject_matches == rejects_before
    assert loop._place_vote_counts(track, usable_only=True) == (2, 1)


def test_relook_with_only_approach_answers_keeps_closing_in_without_a_rejection() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    loop._rejections.clear()
    _relook_votes(loop, track, ["approach_track"] * 3)
    assert not track.contradiction_active
    assert track.verdict == "approach"
    assert loop._rejection_at(track) is None


def test_relook_confirm_withdraws_the_rejection_that_started_it() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    assert loop._rejection_at(track) is not None
    _relook_votes(loop, track, ["confirm_target", "confirm_target", "reject_track"])
    assert track.verdict == "confirm"
    assert loop._rejection_at(track) is None


def test_same_side_give_up_decides_by_usable_majority_and_remembers_a_reject() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    loop._rejections.clear()
    rejects_before = track.identity_reject_matches
    loop._last_pose = (30.0, 0.0, -20.0)
    loop.apply_verdict(track.track_id, "reject_track")  # fresh bearing: 1 confirm vs 2 rejects
    for _ in range(2 * loop.config.contradiction_relook_views):
        loop.step_index += 1
        loop.apply_verdict(track.track_id, "confirm_target")  # same side, held
    assert not track.contradiction_active
    assert track.verdict == "no_match" and not track.confirm_steps
    assert track.identity_reject_matches == rejects_before + 1
    assert loop._rejection_at(track) is not None


def test_blocked_first_relook_keeps_confirm_only_from_usable_views() -> None:
    loop = OnboardLoop(
        camera=CAMERA,
        search_action=lambda _pose, _yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=lambda *_args: False,
        config=LoopConfig(contradiction_relook_enabled=True, stop_on_confirm=False),
    )
    pose = (0.0, -30.0, -20.0)
    (track,) = loop.update_tracks(
        [Detection(_box(640, 500, 12), 0.5)], pose_ned=pose, ground_point=lambda _u, _v: (0.0, 0.0)
    )
    loop.apply_verdict(track.track_id, "confirm_target")  # too small to be usable
    # Another (not co-visible) look at the same place confirmed it from a usable view.
    loop._place_votes.append(
        {
            "ground": (0.0, 0.0),
            "verdict": "confirm",
            "track_id": "other",
            "co_visible_with": set(),
            "appearance": None,
            "footprint_m": None,
            "min_side_px": 24.0,
            "range_m": 30.0,
            "usable": True,
        }
    )
    loop.step_index += 1
    loop.update_tracks(
        [Detection(_box(640, 500, 24), 0.5)], pose_ned=pose, ground_point=lambda _u, _v: (0.0, 0.0)
    )
    assert loop.tracks[track.track_id] is track
    loop.apply_verdict(track.track_id, "reject_track")
    assert track.contradiction_active
    loop.next_action([track], pose_ned=pose, yaw_deg=0.0, ground_point=lambda _u, _v: (0.0, 0.0))
    assert not track.contradiction_active
    assert track.verdict is None and not track.confirm_steps


def test_reaching_the_closest_view_while_stalled_is_not_an_obstacle() -> None:
    loop = OnboardLoop(
        camera=CAMERA,
        search_action=lambda _pose, _yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=lambda *_args: True,
        config=LoopConfig(approach_min_altitude_m=11.0),
    )
    pose, yaw = (0.0, 0.0, -11.0), 0.0
    target = (30.0, 0.0)
    for step in range(4):
        tracks = loop.update_tracks(
            [Detection(_box(640, 800, 44), 0.3)],
            pose_ned=pose,
            yaw_deg=yaw,
            ground_point=lambda _u, _v: target,
        )
        if step == 0:
            loop.apply_verdict(tracks[0].track_id, "approach_track")
        action, info = loop.next_action(
            tracks, pose_ned=pose, yaw_deg=yaw, ground_point=lambda _u, _v: target
        )
        pose, yaw = _fly(action, pose, yaw)
    assert info.get("reason") == "closest_view"
    assert "obstacle_relook" not in info
    assert tracks[0].obstacle_relook_attempts == 0
