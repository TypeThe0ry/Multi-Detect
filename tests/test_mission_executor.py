from __future__ import annotations

import math
from pathlib import Path

from multidetect.autonomy.mission_executor import (
    ExecutorConfig,
    MissionExecutor,
    plan_for_find_and_inspect,
    plan_for_find_task,
    task_text_for,
)
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
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


def test_unimplemented_skill_fails_to_on_fail():
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "survey",
            "steps": [
                {
                    "id": "s",
                    "skill": "survey",
                    "params": {"region": "zone:a", "overlap": 0.7},
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
