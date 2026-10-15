from __future__ import annotations

import itertools
import math
from pathlib import Path

import pytest

from multidetect.autonomy.mission_executor import (
    ExecutorConfig,
    MissionExecutor,
    plan_for_find_and_inspect,
    plan_for_find_task,
    task_text_for,
)
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop, Track
from multidetect.sim_teacher.primitives import ActionPrimitive

ROOT = Path(__file__).resolve().parents[1]
CAMERA = CameraModel(fx=540.0, fy=540.0, cx=640.0, cy=480.0, width=1280, height=960)


def _factory(blocked=lambda pose, n, e: False, tasks=None, regions=None):
    def make(task_text: str, region=None, **overrides) -> OnboardLoop:
        if tasks is not None:
            tasks.append(task_text)
        if region is not None and regions is not None:
            regions.append(region)
        return OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
            segment_clear=lambda pose, n, e: not blocked(pose, n, e),
            config=LoopConfig(**overrides),
        )

    return make


class Sim:
    """Kinematic stand-in: applies primitives to a pose (north, east, down) and yaw."""

    def __init__(self, north=0.0, east=0.0, altitude=20.0, yaw=0.0):
        self.pose = [north, east, -altitude]
        self.yaw = yaw

    def apply(self, action: ActionPrimitive) -> None:
        if action.kind == "turn":
            self.yaw = (self.yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
        elif action.kind == "translate":
            yaw = math.radians(self.yaw)
            self.pose[0] += action.forward_m * math.cos(yaw) - action.right_m * math.sin(yaw)
            self.pose[1] += action.forward_m * math.sin(yaw) + action.right_m * math.cos(yaw)
            self.pose[2] -= action.up_m

    @property
    def ned(self):
        return tuple(self.pose)


def _box(cx, cy, side):
    return (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)


def _cycle(executor, sim, detections=(), ground=None, verdicts=None, frame="f"):
    tracks = executor.observe(detections, pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=ground)
    for track in tracks:
        if verdicts is not None:
            executor.apply_verdict(track.track_id, verdicts(track))
    action, info = executor.next_action(
        pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=ground, frame_id=frame
    )
    sim.apply(action)
    return action, info


def test_find_plan_searches_confirms_returns_home_and_stops():
    tasks = []
    executor = MissionExecutor(plan_for_find_task("red car"), loop_factory=_factory(tasks=tasks))
    assert task_text_for(executor.plan.steps[0]) == "Find the red car and record its position."
    sim = Sim()
    target = (30.0, 0.0)
    seen = [Detection(_box(640, 700, 40), 0.5)]
    actions = []
    for index in range(40):
        action, info = _cycle(
            executor,
            sim,
            seen,
            ground=lambda u, v: target,
            verdicts=lambda t: "confirm_target",
            frame=f"frame-{index}",
        )
        actions.append((action.kind, info.get("step")))
        if executor.done:
            break
    assert executor.done and actions[-1][0] == "stop"
    assert executor.findings[0]["position_ne"] == target
    assert [h["state"] for h in executor.history][:2] == ["succeeded", "succeeded"]
    assert math.hypot(sim.pose[0], sim.pose[1]) <= 3.0
    report = executor.report()
    assert report.findings == ("red car",) and report.positions == ("30.0,0.0",)
    assert "Find the red car and record its position." in tasks


def test_search_budget_failure_follows_on_fail():
    executor = MissionExecutor(
        plan_for_find_task("ambulance"),
        loop_factory=_factory(),
        config=ExecutorConfig(step_budget=5),
    )
    sim = Sim()
    for _ in range(30):
        _cycle(executor, sim)
        if executor.done:
            break
    assert executor.history[0] == {
        "step": "search",
        "skill": "area_search",
        "state": "failed",
        "reason": "step_budget",
    }
    assert executor.history[1]["skill"] == "return_home" and executor.done
    assert executor.report().alerts == ("search:step_budget",)


def _count_plan(max_count):
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "count cones",
            "steps": [
                {
                    "id": "count",
                    "skill": "count",
                    "params": {
                        "region": "zone:lot",
                        "query": "traffic cone",
                        "max_count": max_count,
                    },
                    "on_success": "home",
                    "on_fail": "home",
                },
                {
                    "id": "home",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["count"],
        }
    )


def test_count_keeps_searching_and_dedupes_by_ground_position():
    executor = MissionExecutor(_count_plan(2), loop_factory=_factory())
    sim = Sim()
    # Three confirmations: object A, object A again (new track id, 2 m away), object B.
    script = [(10.0, 0.0), (10.0, 2.0), (40.0, -20.0)]
    counted = []
    for position in script:
        for _ in range(4):  # two confirm views are needed per object
            box = [Detection(_box(640, 700, 40), 0.5)]
            _action, info = _cycle(
                executor,
                sim,
                box,
                ground=lambda u, v, p=position: p,
                verdicts=lambda t: "confirm_target",
            )
            if "counted" in info or "duplicate_of_counted" in info:
                counted.append("counted" if "counted" in info else "duplicate")
                break
        # The object leaves the view before the next one appears.
        for _ in range(6):
            _cycle(executor, sim)
            if executor._run is None or executor._run.step.skill != "count":
                break
    assert counted == ["counted", "duplicate", "counted"]
    assert executor.counts == {"count": 2}
    assert executor.history[0]["reason"] == "max_count"
    assert executor.report().count == 2 and len(executor.findings) == 2


def test_count_default_loop_behaviour_is_unchanged():
    loop = _factory()("Find the cone and record its position.")
    assert loop.config.stop_on_confirm is True and loop.confirmations == []


def test_inspect_orbits_the_finding_skips_blocked_views_and_records_frames():
    centre = (40.0, 0.0)

    def blocked(pose, n, e):
        # Anything ending east of +9 m is behind a wall: the east viewpoint is unreachable.
        return e > 9.0

    executor = MissionExecutor(
        plan_for_find_and_inspect("stop sign", views=4, standoff_m=12.0),
        loop_factory=_factory(blocked=blocked),
    )
    executor.findings.append(
        {
            "step": "pre",
            "query": "stop sign",
            "position_ne": centre,
            "track_id": "t9",
            "rule": "two_views",
            "frame_id": None,
        }
    )
    executor._run = executor._start(executor._steps["inspect"])
    sim = Sim(north=10.0, east=0.0, altitude=20.0)
    asked = []
    lowest = math.inf
    for index in range(200):
        dets = [Detection(_box(640, 700, 30), 0.5)]
        tracks = executor.observe(
            dets, pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=lambda u, v: centre
        )
        for track in tracks:
            asked.append(track.track_id)
            executor.apply_verdict(track.track_id, "confirm_target")
        action, info = executor.next_action(
            pose_ned=sim.ned,
            yaw_deg=sim.yaw,
            ground_point=lambda u, v: centre,
            frame_id=f"frame-{index}",
        )
        if executor._run is not None and executor._run.step.skill == "inspect":
            lowest = min(lowest, -sim.pose[2])
        sim.apply(action)
        if executor._run is None or executor._run.step.skill != "inspect":
            break
    inspect = executor.history[0]
    assert inspect["skill"] == "inspect" and inspect["state"] == "succeeded"
    assert any("blocked" in alert for alert in executor.alerts)
    assert len([a for a in executor.answers if a.startswith("inspect:view")]) == 3
    assert len(executor.evidence) == 3 and asked
    # The orbit flew low enough to keep the centre in view (8 m floor here).
    assert abs(lowest - 8.0) < 1.5


def test_goto_poi_and_unknown_ref_and_hover_duration():
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "go, wait, land",
            "steps": [
                {
                    "id": "a",
                    "skill": "goto",
                    "params": {"target_ref": "poi:gate"},
                    "on_success": "b",
                    "on_fail": "z",
                },
                {
                    "id": "b",
                    "skill": "goto",
                    "params": {"target_ref": "poi:nowhere"},
                    "on_success": "z",
                    "on_fail": "c",
                },
                {
                    "id": "c",
                    "skill": "hover",
                    "params": {"duration_s": 12},
                    "on_success": "z",
                    "on_fail": "z",
                },
                {"id": "z", "skill": "land", "params": {}, "on_success": None, "on_fail": None},
            ],
            "report": [],
        }
    )
    executor = MissionExecutor(plan, loop_factory=_factory(), refs={"poi:gate": (0.0, 40.0)})
    sim = Sim()
    holds = 0.0
    last = None
    for _ in range(60):
        action, _info = _cycle(executor, sim)
        if action.kind == "hold":
            holds += action.seconds
        last = action
        if executor.done:
            break
    assert [(h["step"], h["state"], h["reason"]) for h in executor.history] == [
        ("a", "succeeded", "arrived"),
        ("b", "failed", "unknown_ref"),
        ("c", "succeeded", "duration_elapsed"),
        ("z", "succeeded", "landed"),
    ]
    assert math.hypot(sim.pose[0], sim.pose[1] - 40.0) <= 3.0
    assert holds == 12.0 and last.kind == "stop"


def _skill_plan(skill, params):
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": skill,
            "steps": [
                {
                    "id": "s",
                    "skill": skill,
                    "params": params,
                    "on_success": "end",
                    "on_fail": "end",
                },
                {
                    "id": "end",
                    "skill": "land",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["findings", "positions"],
        }
    )


def test_survey_covers_circle_in_lanes_and_reports_evidence():
    executor = MissionExecutor(
        _skill_plan("survey", {"region": "zone:a", "overlap": 0.7, "altitude_m": 20}),
        loop_factory=_factory(),
        refs={"zone:a": (0.0, 0.0, 30.0)},
    )
    run = executor._run
    sim = Sim(altitude=20.0)
    for index in range(200):
        _cycle(executor, sim, frame=f"survey-{index}")
        if executor.history:
            break
    assert run is not None
    assert len(run.waypoints) >= 6
    assert len({round(point[1], 1) for point in run.waypoints}) >= 3
    assert run.visited == len(run.waypoints)
    assert executor.coverage == {"s": 1.0}
    assert len(executor.evidence) == len(run.waypoints)
    assert executor.history[0]["reason"] == "region_covered"
    assert "s:coverage:1.00" in executor.report().state_answers


@pytest.mark.parametrize("min_depression", [0.35, 0.5])
def test_survey_spacing_uses_reliable_ground_footprint_and_scales_with_altitude(min_depression):
    assert ExecutorConfig().survey_min_depression_rad == 0.35
    radius = 100.0
    overlap = 0.7
    spacings = []
    for altitude in (20.0, 10.0):
        executor = MissionExecutor(
            _skill_plan("survey", {"region": "zone:a", "overlap": overlap, "altitude_m": altitude}),
            loop_factory=_factory(),
            refs={"zone:a": (0.0, 0.0, radius)},
            config=ExecutorConfig(survey_min_depression_rad=min_depression),
        )
        run = executor._run
        _cycle(executor, Sim(altitude=altitude))
        half_vfov = math.atan(CAMERA.height / (2.0 * CAMERA.fy))
        near_range = altitude / math.tan(CAMERA.pitch_down_rad + half_vfov)
        far_range = altitude / math.tan(max(min_depression, CAMERA.pitch_down_rad - half_vfov))
        slant_mid = math.hypot(altitude, (near_range + far_range) / 2.0)
        expected_spacing = max(3.0, min(60.0, slant_mid * CAMERA.width / CAMERA.fx * (1 - overlap)))
        expected_lanes = max(3, math.ceil(2.0 * radius / expected_spacing))
        assert len(run.waypoints) == 2 * expected_lanes
        lane_east = [point[1] for point in run.waypoints[::2]]
        spacing = lane_east[1] - lane_east[0]
        # The executor rounds up the lane count and then divides the region equally.
        assert spacing == pytest.approx(2.0 * radius / expected_lanes)
        assert all(b - a == pytest.approx(spacing) for a, b in itertools.pairwise(lane_east))
        assert 3.0 < spacing <= expected_spacing < 35.0
        spacings.append(spacing)
    assert spacings[1] < spacings[0]


def test_survey_skips_blocked_waypoints_and_reports_partial_coverage():
    executor = MissionExecutor(
        _skill_plan("survey", {"region": "zone:a", "overlap": 0.7}),
        loop_factory=_factory(blocked=lambda pose, n, e: e > 5.0),
        refs={"zone:a": (0.0, 0.0, 30.0)},
    )
    run = executor._run
    sim = Sim(altitude=20.0)
    for index in range(250):
        _cycle(executor, sim, frame=f"partial-{index}")
        if executor.history:
            break
    assert run is not None
    assert executor.history[0]["reason"] == "region_covered"
    assert 0.0 < executor.coverage["s"] < 1.0
    assert run.visited < len(run.waypoints)
    assert len(executor.evidence) == run.visited
    assert any(
        alert.startswith("s:waypoint") and alert.endswith("_blocked") for alert in executor.alerts
    )
    assert f"s:coverage:{executor.coverage['s']:.2f}" in executor.report().state_answers


def test_survey_needs_a_bounded_zone_and_at_least_one_reachable_waypoint():
    for refs, blocked, reason in [
        ({}, lambda pose, n, e: False, "unknown_ref"),
        ({"zone:a": (0.0, 0.0)}, lambda pose, n, e: False, "unknown_ref"),
        ({"zone:a": (0.0, 0.0, 30.0)}, lambda pose, n, e: True, "no_waypoint_reachable"),
    ]:
        executor = MissionExecutor(
            _skill_plan("survey", {"region": "zone:a", "overlap": 0.7}),
            loop_factory=_factory(blocked=blocked),
            refs=refs,
        )
        sim = Sim()
        for _ in range(100):
            _cycle(executor, sim)
            if executor.history:
                break
        assert executor.history[0]["state"] == "failed"
        assert executor.history[0]["reason"] == reason


def test_patrol_visits_route_twice_and_alerts_once_per_position():
    route = [(0.0, 10.0), (10.0, 10.0), (10.0, 0.0)]
    executor = MissionExecutor(
        _skill_plan("patrol", {"route_ref": "route:a", "query": "smoke", "loops": 2}),
        loop_factory=_factory(),
        refs={"route:a": route},
    )
    assert executor.route_of("route:a") == route
    assert executor.resolve("route:a") is None
    run = executor._run
    sim = Sim()
    arrivals = []
    for _ in range(100):
        before = sim.ned[:2]
        visited = run.visited
        _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: (8.0, 8.0),
            verdicts=lambda t: "approach_track",
        )
        if run.visited > visited:
            arrivals.append(before)
        if executor.history:
            break
    assert run.visited == 6
    assert len(arrivals) == 6
    assert all(math.dist(point, route[index % 3]) <= 3.0 for index, point in enumerate(arrivals))
    assert executor.history[0]["reason"] == "duration_elapsed"
    assert executor.alerts == ["s:alert:smoke@8.0,8.0"]
    assert len(executor.findings) == 1
    assert executor.report().positions == ("8.0,8.0",)


def test_patrol_unknown_route_fails_and_duration_can_end_early():
    unknown = MissionExecutor(
        _skill_plan("patrol", {"route_ref": "route:missing", "query": "car", "loops": 2}),
        loop_factory=_factory(),
    )
    _cycle(unknown, Sim())
    assert unknown.history[0]["reason"] == "unknown_ref"

    timed = MissionExecutor(
        _skill_plan(
            "patrol",
            {"route_ref": "route:a", "query": "car", "loops": 100, "duration_s": 3.0},
        ),
        loop_factory=_factory(),
        refs={"route:a": [(0.0, 30.0), (30.0, 30.0)]},
    )
    sim = Sim()
    run = timed._run
    for _ in range(30):
        _cycle(timed, sim)
        if timed.history:
            break
    assert timed.history[0]["reason"] == "duration_elapsed"
    assert run.elapsed_s >= 3.0 and run.visited < 200


def test_monitor_scans_with_holds_and_turns_then_completes():
    executor = MissionExecutor(
        _skill_plan("monitor", {"region": "zone:look", "query": "truck", "duration_s": 7.0}),
        loop_factory=_factory(),
        refs={"zone:look": (0.0, 10.0, 12.0)},
    )
    sim = Sim()
    scan = []
    for _ in range(40):
        action, info = _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: (3.0, 8.0),
            verdicts=lambda t: "confirm_target",
        )
        if info.get("phase") in {"hold", "turn"}:
            scan.append(action)
            assert math.dist(sim.ned[:2], (0.0, 10.0)) <= 12.0
        if executor.history:
            break
    assert {action.kind for action in scan} == {"hold", "turn"}
    assert sum(action.seconds for action in scan if action.kind == "hold") < 7.0
    assert all(action.yaw_delta_deg == 90.0 for action in scan if action.kind == "turn")
    assert 0 < executor.coverage["s"] < 1
    assert executor.history[0]["reason"] == "duration_elapsed"
    assert executor.alerts == ["s:alert:truck@3.0,8.0"]
    assert len(executor.findings) == 1


def test_monitor_accepts_point_ref_and_dedupes_new_tracks_within_four_metres():
    executor = MissionExecutor(
        _skill_plan(
            "monitor",
            {"region": "poi:watch", "query": "truck", "duration_s": 30.0, "altitude_m": 25.0},
        ),
        loop_factory=_factory(),
        refs={"poi:watch": (0.0, 0.0)},
    )
    sim = Sim(altitude=20.0)
    box = [Detection(_box(640, 700, 40), 0.5)]
    action, _ = _cycle(
        executor,
        sim,
        box,
        ground=lambda u, v: (0.0, 0.0),
        verdicts=lambda t: "confirm_target",
    )
    assert action.kind == "translate" and action.up_m == 5.0
    for position in [(0.0, 0.0), (3.0, 0.0), (10.0, 0.0)]:
        if position != (0.0, 0.0):
            for _ in range(6):
                _cycle(executor, sim)
        _cycle(
            executor,
            sim,
            box,
            ground=lambda u, v, p=position: p,
            verdicts=lambda t: "confirm_target",
        )
    assert executor.alerts == ["s:alert:truck@0.0,0.0", "s:alert:truck@10.0,0.0"]
    assert len(executor.findings) == 2


def _alert_vote_fixture(config=None):
    executor = MissionExecutor(
        _skill_plan("patrol", {"route_ref": "route:a", "query": "backpack", "loops": 1}),
        loop_factory=_factory(),
        refs={"route:a": [(0.0, 10.0)]},
        config=config,
    )
    sim = Sim()
    tracks = executor.observe(
        [Detection(_box(640, 700, 40), 0.5)],
        pose_ned=sim.ned,
        yaw_deg=sim.yaw,
        ground_point=lambda _u, _v: (8.0, 8.0),
    )
    assert tracks
    return executor, tracks[0].track_id


def test_patrol_alert_defaults_to_first_positive_vote():
    executor, track_id = _alert_vote_fixture()
    executor.apply_verdict(track_id, "approach_track")
    assert executor.alerts == ["s:alert:backpack@8.0,8.0"]


def test_patrol_alert_minimum_two_identity_votes():
    executor, track_id = _alert_vote_fixture(ExecutorConfig(alert_min_identity_matches=2))
    executor.apply_verdict(track_id, "approach_track")
    assert executor.alerts == []
    executor.apply_verdict(track_id, "confirm_target")
    assert executor.alerts == ["s:alert:backpack@8.0,8.0"]


def test_patrol_alert_pools_fragmented_track_ids_by_ground_position():
    executor, track_id = _alert_vote_fixture(ExecutorConfig(alert_min_identity_matches=2))
    executor.apply_verdict(track_id, "approach_track")
    loop = executor.active_loop
    assert loop is not None
    loop._tracks["t2"] = Track("t2", _box(640, 700, 40), 0, 0, ground=(9.0, 8.0))
    executor.apply_verdict("t2", "confirm_target")
    assert executor.alerts == ["s:alert:backpack@8.5,8.0"]
    assert executor.alert_vote_clusters[0]["positive"] == 2


def test_patrol_alert_majority_blocks_tied_votes():
    executor, track_id = _alert_vote_fixture(
        ExecutorConfig(alert_require_majority=True)
    )
    executor.apply_verdict(track_id, "reject_track")
    executor.apply_verdict(track_id, "approach_track")
    assert executor.alerts == []


def test_patrol_alert_checks_late_threshold_on_each_vote():
    executor, track_id = _alert_vote_fixture(ExecutorConfig(alert_min_identity_matches=3))
    executor.apply_verdict(track_id, "approach_track")
    executor.apply_verdict(track_id, "approach_track")
    assert executor.alerts == []
    executor.apply_verdict(track_id, "confirm_target")
    assert executor.alerts == ["s:alert:backpack@8.0,8.0"]


def _search_follow_plan(duration_s=None):
    follow_params = {"track_ref": "track:1", "query": "car"}
    if duration_s is not None:
        follow_params["duration_s"] = duration_s
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "find and follow car",
            "steps": [
                {
                    "id": "search",
                    "skill": "area_search",
                    "params": {"region": "zone:all", "query": "car"},
                    "on_success": "follow",
                    "on_fail": "end",
                },
                {
                    "id": "follow",
                    "skill": "follow",
                    "params": follow_params,
                    "on_success": "end",
                    "on_fail": "end",
                },
                {
                    "id": "end",
                    "skill": "land",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["positions"],
        }
    )


def _search_intermediate_follow_plan(skill, params):
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "search then follow",
            "steps": [
                {
                    "id": "search",
                    "skill": "area_search",
                    "params": {"region": "zone:all", "query": "car"},
                    "on_success": "middle",
                    "on_fail": "end",
                },
                {
                    "id": "middle",
                    "skill": skill,
                    "params": params,
                    "on_success": "follow",
                    "on_fail": "end",
                },
                {
                    "id": "follow",
                    "skill": "follow",
                    "params": {"track_ref": "track:1", "query": "car"},
                    "on_success": "end",
                    "on_fail": "end",
                },
                {
                    "id": "end",
                    "skill": "land",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["positions"],
        }
    )


def _start_follow(executor, sim, target):
    search_loop = executor.active_loop
    for _ in range(30):
        _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: tuple(target),
            verdicts=lambda t: "confirm_target",
        )
        if executor.history:
            break
    assert executor.history[0]["reason"] == "confirmed"
    assert executor.active_loop is search_loop
    assert executor._run.follow_track_id == executor.findings[0]["track_id"]
    return executor._run


def test_follow_reuses_search_track_keeps_standoff_and_reports_last_position():
    executor = MissionExecutor(_search_follow_plan(), loop_factory=_factory())
    sim = Sim()
    target = [50.0, 0.0]
    run = _start_follow(executor, sim, target)
    moved = 0
    for _ in range(18):
        target[0] += 2.0
        action, info = _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: tuple(target),
            verdicts=lambda t: "confirm_target",
        )
        assert info["step"] == "follow"
        if action.kind == "translate" and action.up_m == 0:
            moved += 1
            assert math.dist(sim.ned[:2], target) >= info["standoff_m"] - 3.0
    assert moved > 0 and run.follow_finding is not None
    assert run.follow_finding["position_ne"] == tuple(target)
    assert run.follow_finding["step"] == "follow"
    for _ in range(6):
        _cycle(executor, sim)
    assert run.lost_cycles == 6 and len(executor.history) == 1
    target[0] += 2.0
    _cycle(
        executor,
        sim,
        [Detection(_box(640, 700, 40), 0.5)],
        ground=lambda u, v: tuple(target),
        verdicts=lambda t: "confirm_target",
    )
    assert run.lost_cycles == 0
    assert executor.current_tracks[0].track_id == run.follow_track_id
    for _ in range(7):
        _cycle(executor, sim)
    assert executor.history[1]["state"] == "succeeded"
    assert executor.history[1]["reason"] == "target_lost_timeout"
    assert executor.report().positions[-1] == f"{target[0]:.1f},{target[1]:.1f}"


def test_follow_duration_and_fresh_loop_acquisition():
    executor = MissionExecutor(_search_follow_plan(duration_s=5.0), loop_factory=_factory())
    sim = Sim()
    target = [50.0, 0.0]
    _start_follow(executor, sim, target)
    for _ in range(30):
        target[0] += 2.0
        _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: tuple(target),
            verdicts=lambda t: "confirm_target",
        )
        if len(executor.history) > 1:
            break
    assert executor.history[1]["reason"] == "duration_elapsed"
    assert executor.findings[-1]["position_ne"] == tuple(target)

    fresh = MissionExecutor(
        _skill_plan("follow", {"track_ref": "track:1", "query": "car", "duration_s": 2.0}),
        loop_factory=_factory(),
    )
    sim = Sim()
    for _ in range(20):
        _cycle(
            fresh,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: (30.0, 0.0),
            verdicts=lambda t: "approach_track",
        )
        if fresh.history:
            break
    assert fresh.history[0]["reason"] == "duration_elapsed"
    assert fresh.findings[0]["position_ne"] == (30.0, 0.0)


@pytest.mark.parametrize("lost_cycles", [0, 6])
def test_follow_budget_succeeds_after_tracking_a_recent_target(lost_cycles):
    executor = MissionExecutor(
        _skill_plan("follow", {"track_ref": "track:1", "query": "car"}),
        loop_factory=_factory(),
        config=ExecutorConfig(travel_budget=1 + lost_cycles),
    )
    sim = Sim()
    run = executor._run
    _cycle(
        executor,
        sim,
        [Detection(_box(640, 700, 40), 0.5)],
        ground=lambda u, v: (30.0, 0.0),
        verdicts=lambda t: "approach_track",
    )
    for _ in range(lost_cycles):
        _cycle(executor, sim)
    assert run.follow_finding is not None
    assert run.lost_cycles == lost_cycles
    assert executor.history == []
    executor.next_action(pose_ned=sim.ned, yaw_deg=sim.yaw)
    assert executor.history[0] == {
        "step": "s",
        "skill": "follow",
        "state": "succeeded",
        "reason": "duration_elapsed",
    }
    assert "s:step_budget" not in executor.alerts


def test_follow_budget_without_a_tracked_target_still_fails():
    executor = MissionExecutor(
        _skill_plan("follow", {"track_ref": "track:1", "query": "car"}),
        loop_factory=_factory(),
        config=ExecutorConfig(travel_budget=2),
    )
    sim = Sim()
    for _ in range(3):
        _cycle(executor, sim)
    assert executor.history[0]["state"] == "failed"
    assert executor.history[0]["reason"] == "step_budget"
    assert executor.findings == []


def test_follow_without_an_acquired_target_fails_after_seven_cycles():
    executor = MissionExecutor(
        _skill_plan("follow", {"track_ref": "track:1", "query": "car"}),
        loop_factory=_factory(),
    )
    sim = Sim()
    for _ in range(7):
        _cycle(executor, sim)
    assert executor.history[0]["state"] == "failed"
    assert executor.history[0]["reason"] == "target_not_found"
    assert executor.findings == []


def test_follow_starts_fresh_after_a_non_perception_step():
    executor = MissionExecutor(
        _search_intermediate_follow_plan("goto", {"target_ref": "home"}),
        loop_factory=_factory(),
    )
    sim = Sim()
    target = [50.0, 0.0]
    search_loop = executor.active_loop
    for _ in range(30):
        _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: tuple(target),
            verdicts=lambda t: "confirm_target",
        )
        if executor.history:
            break
    assert executor.history[0]["reason"] == "confirmed"
    for _ in range(30):
        _cycle(executor, sim)
        if len(executor.history) > 1:
            break
    assert executor.history[1]["reason"] == "arrived"
    assert executor.active_loop is not search_loop
    assert executor._run.acquire_track
    _cycle(
        executor,
        sim,
        [Detection(_box(640, 700, 40), 0.5)],
        ground=lambda u, v: tuple(target),
        verdicts=lambda t: "approach_track",
    )
    assert executor._run.follow_track_id == executor.current_tracks[0].track_id


def test_follow_does_not_reuse_another_steps_same_named_track():
    executor = MissionExecutor(
        _search_intermediate_follow_plan(
            "monitor", {"region": "poi:watch", "query": "truck", "duration_s": 2.0}
        ),
        loop_factory=_factory(),
        refs={"poi:watch": (0.0, 0.0)},
    )
    sim = Sim()
    target = [50.0, 0.0]
    for _ in range(30):
        _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: tuple(target),
            verdicts=lambda t: "confirm_target",
        )
        if executor.history:
            break
    assert executor.history[0]["reason"] == "confirmed"
    monitor_loop = executor.active_loop
    for _ in range(30):
        _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, 40), 0.5)],
            ground=lambda u, v: (100.0, 0.0),
            verdicts=lambda t: "confirm_target",
        )
        if len(executor.history) > 1:
            break
    assert executor.history[1]["reason"] == "duration_elapsed"
    assert executor.findings[0]["track_id"] == "t1"
    assert executor.findings[1]["track_id"] == "t1"
    assert executor.active_loop is not monitor_loop
    assert executor._run.acquire_track


def test_unimplemented_skill_fails_to_on_fail():
    raw = {
        "plan_version": 1,
        "goal": "deliver",
        "steps": [
            {
                "id": "s",
                "skill": "deliver",
                "params": {"target_ref": "poi:a", "payload_ref": "payload:a"},
                "on_success": "h",
                "on_fail": "h",
            },
            {
                "id": "h",
                "skill": "return_home",
                "params": {},
                "on_success": None,
                "on_fail": None,
            },
        ],
        "report": [],
    }
    plan = validate_mission_plan(raw, capabilities=frozenset({"payload"}))
    executor = MissionExecutor(plan, loop_factory=_factory())
    sim = Sim()
    _cycle(executor, sim)
    assert executor.history[0]["reason"] == "skill_not_implemented"


def test_executor_never_reads_the_answer_key():
    source = (ROOT / "src/multidetect/autonomy/mission_executor.py").read_text(encoding="utf-8")
    for word in ("truth", "target_true", "scenario", "true_target"):
        assert word not in source


def test_count_is_reported_while_the_step_runs():
    executor = MissionExecutor(_count_plan(5), loop_factory=_factory())
    sim = Sim()
    _cycle(executor, sim)
    assert executor.counts == {"count": 0} and executor.report().count == 0


def test_search_runs_to_the_executor_budget_not_the_loop_cap():
    executor = MissionExecutor(
        plan_for_find_task("cone"), loop_factory=_factory(), config=ExecutorConfig(step_budget=130)
    )
    sim = Sim()
    kinds = []
    for _ in range(140):
        action, info = _cycle(executor, sim)
        kinds.append((action.kind, info.get("step")))
        if executor._run is None or executor._run.step.id != "search":
            break
    # 130 search primitives (beyond OnboardLoop's default 120-step cap), then home.
    assert sum(1 for kind, step in kinds if step == "search") == 130
    assert all(kind != "stop" for kind, step in kinds if step == "search")
    assert executor.history[0]["reason"] == "step_budget"


def test_a_zone_with_a_radius_bounds_the_search_step():
    regions = []
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "count",
            "steps": [
                {
                    "id": "c",
                    "skill": "count",
                    "params": {"region": "zone:lot", "query": "cone"},
                    "on_success": "h",
                    "on_fail": "h",
                },
                {
                    "id": "h",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": [],
        }
    )
    executor = MissionExecutor(
        plan, loop_factory=_factory(regions=regions), refs={"zone:lot": (10.0, -5.0, 40.0)}
    )
    assert regions == [(10.0, -5.0, 40.0)]
    assert executor.resolve("zone:lot") == (10.0, -5.0)
