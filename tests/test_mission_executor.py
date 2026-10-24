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
    make = _factory()

    def without_reid(task_text, **overrides):
        return make(task_text, **{**overrides, "ground_reid_max_steps": 0})

    # Dedupe is still needed when re-id is disabled or cannot recover an id.
    executor = MissionExecutor(
        _count_plan(2), loop_factory=without_reid, refs={"zone:lot": (0.0, 0.0, 100.0)}
    )
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
    # The 2 m re-observation is outside the small cone footprint radius; it is
    # a distinct object under footprint-based count dedupe.
    assert counted == ["counted", "counted"]
    assert executor.counts == {"count": 2}
    assert executor.history[0]["reason"] == "max_count"
    assert executor.report().count == 2 and len(executor.findings) == 2


def test_count_does_not_reassociate_exhausted_identity_after_it_leaves_view():
    executor = MissionExecutor(
        _count_plan(2), loop_factory=_factory(), refs={"zone:lot": (0.0, 0.0, 100.0)}
    )
    sim = Sim()
    box = [Detection(_box(640, 700, 40), 0.5)]
    for _ in range(4):
        _, info = _cycle(
            executor, sim, box, ground=lambda u, v: (10.0, 0.0),
            verdicts=lambda t: "confirm_target",
        )
        if "counted" in info:
            break
    track = executor.current_tracks[0]
    assert track.exhausted and executor.counts == {"count": 1}
    for _ in range(8):
        _cycle(executor, sim)
    needing = executor.observe(
        box, pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=lambda u, v: (10.0, 1.0)
    )
    assert executor.current_tracks[0] is not track
    assert needing == [executor.current_tracks[0]]
    _, info = executor.next_action(pose_ned=sim.ned, yaw_deg=sim.yaw)
    assert "counted" not in info and "duplicate_of_counted" not in info
    assert executor.counts == {"count": 1}


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
                "step": "search",
            "query": "stop sign",
            "position_ne": centre,
            "track_id": "t9",
            "rule": "two_views",
            "frame_id": None,
        }
    )
    executor._run = executor._start(executor._steps["inspect"])
    executor._successful_step_ids.append("search")
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
        ("z", "succeeded", "landing_commanded_unconfirmed"),
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


def test_runtime_no_fly_guard_rejects_dynamic_finding_leg():
    executor = MissionExecutor(
        _skill_plan("goto", {"target_ref": "finding:1"}),
        loop_factory=_factory(),
        no_fly_zones=(((10.0, 0.0), 2.0),),
    )
    executor.findings.append({"step": "s", "position_ne": (20.0, 0.0)})
    executor._successful_step_ids.append("s")
    sim = Sim()
    action, _ = _cycle(executor, sim)
    assert action.kind == "stop"
    assert executor.history[0]["reason"] == "no_fly_zone"
    assert "s:no_fly_zone" in executor.alerts


def test_return_home_routes_around_no_fly_circle():
    executor = MissionExecutor(
        validate_mission_plan({
            "plan_version": 1, "goal": "return", "steps": [{
                "id": "s", "skill": "return_home", "params": {},
                "on_success": None, "on_fail": None,
            }], "report": [],
        }),
        loop_factory=_factory(),
        no_fly_zones=(((10.0, 0.0), 2.0),),
    )
    executor.home = (0.0, 0.0)
    executor.operating_area = {
        "north_min_m": -30.0, "north_max_m": 30.0,
        "east_min_m": -30.0, "east_max_m": 30.0,
    }
    sim = Sim(north=20.0, east=0.0)
    for _ in range(80):
        action, _ = _cycle(executor, sim)
        assert math.dist(sim.pose[:2], (10.0, 0.0)) > 2.0
        if executor.history:
            break
    assert executor.history[0]["reason"] == "arrived"
    assert executor.history[0]["state"] == "succeeded"
    assert "s:no_fly_zone" not in executor.alerts


def test_return_home_lands_in_place_when_no_detour_fits_area():
    executor = MissionExecutor(
        validate_mission_plan({
            "plan_version": 1, "goal": "return", "steps": [{
                "id": "s", "skill": "return_home", "params": {},
                "on_success": None, "on_fail": None,
            }], "report": [],
        }),
        loop_factory=_factory(),
        no_fly_zones=(((10.0, 0.0), 2.0),),
    )
    executor.home = (0.0, 0.0)
    executor.operating_area = {
        "north_min_m": -30.0, "north_max_m": 30.0,
        "east_min_m": -0.5, "east_max_m": 0.5,
    }
    action, _ = _cycle(executor, Sim(north=20.0, east=0.0))
    assert action.kind == "stop"
    assert executor.history[0]["reason"] == "landing_commanded_unconfirmed"
    assert "s:no_fly_zone_land_fallback" in executor.alerts


def test_return_home_visibility_graph_handles_overlapping_no_fly_circles():
    executor = MissionExecutor(
        validate_mission_plan({
            "plan_version": 1, "goal": "return", "steps": [{
                "id": "s", "skill": "return_home", "params": {},
                "on_success": None, "on_fail": None,
            }], "report": [],
        }),
        loop_factory=_factory(),
        no_fly_zones=(((10.0, 0.0), 2.0), ((10.0, 4.0), 2.0)),
    )
    executor.home = (0.0, 0.0)
    executor.operating_area = {
        "north_min_m": -30.0, "north_max_m": 30.0,
        "east_min_m": -30.0, "east_max_m": 30.0,
    }
    run = executor._run
    assert run is not None
    path = executor._return_home_detour((20.0, 0.0), (0.0, 0.0))
    assert path and len(path) >= 2
    assert all(
        math.dist(point, centre) > radius + executor.config.no_fly_margin_m - 1e-6
        for point in path
        for centre, radius in executor.no_fly_zones
    )


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
            ({}, lambda pose, n, e: False, "invalid_region_ref"),
            ({"zone:a": (0.0, 0.0)}, lambda pose, n, e: False, "invalid_region_ref"),
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


def test_monitor_long_duration_is_not_cut_off_by_fixed_step_budget():
    """A 15-minute watch must run until its duration, past 150 primitives."""
    executor = MissionExecutor(
        _skill_plan(
            "monitor",
            {"region": "zone:look", "query": "truck", "duration_s": 900.0},
        ),
        loop_factory=_factory(),
        refs={"zone:look": (0.0, 10.0, 12.0)},
        # Three-second holds keep this regression test reasonably small while
        # still exercising the duration-derived budget (>150 primitives).
        config=ExecutorConfig(hold_s=3.0),
    )
    sim = Sim()
    run = executor._run
    for index in range(700):
        _cycle(executor, sim, frame=f"long-monitor-{index}")
        if executor.history:
            break
    assert executor.history, "monitor should finish at the requested duration"
    assert executor.history[0]["reason"] == "duration_elapsed"
    assert executor.history[0]["reason"] != "step_budget"
    assert run is not None
    assert run.elapsed_s >= 900.0
    assert run.primitives > 150


def test_patrol_large_loop_budget_runs_to_duration_without_step_budget():
    route = [(0.0, 10.0), (10.0, 10.0), (10.0, 0.0)]
    executor = MissionExecutor(
        _skill_plan(
            "patrol",
            {
                "route_ref": "route:a",
                "query": "smoke",
                "loops": 100,
                "duration_s": 900.0,
            },
        ),
        loop_factory=_factory(),
        refs={"route:a": route},
    )
    sim = Sim()
    run = executor._run
    for index in range(900):
        _cycle(executor, sim, frame=f"long-patrol-{index}")
        if executor.history:
            break
    assert executor.history, "patrol should finish at the requested duration"
    assert executor.history[0]["reason"] == "duration_elapsed"
    assert executor.history[0]["reason"] != "step_budget"
    assert run is not None
    assert run.elapsed_s >= 900.0
    assert run.primitives > 150


def test_count_keeps_step_budget_behavior():
    executor = MissionExecutor(
        _count_plan(100),
        loop_factory=_factory(),
        refs={"zone:lot": (0.0, 0.0, 100.0)},
        config=ExecutorConfig(step_budget=3),
    )
    sim = Sim()
    for index in range(20):
        _cycle(executor, sim, frame=f"count-budget-{index}")
        if executor.history:
            break
    assert executor.history[0]["reason"] == "budget_exhausted"


def test_monitor_accepts_point_ref_and_keeps_separate_small_objects_distinct():
    executor = MissionExecutor(
        _skill_plan(
            "monitor",
                {"region": "zone:watch", "query": "truck", "duration_s": 30.0, "altitude_m": 25.0},
        ),
        loop_factory=_factory(),
            refs={"zone:watch": (0.0, 0.0, 100.0)},
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
    assert executor.alerts == [
        "s:alert:truck@0.0,0.0",
        "s:alert:truck@10.0,0.0",
    ]
    assert len(executor.findings) == 2


def test_monitor_keeps_three_metre_co_visible_objects_as_two_alerts():
    executor = MissionExecutor(
        _skill_plan(
            "monitor",
            {"region": "zone:watch", "query": "truck", "duration_s": 30.0, "altitude_m": 25.0},
        ),
        loop_factory=_factory(),
        refs={"zone:watch": (0.0, 0.0, 100.0)},
    )
    sim = Sim(altitude=20.0)
    detections = [
        Detection(_box(620, 700, 40), 0.5),
        Detection(_box(900, 700, 40), 0.5),
    ]
    _cycle(
        executor,
        sim,
        detections,
        ground=lambda u, v: (0.0, 0.0) if u < 800 else (0.0, 3.0),
        verdicts=lambda t: "confirm_target",
    )
    assert executor.alerts == [
        "s:alert:truck@0.0,0.0",
        "s:alert:truck@0.0,3.0",
    ]
    assert len(executor.findings) == 2



def test_monitor_does_not_realert_when_a_co_visible_track_migrates_onto_an_alerted_object():
    executor = MissionExecutor(
        _skill_plan(
            "monitor",
            {"region": "zone:watch", "query": "truck", "duration_s": 30.0, "altitude_m": 25.0},
        ),
        loop_factory=_factory(),
        refs={"zone:watch": (0.0, 0.0, 100.0)},
    )
    sim = Sim(altitude=20.0)
    truck, other = _box(620, 700, 40), _box(900, 700, 40)
    _cycle(
        executor,
        sim,
        [Detection(truck, 0.5), Detection(other, 0.5)],
        ground=lambda u, v: (0.0, 0.0) if u < 800 else (0.0, 8.0),
        verdicts=lambda t: "confirm_target" if t.bbox == truck else "reject_track",
    )
    assert executor.alerts == ["s:alert:truck@0.0,0.0"]
    # The other track re-associates onto the alerted truck (identity switch):
    # the frame they shared proved nothing about the truck's place.
    executor.observe(
        [Detection(other, 0.5)],
        pose_ned=sim.ned,
        yaw_deg=sim.yaw,
        ground_point=lambda u, v: (0.0, 0.5),
    )
    (mover,) = [t for t in executor.active_loop.tracks.values() if t.bbox == other]
    assert mover.verdict == "no_match" and mover.ground == (0.0, 0.5) and mover.co_visible_with
    executor.apply_verdict(mover.track_id, "confirm_target")
    assert executor.alerts == ["s:alert:truck@0.0,0.0"]
    assert len(executor.findings) == 1


@pytest.mark.parametrize(("same_place_m", "alerts"), [(1.5, 1), (0.0, 2)])
def test_monitor_does_not_realert_one_object_seen_far_then_near(same_place_m, alerts):
    executor = MissionExecutor(
        _skill_plan(
            "monitor",
            {"region": "zone:watch", "query": "backpack", "duration_s": 600.0, "altitude_m": 25.0},
        ),
        loop_factory=_factory(),
        refs={"zone:watch": (0.0, 0.0, 100.0)},
        config=ExecutorConfig(alert_same_place_m=same_place_m, step_budget=200),
    )
    sim = Sim(altitude=20.0)
    # One look first; 65 steps later (past the ground re-id window) a new track
    # id 0.3 m away whose shape no longer matches (SITL: a backpack seen 12x22
    # then 14x18 px raised two alerts).
    _cycle(
        executor,
        sim,
        [Detection((633.0, 689.0, 645.0, 711.0), 0.5)],
        ground=lambda u, v: (15.0, 5.0),
        verdicts=lambda t: "confirm_target",
    )
    for _ in range(65):
        _cycle(executor, sim)
    _cycle(
        executor,
        sim,
        [Detection((300.0, 400.0, 340.0, 410.0), 0.5)],
        ground=lambda u, v: (15.3, 5.0),
        verdicts=lambda t: "confirm_target",
    )
    assert len(executor.alerts) == alerts


def test_alert_same_place_distance_must_be_non_negative():
    with pytest.raises(ValueError):
        ExecutorConfig(alert_same_place_m=-1.0)


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
    assert executor.alerts == ["s:alert:backpack@8.0,8.0"]
    assert executor.findings[0]["position_ne"] == (8.0, 8.0)
    assert executor.alert_vote_clusters[0]["positive"] == 2


def test_patrol_alert_majority_blocks_tied_votes():
    executor, track_id = _alert_vote_fixture(
        ExecutorConfig(alert_min_identity_matches=2, alert_require_majority=True)
    )
    executor.apply_verdict(track_id, "approach_track")
    executor.apply_verdict(track_id, "reject_track")
    assert executor.alert_vote_clusters[0]["positive"] == 1
    assert executor.alert_vote_clusters[0]["negative"] == 1
    assert executor.alerts == []
    executor.apply_verdict(track_id, "approach_track")
    assert executor.alerts == ["s:alert:backpack@8.0,8.0"]


def test_patrol_rejects_do_not_create_clusters_or_move_positive_anchor():
    executor, track_id = _alert_vote_fixture(ExecutorConfig(alert_min_identity_matches=2))
    executor.apply_verdict(track_id, "reject_track")
    assert executor.alert_vote_clusters == ()
    track = executor.active_loop.tracks[track_id]
    track.ground = (8.0, 8.0)
    executor.apply_verdict(track_id, "approach_track")
    track.ground = (11.5, 8.0)
    executor.apply_verdict(track_id, "reject_track")
    assert executor.alert_vote_clusters[0]["centre"] == [8.0, 8.0]
    assert executor.alert_vote_clusters[0]["negative"] == 1
    track.ground = (14.0, 8.0)
    executor.apply_verdict(track_id, "reject_track")
    assert len(executor.alert_vote_clusters) == 1
    assert executor.alert_vote_clusters[0]["negative"] == 1
    track.ground = (8.5, 8.0)
    executor.apply_verdict(track_id, "approach_track")
    assert executor.alerts == ["s:alert:backpack@8.0,8.0"]


def test_patrol_cluster_membership_does_not_chain_neighbouring_objects():
    executor, track_id = _alert_vote_fixture(ExecutorConfig(alert_min_identity_matches=2))
    loop = executor.active_loop
    for index, point in enumerate(((0.0, 0.0), (3.0, 0.0), (6.0, 0.0))):
        identity = f"chain-{index}"
        loop._tracks[identity] = Track(identity, _box(640, 700, 40), 0, 0, ground=point)
        executor.apply_verdict(identity, "approach_track")
    assert executor.alerts == ["s:alert:backpack@0.0,0.0"]
    assert [cluster["centre"] for cluster in executor.alert_vote_clusters] == [
        [0.0, 0.0], [6.0, 0.0]
    ]
    assert [cluster["positive"] for cluster in executor.alert_vote_clusters] == [2, 1]


def test_patrol_default_alerts_match_original_first_positive_script():
    executor, _track_id = _alert_vote_fixture()
    script = [
        ("reject-before", (7.9, 8.0), "reject_track"),
        ("a", (8.0, 8.0), "approach_track"),
        ("reject-between", (11.0, 8.0), "reject_track"),
        ("b", (11.5, 8.0), "confirm_target"),
        ("c", (14.2, 8.0), "approach_track"),
        ("b", (25.0, 8.0), "approach_track"),
        ("a", (30.0, 8.0), "confirm_target"),
        ("d", (18.1, 8.0), "approach_track"),
        ("reject-after", (18.2, 8.0), "reject_track"),
        ("e", (18.3, 8.0), "approach_track"),
    ]
    # The pre-cluster implementation marked each track on its first positive
    # and de-duplicated against positions of previously emitted alerts only.
    original_tracks = set()
    original_positions = []
    original_alerts = []
    for track_id, point, verdict in script:
        if verdict != "reject_track" and track_id not in original_tracks:
            original_tracks.add(track_id)
            if not any(math.dist(point, old) <= 4.0 for old in original_positions):
                original_positions.append(point)
                original_alerts.append(f"s:alert:backpack@{point[0]:.1f},{point[1]:.1f}")
        loop = executor.active_loop
        if track_id not in loop._tracks:
            loop._tracks[track_id] = Track(track_id, _box(640, 700, 40), 0, 0, ground=point)
        loop._tracks[track_id].ground = point
        executor.apply_verdict(track_id, verdict)
    assert executor.alerts == original_alerts
    assert [finding["position_ne"] for finding in executor.findings] == original_positions


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
    assert run.loop.config.ground_reid_max_steps == 0
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
            "monitor", {"region": "zone:watch", "query": "truck", "duration_s": 2.0}
        ),
        loop_factory=_factory(),
        refs={"zone:watch": (0.0, 0.0, 100.0)},
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


def test_deliver_without_capability_fails_to_on_fail():
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
    assert executor.history[0]["reason"] == "payload_capability_required"


def test_executor_never_reads_the_answer_key():
    source = (ROOT / "src/multidetect/autonomy/mission_executor.py").read_text(encoding="utf-8")
    for word in ("truth", "target_true", "scenario", "true_target"):
        assert word not in source


def _deliver_document(target="poi:drop", altitude=None):
    params = {"target_ref": target, "payload_ref": "payload:parcel"}
    if altitude is not None:
        params["altitude_m"] = altitude
    return {
        "plan_version": 1,
        "goal": "deliver parcel",
        "steps": [
            {"id": "d", "skill": "deliver", "params": params,
             "on_success": "h", "on_fail": "h"},
            {"id": "h", "skill": "return_home", "params": {},
             "on_success": None, "on_fail": None},
        ],
        "report": ["status"],
    }


def _deliver_executor(*, target="poi:drop", altitude=None, **kwargs):
    options = {
        "loop_factory": _factory(), "refs": {"poi:drop": (30.0, 0.0)},
        "capabilities": frozenset({"payload"}), "release_authorized": True,
    }
    return MissionExecutor(_deliver_document(target, altitude), **{**options, **kwargs})


def _delivery_request(executor):
    """Script actual pose reports through transit, descent, and the release hold."""
    results = [executor.next_action(pose_ned=pose, yaw_deg=0.0) for pose in (
        (0.0, 0.0, -20.0), (15.0, 0.0, -20.0), (30.0, 0.0, -20.0),
        (30.0, 0.0, -2.0), (30.0, 0.0, -2.0),
    )]
    return results


@pytest.mark.parametrize(("capabilities", "authorized", "reason"), [
    (frozenset(), False, "payload_capability_required"),
    (frozenset(), True, "payload_capability_required"),
    (frozenset({"payload"}), False, "payload_release_not_authorized"),
    (frozenset({"payload"}), "true", "payload_release_not_authorized"),
])
def test_deliver_requires_both_explicit_switches(capabilities, authorized, reason):
    executor = _deliver_executor(capabilities=capabilities, release_authorized=authorized)
    _, info = executor.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    assert executor.history[0]["state"] == "failed"
    assert executor.history[0]["reason"] == reason
    assert executor.report().alerts == (f"d:{reason}",)
    assert "event" not in info


def test_deliver_flies_descends_holds_requests_once_and_climbs_only_after_ack():
    executor = _deliver_executor()
    assert not executor.acknowledge_payload_release("unsolicited")
    results = _delivery_request(executor)
    actions = [action for action, _ in results]
    assert [a.kind for a in actions] == ["translate", "translate", "translate", "hold", "hold"]
    assert [a.forward_m for a in actions[:2]] == [15.0, 15.0]
    assert actions[2].up_m == -18.0
    assert actions[3].seconds == executor.config.deliver_hold_s
    event = results[-1][1]["event"]
    assert event["kind"] == "payload_release"
    assert event["payload_ref"] == "payload:parcel"
    assert event["target_ref"] == "poi:drop"
    assert event["position_ned"] == [30.0, 0.0, -2.0]
    assert event["timeout_s"] == 10.0
    assert executor.history == []
    action, waiting = executor.next_action(pose_ned=(30.0, 0.0, -2.0), yaw_deg=0.0)
    assert action.kind == "hold" and "event" not in waiting
    assert not executor.acknowledge_payload_release("wrong-id")
    assert executor.acknowledge_payload_release(event["request_id"])
    assert not executor.acknowledge_payload_release(event["request_id"])
    action, info = executor.next_action(pose_ned=(30.0, 0.0, -2.0), yaw_deg=0.0)
    assert action.kind == "translate" and action.up_m == 18.0 and info["phase"] == "climb"
    assert executor.history == []
    executor.next_action(pose_ned=(30.0, 0.0, -20.0), yaw_deg=0.0)
    assert executor.history[0] == {
        "step": "d", "skill": "deliver", "state": "succeeded", "reason": "payload_released",
    }
    assert executor.report().state_answers == (f"d:payload_release_ack:{event['request_id']}",)
    assert not executor.acknowledge_payload_release(event["request_id"])


def test_deliver_resolves_a_finding_from_an_earlier_search():
    document = _deliver_document(target="finding:1")
    document["steps"].insert(0, {
        "id": "s", "skill": "area_search", "params": {"region": "zone:all", "query": "box"},
        "on_success": "d", "on_fail": "h",
    })
    executor = MissionExecutor(
        document, loop_factory=_factory(), capabilities=frozenset({"payload"}),
        release_authorized=True,
    )
    sim = Sim()
    for _ in range(8):
        _cycle(executor, sim, [Detection(_box(640, 700, 40), 0.5)],
               ground=lambda u, v: (30.0, 0.0), verdicts=lambda t: "confirm_target")
        if executor.current_step.id == "d":
            break
    assert executor.history[0]["state"] == "succeeded"
    event = _delivery_request(executor)[-1][1]["event"]
    assert event["target_ref"] == "finding:1"
    assert event["position_ned"] == [30.0, 0.0, -2.0]


@pytest.mark.parametrize("bad_pose", [(32.0, 0.0, -2.0), (30.0, 0.0, -4.0)])
def test_deliver_aborts_if_pose_drifts_during_release_hold(bad_pose):
    executor = _deliver_executor()
    for pose in [(0.0, 0.0, -20.0), (30.0, 0.0, -20.0), (30.0, 0.0, -2.0)]:
        _, info = executor.next_action(pose_ned=pose, yaw_deg=0.0)
        assert "event" not in info
    _, info = executor.next_action(pose_ned=bad_pose, yaw_deg=0.0)
    assert executor.history[0]["reason"] == "release_position_error"
    assert "event" not in info


def test_deliver_missing_ack_times_out_and_never_reissues_request():
    now = [100.0]
    executor = _deliver_executor(clock=lambda: now[0])
    event = _delivery_request(executor)[-1][1]["event"]
    now[0] = 109.5
    action, info = executor.next_action(pose_ned=(30.0, 0.0, -2.0), yaw_deg=0.0)
    assert action.kind == "hold" and action.seconds == 0.5 and "event" not in info
    now[0] = 110.0
    assert not executor.acknowledge_payload_release(event["request_id"])
    _, info = executor.next_action(pose_ned=(30.0, 0.0, -2.0), yaw_deg=0.0)
    assert executor.history[0]["reason"] == "payload_release_ack_timeout"
    assert "event" not in info


def test_deliver_rejected_ack_and_drift_while_waiting_fail():
    for rejected in (True, False):
        executor = _deliver_executor()
        event = _delivery_request(executor)[-1][1]["event"]
        if rejected:
            assert executor.acknowledge_payload_release(event["request_id"], released=False)
        pose = (30.0, 0.0, -2.0) if rejected else (33.0, 0.0, -2.0)
        _, info = executor.next_action(pose_ned=pose, yaw_deg=0.0)
        assert executor.history[0]["reason"] == (
            "payload_release_rejected" if rejected else "release_position_error"
        )
        assert "event" not in info


@pytest.mark.parametrize(("blocked", "reason"), [
    (lambda pose, n, e: -pose[2] < 5.0, "release_obstacle"),
    (lambda pose, n, e: n > pose[0] + 1, "blocked"),
])
def test_deliver_aborts_on_obstacles_without_a_release_event(blocked, reason):
    executor = _deliver_executor(loop_factory=_factory(blocked=blocked))
    _, info = executor.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    assert executor.history[0]["reason"] == reason
    assert "event" not in info


@pytest.mark.parametrize("ref", ["poi:missing", "finding:1"])
def test_deliver_unknown_target_fails_before_release(ref):
    executor = _deliver_executor(target=ref)
    _, info = executor.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    expected_reason = "finding_not_found" if ref.startswith("finding:") else "unknown_ref"
    assert executor.history[0]["reason"] == expected_reason and "event" not in info


@pytest.mark.parametrize(("requested", "bounded"), [(1.0, 2.0), (120.0, 4.0), (3.0, 3.0)])
def test_deliver_release_altitude_is_bounded_by_host_config(requested, bounded):
    executor = _deliver_executor(
        altitude=requested,
        config=ExecutorConfig(deliver_min_altitude_m=2.0, deliver_max_altitude_m=4.0),
    )
    executor.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    action, _ = executor.next_action(pose_ned=(30.0, 0.0, -20.0), yaw_deg=0.0)
    assert action.up_m == bounded - 20.0


@pytest.mark.parametrize("options", [
    {"deliver_ack_timeout_s": 0}, {"deliver_hold_s": float("nan")},
    {"deliver_position_tolerance_m": -1}, {"deliver_min_altitude_m": 6},
])
def test_deliver_config_rejects_unsafe_bounds_and_timeouts(options):
    with pytest.raises(ValueError):
        ExecutorConfig(**options)


def test_count_is_reported_while_the_step_runs():
    executor = MissionExecutor(
        _count_plan(5), loop_factory=_factory(), refs={"zone:lot": (0.0, 0.0, 100.0)}
    )
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


def _land_only_plan():
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "land",
            "steps": [
                {
                    "id": "land",
                    "skill": "land",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                }
            ],
            "report": [],
        }
    )


def test_land_requires_host_confirmation_or_records_unknown_state():
    pose = (0.0, 0.0, -20.0)
    confirmed = MissionExecutor(_land_only_plan(), loop_factory=_factory())
    confirmed.next_action(pose_ned=pose, yaw_deg=0.0, landed=True)
    assert confirmed.history[-1]["reason"] == "landed"

    unknown = MissionExecutor(_land_only_plan(), loop_factory=_factory())
    unknown.next_action(pose_ned=pose, yaw_deg=0.0, landed="unknown")
    assert unknown.history[-1]["reason"] == "landing_commanded_unconfirmed"

    airborne = MissionExecutor(
        _land_only_plan(),
        loop_factory=_factory(),
        config=ExecutorConfig(landing_timeout_steps=2),
    )
    for _ in range(4):
        airborne.next_action(pose_ned=pose, yaw_deg=0.0, landed=False)
        if airborne.done:
            break
    assert airborne.history[-1]["reason"] == "landing_timeout"


def test_vertical_move_uses_clearance_and_fails_after_retries():
    executor = MissionExecutor(
        _skill_plan("goto", {"target_ref": "home", "altitude_m": 10.0}),
        loop_factory=_factory(),
        config=ExecutorConfig(max_blocked_legs=1),
    )
    executor._probe.segment_clear = lambda *_args: False
    pose = (0.0, 0.0, -20.0)
    executor.next_action(pose_ned=pose, yaw_deg=0.0)
    executor.next_action(pose_ned=pose, yaw_deg=0.0)
    assert executor.history[0]["reason"] == "blocked"


def test_invalid_region_fails_at_execution_without_global_fallback():
    executor = MissionExecutor(
        _skill_plan("survey", {"region": "zone:missing", "overlap": 0.7}),
        loop_factory=_factory(),
    )
    executor.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)
    assert executor.history[0]["reason"] == "invalid_region_ref"


def test_hover_reaches_region_centre_before_requested_altitude():
    executor = MissionExecutor(
        _skill_plan("hover", {"duration_s": 1.0, "region": "zone:pad", "altitude_m": 10.0}),
        loop_factory=_factory(),
        refs={"zone:pad": (20.0, 0.0, 5.0)},
    )
    sim = Sim(altitude=20.0)
    first, _ = _cycle(executor, sim)
    assert first.kind == "translate" and first.up_m == 0.0
    sim.apply(first)
    while executor._run is not None and executor._run.step.skill == "hover":
        action, _ = _cycle(executor, sim)
        if action.kind == "translate" and action.up_m != 0.0:
            assert math.isclose(sim.pose[0], 20.0, abs_tol=1e-6)
            break


def test_inspect_fails_when_no_view_contains_the_target():
    executor = MissionExecutor(
        plan_for_find_and_inspect("stop sign", views=2, standoff_m=8.0),
        loop_factory=_factory(),
    )
    executor.findings.append(
        {
            "step": "search",
            "query": "stop sign",
            "position_ne": (20.0, 0.0),
            "track_id": "target",
            "rule": "two_views",
            "frame_id": None,
        }
    )
    executor._successful_step_ids.append("search")
    executor._run = executor._start(executor._steps["inspect"])
    sim = Sim()
    for index in range(100):
        action, _ = _cycle(executor, sim, frame=f"empty-{index}")
        if executor.done:
            break
    assert executor.history[0]["reason"] == "target_not_seen"


def _see_small_candidate_then_lose_it(executor, sim, place, verdict="approach_track"):
    """A small far box judged positive once, then dropped by the tracker."""
    small = [Detection(_box(640, 700, 13), 0.5)]
    _cycle(executor, sim, small, ground=lambda u, v: place, verdicts=lambda t: verdict)
    lost = executor.active_loop.tracks["t1"]
    assert lost.identity_verdict_history == [verdict.split("_")[0]]
    for _ in range(8):
        _cycle(executor, sim)
    assert not lost.exhausted and executor.history == []
    return lost


def _search_ends(executor):
    # An obstacle-limited search pattern ends the loop's search the same way.
    executor.active_loop.state = "blocked"


@pytest.mark.parametrize("skill", ["count", "area_search"])
def test_search_end_revisits_a_lost_unconfirmed_positive_place(skill):
    place = (-22.0, 28.6)
    plan = _count_plan(3) if skill == "count" else plan_for_find_task("traffic cone")
    executor = MissionExecutor(
        plan,
        loop_factory=_factory(),
        refs={"zone:lot": (0.0, 0.0, 60.0)},
        config=ExecutorConfig(step_budget=140),
    )
    sim = Sim(altitude=8.0)
    lost = _see_small_candidate_then_lose_it(executor, sim, place)
    _search_ends(executor)
    action, info = _cycle(executor, sim)
    assert executor.history == []
    assert info["unresolved_revisit"] == {"track_id": lost.track_id, "centre": list(place)}
    assert info["second_look"]["kind"] == "place_revisit"
    assert action.kind in {"turn", "translate"}
    near = [Detection(_box(640, 700, 40), 0.5)]
    for _ in range(40):
        seen = math.dist(sim.ned[:2], place) <= 16.0 and executor.active_loop._revisit is None
        _cycle(
            executor,
            sim,
            near if seen or executor.active_loop.state == "approaching" else (),
            ground=lambda u, v: place,
            verdicts=lambda t: "confirm_target",
        )
        if executor.findings:
            break
    assert [finding["position_ne"] for finding in executor.findings] == [place]
    if skill == "count":
        assert executor.counts == {"count": 1}
        for _ in range(6):
            _cycle(executor, sim)
        _search_ends(executor)
        _cycle(executor, sim)
        assert executor.history[0]["reason"] == "search_exhausted"
        assert executor.counts == {"count": 1}
    else:
        assert executor.history[0]["reason"] == "confirmed"
    revisits = [e for e in executor.mission_log if e["event"] == "unresolved_place_revisit"]
    assert len(revisits) == 1


@pytest.mark.parametrize("reid_steps", [None, 0])
def test_search_end_does_not_revisit_a_place_rejected_later(reid_steps):
    place = (30.0, 20.0)
    make = _factory()
    executor = MissionExecutor(
        _count_plan(3),
        loop_factory=lambda text, **o: make(text, **{**o, "ground_reid_max_steps": reid_steps}),
        refs={"zone:lot": (0.0, 0.0, 100.0)},
        config=ExecutorConfig(step_budget=140),
    )
    sim = Sim(altitude=8.0)
    _see_small_candidate_then_lose_it(executor, sim, place)
    # The same place is seen again and the slow model now rejects it.
    _cycle(
        executor,
        sim,
        [Detection(_box(640, 700, 30), 0.5)],
        ground=lambda u, v: place,
        verdicts=lambda t: "reject_track",
    )
    for _ in range(6):
        _cycle(executor, sim)
    loop = executor.active_loop
    assert loop.unresolved_positive_tracks() == []
    _search_ends(executor)
    _cycle(executor, sim)
    assert executor.history[0]["reason"] == "search_exhausted"
    assert not any(e["event"] == "unresolved_place_revisit" for e in executor.mission_log)


def test_count_search_end_revisits_far_small_rejection_with_cap() -> None:
    executor = MissionExecutor(
        _count_plan(3),
        loop_factory=_factory(),
        refs={"zone:lot": (0.0, 0.0, 100.0)},
        config=ExecutorConfig(step_budget=140, count_rejection_revisit_attempts=1),
    )
    loop = executor.active_loop
    loop._rejections.extend(
        {
            "ground": centre,
            "min_side_px": [12.0],
            "range_m": [25.0],
            "revisited": False,
            "track_id": f"reject-{index}",
            "co_visible_with": set(),
        }
        for index, centre in enumerate(((20.0, 0.0), (30.0, 0.0)))
    )
    loop.state = "blocked"
    sim = Sim(altitude=8.0)
    action, info = _cycle(executor, sim)
    assert action.kind in {"turn", "translate"}
    assert info["count_rejection_revisit"]["centre"] == [20.0, 0.0]
    assert len(executor.mission_log) == 2  # visibility graph + the bounded look
    assert executor.history == []


def test_count_search_end_records_why_a_far_small_rejection_was_not_revisited() -> None:
    executor = MissionExecutor(
        _count_plan(3),
        loop_factory=_factory(),
        refs={"zone:lot": (0.0, 0.0, 40.0)},
        config=ExecutorConfig(step_budget=140),
    )
    loop = executor.active_loop
    loop._rejections.append(
        {
            "ground": (60.0, 0.0),
            "min_side_px": [12.0],
            "range_m": [25.0],
            "revisited": False,
            "track_id": "reject-0",
            "co_visible_with": set(),
        }
    )
    loop.state = "blocked"
    _cycle(executor, Sim(altitude=8.0))
    # The finished step's history says why the pending place got no look.
    assert executor.history[0]["rejection_revisit_skipped"] == [
        {"centre": [60.0, 0.0], "reason": "outside_region"}
    ]
    assert executor.history[0]["count_is_lower_bound"] is True
    assert any(e["event"] == "count_rejection_revisit_skipped" for e in executor.mission_log)


@pytest.mark.parametrize(
    ("case", "lower_bound"),
    [
        # Outside the step's zone: not part of this count.
        ("outside_region", False),
        # Already looked at again: it has had its second look.
        ("already_revisited", False),
        # The legacy reason remains valid when the counted object is at the same place.
        ("co_visible_with_counted", False),
        # Never looked at for lack of budget: the count may still miss it.
        ("budget", True),
    ],
)
def test_count_lower_bound_follows_the_rejection_revisit_skip_reason(case, lower_bound) -> None:
    executor = MissionExecutor(
        _count_plan(3),
        loop_factory=_factory(),
        refs={"zone:lot": (0.0, 0.0, 40.0 if case == "outside_region" else 100.0)},
        config=ExecutorConfig(step_budget=3 if case == "budget" else 140),
    )
    loop = executor.active_loop
    loop._rejections.append(
        {
            "ground": (60.0, 0.0) if case == "outside_region" else (20.0, 0.0),
            "min_side_px": [12.0],
            "range_m": [25.0],
            "revisited": False,
            "track_id": "reject-0",
            "co_visible_with": {"t9"} if case == "co_visible_with_counted" else set(),
        }
    )
    if case == "already_revisited":
        executor._run.rejection_revisit_places.append((20.5, 0.0))
    if case == "co_visible_with_counted":
        loop.confirmations.append(
            {"position_estimate_ne": [20.5, 0.0], "track_id": "t9", "co_visible_with": set()}
        )
    loop.state = "completed"  # a full coverage pass
    _cycle(executor, Sim(altitude=8.0))
    status = executor.history[0]
    assert status["reason"] == "coverage_complete"
    assert [item["reason"] for item in status["rejection_revisit_skipped"]] == [case]
    assert status["count_is_lower_bound"] is lower_bound


def test_search_end_revisits_each_unresolved_place_a_bounded_number_of_times():
    place = (30.0, 20.0)
    executor = MissionExecutor(
        _count_plan(3),
        loop_factory=_factory(),
        refs={"zone:lot": (0.0, 0.0, 100.0)},
        config=ExecutorConfig(step_budget=140),
    )
    sim = Sim(altitude=8.0)
    _see_small_candidate_then_lose_it(executor, sim, place)
    _search_ends(executor)
    _, info = _cycle(executor, sim)
    assert "unresolved_revisit" in info
    # The object is never seen again: the look runs out and the search resumes.
    for _ in range(30):
        _cycle(executor, sim)
        if executor.active_loop._revisit is None:
            break
    assert executor.active_loop._revisit is None and executor.history == []
    # Still unresolved, but its one revisit is spent.
    assert [t.track_id for t in executor.active_loop.unresolved_positive_tracks()] == ["t1"]
    _search_ends(executor)
    _, info = _cycle(executor, sim)
    assert "unresolved_revisit" not in info
    assert executor.history[0]["reason"] == "search_exhausted"
    assert len([e for e in executor.mission_log if e["event"] == "unresolved_place_revisit"]) == 1


@pytest.mark.parametrize(
    ("config", "refs"),
    [
        (ExecutorConfig(step_budget=140, unresolved_revisit_attempts=0), (0.0, 0.0, 100.0)),
        # Too few primitives remain to fly there.
        (ExecutorConfig(step_budget=12), (0.0, 0.0, 100.0)),
        # The candidate's place lies outside the step's zone.
        (ExecutorConfig(step_budget=140), (0.0, 0.0, 20.0)),
    ],
)
def test_search_end_skips_revisits_that_are_disabled_unaffordable_or_out_of_zone(config, refs):
    executor = MissionExecutor(
        _count_plan(3), loop_factory=_factory(), refs={"zone:lot": refs}, config=config
    )
    sim = Sim(altitude=8.0)
    _see_small_candidate_then_lose_it(executor, sim, (60.0, 60.0))
    _search_ends(executor)
    _, info = _cycle(executor, sim)
    assert "unresolved_revisit" not in info
    assert executor.history[0]["reason"] == "search_exhausted"


def test_unresolved_revisit_attempts_must_be_a_non_negative_integer():
    for value in (-1, True, 1.5):
        with pytest.raises(ValueError):
            ExecutorConfig(unresolved_revisit_attempts=value)


def _return_plan():
    step = {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None}
    return validate_mission_plan({"plan_version": 1, "goal": "g", "steps": [step], "report": []})


def test_fixed_wing_return_home_hands_the_flight_to_the_autopilot():
    from multidetect.autonomy.motion_intent import AirframeMotionAdapter

    adapter = AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT)
    executor = MissionExecutor(
        plan_for_find_task("red truck"), loop_factory=_factory(), airframe_adapter=adapter
    )
    executor.home = (0.0, 0.0)
    executor._finish("succeeded", "confirmed")  # the search found it
    action, info = executor.next_action(pose_ned=(-60.0, 10.0, -30.0), yaw_deg=120.0)
    # One stop, straight away: no turn/translate legs that circle home (SITL: 30+ steps).
    assert action.kind == "stop" and info["phase"] == "autopilot_return"
    assert executor.done and executor.history[-1]["reason"] == "autopilot_return"


def test_fixed_wing_return_home_keeps_the_no_fly_detour_and_multirotor_is_unchanged():
    from multidetect.autonomy.motion_intent import AirframeMotionAdapter

    adapter = AirframeMotionAdapter("rc_cessna_runway", base_dir=ROOT)
    executor = MissionExecutor(
        _return_plan(),
        loop_factory=_factory(),
        no_fly_zones=(((-30.0, 5.0), 5.0),),
        airframe_adapter=adapter,
    )
    executor.home = (0.0, 0.0)
    executor.operating_area = {
        "north_min_m": -100.0,
        "north_max_m": 100.0,
        "east_min_m": -100.0,
        "east_max_m": 100.0,
    }
    action, info = executor.next_action(pose_ned=(-60.0, 10.0, -30.0), yaw_deg=0.0)
    assert action.kind != "stop" and info.get("phase") != "autopilot_return"
    multirotor = MissionExecutor(_return_plan(), loop_factory=_factory())
    multirotor.home = (0.0, 0.0)
    action, _ = multirotor.next_action(pose_ned=(-60.0, 10.0, -20.0), yaw_deg=90.0)
    assert action.kind == "turn" and not multirotor.done


def test_fixed_wing_search_loops_plan_passes_outside_package_no_fly_zones():
    executor = MissionExecutor(
        plan_for_find_task("red truck"),
        loop_factory=_factory(),
        no_fly_zones=(((50.0, 50.0), 10.0),),
    )
    loop = executor._run.loop
    assert loop.pass_clear is not None
    assert not loop.pass_clear((0.0, 50.0), (100.0, 50.0))
    assert loop.pass_clear((0.0, 0.0), (0.0, 30.0))


def test_follow_after_search_keeps_a_fast_target_through_a_long_ground_jump():
    # The search loop vetoes far ground jumps (a static-object rule); the follow
    # step reuses that loop, and a mover re-seen > max(20 m, 5 gates) away must
    # keep its track instead of becoming a new one and timing the follow out.
    executor = MissionExecutor(_search_follow_plan(), loop_factory=_factory())
    sim = Sim()
    target = [50.0, 0.0]
    run = _start_follow(executor, sim, target)
    assert run.loop.config.static_ground_veto is False
    reference = math.dist(sim.ned[:2], target)
    _cycle(executor, sim)  # briefly lost
    target[1] += 40.0
    for _ in range(2):
        side = 40.0 * reference / max(1.0, math.dist(sim.ned[:2], target))
        _cycle(
            executor,
            sim,
            [Detection(_box(640, 700, side), 0.5)],
            ground=lambda u, v: tuple(target),
            verdicts=lambda t: "confirm_target",
        )
    assert [t.track_id for t in executor.current_tracks] == [run.follow_track_id]
    assert run.lost_cycles == 0
