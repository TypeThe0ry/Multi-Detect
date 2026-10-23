"""Patrol/monitor recall of small objects: grown re-asks and closer looks.

SITL fullsuite 20261023 (world gen_cnt2_w0003, three backpacks, r18 identity
model).  Patrol and monitor hold the takeoff height (18.5 m) and never close in,
so a 0.44 x 0.26 m backpack stays at 7-19 px:

* patrol-fence-backpacks 33415ad9 (1/3): backpack A (16.03, -38.15) rejected
  at 10 px from 28 m on each of three loops; backpack C (-34.62, -24.54) seen
  end-on at most 8 x 17 px, never judged.
* monitor-backpacks 158d906e (2/3): C rejected at 12 px (step 55), then 14 and
  19 px within the verdict TTL and never asked again; replaying the same model
  on the 19 px frame gives "approach".
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.obstacle_source import StaticMapSource
from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
from multidetect.sim_teacher.policies import load_sdf_obstacles
from multidetect.sim_teacher.primitives import ActionPrimitive

ROOT = Path(__file__).resolve().parents[1]
CAMERA = CameraModel(fx=539.936, fy=539.936, cx=640.0, cy=480.0, width=1280, height=960)
SOURCE = StaticMapSource(load_sdf_obstacles(ROOT / "simulation/gazebo/worlds/gen_cnt2_w0003.sdf"))
FENCE = [(-30.0, -30.0), (-30.0, 30.0), (30.0, 30.0), (30.0, -30.0), (-30.0, -30.0)]
# (north, east, label, min side x slant range in px*m): A 10.5 px at 33.6 m and
# C 8 px at 28 m as in the SITL patrol; the woman distractor is larger.
BACKPACK_A = (16.03, -38.152, "backpack", 350.0)
BACKPACK_C = (-34.615, -24.537, "backpack", 224.0)
WOMAN = (39.728, 1.242, "person", 420.0)


def _make_loop(task_text, region=None, obstacle_source=SOURCE, **overrides):
    return OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
        config=LoopConfig(**overrides),
        obstacle_source=obstacle_source,
    )


def _plan(skill, params):
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": skill,
            "steps": [
                {
                    "id": "s",
                    "skill": skill,
                    "params": params,
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
            "report": ["findings", "positions"],
        }
    )


class Sim:
    def __init__(self, north=0.0, east=0.0, altitude=18.5, yaw=0.0):
        self.pose = [north, east, -altitude]
        self.yaw = yaw

    def apply(self, action):
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


def _project(sim, obj):
    """The object's box: pinhole projection, size inverse to the slant range."""
    north, east, label, k = obj
    height = -sim.pose[2]
    heading = math.radians(sim.yaw)
    dn, de = north - sim.pose[0], east - sim.pose[1]
    forward = dn * math.cos(heading) + de * math.sin(heading)
    right = -dn * math.sin(heading) + de * math.cos(heading)
    pitch = CAMERA.pitch_down_rad
    depth = forward * math.cos(pitch) + height * math.sin(pitch)
    if depth <= 1e-6:
        return None
    u = CAMERA.cx + CAMERA.fx * right / depth
    v = CAMERA.cy + CAMERA.fy * (-forward * math.sin(pitch) + height * math.cos(pitch)) / depth
    side = k / math.sqrt(dn * dn + de * de + height * height)
    box = (u - side / 2, v - 1.4 * side, u + side / 2, v)
    if box[0] < 0 or box[2] > CAMERA.width or box[1] < 0 or box[3] > CAMERA.height:
        return None
    return Detection(box, 0.6, label)


def _model(track):
    """r18-like: a backpack is recognised from about 13 px; nothing else is."""
    side = min(track.bbox[2] - track.bbox[0], track.bbox[3] - track.bbox[1])
    if track.label == "backpack" and side >= 13.0:
        return "approach_track"
    return "reject_track"


def _fly(executor, sim, objects, max_cycles=400):
    asked, legs = [], []
    while not executor.history and max_cycles:
        max_cycles -= 1
        detections = [d for d in (_project(sim, obj) for obj in objects) if d is not None]

        def ground(u, v):
            return CAMERA.ground_point(
                u, v, north=sim.pose[0], east=sim.pose[1], height_m=-sim.pose[2], yaw_deg=sim.yaw
            )

        for track in executor.observe(
            detections, pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=ground
        ):
            side = min(track.bbox[2] - track.bbox[0], track.bbox[3] - track.bbox[1])
            asked.append((track.label, round(side, 1), _model(track)))
            executor.apply_verdict(track.track_id, _model(track))
        action, _ = executor.next_action(pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=ground)
        before = sim.ned
        sim.apply(action)
        if action.kind == "translate":
            legs.append((before, sim.ned))
    return asked, legs


def _alerted(executor, obj):
    points = [
        tuple(map(float, a.split("@")[1].split(","))) for a in executor.alerts if ":alert:" in a
    ]
    return any(math.dist(point, obj[:2]) <= 2.0 for point in points)


def _patrol(config=None):
    return MissionExecutor(
        _plan("patrol", {"route_ref": "route:fence", "query": "backpack", "loops": 1}),
        loop_factory=_make_loop,
        refs={"route:fence": FENCE},
        config=config or ExecutorConfig(step_budget=140),
        obstacle_source=SOURCE,
    )


def test_patrol_looks_closer_at_a_never_judged_and_a_small_rejected_backpack():
    executor, sim = _patrol(), Sim()
    asked, legs = _fly(executor, sim, [BACKPACK_A, BACKPACK_C, WOMAN])
    status = executor.history[0]
    assert status["state"] == "succeeded" and status["reason"] == "duration_elapsed"
    looks = {record["kind"]: record for record in status["alert_looks"]}
    # C: only an end-on 6-8 px view from the route; A: rejected at 10-12 px.
    assert set(looks) == {"small_unjudged", "small_rejected"}
    assert math.dist(looks["small_unjudged"]["centre"], BACKPACK_C[:2]) < 1.5
    assert math.dist(looks["small_rejected"]["centre"], BACKPACK_A[:2]) < 1.5
    assert all(record["outcome"] == "judged_approach" for record in looks.values())
    assert all(record["predicted_min_side_px"] >= 13.9 for record in looks.values())
    assert any(label == "backpack" and verdict == "reject_track" for label, _, verdict in asked)
    assert _alerted(executor, BACKPACK_A) and _alerted(executor, BACKPACK_C)
    assert not _alerted(executor, WOMAN)
    assert len(executor.alerts) == 2
    # Every leg (level and vertical) stays clear of the world's buildings, and the
    # route resumes at the patrol height.
    assert all(SOURCE.segment_clear(start, end) for start, end in legs)
    assert min(-start[2] for start, _ in legs) >= 7.5
    assert abs(-sim.pose[2] - 18.5) < 1.0 or executor.history[-1]["skill"] == "return_home"


def test_patrol_without_closer_looks_keeps_the_sitl_recall():
    config = ExecutorConfig(step_budget=140, alert_look_attempts=0, alert_grown_reask_ratio=None)
    executor, sim = _patrol(config), Sim()
    _fly(executor, sim, [BACKPACK_A, BACKPACK_C, WOMAN])
    assert executor.alerts == []
    assert "alert_looks" not in executor.history[0]


def test_patrol_look_needs_budget_and_skips_unrelated_or_alerted_places():
    tight = ExecutorConfig(step_budget=140, travel_budget=24)
    executor, sim = _patrol(tight), Sim()
    _fly(executor, sim, [BACKPACK_C])
    assert "alert_looks" not in executor.history[0]
    reasons = {
        item["reason"]
        for event in executor.mission_log
        if event.get("event") == "alert_look_skipped"
        for item in event["skipped"]
    }
    assert "budget" in reasons

    # A person seen as small as the backpacks never gets a look.
    executor, sim = _patrol(), Sim()
    small_woman = (-34.615, -24.537, "person", 224.0)
    _fly(executor, sim, [small_woman])
    assert "alert_looks" not in executor.history[0]
    assert executor.alerts == []


def _monitor_executor(config=None, duration_s=300.0):
    return MissionExecutor(
        _plan("monitor", {"region": "zone:site", "query": "backpack", "duration_s": duration_s}),
        loop_factory=_make_loop,
        refs={"zone:site": (0.0, 0.0, 55.0)},
        config=config or ExecutorConfig(step_budget=140),
        obstacle_source=SOURCE,
    )


def _see(executor, sim, box, label="backpack", ground=(-34.6, -24.5), min_side=15.0):
    asked = []
    for track in executor.observe(
        [Detection(box, 0.6, label)],
        pose_ned=sim.ned,
        yaw_deg=sim.yaw,
        ground_point=lambda u, v: ground,
    ):
        side = min(track.bbox[2] - track.bbox[0], track.bbox[3] - track.bbox[1])
        verdict = "approach_track" if label == "backpack" and side >= min_side else "reject_track"
        asked.append(verdict)
        executor.apply_verdict(track.track_id, verdict)
    return asked


# Monitor 158d906e steps 55, 57, 59: one backpack seen from a viewpoint at
# 12.3, 14.4 and 19.3 px (box min side), the model rejecting below ~15 px.
GROWING = [(961.0, 794.0, 973.3, 808.0), (997.0, 824.0, 1011.4, 840.0), (54.0, 941.0, 75.0, 960.0)]


@pytest.mark.parametrize(
    ("label", "ratio", "asked", "alerts"),
    [
        ("backpack", 1.25, [["reject_track"], [], ["approach_track"]], 1),
        ("backpack", None, [["reject_track"], [], []], 0),
        ("person", 1.25, [["reject_track"], [], []], 0),
    ],
)
def test_monitor_reasks_a_grown_small_rejection_of_the_queried_object(label, ratio, asked, alerts):
    executor = _monitor_executor(ExecutorConfig(step_budget=140, alert_grown_reask_ratio=ratio))
    sim = Sim(north=-41.1, east=-1.9, yaw=-113.0)
    assert [_see(executor, sim, box, label) for box in GROWING] == asked
    assert len([a for a in executor.alerts if ":alert:" in a]) == alerts


def test_usable_size_rejection_is_not_reasked_when_it_grows():
    executor = _monitor_executor()
    sim = Sim(north=-41.1, east=-1.9, yaw=-113.0)
    # Rejected at 21 px (usable size): growing to 30 px asks nothing new.
    assert _see(executor, sim, (900.0, 800.0, 921.0, 830.0), min_side=40.0) == ["reject_track"]
    assert _see(executor, sim, (900.0, 800.0, 930.0, 840.0), min_side=40.0) == []


def test_small_rejected_places_exclude_judged_usable_and_unrelated_places():
    loop = _make_loop("t", small_unjudged_query="backpack")
    pose = (0.0, 0.0, -18.5)

    def seen(box, label, ground, verdict):
        (track,) = loop.update_tracks(
            [Detection(box, 0.6, label)],
            pose_ned=pose,
            yaw_deg=0.0,
            ground_point=lambda u, v: ground,
        )
        loop.apply_verdict(track.track_id, verdict)
        return track

    seen((600.0, 700.0, 611.0, 715.0), "backpack", (20.0, 0.0), "reject_track")
    loop.step_index += 20  # lost: the next box is a new track
    seen((300.0, 700.0, 311.0, 715.0), "person", (20.0, 10.0), "reject_track")
    loop.step_index += 20
    seen((900.0, 700.0, 925.0, 730.0), "backpack", (20.0, -10.0), "reject_track")
    loop.step_index += 20
    seen((100.0, 700.0, 111.0, 715.0), "backpack", (30.0, 0.0), "reject_track")
    loop.step_index += 20
    seen((100.0, 300.0, 111.0, 315.0), "backpack", (30.5, 0.0), "approach_track")
    places = loop.small_rejected_places()
    assert [place["centre"] for place in places] == [(20.0, 0.0)]
    assert places[0]["min_side_px"] == pytest.approx(11.0)


def test_monitor_skips_a_look_that_does_not_fit_the_remaining_duration():
    executor = _monitor_executor(duration_s=12.0)
    sim = Sim()
    _fly(executor, sim, [BACKPACK_C], max_cycles=200)
    status = executor.history[0]
    assert "alert_looks" not in status


@pytest.mark.parametrize(
    "kwargs",
    [
        {"alert_look_attempts": -1},
        {"alert_look_attempts": True},
        {"alert_look_max_detour_m": 0.0},
        {"alert_grown_reask_ratio": 1.0},
        {"alert_grown_reask_ratio": float("nan")},
    ],
)
def test_executor_rejects_invalid_closer_look_settings(kwargs):
    with pytest.raises(ValueError):
        ExecutorConfig(**kwargs)


@pytest.mark.parametrize("ratio", [1.0, 0.5, float("inf"), True])
def test_loop_rejects_invalid_grown_reask_ratio(ratio):
    with pytest.raises(ValueError):
        LoopConfig(grown_reject_reask_ratio=ratio)
