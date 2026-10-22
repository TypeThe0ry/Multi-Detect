from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive

ROOT = Path(__file__).resolve().parents[1]
CAMERA = CameraModel(fx=540.0, fy=540.0, cx=640.0, cy=480.0, width=1280, height=960)


def _loop(**config) -> OnboardLoop:
    return OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=lambda pose, n, e: True,
        config=LoopConfig(**config),
    )


def _box(cx: float, cy: float, side: float) -> tuple[float, float, float, float]:
    return (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)


def test_loop_without_clearance_configuration_fails_closed() -> None:
    loop = OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.translate(1.0, 0.0, 0.0),
    )
    assert loop._segment_clear((0.0, 0.0, -10.0), 1.0, 0.0) is False


class _BlockSource:
    def segment_clear(self, _start, _end):
        return False


def test_search_action_keeps_legacy_behavior_without_source_but_is_gated_with_source() -> None:
    pose = (0.0, 0.0, -10.0)
    legacy = _loop()
    assert legacy._search_step(pose, 0.0).kind == "translate"
    guarded = OnboardLoop(
        camera=CAMERA,
        search_action=lambda _pose, _yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        obstacle_source=_BlockSource(),
    )
    assert guarded._search_step(pose, 0.0).kind == "hold"


def test_repeated_blocked_search_legs_reach_terminal_blocked_state() -> None:
    guarded = OnboardLoop(
        camera=CAMERA,
        search_action=lambda _pose, _yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        obstacle_source=_BlockSource(),
        config=LoopConfig(max_blocked_legs=2),
    )
    pose = (0.0, 0.0, -10.0)
    assert guarded._search_step(pose, 0.0).kind == "hold"
    assert guarded.state == "searching"
    assert guarded._search_step(pose, 0.0).kind == "hold"
    assert guarded.state == "blocked"
    assert guarded.next_action([], pose_ned=pose, yaw_deg=0.0)[0].kind == "stop"


def test_ground_point_of_image_centre_follows_camera_pitch() -> None:
    north, east = CAMERA.ground_point(640.0, 480.0, north=0.0, east=0.0, height_m=20.0, yaw_deg=0.0)
    assert math.isclose(north, 20.0 / math.tan(0.18), rel_tol=1e-6)
    assert abs(east) < 1e-9
    _, east_right = CAMERA.ground_point(
        900.0, 480.0, north=0.0, east=0.0, height_m=20.0, yaw_deg=0.0
    )
    assert east_right > 0
    north_90, east_90 = CAMERA.ground_point(
        640.0, 480.0, north=0.0, east=0.0, height_m=20.0, yaw_deg=90.0
    )
    assert abs(north_90) < 1e-6 and east_90 > 0


def test_tracks_keep_ids_across_small_motion_and_filter_tiny_boxes() -> None:
    loop = _loop()
    first = loop.update_tracks(
        [Detection(_box(600, 500, 30), 0.5), Detection(_box(100, 100, 3), 0.9)]
    )
    assert [t.track_id for t in first] == ["t1"]
    loop.step_index += 1
    second = loop.update_tracks([Detection(_box(606, 504, 32), 0.5)])
    assert [t.track_id for t in second] == ["t1"]


def _jump_scene(**config):
    """A large box at one ground place, then a small box at the same image edge 46 m away."""
    loop = _loop(**config)
    pose = (0.0, 0.0, -17.0)
    places = iter([(-39.0, 14.0), (-35.5, -31.6)])
    place = next(places)
    big = loop.update_tracks(
        [Detection(_box(400, 860, 135), 0.5)],
        pose_ned=pose,
        yaw_deg=0.0,
        ground_point=lambda u, v: place,
    )
    place = next(places)
    loop.step_index += 3
    small = loop.update_tracks(
        [Detection(_box(347, 944, 15), 0.5)],
        pose_ned=pose,
        yaw_deg=0.0,
        ground_point=lambda u, v: place,
    )
    return big, small


def test_a_far_unlike_detection_never_continues_a_track_through_the_pixel_gate() -> None:
    big, small = _jump_scene()
    assert [t.track_id for t in big] == ["t1"]
    assert [t.track_id for t in small] == ["t2"]
    assert small[0].verdict is None


def test_ground_veto_can_be_disabled() -> None:
    _, small = _jump_scene(ground_veto_gates=0.0)
    assert [t.track_id for t in small] == ["t1"]  # the old pixel-only continuation
    with pytest.raises(ValueError):
        LoopConfig(ground_veto_gates=-1.0)


def test_approach_verdict_steers_but_only_two_confirm_views_confirm() -> None:
    loop = _loop()
    pose = (0.0, 0.0, -20.0)
    tracks = loop.update_tracks([Detection(_box(1000, 600, 12), 0.3)])
    assert [t.track_id for t in loop.tracks_needing_verdict(tracks)] == ["t1"]
    action, _ = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert action.kind == "translate"  # unverified tracks never steer the vehicle
    loop.apply_verdict("t1", "approach_track")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert loop.state == "approaching" and action.kind == "turn" and info["bearing_deg"] > 8
    u, v = CAMERA.rotate_pixel(1000.0, 600.0, info["bearing_deg"])
    tracks = loop.update_tracks([Detection(_box(u + 5, v, 14), 0.3)])
    assert [t.track_id for t in loop.tracks_needing_verdict(tracks)] == ["t1"]  # re-verify focus
    loop.apply_verdict("t1", "approach_track")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert action.kind == "translate" and info["confirm_views"] == 0
    tracks = loop.update_tracks([Detection(_box(u + 5, v, 30), 0.3)])
    loop.apply_verdict("t1", "confirm_target")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert loop.state == "approaching" and info["confirm_views"] == 1  # one view is not enough
    tracks = loop.update_tracks([Detection(_box(u + 5, v, 34), 0.3)])
    loop.apply_verdict("t1", "confirm_target")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert loop.state == "returning" and info["confirmed"]["rule"] == "two_views"
    assert info["confirmed"]["position_estimate_ne"] is not None


def test_reject_resets_votes_and_unconfirmed_focus_is_dropped() -> None:
    loop = _loop(max_approach_steps=3)
    pose = (0.0, 0.0, -20.0)
    tracks = loop.update_tracks([Detection(_box(640, 400, 12), 0.3)])
    loop.apply_verdict("t1", "confirm_target")
    loop.apply_verdict("t1", "reject_track")
    action, _ = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert loop.state == "searching" and action.forward_m == 10.0
    for _ in range(5):
        tracks = loop.update_tracks([Detection(_box(640, 400, 12), 0.3)])
        loop.apply_verdict("t1", "approach_track")
        action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
        if info.get("reason") == "approach_exhausted":
            break
    assert info.get("reason") == "approach_exhausted" and loop.state == "searching"
    assert loop.tracks["t1"].exhausted and loop.tracks_needing_verdict(tracks) == []


def test_tiny_boxes_are_not_sent_to_the_slow_model() -> None:
    loop = _loop()
    tracks = loop.update_tracks([Detection(_box(640, 400, 8), 0.3)])
    assert tracks and loop.tracks_needing_verdict(tracks) == []


def test_small_reject_recheck_is_opt_in_and_pending_until_confirm_size() -> None:
    disabled = _loop()
    tracks = disabled.update_tracks([Detection(_box(640, 400, 12), 0.3)])
    disabled.apply_verdict("t1", "reject_track")
    assert disabled.tracks["t1"].verdict == "no_match"
    assert disabled.tracks["t1"].identity_reject_matches == 1

    loop = _loop(
        small_reject_recheck_px=16.0,
        small_reject_recheck_max=1,
        confirm_min_side_px=20.0,
    )
    tracks = loop.update_tracks([Detection(_box(640, 400, 12), 0.3)])
    loop.apply_verdict("t1", "reject_track")
    track = loop.tracks["t1"]
    assert track.verdict == "pending_near"
    assert track.identity_reject_matches == 0
    assert track.small_reject_rechecks == 1
    assert loop.tracks_needing_verdict(tracks) == []

    loop.step_index += 1
    tracks = loop.update_tracks([Detection(_box(640, 400, 20), 0.3)])
    assert [t.track_id for t in loop.tracks_needing_verdict(tracks)] == ["t1"]
    loop.apply_verdict("t1", "reject_track")
    assert loop.tracks["t1"].verdict == "no_match"
    assert loop.tracks["t1"].identity_reject_matches == 1


def test_static_ground_veto_rejects_far_jump_even_with_matching_appearance() -> None:
    loop = _loop(static_ground_veto=True, ground_veto_gates=3.0)
    pose = (0.0, 0.0, -20.0)
    loop.update_tracks(
        [Detection(_box(640, 500, 18), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    loop.step_index += 1
    tracks = loop.update_tracks(
        [Detection(_box(640, 500, 18), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (30.0, 0.0),
    )
    assert [track.track_id for track in tracks] == ["t2"]


def test_far_small_reject_does_not_start_contradiction_relook() -> None:
    loop = _loop(
        contradiction_relook_enabled=True,
        confirm_max_range_m=20.0,
        stop_on_confirm=False,
    )
    pose = (0.0, 0.0, -20.0)
    (track,) = loop.update_tracks(
        [Detection(_box(640, 500, 11), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (40.0, 0.0),
    )
    loop.apply_verdict(track.track_id, "reject_track")
    assert not track.contradiction_active
    loop.step_index += 1
    (track,) = loop.update_tracks(
        [Detection(_box(640, 500, 28), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (8.0, 0.0),
    )
    loop.apply_verdict(track.track_id, "confirm_target")
    assert not track.contradiction_active and track.verdict == "confirm"


def test_moving_target_keeps_old_association_when_static_veto_is_off() -> None:
    loop = _loop(static_ground_veto=False, ground_veto_gates=0.0)
    pose = (0.0, 0.0, -20.0)
    loop.update_tracks(
        [Detection(_box(640, 500, 18), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    loop.step_index += 1
    tracks = loop.update_tracks(
        [Detection(_box(640, 500, 18), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (30.0, 0.0),
    )
    assert [track.track_id for track in tracks] == ["t1"]


def test_blocked_approach_routes_around_obstacle_and_confirms() -> None:
    target = (30.0, 0.0)

    def clear(start, north, east):
        # A building ring blocks the straight south-to-target leg, while either
        # side of the footprint and the final diagonal are clear.
        end = (north, east)
        return not (abs(start[1]) < 3.0 and abs(end[1]) < 3.0 and end[0] > 10.0)

    loop = OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=clear,
        config=LoopConfig(
            stop_on_confirm=False,
            confirm_matches=1,
            obstacle_relook_max=2,
        ),
    )
    pose = [0.0, 0.0, -20.0]
    yaw = 0.0
    (track,) = loop.update_tracks(
        [Detection(_box(640, 700, 16), 0.5)],
        pose_ned=tuple(pose),
        yaw_deg=yaw,
        ground_point=lambda _u, _v: target,
    )
    loop.apply_verdict(track.track_id, "approach_track")
    for _ in range(30):
        tracks = loop.update_tracks(
            [Detection(_box(640, 700, 16), 0.5)],
            pose_ned=tuple(pose),
            yaw_deg=yaw,
            ground_point=lambda _u, _v: target,
        )
        action, info = loop.next_action(
            tracks, pose_ned=tuple(pose), yaw_deg=yaw, ground_point=lambda _u, _v: target
        )
        if action.kind == "turn":
            yaw = (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
        elif action.kind == "translate":
            heading = math.radians(yaw)
            pose[0] += action.forward_m * math.cos(heading)
            pose[1] += action.forward_m * math.sin(heading)
        if info.get("obstacle_relook", {}).get("phase") == "complete":
            break
    assert track.obstacle_relook_attempts == 1
    assert loop._obstacle_relook is None
    tracks = loop.update_tracks(
        [Detection(_box(640, 700, 24), 0.5)],
        pose_ned=tuple(pose),
        yaw_deg=yaw,
        ground_point=lambda _u, _v: target,
    )
    loop.apply_verdict(track.track_id, "confirm_target")
    loop.next_action(tracks, pose_ned=tuple(pose), yaw_deg=yaw, ground_point=lambda _u, _v: target)
    assert track.verdict == "confirm" and loop.confirmations


def test_fully_enclosed_focus_gives_up_without_unsafe_leg() -> None:
    loop = OnboardLoop(
        camera=CAMERA,
        search_action=lambda _pose, _yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=lambda *_args: False,
        config=LoopConfig(obstacle_relook_max=1, stop_on_confirm=False),
    )
    pose = (0.0, -20.0, -15.0)
    (track,) = loop.update_tracks(
        [Detection(_box(640, 650, 16), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    loop.apply_verdict(track.track_id, "approach_track")
    tracks = loop.update_tracks(
        [Detection(_box(640, 650, 16), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    action, info = loop.next_action(
        tracks, pose_ned=pose, yaw_deg=0.0, ground_point=lambda _u, _v: (0.0, 0.0)
    )
    assert action.kind == "hold" or loop.state == "searching"
    assert track.exhausted or info.get("reason") in {"obstacle_relook_exhausted", "closest_view"}


def _contradiction_loop() -> OnboardLoop:
    return _loop(
        stop_on_confirm=False,
        contradiction_relook_enabled=True,
        contradiction_relook_max=1,
        contradiction_relook_views=3,
        contradiction_relook_bearing_deg=60.0,
    )


def _start_contradiction(loop: OnboardLoop):
    pose = (0.0, -30.0, -20.0)
    (track,) = loop.update_tracks(
        [Detection(_box(640, 500, 24), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    loop.apply_verdict(track.track_id, "confirm_target")
    loop.step_index += 1
    loop.update_tracks(
        [Detection(_box(640, 500, 24), 0.5)],
        pose_ned=pose,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    loop.apply_verdict(track.track_id, "reject_track")
    return track


def _vote_at_close_view(loop: OnboardLoop, track, verdict: str) -> None:
    loop.step_index += 1
    loop.update_tracks(
        [Detection(_box(640, 500, 24), 0.5)],
        pose_ned=(0.0, -30.0, -20.0),
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    loop.apply_verdict(track.track_id, verdict)


def test_one_stray_reject_among_confirms_is_not_a_contradiction() -> None:
    # SITL hydrant at 8 m: confirm, confirm, confirm, reject, confirm, confirm.
    loop = _contradiction_loop()
    pose = (0.0, -30.0, -20.0)
    (track,) = loop.update_tracks(
        [Detection(_box(640, 500, 24), 0.5)], pose_ned=pose, ground_point=lambda _u, _v: (0.0, 0.0)
    )
    for verdict in ["confirm_target", "confirm_target", "confirm_target", "reject_track"]:
        _vote_at_close_view(loop, track, verdict)
    assert not track.contradiction_active
    for verdict in ["confirm_target", "confirm_target"]:
        _vote_at_close_view(loop, track, verdict)
    assert not track.contradiction_active
    assert track.verdict == "confirm" and len(track.confirm_steps) >= 2


def test_contradiction_relook_gives_up_when_views_never_change_bearing() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    assert track.contradiction_active
    loop._last_pose = (30.0, 0.0, -20.0)
    loop.apply_verdict(track.track_id, "confirm_target")  # first fresh bearing
    for _ in range(2 * loop.config.contradiction_relook_views):
        loop.step_index += 1
        loop.apply_verdict(track.track_id, "confirm_target")  # same side, held
    assert not track.contradiction_active
    assert track.verdict == "confirm"


def test_contradiction_relook_counts_target_after_two_of_three_fresh_bearings() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    assert track.contradiction_active and track.contradiction_relooks == 1
    action, info = loop.next_action(
        [track],
        pose_ned=(0.0, -30.0, -20.0),
        yaw_deg=0.0,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    assert action.kind in {"turn", "translate", "hold"}
    assert info["second_look"]["kind"] == "contradiction_relook"
    look = loop._revisit
    assert look is not None and look["altitude_m"] == 20.0
    goal = look["goal"]
    yaw = math.degrees(math.atan2(look["centre"][1] - goal[1], look["centre"][0] - goal[0]))
    for _ in range(3):
        loop._second_look_action((goal[0], goal[1], -look["altitude_m"]), yaw, {})
    assert look["phase"] == "collect"
    loop._last_pose = (goal[0], goal[1], -look["altitude_m"])
    loop.apply_verdict(track.track_id, "confirm_target")
    assert len(track.contradiction_votes) == 1
    for pose, verdict in [
        ((30.0, 0.0, -20.0), "confirm_target"),
        ((0.0, 30.0, -20.0), "reject_track"),
        ((-30.0, 0.0, -20.0), "confirm_target"),
    ]:
        loop._last_pose = pose
        loop.step_index += 1
        loop.apply_verdict(track.track_id, verdict)
    assert not track.contradiction_active
    assert track.verdict == "confirm" and track.confirm_steps


def test_contradiction_relook_rejects_distractor_with_one_flip() -> None:
    loop = _contradiction_loop()
    track = _start_contradiction(loop)
    for pose, verdict in [
        ((30.0, 0.0, -20.0), "reject_track"),
        ((0.0, 30.0, -20.0), "confirm_target"),
        ((-30.0, 0.0, -20.0), "reject_track"),
    ]:
        loop._last_pose = pose
        loop.step_index += 1
        loop.apply_verdict(track.track_id, verdict)
    assert not track.contradiction_active
    assert track.verdict == "no_match" and not track.confirm_steps


def test_contradiction_relook_blocked_route_preserves_usable_confirm() -> None:
    loop = OnboardLoop(
        camera=CAMERA,
        search_action=lambda _pose, _yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=lambda *_args: False,
        config=LoopConfig(contradiction_relook_enabled=True, stop_on_confirm=False),
    )
    track = _start_contradiction(loop)
    loop.next_action(
        [track],
        pose_ned=(0.0, -30.0, -20.0),
        yaw_deg=0.0,
        ground_point=lambda _u, _v: (0.0, 0.0),
    )
    assert not track.contradiction_active
    assert track.verdict == "confirm"

    loop = _loop(
        small_reject_recheck_px=16.0,
        small_reject_recheck_max=1,
        confirm_min_side_px=20.0,
    )
    tracks = loop.update_tracks([Detection(_box(640, 400, 12), 0.3)])
    loop.apply_verdict("t1", "reject_track")
    track = loop.tracks["t1"]
    assert track.verdict == "pending_near"
    assert track.identity_reject_matches == 0
    assert track.small_reject_rechecks == 1
    assert loop.tracks_needing_verdict(tracks) == []

    loop.step_index += 1
    tracks = loop.update_tracks([Detection(_box(640, 400, 20), 0.3)])
    assert [t.track_id for t in loop.tracks_needing_verdict(tracks)] == ["t1"]
    loop.apply_verdict("t1", "reject_track")
    assert loop.tracks["t1"].verdict == "no_match"
    assert loop.tracks["t1"].identity_reject_matches == 1


def test_small_reject_recheck_does_not_bypass_confirm_size_gate() -> None:
    loop = _loop(
        small_reject_recheck_px=16.0,
        confirm_min_side_px=20.0,
        confirm_matches=1,
    )
    pose = (0.0, 0.0, -20.0)
    tracks = loop.update_tracks([Detection(_box(640, 400, 12), 0.3)])
    loop.apply_verdict("t1", "reject_track")
    loop.step_index += 1
    tracks = loop.update_tracks([Detection(_box(640, 400, 19), 0.3)])
    assert loop.tracks_needing_verdict(tracks) == []
    _action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert loop.state == "approaching"
    assert info["min_side_px"] == 19.0

    loop.step_index += 1
    tracks = loop.update_tracks([Detection(_box(640, 400, 20), 0.3)])
    assert [t.track_id for t in loop.tracks_needing_verdict(tracks)] == ["t1"]
    loop.apply_verdict("t1", "confirm_target")
    _action, _info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert loop.state == "returning"


def test_small_reject_recheck_is_not_spent_by_ttl_while_still_small() -> None:
    loop = _loop(small_reject_recheck_px=16.0, confirm_min_side_px=20.0, verdict_ttl_steps=2)
    tracks = loop.update_tracks([Detection(_box(640, 400, 12), 0.3)])
    loop.apply_verdict("t1", "reject_track")
    for _ in range(5):
        loop.step_index += 1
        tracks = loop.update_tracks([Detection(_box(640, 400, 14), 0.3)])
        assert loop.tracks_needing_verdict(tracks) == []
    loop.step_index += 1
    tracks = loop.update_tracks([Detection(_box(640, 400, 20), 0.3)])
    assert [t.track_id for t in loop.tracks_needing_verdict(tracks)] == ["t1"]


def test_return_home_turns_then_flies_and_stops() -> None:
    loop = _loop()
    loop.home = (0.0, 0.0)
    loop.state = "returning"
    action, _ = loop.next_action([], pose_ned=(0.0, 30.0, -20.0), yaw_deg=0.0)
    assert action.kind == "turn" and math.isclose(action.yaw_delta_deg, -90.0)
    action, _ = loop.next_action([], pose_ned=(0.0, 30.0, -20.0), yaw_deg=-90.0)
    assert action.kind == "translate" and action.forward_m == 15.0
    action, _ = loop.next_action([], pose_ned=(0.0, 1.0, -20.0), yaw_deg=-90.0)
    assert action.kind == "stop" and loop.state == "done"


def test_onboard_modules_never_read_scenario_truth() -> None:
    for path in ("src/multidetect/autonomy/onboard_loop.py", "scripts/onboard_policy.py"):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        strings = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert not any("truth" in name.lower() for name in names), path
        assert not any(".truth.json" in text or "true_target" in text for text in strings), path


def test_box_clipped_by_the_image_border_is_never_confirmed() -> None:
    loop = _loop()
    pose = (0.0, 0.0, -20.0)
    for _ in range(3):
        tracks = loop.update_tracks([Detection((620.0, 900.0, 680.0, 960.0), 0.3)])
        loop.apply_verdict("t1", "confirm_target")
        loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert loop.state == "approaching" and loop.confirmed is None


def test_approach_descends_when_map_is_clear_and_climbs_before_return() -> None:
    loop = _loop(confirm_matches=2)
    pose = (0.0, 0.0, -20.0)
    tracks = loop.update_tracks([Detection(_box(645, 600, 14), 0.3)])
    loop.apply_verdict("t1", "approach_track")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0)
    assert action.kind == "translate" and action.up_m == -4.0 and info["descend_m"] == 4.0
    low = (0.0, 0.0, -9.0)
    tracks = loop.update_tracks([Detection(_box(645, 600, 14), 0.3)])
    action, _ = loop.next_action(tracks, pose_ned=low, yaw_deg=0.0)
    assert action.up_m == -1.0  # never below the approach floor of 8 m
    loop.state = "returning"
    action, _ = loop.next_action([], pose_ned=(5.0, 0.0, -8.0), yaw_deg=0.0)
    assert action.kind == "translate" and action.up_m == 12.0 and action.forward_m == 0.0


def test_no_descent_when_the_lower_leg_hits_the_map() -> None:
    loop = OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=lambda pose, n, e: -pose[2] > 18.0,  # obstacles below 18 m
    )
    tracks = loop.update_tracks([Detection(_box(645, 600, 14), 0.3)])
    loop.apply_verdict("t1", "approach_track")
    action, info = loop.next_action(tracks, pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    assert action.kind == "translate" and action.up_m == 0.0 and "descend_m" not in info


def test_turn_prediction_moves_points_vertically_on_a_pitched_camera() -> None:
    # A point low in the frame rises toward the horizon line as it swings to the centre.
    u, v = CAMERA.rotate_pixel(865.0, 850.0, 23.6)
    assert abs(u - 669.0) < 15 and abs(v - 803.0) < 15
    assert CAMERA.rotate_pixel(640.0, 480.0, 0.0) == (640.0, 480.0)
    loop = _loop()
    loop.update_tracks([Detection(_box(865, 850, 11), 0.3)], yaw_deg=56.4)
    loop.step_index += 1
    # The measured yaw change (not the commanded turn) drives the prediction.
    tracks = loop.update_tracks([Detection(_box(669, 803, 10), 0.3)], yaw_deg=80.0)
    assert [t.track_id for t in tracks] == ["t1"]


def test_ground_anchor_keeps_identity_and_lost_focus_keeps_closing_in() -> None:
    loop = _loop()
    pose = (0.0, 0.0, -20.0)
    target = (40.0, 10.0)

    def ground_point(u: float, v: float) -> tuple[float, float]:
        return target if u > 300 else (5.0, -30.0)

    tracks = loop.update_tracks(
        [Detection(_box(700, 700, 12), 0.3)], pose_ned=pose, yaw_deg=0.0, ground_point=ground_point
    )
    loop.apply_verdict("t1", "confirm_target")
    loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    pose = (8.0, 2.0, -18.0)
    # Same ground point, very different pixels after a translation: still t1.
    tracks = loop.update_tracks(
        [Detection(_box(400, 860, 16), 0.3)], pose_ned=pose, yaw_deg=0.0, ground_point=ground_point
    )
    assert [t.track_id for t in tracks] == ["t1"] and loop.tracks["t1"].confirm_steps
    # A frame without the target: fly toward the remembered ground point, do not hover.
    tracks = loop.update_tracks([], pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    assert info["reason"] == "focus_briefly_lost" and info["focus"] == "t1"
    assert action.kind in {"turn", "translate"} and action.kind != "hold"


def test_approach_stops_before_the_focus_leaves_the_bottom_of_the_image() -> None:
    loop = _loop(approach_min_altitude_m=11.0)
    pose = (0.0, 0.0, -11.0)
    target = (9.0, 0.0)

    def ground_point(u: float, v: float) -> tuple[float, float]:
        return target

    tracks = loop.update_tracks([Detection(_box(640, 800, 44), 0.3)])
    loop.apply_verdict("t1", "confirm_target")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    # 9 m ahead at 11 m altitude is already the closest in-frame view: no leg forward.
    assert action.kind == "hold" and info["reason"] == "closest_view"
    tracks = loop.update_tracks([Detection(_box(640, 800, 44), 0.3)])
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    assert loop.state == "returning" and info["confirmed"]["rule"] == "closest_view"


def test_too_close_above_the_floor_descends_in_place() -> None:
    loop = _loop()
    pose = (0.0, 0.0, -20.0)

    def ground_point(u: float, v: float) -> tuple[float, float]:
        return (18.0, 0.0)

    tracks = loop.update_tracks([Detection(_box(640, 800, 18), 0.3)])
    loop.apply_verdict("t1", "approach_track")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    assert action.kind == "translate" and action.forward_m == 0.0 and action.up_m < 0


def test_focus_rejected_while_visible_is_dropped_not_waited_for() -> None:
    loop = _loop()
    pose = (0.0, 0.0, -20.0)

    def ground_point(u: float, v: float) -> tuple[float, float]:
        return (80.0, 0.0)

    tracks = loop.update_tracks(
        [Detection(_box(640, 600, 30), 0.3)], pose_ned=pose, yaw_deg=0.0, ground_point=ground_point
    )
    loop.apply_verdict("t1", "approach_track")
    loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    tracks = loop.update_tracks(
        [Detection(_box(640, 600, 30), 0.3)], pose_ned=pose, yaw_deg=0.0, ground_point=ground_point
    )
    loop.apply_verdict("t1", "reject_track")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    assert loop.state == "searching" and info.get("reason") is None
    assert action.forward_m == 10.0


def test_second_look_flies_to_the_far_side_of_a_one_sided_rejection():
    loop = _loop(second_look_after_steps=3)
    pose = [0.0, 0.0, -20.0]
    yaw = 0.0
    target = (40.0, 0.0)

    def ground_point(u, v):
        return target

    # Seen from the south and rejected (e.g. the grey back of a sign).
    tracks = loop.update_tracks(
        [Detection(_box(640, 600, 16), 0.3)],
        pose_ned=tuple(pose),
        yaw_deg=yaw,
        ground_point=ground_point,
    )
    loop.apply_verdict("t1", "reject_track")
    looks = []
    asked_again = False
    for _ in range(40):
        visible = math.dist(pose[:2], target) > 15 and abs(yaw) > 150
        dets = [Detection(_box(640, 600, 18), 0.3)] if visible else []
        tracks = loop.update_tracks(
            dets, pose_ned=tuple(pose), yaw_deg=yaw, ground_point=ground_point
        )
        for track in loop.tracks_needing_verdict(tracks):
            asked_again = True
            loop.apply_verdict(track.track_id, "approach_track")
        action, info = loop.next_action(
            tracks, pose_ned=tuple(pose), yaw_deg=yaw, ground_point=ground_point
        )
        if "second_look" in info:
            looks.append(info["second_look"]["phase"])
        if action.kind == "turn":
            yaw = (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
        elif action.kind == "translate":
            heading = math.radians(yaw)
            pose[0] += action.forward_m * math.cos(heading)
            pose[1] += action.forward_m * math.sin(heading)
            pose[2] -= action.up_m
        if loop.state == "approaching":
            break
    assert "fly" in looks
    assert pose[0] > target[0]  # it went round to the north (far) side
    assert asked_again and loop.state == "approaching"


def test_second_look_is_off_by_default():
    loop = _loop()
    assert loop.config.second_look_after_steps is None


def test_search_descends_to_the_search_altitude_first():
    loop = _loop(search_altitude_m=10.0)
    action, _ = loop.next_action([], pose_ned=(0.0, 0.0, -27.0), yaw_deg=0.0)
    assert action.kind == "translate" and action.forward_m == 0.0 and action.up_m == -8.0
    action, _ = loop.next_action([], pose_ned=(0.0, 0.0, -10.5), yaw_deg=0.0)
    assert action.forward_m == 10.0  # at the search altitude: the search pattern resumes


def test_typical_object_size_uses_the_longest_known_phrase():
    from multidetect.simulator.task_semantics import typical_object_size

    assert typical_object_size("Find the red stop sign and record its position.") == 0.8
    assert typical_object_size("找到消防车并记录位置") == 9.0
    assert typical_object_size("Find the gizmo") == 2.0


def test_identity_matches_confirm_at_the_closest_view():
    loop = _loop(approach_min_altitude_m=11.0)
    pose = (0.0, 0.0, -11.0)

    def ground_point(u, v):
        return (9.0, 0.0)

    tracks = loop.update_tracks([Detection(_box(640, 800, 18), 0.3)])
    loop.apply_verdict("t1", "approach_track")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    assert action.kind == "hold" and info["reason"] == "closest_view"
    tracks = loop.update_tracks([Detection(_box(640, 800, 20), 0.3)])
    loop.apply_verdict("t1", "approach_track")  # one more view, still "approach"
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    assert loop.state == "returning"
    assert info["confirmed"]["rule"] == "identity_at_closest_view"


def test_a_single_identity_match_is_not_enough_at_the_closest_view():
    loop = _loop(approach_min_altitude_m=11.0)
    pose = (0.0, 0.0, -11.0)
    tracks = loop.update_tracks([Detection(_box(640, 800, 18), 0.3)])
    loop.apply_verdict("t1", "approach_track")
    loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=lambda u, v: (9.0, 0.0))
    tracks = loop.update_tracks([Detection(_box(640, 800, 18), 0.3)])
    loop.apply_verdict("t1", "reject_track")
    loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=lambda u, v: (9.0, 0.0))
    assert loop.state != "returning" and loop.tracks["t1"].match_steps == []


def test_closest_view_identity_rule_can_be_disabled():
    loop = _loop(approach_min_altitude_m=11.0, closest_view_identity_matches=None)
    pose = (0.0, 0.0, -11.0)
    for _ in range(3):
        tracks = loop.update_tracks([Detection(_box(640, 800, 18), 0.3)])
        loop.apply_verdict("t1", "approach_track")
        loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=lambda u, v: (9.0, 0.0))
    assert loop.state != "returning"


def test_far_confirm_verdicts_only_count_as_matches():
    loop = _loop(confirm_max_range_m=40.0)
    pose = (0.0, 0.0, -20.0)

    def far(u, v):
        return (85.0, 0.0)

    for _ in range(2):
        tracks = loop.update_tracks(
            [Detection(_box(640, 500, 32), 0.3)], pose_ned=pose, yaw_deg=0.0, ground_point=far
        )
        loop.apply_verdict("t1", "confirm_target")
        action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=far)
    # Two confirm answers 85 m away: still approaching, no confirmation yet.
    assert loop.state == "approaching" and loop.tracks["t1"].confirm_steps == []
    assert len(loop.tracks["t1"].match_steps) == 2


def test_unresolved_positive_tracks_survive_loss_and_revival_until_judged() -> None:
    loop = _loop(stop_on_confirm=False)
    pose = (0.0, 0.0, -8.0)
    places = {400.0: (30.0, 20.0), 700.0: (30.5, 20.0), 1000.0: (-10.0, 40.0)}

    def ground(u, _v):
        return places[min(places, key=lambda x: abs(x - u))]

    seen = [Detection(_box(u, 700, 13), 0.5) for u in places]
    small, distinct, counted = loop.update_tracks(
        seen, pose_ned=pose, yaw_deg=0.0, ground_point=ground
    )
    loop.apply_verdict(small.track_id, "approach_track")
    # A co-visible neighbour is a different object; its rejection is not this place's.
    loop.apply_verdict(distinct.track_id, "reject_track")
    loop.apply_verdict(counted.track_id, "approach_track")
    loop.confirmations.append({"position_estimate_ne": [-10.0, 40.2], "track_id": "other"})
    loop.step_index += loop.config.lost_after_steps + 2
    assert loop.unresolved_positive_tracks() == [small]
    # A ground revival clears the cached verdict but not what the model said.
    (revived,) = loop.update_tracks(
        [Detection(_box(400, 700, 10), 0.5)], pose_ned=pose, yaw_deg=0.0, ground_point=ground
    )
    assert revived is small and small.verdict is None
    assert loop.unresolved_positive_tracks() == [small]
    loop.apply_verdict(small.track_id, "reject_track")
    assert loop.unresolved_positive_tracks() == []


def test_place_revisit_flies_to_a_near_side_view_without_spending_second_looks() -> None:
    loop = _loop(stop_on_confirm=False)
    pose = (0.0, 0.0, -8.0)
    (track,) = loop.update_tracks(
        [Detection(_box(640, 700, 13), 0.5)],
        pose_ned=pose,
        yaw_deg=0.0,
        ground_point=lambda u, v: (40.0, 0.0),
    )
    loop.apply_verdict(track.track_id, "approach_track")
    loop.state = "blocked"
    look = loop.plan_place_revisit(track, pose)
    assert look["kind"] == "place_revisit" and look["centre"] == (40.0, 0.0)
    assert 0.0 < look["goal"][0] < 40.0 and abs(look["goal"][1]) < 1e-9
    info: dict = {}
    action = loop.begin_place_revisit(look, pose, 0.0, info)
    assert loop.state == "searching" and action.kind == "translate"
    assert info["second_look"] == {"centre": [40.0, 0.0], "phase": "fly", "kind": "place_revisit"}
    blocked = OnboardLoop(
        camera=CAMERA,
        search_action=lambda _pose, _yaw: ActionPrimitive.hold(1.0),
        segment_clear=lambda *_args: False,
    )
    assert blocked.plan_place_revisit(track, pose) is None
    # Flying the look to completion does not consume the second-look budget.
    north = 0.0
    for _ in range(12):
        action, _ = loop.next_action([], pose_ned=(north, 0.0, -8.0), yaw_deg=0.0)
        if action.kind == "translate":
            north += action.forward_m
        if loop._revisit is None:
            break
    assert loop._revisit is None and loop._revisits_done == 0


def test_rejected_far_small_places_exclude_any_usable_view() -> None:
    loop = _loop(stop_on_confirm=False, confirm_min_side_px=20.0)
    loop._rejections.extend(
        [
            {
                "ground": (30.0, 0.0),
                "min_side_px": [12.0, 15.0],
                "range_m": [18.0, 22.0],
                "revisited": False,
            },
            {
                "ground": (40.0, 0.0),
                "min_side_px": [24.0],
                "range_m": [40.0],
                "revisited": False,
                "usable_seen": True,
            },
        ]
    )
    assert [item["ground"] for item in loop.rejected_far_small_places(15.0)] == [(30.0, 0.0)]


def _count(loop: OnboardLoop, track, position) -> dict:
    record = {"track_id": track.track_id, "position_estimate_ne": list(position)}
    _, info = loop._count_confirmation(track, record, (0.0, 0.0, -8.0), 0.0, {})
    return info


def _observe(loop: OnboardLoop, step: int, boxes: dict) -> list:
    """``boxes`` maps an image box to the ground point under its bottom centre."""
    loop.step_index = step
    by_u = {(box[0] + box[2]) / 2: point for box, point in boxes.items()}
    return loop.update_tracks(
        [Detection(box, 0.5) for box in boxes],
        pose_ned=(0.0, 0.0, -8.0),
        yaw_deg=0.0,
        ground_point=lambda u, _v: by_u[min(by_u, key=lambda x: abs(x - u))],
    )


def test_count_dedupe_ignores_co_visibility_after_a_track_migrates_onto_a_counted_object():
    # 2026-10-19 SIM count (gen_cnt2_w0000): t2 (another object, 6 m away) and
    # t3 (a cone) shared step 0; t3 was counted, then t2 re-associated onto the
    # cone and was confirmed there.  The shared frame proved t2 differed from the
    # cone at t2's old place only, so its confirmation is the same cone again.
    loop = _loop(stop_on_confirm=False)
    cone, other = _box(400, 700, 13), _box(900, 700, 13)
    counted, mover = _observe(loop, 0, {cone: (30.0, 0.0), other: (30.0, 6.0)})
    assert mover.track_id in counted.co_visible_with
    assert "counted" in _count(loop, counted, (30.0, 0.0))
    (migrated,) = _observe(loop, 1, {other: (30.0, 0.3)})
    assert migrated is mover and migrated.ground == (30.0, 0.3)
    info = _count(loop, migrated, (30.0, 0.3))
    assert info["duplicate_of_counted"] == mover.track_id
    assert len(loop.confirmations) == 1


def test_count_keeps_co_visible_objects_distinct_while_they_stay_apart():
    # Two cones 1 m apart (inside the 1.5 m object radius) in one frame as
    # separate boxes are two objects, also after small projection drift.
    loop = _loop(stop_on_confirm=False)
    left, right = _box(400, 700, 13), _box(900, 700, 13)
    first, second = _observe(loop, 0, {left: (30.0, 0.0), right: (30.0, 1.0)})
    assert "counted" in _count(loop, first, (30.0, 0.0))
    (second_again,) = _observe(loop, 1, {right: (30.0, 0.7)})
    assert second_again is second
    assert "counted" in _count(loop, second, (30.0, 0.7))
    assert len(loop.confirmations) == 2


def test_count_treats_nested_co_visible_boxes_as_one_object():
    # Chosen behaviour: a box (mostly) inside another one in the same frame is
    # one object detected twice (or an object and a part of it), not proof of
    # two objects.  If both are later confirmed within the de-duplication
    # radius they count once; separate boxes at the same spot still count twice.
    loop = _loop(stop_on_confirm=False)
    small, large = _box(640, 700, 13), (610.0, 660.0, 675.0, 707.0)
    inner, outer = _observe(loop, 0, {small: (30.0, 0.0), large: (30.0, 0.5)})
    assert outer.track_id in inner.co_visible_with
    assert inner.co_visible_geometry == {} and outer.co_visible_geometry == {}
    assert "counted" in _count(loop, inner, (30.0, 0.0))
    # As at steps 23-24 of that SIM run, the outer track then takes the cone's box.
    (outer_again,) = _observe(loop, 1, {_box(645, 700, 13): (30.0, 0.4)})
    assert outer_again is outer
    assert "duplicate_of_counted" in _count(loop, outer, (30.0, 0.4))
    assert len(loop.confirmations) == 1

    apart = _loop(stop_on_confirm=False)
    left, right = _observe(
        apart, 0, {_box(600, 700, 13): (30.0, 0.0), _box(680, 700, 13): (30.0, 0.5)}
    )
    assert left.co_visible_geometry[right.track_id] == (0, (30.0, 0.0), (30.0, 0.5))
    assert "counted" in _count(apart, left, (30.0, 0.0))
    assert "counted" in _count(apart, right, (30.0, 0.5))


def test_count_dedupe_uses_the_counted_objects_snapshot_and_latest_shared_frame():
    cone, other = _box(400, 700, 13), _box(900, 700, 13)
    # The live relation can be pruned; the counted record keeps its snapshot.
    loop = _loop(stop_on_confirm=False)
    counted, neighbour = _observe(loop, 0, {cone: (30.0, 0.0), other: (30.0, 1.0)})
    assert "counted" in _count(loop, counted, (30.0, 0.0))
    neighbour.co_visible_with.clear()
    neighbour.co_visible_geometry.clear()
    assert "counted" in _count(loop, neighbour, (30.0, 1.0))
    # A later shared frame with separate boxes supersedes an earlier one: the
    # mover has really been seen as a second object 1 m beside the cone.
    loop = _loop(stop_on_confirm=False)
    counted, mover = _observe(loop, 0, {cone: (30.0, 0.0), other: (30.0, 6.0)})
    assert "counted" in _count(loop, counted, (30.0, 0.0))
    _observe(loop, 1, {cone: (30.0, 0.0), other: (30.0, 1.0)})
    assert mover.co_visible_geometry[counted.track_id] == (1, (30.0, 1.0), (30.0, 0.0))
    assert "counted" in _count(loop, mover, (30.0, 1.0))


def test_co_visibility_geometry_is_pruned_with_its_relation():
    loop = _loop(stop_on_confirm=False)
    loop.co_visibility_max_pairs = 1
    first = _observe(loop, 0, {_box(400, 700, 13): (30.0, 0.0), _box(900, 700, 13): (30.0, 8.0)})
    _observe(loop, 8, {_box(100, 300, 13): (60.0, 20.0), _box(300, 300, 13): (60.0, 28.0)})
    for track in loop.tracks.values():
        assert set(track.co_visible_geometry) <= track.co_visible_with
    assert first[0].co_visible_geometry == {} and first[1].co_visible_geometry == {}


# -- fixed-wing fly-by passes ----------------------------------------------------
# rc_cessna: 640x480 level forward camera, 15 m/s cruise, 50 deg bank, 30 m altitude.
FW_CAMERA = CameraModel(
    fx=269.968, fy=269.968, cx=320.0, cy=240.0, width=640, height=480, pitch_down_rad=0.0
)


def _fw_loop(segment_clear=lambda pose, n, e: True, **config) -> OnboardLoop:
    return OnboardLoop(
        camera=FW_CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.translate(28.0, 0.0, 0.0),
        segment_clear=segment_clear,
        config=LoopConfig(
            fixed_wing_pass=True, fw_min_turn_radius_m=21.2, autopilot_return=True, **config
        ),
    )


class _Plane:
    """Kinematic plane: straight on, or converging onto a clockwise guided orbit."""

    def __init__(self, north: float, east: float, yaw_deg: float, altitude: float = 30.0):
        self.north, self.east = north, east
        self.psi, self.altitude = math.radians(yaw_deg), altitude
        self.orbit: tuple[float, float, float] | None = None

    def fly(self, seconds: float, speed: float = 15.0) -> None:
        rate = 9.81 * math.tan(math.radians(50.0)) / speed
        for _ in range(int(round(seconds / 0.1))):
            desired = self.psi
            if self.orbit is not None:
                cn, ce, radius = self.orbit
                azimuth = math.atan2(self.east - ce, self.north - cn)
                error = math.hypot(self.north - cn, self.east - ce) - radius
                desired = azimuth + math.pi / 2 + math.atan2(error, 30.0)
            delta = (desired - self.psi + math.pi) % (2 * math.pi) - math.pi
            self.psi += max(-rate * 0.1, min(rate * 0.1, delta))
            self.north += speed * 0.1 * math.cos(self.psi)
            self.east += speed * 0.1 * math.sin(self.psi)

    @property
    def pose(self) -> tuple[float, float, float]:
        return (self.north, self.east, -self.altitude)

    @property
    def yaw_deg(self) -> float:
        return math.degrees(self.psi)

    def box(self, target, size_m: float = 4.0):
        """Image box of a ``size_m`` object standing at ``target``, if the camera sees it."""
        dn, de = target[0] - self.north, target[1] - self.east
        forward = dn * math.cos(self.psi) + de * math.sin(self.psi)
        right = -dn * math.sin(self.psi) + de * math.cos(self.psi)
        if forward <= 1.0:
            return None
        u = FW_CAMERA.cx + FW_CAMERA.fx * right / forward
        v = FW_CAMERA.cy + FW_CAMERA.fy * self.altitude / forward
        side = FW_CAMERA.fx * size_m / math.hypot(forward, self.altitude)
        box = (u - side / 2, v - side, u + side / 2, v)
        inside = box[0] > 0 and box[1] > 0 and box[2] < 640 and box[3] < 480
        return box if inside and side >= 6.0 else None

    def ground_point(self, u: float, v: float):
        return FW_CAMERA.ground_point(
            u, v, north=self.north, east=self.east, height_m=self.altitude, yaw_deg=self.yaw_deg
        )


def _fw_step(loop: OnboardLoop, plane: _Plane, target, verdict):
    box = plane.box(target) if target is not None else None
    tracks = loop.update_tracks(
        [Detection(box, 0.5)] if box else [],
        pose_ned=plane.pose,
        yaw_deg=plane.yaw_deg,
        ground_point=plane.ground_point,
        camera_ray=lambda u, v: (plane.pose, FW_CAMERA.ray_ned(u, v, plane.yaw_deg)),
    )
    for track in loop.tracks_needing_verdict(tracks):
        loop.apply_verdict(track.track_id, verdict(track, plane))
    action, info = loop.next_action(
        tracks, pose_ned=plane.pose, yaw_deg=plane.yaw_deg, ground_point=plane.ground_point
    )
    orbit = info.get("fw_orbit")
    if orbit is not None:
        plane.orbit = (orbit["centre_ne"][0], orbit["centre_ne"][1], orbit["radius_m"])
    elif action.kind == "translate":
        plane.orbit = None
    return action, info, box is not None


def _approach(track, plane) -> str:
    return "approach_track"


def test_fixed_wing_co_visible_tracks_do_not_inherit_position_views() -> None:
    loop = _fw_loop(
        step_triangulation=True,
        ground_height_sigma_m=20.0,
        step_triangulation_parallax_deg_per_m=0.001,
        step_triangulation_min_ray_angle_deg=5.0,
        stop_on_confirm=False,
        ground_reid_max_steps=0,
    )
    plane = _Plane(0.0, -10.0, 0.0)
    target = (100.0, 0.0)
    _fw_step(loop, plane, target, _approach)
    first = loop.tracks["t1"]
    box = plane.box(target)
    assert box is not None
    shifted = (box[0] + 90.0, box[1], box[2] + 90.0, box[3])
    current = loop.update_tracks(
        [Detection(box, 0.5), Detection(shifted, 0.5)],
        pose_ned=plane.pose,
        yaw_deg=plane.yaw_deg,
        ground_point=lambda u, v: (100.0, 0.0),
        camera_ray=lambda u, v: (plane.pose, FW_CAMERA.ray_ned(u, v, plane.yaw_deg)),
    )

    second = loop.tracks["t2"]
    assert first in current and second in current
    assert second.track_id in first.co_visible_with
    assert len(second.position_views) == 1


def test_fixed_wing_new_track_inherits_views_from_an_earlier_lap() -> None:
    loop = _fw_loop(
        step_triangulation=True,
        ground_height_sigma_m=20.0,
        step_triangulation_parallax_deg_per_m=0.001,
        step_triangulation_min_ray_angle_deg=5.0,
        stop_on_confirm=False,
        ground_reid_max_steps=0,
    )
    plane = _Plane(0.0, -10.0, 0.0)
    target = (100.0, 0.0)
    _fw_step(loop, plane, target, _approach)
    assert len(loop.tracks["t1"].position_views) == 1

    for _ in range(loop.config.lost_after_steps + 2):
        _fw_step(loop, plane, None, _approach)
    loop.tracks["t1"].exhausted = True
    loop._focus = None
    loop._end_fw_pass()
    plane.north, plane.east = 0.0, 10.0
    _fw_step(loop, plane, target, _approach)

    assert set(loop.tracks) == {"t1", "t2"}
    second = loop.tracks["t2"]
    assert len(second.position_views) == 2
    assert second.position_estimate is not None
    assert second.position_estimate.source == "fused"
    assert second.position_estimate.view_count == 2


def test_fixed_wing_candidate_gets_a_fly_by_orbit_through_it_not_a_turn() -> None:
    loop = _fw_loop()
    plane = _Plane(0.0, 0.0, 10.0)
    target = (100.0, 10.0)
    _fw_step(loop, plane, target, _approach)
    action, info, seen = _fw_step(loop, plane, target, _approach)
    assert seen and loop.state == "approaching"
    # A multirotor would turn toward it; a plane holds a guided orbit instead.
    assert action.kind == "hold" and info["focus"] == "t1"
    orbit = info["fw_orbit"]
    centre, radius = orbit["centre_ne"], orbit["radius_m"]
    assert radius == pytest.approx(89.9, abs=0.5)
    assert orbit["view_range_m"] == [pytest.approx(45.1, abs=0.3), pytest.approx(120.0)]
    # The target is on the rim and the centre to the right of the line of sight:
    # the clockwise orbit reaches the target along the current line of sight.
    assert math.dist(centre, target) == pytest.approx(radius, abs=1.0)
    los = (target[0] - plane.north, target[1] - plane.east)
    to_centre = (centre[0] - target[0], centre[1] - target[1])
    assert los[0] * to_centre[1] - los[1] * to_centre[0] > 0


def test_fixed_wing_passes_keep_the_focus_through_blind_laps_then_hand_home_over() -> None:
    loop = _fw_loop()
    plane = _Plane(40.0, 30.0, -150.0)
    target = (-40.0, 14.0)

    def verdict(track, plane) -> str:
        near = math.dist(track.ground, (plane.north, plane.east)) < 70.0
        return "confirm_target" if near else "approach_track"

    reasons, seen_steps = [], []
    for step in range(40):
        action, info, seen = _fw_step(loop, plane, target, verdict)
        reasons.append(info.get("reason"))
        if seen:
            seen_steps.append(step)
        if action.kind == "stop":
            break
        plane.fly(2.0)
    # Confirmed and handed straight to the autopilot (no turn/translate legs home).
    assert action.kind == "stop" and loop.state == "done" and info["return"] == "autopilot"
    assert loop.confirmed is not None and loop.confirmed["track_id"] == "t1"
    assert list(loop.tracks) == ["t1"]
    # The target was out of view for most of a lap, far longer than lost_after_steps,
    # and the same track picked it up again by its ground position.
    assert "fw_pass_out_of_view" in reasons
    gaps = [b - a for a, b in zip(seen_steps, seen_steps[1:], strict=False)]
    assert max(gaps) > loop.config.lost_after_steps
    assert math.dist(loop.confirmed["position_estimate_ne"], target) < 3.0


def test_fixed_wing_pass_gives_up_after_its_lap_budget() -> None:
    loop = _fw_loop(fw_pass_max_laps=1.5)
    plane = _Plane(0.0, 0.0, 0.0)
    target = (90.0, 0.0)
    _fw_step(loop, plane, target, _approach)
    _fw_step(loop, plane, target, _approach)
    assert loop.state == "approaching"
    reasons = []
    for _ in range(60):
        plane.fly(3.0)
        # The candidate vanished (e.g. a false detection): nothing is seen again.
        _, info, _ = _fw_step(loop, plane, None, _approach)
        reasons.append(info.get("reason"))
        if loop.state == "searching":
            break
    assert reasons[-1] == "fw_pass_exhausted" and loop.tracks["t1"].exhausted
    assert reasons.count("fw_pass_out_of_view") >= 10  # about 1.5 laps of 38 s at 3 s


def test_fixed_wing_pass_shrinks_the_orbit_around_a_blocked_area_or_drops_the_focus() -> None:
    target = (100.0, 10.0)
    # Nothing may be flown more than 120 m east: the 90 m orbit does not fit.
    loop = _fw_loop(segment_clear=lambda pose, n, e: e < 120.0)
    plane = _Plane(0.0, 0.0, 10.0)
    _fw_step(loop, plane, target, _approach)
    _, info, _ = _fw_step(loop, plane, target, _approach)
    assert info["fw_orbit"]["radius_m"] < 89.0
    assert info["fw_orbit"]["centre_ne"][1] + info["fw_orbit"]["radius_m"] < 120.0
    # A fence the smallest pass cannot fit: a plain hold, then the focus is dropped.
    blocked = _fw_loop(segment_clear=lambda pose, n, e: e < 25.0)
    plane = _Plane(0.0, 0.0, 10.0)
    _fw_step(blocked, plane, target, _approach)
    action, info, _ = _fw_step(blocked, plane, target, _approach)
    assert action.kind == "hold" and info["reason"] == "fw_pass_blocked"
    assert "fw_orbit" not in info
    for _ in range(3):
        _fw_step(blocked, plane, target, _approach)
    assert blocked.state == "searching" and blocked.tracks["t1"].exhausted


def test_autopilot_return_stops_instead_of_flying_legs_home() -> None:
    loop = _fw_loop()
    loop.home = (0.0, 0.0)
    loop.state = "returning"
    action, info = loop.next_action([], pose_ned=(80.0, 40.0, -30.0), yaw_deg=90.0)
    assert action.kind == "stop" and loop.state == "done" and info["return"] == "autopilot"
    budget = _fw_loop(max_steps=1)
    budget.next_action([], pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
    action, info = budget.next_action([], pose_ned=(30.0, 0.0, -30.0), yaw_deg=0.0)
    assert action.kind == "stop" and info["reason"] == "step_budget"


def test_a_closest_view_clipped_by_the_image_bottom_backs_off() -> None:
    loop = _loop(approach_min_altitude_m=11.0, stop_on_confirm=False)
    pose = (0.0, 0.0, -11.0)

    def ground_point(u: float, v: float) -> tuple[float, float]:
        return (7.0, 0.0)

    # The base is below the frame: a confirm verdict alone can never count it.
    clipped = (620.0, 900.0, 660.0, float(CAMERA.height))
    tracks = loop.update_tracks([Detection(clipped, 0.3)])
    loop.apply_verdict("t1", "confirm_target")
    action, info = loop.next_action(tracks, pose_ned=pose, yaw_deg=0.0, ground_point=ground_point)
    assert info["reason"] == "closest_view_backoff"
    assert action.kind == "translate" and action.forward_m < 0


def test_a_closest_view_that_never_confirms_is_dropped() -> None:
    loop = _loop(approach_min_altitude_m=11.0, stop_on_confirm=False, closest_view_max_holds=3)
    pose = (0.0, 0.0, -11.0)
    reasons = []
    for _ in range(6):
        tracks = loop.update_tracks([Detection(_box(640, 800, 18), 0.3)])
        loop.apply_verdict("t1", "reject_track")
        loop.tracks["t1"].verdict = "approach"
        _, info = loop.next_action(
            tracks, pose_ned=pose, yaw_deg=0.0, ground_point=lambda u, v: (9.0, 0.0)
        )
        reasons.append(info.get("reason"))
    assert "closest_view_exhausted" in reasons and loop.tracks["t1"].exhausted


def _fw_single_view_confirm(reject_first: bool):
    loop = _fw_loop()
    plane = _Plane(0.0, 0.0, 0.0)
    target = (40.0, 0.0)
    if reject_first:
        _fw_step(loop, plane, target, lambda track, plane: "reject_track")
        for _ in range(loop.config.lost_after_steps + 2):
            _fw_step(loop, plane, None, _approach)
    _, info, seen = _fw_step(loop, plane, target, lambda track, plane: "confirm_target")
    assert seen
    return loop, info


def test_fixed_wing_single_view_confirms_an_unrejected_place() -> None:
    loop, info = _fw_single_view_confirm(reject_first=False)
    assert loop.confirmed is not None and info["confirmed"]["confirm_views"] == 1


def test_fixed_wing_single_view_does_not_overrule_a_rejection_at_the_same_place() -> None:
    loop, info = _fw_single_view_confirm(reject_first=True)
    assert info["focus"] != "t1"  # a new track id at the rejected place
    assert loop.confirmed is None and info["contradicted_by_rejection"]


def test_fixed_wing_single_view_counts_once_confirms_outnumber_rejections() -> None:
    loop = _fw_loop()
    target = (40.0, 0.0)
    far = _Plane(-60.0, 0.0, 0.0)  # 100 m out: a confirm there is only a vote
    _fw_step(loop, far, target, lambda track, plane: "confirm_target")
    assert loop.confirmed is None
    _fw_step(loop, far, target, lambda track, plane: "reject_track")
    for _ in range(loop.config.lost_after_steps + 2):
        _fw_step(loop, far, None, _approach)
    near = _Plane(0.0, 0.0, 0.0)
    _, info, _ = _fw_step(loop, near, target, lambda track, plane: "confirm_target")
    assert info["focus"] != "t1"
    # Two confirms against one rejection at this place: the single view counts.
    assert loop.confirmed is not None and info["confirmed"]["confirm_views"] == 1


def test_search_climbs_back_to_the_search_altitude_after_an_approach() -> None:
    loop = _loop(search_altitude_m=11.3)
    action, _ = loop.next_action([], pose_ned=(20.0, 4.0, -8.0), yaw_deg=0.0)
    assert action.kind == "translate" and action.forward_m == 0.0
    assert action.up_m == pytest.approx(3.3)


def test_count_dedupe_widens_with_the_position_uncertainty_of_both_estimates():
    from multidetect.autonomy.onboard_loop import AppearanceSignature

    cone = AppearanceSignature(size_range=0.02, aspect_ratio=0.5, footprint_m=0.5)

    def count_twice(first_sigma, second_sigma) -> int:
        loop = _loop(stop_on_confirm=False)
        (first,) = _observe(loop, 0, {_box(400, 700, 13): (-20.8, 28.2)})
        first.appearance = cone
        record = {"track_id": first.track_id, "position_estimate_ne": [-20.8, 28.2]}
        record["position_sigma_m"] = first_sigma
        loop._count_confirmation(first, record, (0.0, 0.0, -8.0), 0.0, {})
        (second,) = _observe(loop, 90, {_box(900, 300, 13): (-22.4, 28.7)})
        second.appearance = cone
        record = {"track_id": second.track_id, "position_estimate_ne": [-22.4, 28.7]}
        record["position_sigma_m"] = second_sigma
        loop._count_confirmation(second, record, (0.0, 0.0, -8.0), 0.0, {})
        return len(loop.confirmations)

    # SITL: one cone, estimates 1.7 m apart with sigmas 3.6 m and 1.2 m.
    assert count_twice(3.6, 1.2) == 1
    # Precise estimates that far apart are two objects (1.5 m footprint radius).
    assert count_twice(0.3, 0.3) == 2


def test_count_does_not_approach_a_new_track_id_on_a_counted_object() -> None:
    # SITL 2026-10-20: each frame re-detected a counted cone under a new id; every
    # one was approached, confirmed and de-duplicated in turn until the budget ran out.
    loop = _loop(stop_on_confirm=False, ground_reid_max_steps=0)
    (counted,) = _observe(loop, 0, {_box(640, 700, 13): (30.0, 0.0)})
    assert "counted" in _count(loop, counted, (30.0, 0.0))
    loop._focus = None
    loop.step_index = 40  # long after: no association with the counted track
    (fresh,) = loop.update_tracks(
        [Detection(_box(200, 300, 13), 0.5)],
        pose_ned=(0.0, 0.0, -8.0),
        yaw_deg=0.0,
        ground_point=lambda u, v: (30.4, 0.2),
    )
    assert fresh.track_id != counted.track_id
    # Seen from far away its appearance does not match the close-up one.
    from multidetect.autonomy.onboard_loop import AppearanceSignature

    counted_record = loop.confirmations[0]
    counted_record["appearance"] = AppearanceSignature(size_range=0.05, aspect_ratio=0.5)
    fresh.appearance = AppearanceSignature(size_range=0.01, aspect_ratio=2.0)
    loop.apply_verdict(fresh.track_id, "confirm_target")
    _, info = loop.next_action([fresh], pose_ned=(0.0, 0.0, -8.0), yaw_deg=0.0)
    assert info.get("focus") is None and loop.state == "searching"
