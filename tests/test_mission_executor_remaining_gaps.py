"""Executor regressions using observations and kinematics only; no simulator/model."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
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
