"""Executor regressions using observations and kinematics only; no simulator/model."""

from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.onboard_loop import AppearanceSignature, CameraModel, Detection, LoopConfig, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive

CAMERA = CameraModel(fx=540, fy=540, cx=640, cy=480, width=1280, height=960)
BOX = Detection((620, 680, 660, 720), 0.9)


def _executor(skill, params, *, refs=None, camera=CAMERA, clear=lambda *args: True, config=None):
    plan = validate_mission_plan({
        "plan_version": 1,
        "goal": skill,
        "steps": [
            {"id": "s", "skill": skill, "params": params,
             "on_success": "end", "on_fail": "end"},
            {"id": "end", "skill": "land", "params": {},
             "on_success": None, "on_fail": None},
        ],
        "report": ["count", "positions"],
    })
    return MissionExecutor(
        plan, refs=refs, config=config,
        loop_factory=lambda task, region=None, **overrides: OnboardLoop(
            camera=camera,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            segment_clear=clear,
            config=LoopConfig(**overrides),
        ),
    )


class Flight:
    def __init__(self, *, altitude=20.0):
        self.pose = [0.0, 0.0, -altitude]
        self.yaw = 0.0

    def tick(self, executor, *, stamp=None, detections=(), ground=None):
        tracks = executor.observe(
            detections, pose_ned=tuple(self.pose), yaw_deg=self.yaw, ground_point=ground
        )
        for track in tracks:
            executor.apply_verdict(track.track_id, "approach_track")
        action, info = executor.next_action(
            pose_ned=tuple(self.pose), yaw_deg=self.yaw,
            timestamp_s=stamp, frame_id="observed-frame",
        )
        heading = math.radians(self.yaw)
        if action.kind == "translate":
            self.pose[0] += (
                action.forward_m * math.cos(heading) - action.right_m * math.sin(heading)
            )
            self.pose[1] += (
                action.forward_m * math.sin(heading) + action.right_m * math.cos(heading)
            )
            self.pose[2] -= action.up_m
        elif action.kind == "turn":
            self.yaw = (self.yaw + action.yaw_delta_deg + 180) % 360 - 180
        return action, info


def _colored_follow_tick(executor, flight, rgb, stamp):
    import numpy as np

    frame = np.zeros((960, 1280, 3), dtype=np.uint8)
    frame[:, :] = rgb
    tracks = executor.observe(
        [BOX], pose_ned=tuple(flight.pose), yaw_deg=flight.yaw,
        ground_point=lambda u, v: (30.0, 0.0), frame_rgb=frame,
    )
    for track in tracks:
        executor.apply_verdict(track.track_id, "approach_track")
    return executor.next_action(
        pose_ned=tuple(flight.pose), yaw_deg=flight.yaw,
        timestamp_s=stamp, frame_id="colored-frame",
    )


def test_monitor_raises_centre_view_to_cover_a_nadir_zone_within_altitude_limit():
    executor = _executor(
        "monitor", {"region": "zone:a", "query": "car", "duration_s": 5},
        refs={"zone:a": (0, 0, 30)}, camera=replace(CAMERA, pitch_down_rad=math.pi / 2),
    )
    flight = Flight(altitude=8)
    run = executor._run
    action, _ = flight.tick(executor)
    assert action.kind == "translate" and action.up_m > 0
    assert 8 < -flight.pose[2] <= executor.config.max_altitude_m
    assert executor.coverage["s"] == 0  # A planned footprint is not an observation.
    action, _ = flight.tick(executor)
    assert action.kind == "hold"
    assert run.waypoints == [(0, 0)]
    assert executor.coverage["s"] == 1
    assert "s:coverage:1.00" in executor.report().state_answers


@pytest.mark.parametrize("nadir", [False, True])
def test_monitor_uses_ring_when_camera_or_altitude_limit_prevents_centre_coverage(nadir):
    camera = replace(CAMERA, pitch_down_rad=math.pi / 2) if nadir else CAMERA
    executor = _executor(
        "monitor", {"region": "zone:a", "query": "car", "duration_s": 100},
        refs={"zone:a": (0, 0, 300)}, camera=camera,
    )
    flight = Flight()
    flight.tick(executor)
    run = executor._run
    assert len(run.waypoints) == 8
    assert all(0 < math.hypot(*point) <= 300 for point in run.waypoints)
    assert executor.config.min_altitude_m <= run.goal_altitude_m <= 120


def test_monitor_visits_multiple_ring_views_holds_turns_and_accumulates_observed_coverage():
    executor = _executor(
        "monitor", {"region": "zone:a", "query": "car", "duration_s": 100},
        refs={"zone:a": (0, 0, 30)},
    )
    flight = Flight()
    held_at, kinds, coverage = set(), set(), []
    for _ in range(130):
        action, info = flight.tick(executor)
        if info.get("phase") == "hold":
            held_at.add(tuple(round(value, 1) for value in flight.pose[:2]))
        kinds.add(action.kind)
        coverage.append(executor.coverage.get("s", 0))
        if executor.history:
            break
    assert len(held_at) > 1
    assert {"hold", "turn", "translate"} <= kinds
    assert coverage == sorted(coverage)
    assert 0 < coverage[-1] <= 1
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_monitor_short_duration_reports_partial_footprint_instead_of_completed_zone():
    executor = _executor(
        "monitor", {"region": "zone:a", "query": "car", "duration_s": 1},
        refs={"zone:a": (0, 0, 300)}, camera=replace(CAMERA, pitch_down_rad=math.pi / 2),
    )
    flight = Flight()
    for _ in range(50):
        flight.tick(executor)
        if executor.history:
            break
    assert executor.history[0]["reason"] == "duration_elapsed"
    assert 0 < executor.coverage["s"] < 0.1
    assert "s:coverage:1.00" not in executor.report().state_answers


def test_monitor_blocked_first_view_reports_zero_coverage():
    executor = _executor(
        "monitor", {"region": "zone:a", "query": "car", "duration_s": 10},
        refs={"zone:a": (100, 0, 20)}, clear=lambda *args: False,
    )
    flight = Flight()
    for _ in range(10):
        flight.tick(executor)
        if executor.history:
            break
    assert executor.history[0]["reason"] == "blocked"
    assert executor.coverage["s"] == 0


def test_patrol_duration_includes_actual_turn_and_control_latency():
    executor = _executor(
        "patrol", {"route_ref": "route:a", "query": "car", "loops": 100, "duration_s": 5},
        refs={"route:a": [(0, 100), (100, 100)]},
    )
    flight = Flight()
    action, _ = flight.tick(executor, stamp=100)
    assert action.kind == "turn"
    flight.tick(executor, stamp=108)
    assert executor.history[0]["reason"] == "duration_elapsed"
    assert flight.pose[:2] == [0, 0]


@pytest.mark.parametrize("skill", ["hover", "monitor"])
def test_requested_holds_do_not_advance_duration_faster_than_pose_clock(skill):
    params = {"duration_s": 6}
    if skill == "monitor":
        params.update(region="zone:a", query="car")
    executor = _executor(skill, params, refs={"zone:a": (0, 0, 20)})
    flight = Flight()
    run = executor._run
    for stamp in (0, 0.25, 0.5, 0.75, 1):
        flight.tick(executor, stamp=stamp)
        assert not executor.history
    # The monitor planner may spend the first measured interval reaching a
    # valid observation altitude/viewpoint; only time after that starts the
    # duration clock.
    expected_held = 1.0 if skill == "hover" else 0.5
    assert run.held_s == pytest.approx(expected_held)
    flight.tick(executor, stamp=6)
    assert run.held_s == pytest.approx(6.0 if skill == "hover" else 5.5)
    if skill == "monitor":
        flight.tick(executor, stamp=7)
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_monitor_clock_starts_after_initial_transit_and_counts_turn_time():
    executor = _executor(
        "monitor", {"region": "zone:a", "query": "car", "duration_s": 5},
        refs={"zone:a": (100, 0, 20)},
    )
    flight = Flight()
    run = executor._run
    flight.tick(executor, stamp=100)
    flight.pose[0] = 100
    action, _ = flight.tick(executor, stamp=200)
    assert action.kind == "turn" and run.held_s == pytest.approx(0)
    action, _ = flight.tick(executor, stamp=201)
    assert action.kind == "translate" and run.held_s == pytest.approx(0)
    flight.pose[0], flight.pose[1] = run.waypoints[0]
    flight.tick(executor, stamp=202)
    action, _ = flight.tick(executor, stamp=203)
    assert action.kind in {"turn", "hold"} and run.held_s == pytest.approx(1)
    flight.tick(executor, stamp=207)
    assert run.held_s == 5
    flight.tick(executor, stamp=208)
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_clock_duplicate_and_backward_stamps_do_not_complete_hover():
    executor = _executor("hover", {"duration_s": 6})
    flight = Flight()
    run = executor._run
    for stamp in (100, 100, 99, 101):
        flight.tick(executor, stamp=stamp)
        assert not executor.history
    assert run.held_s == 1
    flight.tick(executor, stamp=106)
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_clock_reconciles_estimated_interval_when_pose_timestamps_resume():
    executor = _executor("hover", {"duration_s": 10})
    flight = Flight()
    run = executor._run
    flight.tick(executor, stamp=100)
    flight.tick(executor)
    assert run.held_s == 5
    flight.tick(executor, stamp=102)
    assert run.held_s == 2 and not executor.history
    flight.tick(executor, stamp=110)
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_hover_falls_back_to_executed_primitive_estimate_without_pose_clock():
    executor = _executor("hover", {"duration_s": 6})
    flight = Flight()
    holds = []
    for _ in range(3):
        action, _ = flight.tick(executor)
        if action.kind == "hold":
            holds.append(action.seconds)
    assert holds == [5, 1]
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_first_pose_timestamp_preserves_preceding_fallback_action_time():
    executor = _executor("hover", {"duration_s": 10})
    flight = Flight()
    run = executor._run
    flight.tick(executor)
    flight.tick(executor, stamp=100)
    assert run.held_s == 5 and not executor.history
    flight.tick(executor, stamp=101)
    assert run.held_s == 6 and not executor.history
    flight.tick(executor, stamp=105)
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_follow_counts_actual_elapsed_turn_time():
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car", "duration_s": 5}
    )
    flight = Flight()
    action, _ = flight.tick(
        executor, stamp=100, detections=[BOX], ground=lambda u, v: (0, 200)
    )
    assert action.kind == "turn"
    flight.tick(executor, stamp=108, detections=[BOX], ground=lambda u, v: (0, 200))
    assert executor.history[0]["reason"] == "duration_elapsed"


def test_follow_budget_cannot_claim_unelapsed_requested_duration():
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car", "duration_s": 100},
        config=ExecutorConfig(travel_budget=1),
    )
    flight = Flight()
    for stamp in (100, 101):
        flight.tick(executor, stamp=stamp, detections=[BOX], ground=lambda u, v: (200, 0))
    assert executor.history[0]["state"] == "failed"
    assert executor.history[0]["reason"] == "step_budget"


def test_follow_short_losses_and_boxes_without_new_ground_cannot_refresh_stale_fix():
    executor = _executor("follow", {"track_ref": "track:1", "query": "car"})
    flight = Flight()
    flight.tick(executor, stamp=100, detections=[BOX], ground=lambda u, v: (200, 0))
    run = executor._run
    track_id = run.follow_track_id
    held_at = tuple(flight.pose)
    confidences = []
    for index in range(1, 7):
        action, info = flight.tick(
            executor, stamp=100 + index,
            detections=[BOX] if index % 2 == 0 else (), ground=lambda u, v: None,
        )
        assert action.kind in {"hold", "turn"}
        assert tuple(flight.pose) == held_at
        assert run.lost_cycles == index
        confidences.append(info["follow_confidence"])
        if index == 2:
            assert executor.current_tracks[0].track_id == track_id
            assert executor.current_tracks[0].ground == (200, 0)
        if index >= 2:
            assert info["follow_state"] == "searching"
    assert confidences == sorted(confidences, reverse=True)
    assert confidences[-1] < confidences[0] < 1
    assert run.follow_finding["position_ne"] == (200, 0)
    flight.tick(executor, stamp=107)
    assert executor.history[0]["reason"] == "target_lost_timeout"


def test_follow_recovers_confidence_and_updates_position_only_after_fresh_ground():
    executor = _executor("follow", {"track_ref": "track:1", "query": "car"})
    flight = Flight()
    flight.tick(executor, stamp=100, detections=[BOX], ground=lambda u, v: (200, 0))
    flight.tick(executor, stamp=101, detections=[BOX], ground=lambda u, v: None)
    action, info = flight.tick(
        executor, stamp=102, detections=[BOX], ground=lambda u, v: (201, 0)
    )
    assert executor._run.lost_cycles == 0
    assert info["follow_confidence"] == 1
    assert action.kind == "translate"
    assert executor.findings[0]["position_ne"] == (201, 0)


@pytest.mark.parametrize("threshold", ["follow_stale_s", "follow_stale_cycles"])
def test_follow_zero_stale_threshold_searches_immediately_without_dividing_by_zero(threshold):
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car"},
        config=ExecutorConfig(**{threshold: 0}),
    )
    flight = Flight()
    flight.tick(executor, stamp=100, detections=[BOX], ground=lambda u, v: (200, 0))
    position = tuple(flight.pose)
    action, info = flight.tick(executor, stamp=101)
    assert action.kind == "turn" and tuple(flight.pose) == position
    assert info["follow_state"] == "searching"
    assert 0 <= info["follow_confidence"] < 1


def test_follow_repeated_blocked_standoff_legs_are_bounded_despite_intermittent_clear_legs():
    clear = False
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car"},
        clear=lambda *args: clear,
    )
    flight = Flight()
    for leg_clear in (False, True, False, True, False):
        clear = leg_clear
        flight.tick(executor, detections=[BOX], ground=lambda u, v: (200, 0))
    assert executor.history[0]["state"] == "failed"
    assert executor.history[0]["reason"] == "blocked"


def test_follow_does_not_cross_the_operating_area_to_reach_a_moving_target():
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car"},
    )
    executor.operating_area = {
        "north_min_m": -20.0, "north_max_m": 20.0,
        "east_min_m": -20.0, "east_max_m": 20.0,
    }
    flight = Flight()
    for _ in range(5):
        action, _ = flight.tick(executor, detections=[BOX], ground=lambda u, v: (80.0, 0.0))
        assert action.kind in {"hold", "turn", "translate"}
        assert math.dist(flight.pose[:2], (0.0, 0.0)) <= 20.0


def test_follow_stops_at_no_fly_boundary_and_keeps_observing_target():
    executor = MissionExecutor(
        validate_mission_plan({
            "plan_version": 1, "goal": "follow", "report": [],
            "steps": [{"id": "s", "skill": "follow",
                       "params": {"track_ref": "track:1", "query": "car"},
                       "on_success": "end", "on_fail": "end"},
                      {"id": "end", "skill": "land", "params": {},
                       "on_success": None, "on_fail": None}],
        }),
        config=ExecutorConfig(max_translate_m=5.0),
        no_fly_zones=(((12.0, 0.0), 4.0),),
        loop_factory=lambda task, region=None, **overrides: OnboardLoop(
            camera=CAMERA, search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            segment_clear=lambda pose, north, east: True, config=LoopConfig(**overrides),
        ),
    )
    flight = Flight()
    actions = []
    for _ in range(8):
        action, _ = flight.tick(executor, detections=[BOX], ground=lambda u, v: (80.0, 0.0))
        actions.append(action.kind)
        assert math.dist(flight.pose[:2], (0.0, 0.0)) <= 8.0 + 1e-6
    assert executor.history == []
    assert "translate" not in actions or all(
        math.dist(flight.pose[:2], (0.0, 0.0)) <= 8.0 + 1e-6
    )


def test_follow_duration_success_transitions_to_return_home():
    plan = validate_mission_plan({
        "plan_version": 1,
        "goal": "follow then return",
        "steps": [
            {"id": "follow", "skill": "follow",
             "params": {"track_ref": "track:1", "query": "car", "duration_s": 2},
             "on_success": "home", "on_fail": "home"},
            {"id": "home", "skill": "return_home", "params": {},
             "on_success": None, "on_fail": None},
        ],
        "report": ["positions"],
    })
    executor = MissionExecutor(plan, loop_factory=lambda task, region=None, **overrides: OnboardLoop(
        camera=CAMERA, search_action=lambda pose, yaw: ActionPrimitive.hold(1),
        segment_clear=lambda pose, north, east: True, config=LoopConfig(**overrides),
    ))
    flight = Flight()
    for stamp in range(5):
        flight.tick(executor, stamp=stamp, detections=[BOX], ground=lambda u, v: (80.0, 0.0))
        if len(executor.history):
            break
    assert executor.history[0]["reason"] == "duration_elapsed"
    assert executor.current_step.skill == "return_home"


def test_follow_stops_safely_when_target_speed_exceeds_configured_cap():
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car"},
        config=ExecutorConfig(follow_max_target_speed_mps=5.0, follow_speed_violation_cycles=3),
    )
    flight = Flight()
    flight.tick(executor, stamp=100, detections=[BOX], ground=lambda u, v: (30.0, 0.0))
    action, info = flight.tick(
        executor, stamp=101, detections=[BOX], ground=lambda u, v: (50.0, 0.0)
    )
    assert action.kind == "hold"
    assert info["follow_state"] == "target_too_fast"
    assert executor.history == []


def test_follow_ends_only_after_consecutive_speed_violations():
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car"},
        config=ExecutorConfig(follow_max_target_speed_mps=5.0, follow_speed_violation_cycles=3),
    )
    flight = Flight()
    flight.tick(executor, stamp=100, detections=[BOX], ground=lambda u, v: (30.0, 0.0))
    for stamp, north in ((101, 50.0), (102, 70.0)):
        action, info = flight.tick(executor, stamp=stamp, detections=[BOX], ground=lambda u, v, n=north: (n, 0.0))
        assert action.kind == "hold"
        assert info["speed_violation_cycles"] in {1, 2}
        assert executor.history == []
    action, info = flight.tick(executor, stamp=103, detections=[BOX], ground=lambda u, v: (90.0, 0.0))
    assert action.kind == "hold"
    assert info["speed_violation_cycles"] == 3
    assert executor.history[0]["reason"] == "target_exceeds_follow_speed"


def test_follow_speed_violation_limit_must_be_positive():
    with pytest.raises(ValueError, match="follow_speed_violation_cycles"):
        ExecutorConfig(follow_speed_violation_cycles=0)


def test_follow_normal_sample_clears_a_speed_violation_streak():
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car"},
        config=ExecutorConfig(follow_max_target_speed_mps=5.0, follow_speed_violation_cycles=3),
    )
    flight = Flight()
    flight.tick(executor, stamp=100, detections=[BOX], ground=lambda u, v: (30.0, 0.0))
    flight.tick(executor, stamp=101, detections=[BOX], ground=lambda u, v: (50.0, 0.0))
    action, info = flight.tick(executor, stamp=102, detections=[BOX], ground=lambda u, v: (51.0, 0.0))
    assert action.kind in {"translate", "hold", "turn"}
    assert info.get("speed_violation_cycles", 0) == 0
    assert executor.history == []


def test_follow_reacquisition_resets_cross_track_speed_baseline():
    executor = _executor(
        "follow", {"track_ref": "track:1", "query": "car"},
        config=ExecutorConfig(max_translate_m=10.0, follow_max_target_speed_mps=20.0),
    )
    flight = Flight()
    flight.tick(executor, stamp=100, detections=[BOX], ground=lambda u, v: (30.0, 0.0))
    run = executor._run
    appearance = AppearanceSignature(300.0, 1.0, 2.0)
    run.follow_finding["appearance"] = appearance
    run.follow_last_position = (0.0, 0.0)
    run.last_ground_s = 0.0
    run.elapsed_s = 0.5
    replacement = SimpleNamespace(
        track_id="replacement", verdict="approach", ground=(15.0, 0.0),
        appearance=appearance, bbox=(620.0, 680.0, 660.0, 720.0),
    )
    executor._current = [replacement]
    run.fresh_ground_tracks = {"replacement"}
    executor._follow(run, tuple(flight.pose), flight.yaw, None, "replacement-1")
    action, info = executor._follow(run, tuple(flight.pose), flight.yaw, None, "replacement-2")
    assert run.follow_track_id == "replacement"
    assert run.follow_speed_violation_count == 0
    assert info["follow_state"] == "tracking"
    assert action.kind != "stop"
    assert executor.history == []


def test_follow_keeps_a_stationary_target_at_standoff_without_chasing():
    executor = _executor("follow", {"track_ref": "track:1", "query": "car"})
    flight = Flight()
    for stamp in range(30):
        action, info = flight.tick(
            executor, stamp=stamp, detections=[BOX], ground=lambda u, v: (30.0, 0.0)
        )
        if info.get("follow_state") == "tracking" and info.get("range_m", 0) <= info.get("standoff_m", 0) + 3:
            assert action.kind == "hold"
            break
    else:
        pytest.fail("stationary follow target never reached the standoff hold")


def test_follow_does_not_accept_an_appearance_switch_on_the_locked_track():
    executor = _executor("follow", {"track_ref": "track:1", "query": "car"})
    flight = Flight()
    _colored_follow_tick(executor, flight, (230, 20, 20), 100)
    locked_id = executor._run.follow_track_id
    assert executor._run.follow_finding is not None
    executor._run.follow_finding["appearance"] = AppearanceSignature(99999.0, 1.0, 10.0)
    action, info = _colored_follow_tick(executor, flight, (20, 20, 230), 101)
    assert executor._run.follow_track_id == locked_id
    assert info["follow_state"] in {"holding", "searching"}
    assert action.kind in {"hold", "turn"}


def test_return_home_climbs_out_of_obstacle_footprint_before_clearance_check():
    def clear(pose, _north, _east):
        return -pose[2] >= 10.0

    plan = validate_mission_plan({
        "plan_version": 1, "goal": "return", "report": [],
        "steps": [{"id": "s", "skill": "return_home", "params": {},
                   "on_success": None, "on_fail": None}],
    })
    executor = MissionExecutor(
        plan, config=None,
        loop_factory=lambda task, region=None, **overrides: OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            segment_clear=clear,
            config=LoopConfig(**overrides),
        ),
    )
    executor.home = (0.0, 0.0)
    executor.cruise_altitude_m = 10.0
    flight = Flight(altitude=2.0)

    action, _ = flight.tick(executor)
    assert action.kind == "translate" and action.up_m == 8.0
    flight.tick(executor)
    assert executor.history[0]["state"] == "succeeded"
    assert executor.history[0]["reason"] == "arrived"


def test_return_home_climb_failure_replays_operator_abort_after_two_failed_climbs():
    from pathlib import Path
    import json

    episode = Path(
        "artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/"
        "campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d"
    )
    if not (episode / "steps.jsonl").is_file():
        pytest.skip("shared read-only benchmark artifacts are unavailable")
    plan = validate_mission_plan({
        "plan_version": 1, "goal": "return", "report": [],
        "steps": [{"id": "s", "skill": "return_home", "params": {},
                   "on_success": None, "on_fail": None}],
    })
    executor = MissionExecutor(
        plan,
        loop_factory=lambda task, region=None, **overrides: OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            config=LoopConfig(**overrides),
        ),
    )
    rows = [json.loads(line) for line in (episode / "steps.jsonl").read_text().splitlines()]
    matches = []
    for row in rows:
        action = ActionPrimitive(**row["action"])
        if executor.report_primitive_outcome(
            action,
            row["outcome"]["status"],
            before=row["obs_before"],
            after=row["obs_after"],
        ):
            matches.append(row["step_index"])
            break
    assert matches == [41]
    assert executor.history[0]["reason"] == "climb_failed_suspected_loss_of_control"
    assert executor.mission_log[-1]["event"] == "climb_failed_suspected_loss_of_control"


def test_return_home_climb_failure_ignores_single_timeout_progress_and_terminal_actions():
    plan = validate_mission_plan({
        "plan_version": 1, "goal": "return", "report": [],
        "steps": [{"id": "s", "skill": "return_home", "params": {},
                   "on_success": None, "on_fail": None}],
    })
    executor = MissionExecutor(
        plan,
        loop_factory=lambda task, region=None, **overrides: OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            config=LoopConfig(**overrides),
        ),
    )

    def observations(z_before, z_after, boot):
        before = {"pose": {"local_ned_m": [0.0, 0.0, z_before]}, "pose_time_boot_ms": boot,
                  "landed": False, "flight_state": {"landed_state": 2}}
        after = {
            "pose": {"local_ned_m": [0.0, 0.0, z_after]},
            "pose_time_boot_ms": boot + 20000,
            "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0},
            "landed": False,
            "flight_state": {"landed_state": 2},
        }
        return before, after

    climb = ActionPrimitive.translate(0, 0, 15)
    before, after = observations(-0.8, -0.8, 1)
    assert not executor.report_primitive_outcome(climb, "timeout", before=before, after=after)
    assert executor._return_climb_failures == 1
    before, after = observations(-0.8, -1.5, 21000)
    assert not executor.report_primitive_outcome(climb, "success", before=before, after=after)
    assert executor._return_climb_failures == 0
    before, after = observations(-0.8, -0.8, 41000)
    assert not executor.report_primitive_outcome(ActionPrimitive.land(), "timeout", before=before, after=after)
    assert not executor.report_primitive_outcome(ActionPrimitive.hold(1), "timeout", before=before, after=after)
    assert not executor.report_primitive_outcome(climb, "timeout", before=before, after=after)
    assert not executor.return_climb_failure_suspected


def test_return_home_climb_failure_requires_near_ground_airborne_state():
    plan = validate_mission_plan({
        "plan_version": 1, "goal": "return", "report": [],
        "steps": [{"id": "s", "skill": "return_home", "params": {},
                   "on_success": None, "on_fail": None}],
    })
    executor = MissionExecutor(
        plan,
        loop_factory=lambda task, region=None, **overrides: OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            config=LoopConfig(**overrides),
        ),
    )
    climb = ActionPrimitive.translate(0, 0, 15)
    for boot, altitude in ((1, 10.0), (21000, 10.0)):
        before = {"pose": {"local_ned_m": [0.0, 0.0, -altitude]}, "pose_time_boot_ms": boot,
                  "landed": False, "flight_state": {"landed_state": 2}}
        after = {
            "pose": {"local_ned_m": [0.0, 0.0, -altitude]},
            "pose_time_boot_ms": boot + 20000,
            "landed": False,
        }
        assert not executor.report_primitive_outcome(climb, "timeout", before=before, after=after)
    assert not executor.return_climb_failure_suspected


def test_return_home_slow_px4_climb_uses_sim_time_without_wall_timeout():
    plan = validate_mission_plan({
        "plan_version": 1, "goal": "return", "report": [],
        "steps": [{"id": "s", "skill": "return_home", "params": {},
                   "on_success": None, "on_fail": None}],
    })
    executor = MissionExecutor(
        plan,
        loop_factory=lambda task, region=None, **overrides: OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            config=LoopConfig(**overrides),
        ),
    )
    climb = ActionPrimitive.translate(0, 0, 15)
    for boot, z_before, z_after, count in (
        (1000, -0.8, -0.8, 1), (101000, -0.8, -1.6, 0), (201000, -1.6, -1.6, 1)
    ):
        before = {"pose": {"local_ned_m": [0.0, 0.0, z_before]}, "pose_time_boot_ms": boot,
                  "landed": False, "flight_state": {"landed_state": 2}}
        after = {
            "pose": {"local_ned_m": [0.0, 0.0, z_after]},
            "pose_time_boot_ms": boot + 90000,
            "landed": False,
            "flight_state": {"landed_state": 2},
        }
        assert not executor.report_primitive_outcome(climb, "timeout", before=before, after=after)
        assert executor._return_climb_failures == count
    assert not executor.return_climb_failure_suspected


def _climb_observations(boot):
    def sample(stamp):
        return {"pose": {"local_ned_m": [0.0, 0.0, -0.8]}, "pose_time_boot_ms": stamp,
                "landed": False, "flight_state": {"landed_state": 2}}
    return sample(boot), sample(boot + 20000)


def _climb_executor(skill="return_home"):
    plan = validate_mission_plan({
        "plan_version": 1, "goal": "return", "report": [], "steps": [
            {"id": "s", "skill": skill, "params": {},
             "on_success": None, "on_fail": None},
        ],
    })
    return MissionExecutor(plan, loop_factory=lambda *_args, **_kwargs: None)


@pytest.mark.parametrize("interruption", [
    "success", "progress", "high_before", "high_after", "landed_before", "landed_after",
    "landing", "hold", "land", "horizontal", "yaw", "descent", "same_boot",
    "rollback_within", "rollback_between", "duplicate", "missing_boot", "nan_boot",
    "nan_altitude", "unknown_airborne", "stale_landed_boolean", "boolean_boot",
])
def test_climb_failure_requires_two_adjacent_eligible_outcomes(interruption):
    executor = _climb_executor()
    climb = ActionPrimitive.translate(0, 0, 15)

    def feed(action, status, before, after):
        return executor.report_primitive_outcome(action, status, before=before, after=after)

    assert not feed(climb, "timeout", *_climb_observations(1000))
    assert executor._return_climb_failures == 1
    before, after = _climb_observations(21000)
    action, status = climb, "timeout"
    if interruption == "success":
        status = "success"
    elif interruption == "progress":
        after["pose"]["local_ned_m"][2] = -1.3  # Exactly 0.5 m is meaningful progress.
    elif interruption in {"high_before", "high_after"}:
        (before if interruption == "high_before" else after)["pose"]["local_ned_m"][2] = -10
    elif interruption in {"landed_before", "landed_after"}:
        (before if interruption == "landed_before" else after)["landed"] = True
    elif interruption in {"landing", "stale_landed_boolean"}:
        after["flight_state"]["landed_state"] = 4 if interruption == "landing" else 1
    elif interruption == "unknown_airborne":
        after.pop("flight_state")
        after.pop("landed")
    elif interruption == "hold":
        action = ActionPrimitive.hold(1)
    elif interruption == "land":
        action = ActionPrimitive.land()
    elif interruption == "horizontal":
        action = ActionPrimitive.translate(0.1, 0, 15)
    elif interruption == "yaw":
        action = ActionPrimitive("translate", up_m=15, yaw_delta_deg=1)
    elif interruption == "descent":
        action = ActionPrimitive.translate(0, 0, -1)
    elif interruption == "same_boot":
        after["pose_time_boot_ms"] = before["pose_time_boot_ms"]
    elif interruption == "rollback_within":
        after["pose_time_boot_ms"] = 20000
    elif interruption == "rollback_between":
        before["pose_time_boot_ms"] = 20000  # Internally advancing, older than prior after.
    elif interruption == "duplicate":
        before, after = _climb_observations(1000)
    elif interruption == "missing_boot":
        after.pop("pose_time_boot_ms")
    elif interruption == "nan_boot":
        after["pose_time_boot_ms"] = float("nan")
    elif interruption == "boolean_boot":
        before["pose_time_boot_ms"] = True
    elif interruption == "nan_altitude":
        after["pose"]["local_ned_m"][2] = float("nan")
    assert not feed(action, status, before, after)
    assert executor._return_climb_failures == 0
    assert not feed(climb, "timeout", *_climb_observations(61000))
    assert not executor.return_climb_failure_suspected
    assert feed(climb, "timeout", *_climb_observations(81000))


def test_climb_failure_stops_and_preserves_both_health_samples():
    executor = _climb_executor()
    executor.home, executor.cruise_altitude_m = (0, 0), 15.8
    for index, boot in enumerate((1000, 21000)):
        before, after = _climb_observations(boot)
        if index == 0:
            after["attitude"] = {"roll_deg": 171, "pitch_deg": 0}
            after["energy_telemetry"] = [{"message": {
                "mavpackettype": "SYS_STATUS", "onboard_control_sensors_present": 3,
                "onboard_control_sensors_health": 1,
            }}]
        action, _ = executor.next_action(pose_ned=(0, 0, -0.8), yaw_deg=0)
        assert action == ActionPrimitive.translate(0, 0, 15)
        assert executor.report_primitive_outcome(action, "timeout", before=before, after=after) == (index == 1)
    assert executor.done
    assert executor.next_action(pose_ned=(0, 0, -0.8), yaw_deg=0)[0].kind == "stop"
    event = executor.mission_log[-1]
    assert event["attitude_bad"] and event["sensor_bad"]
    assert [sample["attitude_bad"] for sample in event["samples"]] == [True, False]
    assert [sample["gain_m"] for sample in event["samples"]] == [0, 0]
    assert not executor.report_primitive_outcome(action, "timeout", before=before, after=after)
    assert len(executor.history) == 1


@pytest.mark.parametrize("skill,params", [("hover", {"duration_s": 10}), ("land", {})])
def test_climb_outcome_outside_return_home_never_counts(skill, params):
    executor = _climb_executor(skill) if skill == "land" else _executor(skill, params)
    climb = ActionPrimitive.translate(0, 0, 15)
    for boot in (1000, 21000, 41000):
        before, after = _climb_observations(boot)
        assert not executor.report_primitive_outcome(climb, "timeout", before=before, after=after)
    assert executor._return_climb_failures == 0


@pytest.mark.parametrize("confirmed", [0, 2])
def test_count_budget_reports_successful_lower_bound_even_with_zero_detections(confirmed):
    executor = _executor(
        "count", {"region": "zone:all", "query": "car"},
        config=ExecutorConfig(step_budget=2),
    )
    flight = Flight()
    executor.active_loop.confirmations.extend({"track_id": str(i)} for i in range(confirmed))
    for _ in range(3):
        flight.tick(executor)
    assert executor.history[0] == {
        "step": "s", "skill": "count", "state": "succeeded",
        "reason": "budget_exhausted", "count_is_lower_bound": True,
    }
    assert executor.report().count == confirmed


def test_count_search_completion_is_distinct_from_budget_exhaustion():
    executor = _executor("count", {"region": "zone:all", "query": "car"})
    executor.active_loop.state = "done"
    Flight().tick(executor)
    assert executor.history[0]["reason"] == "search_exhausted"
    # A search that ended without a reported full pass (coverage_complete) can
    # have missed objects: the count is a lower bound, as the summary says.
    assert executor.history[0]["count_is_lower_bound"] is True
