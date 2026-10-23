"""Closer-look viewpoints sized with the optical-axis depth, re-approach, rejection looks.

SITL mission-suite-patrolrecall-20261023 (r18 identity model, worlds
gen_cnt2_w0003 / gen_cnt2_w0001, camera 1280x960, fx 540, pitched 0.18 rad):

* Patrol/monitor closer looks were planned for 14 px and saw 6-11 px.  A box
  side is f * size / depth with the depth along the optical axis, but the
  looks scaled the largest view by the slant range.  With this ~100 deg lens
  the largest views sit 40-50 deg off axis (depth 0.64-0.77 x slant), so the
  object looked 30-50 % larger than it is: monitor-backpacks d1d02c3b look A
  (9.7 px at 34 m, 41 deg off axis) saw 11 px, look C 9.5 px; ecf0af51 look A
  (7.6 px at 45.5 m) saw 9.5 px, unjudged.  Across the 14 looks of the suite
  side x depth is constant, side x slant is not (median prediction +50 %).
* Look B of d1d02c3b reached its viewpoint and saw 6.3-7.6 px (the narrow
  side of the backpack), and ended still unjudged without moving closer.
* count-fire-hydrants 6a87070d: hydrant h2 was rejected at 11 px from 23.5 m;
  the rejection revisit flew to the cruise-altitude standoff 21 m out and saw
  9 px, too small to judge, and the count finished at 2/3.
"""

from __future__ import annotations

import math
import statistics
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
F = 539.936
PITCH = CAMERA.pitch_down_rad
BACKPACK_WORLD = StaticMapSource(
    load_sdf_obstacles(ROOT / "simulation/gazebo/worlds/gen_cnt2_w0003.sdf")
)
HYDRANT_WORLD = StaticMapSource(
    load_sdf_obstacles(ROOT / "simulation/gazebo/worlds/gen_cnt2_w0001.sdf")
)
ZONE = (0.0, 0.0, 55.0)


def _depth(box, pose, ground):
    """Optical-axis depth of a logged box: slant over the off-axis ray length."""
    u, v = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    slant = math.hypot(math.dist(ground, pose[:2]), -pose[2])
    return slant / math.hypot(1.0, (u - CAMERA.cx) / F, (v - CAMERA.cy) / F)


def _side(box):
    return min(box[2] - box[0], box[3] - box[1])


def _measured_size_m(frames):
    """The object's size from the boxes SITL saw at the flown viewpoint."""
    return statistics.median(_side(box) * _depth(box, pose, g) / F for _, pose, _, box, g in frames)


def _faced_px(size_m, horizontal, height):
    return F * size_m / (horizontal * math.cos(PITCH) + height * math.sin(PITCH))


def _loop(source, **overrides):
    return OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
        config=LoopConfig(small_unjudged_query="backpack", **overrides),
        obstacle_source=source,
    )


def _see(loop, frames, label="backpack"):
    for step, pose, yaw, box, ground in frames:
        loop.step_index = step
        loop.update_tracks(
            [Detection(box, 0.87, label)],
            pose_ned=pose,
            yaw_deg=yaw,
            ground_point=lambda u, v, ground=ground: ground,
        )


def _in_zone(point):
    return math.dist(point, ZONE[:2]) <= ZONE[2]


# (step, pose NED, yaw deg, box, onboard ground estimate) from steps.jsonl.
LOOKS = {
    "d1d02c3b-A": {
        "centre": (16.06, -38.24),
        "start": (-0.8, -41.7, -18.5),
        "views": [
            (2, (-0.30, -14.64, -18.48), -91.1, (988.9, 773.2, 998.6, 786.1), (15.99, -38.08)),
            (6, (-0.76, -41.48, -18.48), 5.5, (664.3, 898.1, 673.4, 919.2), (15.89, -38.29)),
            (7, (-0.81, -41.53, -18.50), -1.1, (729.0, 886.5, 738.2, 907.2), (15.86, -38.23)),
        ],
        # Viewpoint (0.89, -51.70) at 11.86 m, planned for 14 px.
        "at": [
            (17, (1.22, -51.55, -11.96), 47.8, (571.0, 691.3, 582.0, 706.2), (16.10, -38.36)),
            (18, (1.19, -51.64, -12.00), 30.4, (745.9, 677.4, 757.0, 692.7), (15.96, -38.19)),
        ],
    },
    "d1d02c3b-C": {
        "centre": (-34.75, -24.64),
        "start": (-40.5, 1.5, -18.4),
        "views": [
            (10, (-0.86, -41.65, -18.48), -173.3, (288.4, 678.3, 295.0, 688.6), (-34.35, -24.32)),
            (11, (-0.78, -41.70, -18.50), 178.3, (397.0, 645.1, 404.0, 654.1), (-34.48, -24.58)),
            (20, (1.14, -51.61, -18.25), 106.7, (995.1, 654.4, 1001.6, 662.4), (-34.26, -24.42)),
            (21, (-0.75, -42.23, -18.35), 101.5, (1210.2, 693.3, 1221.2, 703.8), (-34.60, -24.57)),
            (76, (-11.24, 35.26, -18.38), -161.3, (1244.9, 618.8, 1253.2, 626.1), (-34.57, -24.56)),
            (87, (-34.62, 15.77, -18.40), -112.9, (851.1, 639.9, 857.3, 647.3), (-34.72, -24.48)),
            (88, (-40.41, 1.95, -18.37), -112.7, (973.0, 787.9, 984.4, 801.6), (-34.78, -24.46)),
            (89, (-40.45, 1.75, -18.41), -112.7, (967.3, 777.6, 978.6, 791.4), (-34.76, -24.40)),
        ],
        # Viewpoint (-39.67, -2.39) at 13.33 m.
        "at": [
            (99, (-39.65, -2.37, -13.44), -77.0, (625.8, 673.4, 635.2, 685.8), (-34.93, -24.55)),
            (100, (-39.66, -2.35, -13.36), -76.9, (626.1, 668.8, 635.6, 681.2), (-34.93, -24.62)),
            (101, (-39.66, -2.31, -13.32), -76.9, (625.3, 666.0, 634.5, 678.7), (-34.92, -24.59)),
        ],
    },
    "ecf0af51-A": {
        "centre": (16.23, -38.27),
        "start": (-15.9, -38.6, -18.5),
        "views": [
            (1, (-0.10, -0.02, -18.41), -104.9, (1025.6, 677.5, 1033.2, 686.0), (16.23, -38.27)),
        ],
        # Viewpoint (5.38, -19.89) at 12.48 m.
        "at": [
            (18, (6.22, -20.34, -12.50), -64.2, (676.2, 690.8, 685.7, 706.1), (16.22, -38.07)),
            (19, (6.25, -20.26, -12.56), -50.7, (535.5, 682.6, 545.0, 698.4), (16.05, -38.17)),
            (20, (6.18, -20.28, -12.52), -60.4, (631.2, 676.6, 640.6, 692.1), (16.02, -38.18)),
            (21, (6.27, -20.27, -12.49), -59.6, (618.9, 681.2, 628.2, 696.4), (16.05, -38.15)),
        ],
    },
}


def test_box_size_follows_the_optical_axis_depth_not_the_slant_range():
    # Look A: the same backpack 41 deg off axis from 34 m and faced from 23 m.
    far, near = LOOKS["d1d02c3b-A"]["views"][0], LOOKS["d1d02c3b-A"]["at"][0]
    far_slant = math.hypot(math.dist(far[4], far[1][:2]), -far[1][2])
    near_slant = math.hypot(math.dist(near[4], near[1][:2]), -near[1][2])
    # Scaled by the slant range the far box says 14.3 px; SITL saw 11.0 px.
    assert _side(far[3]) * far_slant / near_slant > 1.25 * _side(near[3])
    extent = CAMERA.box_extent_m(far[3], far_slant)
    predicted = CAMERA.side_px(extent, CAMERA.axis_depth(576.5, 698.8, near_slant))
    assert predicted == pytest.approx(_side(near[3]), rel=0.08)
    # A faced ground point: horizontal * cos(pitch) + height * sin(pitch).
    assert CAMERA.facing_depth(20.0, 10.0) == pytest.approx(
        20.0 * math.cos(PITCH) + 10.0 * math.sin(PITCH)
    )


@pytest.mark.parametrize("case", sorted(LOOKS))
def test_closer_look_viewpoint_makes_the_sitl_measured_box_judgeable(case):
    look = LOOKS[case]
    loop = _loop(BACKPACK_WORLD)
    _see(loop, look["views"])
    place = min(loop.small_unjudged_places(), key=lambda p: math.dist(p["centre"], look["centre"]))
    planned, reason = loop.plan_small_unjudged_revisit(place, look["start"], _in_zone)
    assert planned is not None, reason
    record = planned["record"]
    size = _measured_size_m(look["at"])
    measured = statistics.median(_side(frame[3]) for frame in look["at"])
    assert measured < LoopConfig().small_unjudged_target_side_px  # the SITL look fell short
    seen = _faced_px(size, record["view_range_m"], record["view_altitude_m"])
    # The object as SITL measured it fills the judging size from the new
    # viewpoint, and more than it did from the flown one.
    assert seen >= max(LoopConfig().min_verdict_side_px + 0.5, 1.1 * measured)
    assert record["view_altitude_m"] >= LoopConfig().approach_min_altitude_m - 1e-6
    assert record["extent_m"] == pytest.approx(place["extent_m"], abs=1e-3)
    # The leg and the descent at the viewpoint are clear of the world's buildings.
    goal, start = planned["goal"], look["start"]
    assert BACKPACK_WORLD.segment_clear(start, (goal[0], goal[1], start[2]))
    assert BACKPACK_WORLD.segment_clear(
        (goal[0], goal[1], start[2]), (goal[0], goal[1], -record["view_altitude_m"])
    )


# -- re-approach --------------------------------------------------------------

LOOK_B_VIEWS = [
    (8, (-0.80, -41.50, -18.46), 96.2, (284.5, 619.3, 292.0, 626.4), (21.77, 1.53)),
    (22, (-0.71, -42.12, -18.36), 16.5, (1189.4, 651.4, 1197.3, 659.5), (21.35, 1.93)),
]
LOOK_B_AT = [
    (37, (36.12, -18.49, -15.04), 127.1, (611.9, 688.9, 618.7, 701.7), (21.89, 1.67)),
    (38, (36.20, -18.40, -15.04), 117.7, (716.6, 684.2, 724.2, 697.1), (21.73, 1.51)),
    (39, (36.14, -18.46, -15.00), 125.6, (635.5, 677.8, 641.9, 690.3), (21.72, 1.55)),
    (40, (36.17, -18.43, -14.98), 125.0, (643.8, 678.8, 650.1, 691.2), (21.75, 1.52)),
]
BACKPACK_B = (21.524, 1.707)


class Sim:
    def __init__(self, pose, yaw):
        self.pose, self.yaw = list(pose), yaw

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


def _render(sim, obj, size_m):
    """The object's box: pinhole projection, side f * size / axis depth."""
    heading = math.radians(sim.yaw)
    height = -sim.pose[2]
    dn, de = obj[0] - sim.pose[0], obj[1] - sim.pose[1]
    forward = dn * math.cos(heading) + de * math.sin(heading)
    right = -dn * math.sin(heading) + de * math.cos(heading)
    depth = forward * math.cos(PITCH) + height * math.sin(PITCH)
    if depth <= 1e-6:
        return None
    u = CAMERA.cx + F * right / depth
    v = CAMERA.cy + F * (-forward * math.sin(PITCH) + height * math.cos(PITCH)) / depth
    side = F * size_m / depth
    box = (u - side / 2, v - 1.8 * side, u + side / 2, v)
    if box[0] < 0 or box[2] > CAMERA.width or box[1] < 0 or box[3] > CAMERA.height:
        return None
    return Detection(box, 0.8, "backpack")


def _fly_alert_look(loop, sim, size_m, cycles=60):
    """Run a started alert look; the r18-like model says yes from 13 px."""
    legs, asked = [], []
    info: dict = {}
    for _ in range(cycles):
        size = size_m(sim) if callable(size_m) else size_m
        detection = _render(sim, BACKPACK_B, size)
        loop.step_index += 1
        current = loop.update_tracks(
            [detection] if detection is not None else [],
            pose_ned=sim.ned,
            yaw_deg=sim.yaw,
            ground_point=lambda u, v: BACKPACK_B,
        )
        for track in loop.tracks_needing_verdict(current):
            side = _side(track.bbox)
            asked.append(round(side, 1))
            loop.apply_verdict(track.track_id, "approach_track" if side >= 13.0 else "reject_track")
        action = loop.continue_revisit(sim.ned, sim.yaw, info)
        if action is None:
            break
        before = sim.ned
        sim.apply(action)
        if action.kind == "translate":
            legs.append((before, sim.ned))
    return legs, asked


def _start_look_b(**overrides):
    loop = _loop(BACKPACK_WORLD, **overrides)
    _see(loop, LOOK_B_VIEWS)
    (place,) = [p for p in loop.small_unjudged_places() if math.dist(p["centre"], BACKPACK_B) < 2]
    start, yaw = (26.7, -30.0, -18.4), -67.2
    planned, reason = loop.plan_small_unjudged_revisit(place, start, _in_zone)
    assert planned is not None, reason
    planned["alert_look"] = True
    sim = Sim(start, yaw)
    action = loop.begin_place_revisit(planned, sim.ned, sim.yaw, {})
    assert action is not None
    sim.apply(action)
    return loop, planned, sim


def test_look_still_too_small_at_the_viewpoint_moves_closer_on_the_same_bearing():
    # From the side the look approaches, B is as narrow as SITL saw it at its
    # viewpoint (6.3-7.6 px at 27 m axis depth): the far views overstate it.
    narrow = _measured_size_m(LOOK_B_AT)
    loop, planned, sim = _start_look_b()
    legs, asked = _fly_alert_look(loop, sim, narrow)
    record = planned["record"]
    assert record["seen_again_min_side_px"] < LoopConfig().min_verdict_side_px
    (attempt,) = record["reapproaches"]
    assert attempt["outcome"] == "started"
    assert attempt["seen_min_side_px"] < LoopConfig().min_verdict_side_px
    assert attempt["extent_m"] == pytest.approx(narrow, rel=0.1)
    # Closer and lower along the bearing the first view was taken from.
    first, second = record["viewpoint"], attempt["viewpoint"]
    centre = record["centre"]
    assert math.dist(second, centre) < math.dist(first, centre) - 2.0
    assert attempt["view_altitude_m"] < record["view_altitude_m"]
    bearing = lambda p: math.atan2(p[1] - centre[1], p[0] - centre[0])  # noqa: E731
    assert abs(math.degrees(bearing(second) - bearing(first))) < 3.0
    assert attempt["view_altitude_m"] >= LoopConfig().approach_min_altitude_m - 1e-6
    # From there the box is judged, the look ends and the vehicle climbs back.
    assert record["outcome"] == "judged_approach"
    assert max(asked) >= 13.0
    assert loop._revisit is None
    assert abs(sim.pose[2] - (-18.4)) <= 1.0
    assert all(BACKPACK_WORLD.segment_clear(a, b) for a, b in legs)


def test_re_approach_is_bounded_and_can_be_disabled():
    narrow = _measured_size_m(LOOK_B_AT)
    loop, planned, sim = _start_look_b(small_unjudged_reapproach_max=0)
    _fly_alert_look(loop, sim, narrow)
    assert planned["record"]["outcome"] == "still_unjudged"
    assert "reapproaches" not in planned["record"]
    # One attempt: an object that is narrower still from the closer view (seen
    # end-on) is left after it, and the vehicle climbs back.
    loop, planned, sim = _start_look_b()

    def end_on(sim):
        return narrow if math.dist(sim.pose[:2], BACKPACK_B) > 14.0 else 0.45 * narrow

    _fly_alert_look(loop, sim, end_on)
    record = planned["record"]
    assert [a["outcome"] for a in record["reapproaches"]] == ["started"]
    assert record["seen_again_min_side_px"] < LoopConfig().min_verdict_side_px
    assert record["outcome"] == "still_unjudged"
    assert loop._revisit is None
    assert abs(sim.pose[2] - (-18.4)) <= 1.0


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_loop_rejects_invalid_re_approach_limit(value):
    with pytest.raises(ValueError):
        LoopConfig(small_unjudged_reapproach_max=value)


# -- count rejection revisit -----------------------------------------------------

HYDRANT_H2 = (-35.442, -31.80)
H2_FRAMES = [
    # Judged a match at 14.5 px, then rejected at 11 px (confidence 0.9996).
    (
        43,
        (-27.62, -17.09, -17.19),
        -89.6,
        (377.0, 920.2, 391.5, 948.9),
        (-35.56, -31.71),
        "approach",
    ),
    (
        44,
        (-27.62, -17.24, -17.21),
        -117.9,
        (632.7, 860.4, 643.7, 886.1),
        (-35.49, -31.72),
        "reject",
    ),
]
# The SITL revisit viewpoint, 21 m out at 17.2 m: 9 px, never judged.
H2_AT = [(82, (-18.92, -18.20, -17.19), -140.9, (638.6, 772.8, 647.6, 793.2), (-35.48, -31.80))]
SEARCH_END = ((15.67, 9.90, -17.18), 134.4)


def _count_plan():
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "count hydrants",
            "steps": [
                {
                    "id": "count",
                    "skill": "count",
                    "params": {"region": "zone:site", "query": "fire hydrant", "max_count": 5},
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


def _count_executor():
    def make_loop(task_text, region=None, obstacle_source=None, **overrides):
        return OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
            config=LoopConfig(**overrides),
            obstacle_source=obstacle_source,
        )

    return MissionExecutor(
        _count_plan(),
        loop_factory=make_loop,
        refs={"zone:site": ZONE},
        config=ExecutorConfig(step_budget=140),
        obstacle_source=HYDRANT_WORLD,
    )


def _reject_h2(executor):
    for _, pose, yaw, box, ground, verdict in H2_FRAMES:
        for track in executor.observe(
            [Detection(box, 0.9, "fire hydrant")],
            pose_ned=pose,
            yaw_deg=yaw,
            ground_point=lambda u, v, ground=ground: ground,
        ):
            executor.apply_verdict(track.track_id, f"{verdict}_track", 0.9996)
        executor.next_action(
            pose_ned=pose, yaw_deg=yaw, ground_point=lambda u, v, ground=ground: ground
        )
    loop = executor.active_loop
    (item,) = loop.rejected_far_small_places(15.0)
    assert item["min_side_px"] == [pytest.approx(11.0)]
    loop.state = "completed"  # the coverage pass ends


def test_small_box_rejection_is_revisited_from_a_view_that_makes_it_judgeable():
    executor = _count_executor()
    _reject_h2(executor)
    pose, yaw = SEARCH_END
    action, info = executor.next_action(pose_ned=pose, yaw_deg=yaw, ground_point=lambda u, v: None)
    record = info.get("count_rejection_revisit")
    assert record is not None
    assert math.dist(record["centre"], HYDRANT_H2) < 0.5
    look = executor.active_loop._revisit
    goal, altitude = look["goal"], -look.get("view_down", pose[2])
    # The hydrant as SITL measured it 21 m out (9 px, unjudgeable) reaches the
    # target size from the new viewpoint, which descends below the cruise altitude.
    assert _side(H2_AT[0][3]) < LoopConfig().min_verdict_side_px
    seen = _faced_px(_measured_size_m(H2_AT), math.dist(goal, HYDRANT_H2), altitude)
    assert seen >= 13.0
    assert altitude < -pose[2] - 4.0
    assert math.dist(goal, HYDRANT_H2) < 16.0
    assert record["kind"] == "small_rejected"
    assert record["target_side_px"] == pytest.approx(LoopConfig().small_unjudged_target_side_px)
    assert record["view_altitude_m"] == pytest.approx(altitude, abs=0.01)
    assert HYDRANT_WORLD.segment_clear(pose, (goal[0], goal[1], pose[2]))
    assert HYDRANT_WORLD.segment_clear((goal[0], goal[1], pose[2]), (goal[0], goal[1], -altitude))
    assert executor._run.rejection_revisits == [record]


def test_rejection_revisit_asks_again_at_the_viewpoint_and_counts_a_match():
    executor = _count_executor()
    _reject_h2(executor)
    pose, yaw = SEARCH_END
    sim = Sim(pose, yaw)
    size = _measured_size_m(H2_AT)
    asked = []
    for _ in range(80):
        detection = _render(sim, HYDRANT_H2, size)
        detections = [] if detection is None else [Detection(detection.bbox, 0.9, "fire hydrant")]
        for track in executor.observe(
            detections, pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=lambda u, v: HYDRANT_H2
        ):
            side = _side(track.bbox)
            asked.append(round(side, 1))
            verdict = "confirm_target" if side >= 13.0 else "reject_track"
            executor.apply_verdict(track.track_id, verdict)
        action, _ = executor.next_action(
            pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=lambda u, v: HYDRANT_H2
        )
        if executor.counts.get("count") == 1:
            break
        sim.apply(action)
    assert max(asked) >= 13.0
    assert executor.counts.get("count") == 1
    (record,) = executor._run.rejection_revisits
    assert record["outcome"] in {"judged_confirm", "judged_approach"}
