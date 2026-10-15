from __future__ import annotations

import ast
import math
from pathlib import Path

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

    tracks = loop.update_tracks([Detection(_box(640, 800, 20), 0.3)])
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
    tracks = loop.update_tracks([Detection(_box(640, 800, 18), 0.3)])
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
