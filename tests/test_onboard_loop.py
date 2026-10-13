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
