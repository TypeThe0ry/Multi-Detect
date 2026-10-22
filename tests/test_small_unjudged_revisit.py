"""Small unjudged revisit: a query-relevant object only ever too small to judge.

Geometry from SITL gen_cnt2_w0001 episode 7d6ea791: at step 57 the count saw
hydrant count_member_0001_02 (NED -35.44, -31.80) once, as a 7.1 px box from
(-11.42, -16.97) at 17.2 m, never judged it (min_verdict_side_px 10), and ended
its coverage pass at 2/3.  The 14.8 m apartment obstacle_01 stands next to it.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.obstacle_source import StaticMapSource
from multidetect.autonomy.onboard_loop import (
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
)
from multidetect.sim_teacher.policies import load_sdf_obstacles
from multidetect.sim_teacher.primitives import ActionPrimitive

ROOT = Path(__file__).resolve().parents[1]
CAMERA = CameraModel(fx=539.936, fy=539.936, cx=640.0, cy=480.0, width=1280, height=960)
HYDRANT = (-35.442, -31.80)
HYDRANT_WIDTH_M = 0.4236
SEEN_FROM = (-11.42, -16.97, 17.21)
SEEN_BOX = (648.0, 687.8, 655.1, 703.5)
OBSTACLES = load_sdf_obstacles(ROOT / "simulation/gazebo/worlds/gen_cnt2_w0001.sdf")
APARTMENT = next(o for o in OBSTACLES if abs(o.height_m - 14.8) < 0.1)
SOURCE = StaticMapSource(OBSTACLES)


def _plan(query="fire hydrants"):
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "count hydrants",
            "steps": [
                {
                    "id": "count",
                    "skill": "count",
                    "params": {"region": "zone:site", "query": query, "max_count": 5},
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


def _make_loop(task_text, region=None, obstacle_source=None, **overrides):
    return OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
        config=LoopConfig(**overrides),
        obstacle_source=obstacle_source,
    )


class Sim:
    def __init__(self, north, east, altitude, yaw):
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


def _seen_yaw():
    bearing = math.degrees(math.atan2(HYDRANT[1] - SEEN_FROM[1], HYDRANT[0] - SEEN_FROM[0]))
    return bearing - CAMERA.bearing_deg((SEEN_BOX[0] + SEEN_BOX[2]) / 2)


def _executor(config=None):
    return MissionExecutor(
        _plan(),
        loop_factory=_make_loop,
        refs={"zone:site": (0.0, 0.0, 55.0)},
        config=config or ExecutorConfig(step_budget=140),
        obstacle_source=SOURCE,
    )


def _cycle(executor, sim, detections=(), verdict=None, legs=None):
    tracks = executor.observe(
        detections, pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=lambda u, v: HYDRANT
    )
    for track in tracks:
        if verdict is not None:
            executor.apply_verdict(track.track_id, verdict)
    action, info = executor.next_action(
        pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=lambda u, v: HYDRANT
    )
    before = sim.ned
    sim.apply(action)
    if legs is not None and action.kind == "translate":
        legs.append((before, sim.ned))
    return action, info, tracks


def _see_once_then_finish_coverage(executor, sim, label="fire hydrant", score=0.80):
    _, _, asked = _cycle(executor, sim, [Detection(SEEN_BOX, score, label)])
    assert asked == []  # 7.1 px: below min_verdict_side_px, never sent to the slow model
    for _ in range(3):
        _cycle(executor, sim)
    executor.active_loop.state = "completed"  # the coverage pass ends


def _outside_apartment_ring(point):
    radius = math.hypot(APARTMENT.size_north_m, APARTMENT.size_east_m) / 2 + SOURCE.margin_m
    return math.dist(point, (APARTMENT.north_m, APARTMENT.east_m)) > radius


def test_label_matches_query():
    from multidetect.autonomy.onboard_loop import label_matches_query

    assert label_matches_query("fire hydrant", "fire hydrants")
    assert label_matches_query("traffic cone", "orange traffic cones")
    assert label_matches_query("post box", "postbox")
    assert label_matches_query("bus", "buses")
    assert not label_matches_query("car", "cardboard boxes")
    assert not label_matches_query("backpack", "fire hydrants")
    assert not label_matches_query("traffic cone", "fire hydrants")
    assert not label_matches_query(None, "fire hydrants")


def test_one_time_small_hydrant_is_revisited_from_a_judging_view_clear_of_the_apartment():
    executor = _executor()
    sim = Sim(*SEEN_FROM, yaw=_seen_yaw())
    _see_once_then_finish_coverage(executor, sim)
    legs: list = []
    action, info, _ = _cycle(executor, sim, legs=legs)
    record = info.get("small_unjudged_revisit")
    assert record is not None, "coverage ended without looking at the small hydrant again"
    assert math.dist(record["centre"], HYDRANT) < 0.5
    assert record["label"] == "fire hydrant" and record["seen_min_side_px"] == 7.1
    view = tuple(record["viewpoint"])
    altitude = record["view_altitude_m"]
    slant = math.hypot(math.dist(view, HYDRANT), altitude)
    # The view makes the real 0.42 m hydrant at least the judging size.
    assert record["predicted_min_side_px"] >= 10.0
    assert HYDRANT_WIDTH_M * CAMERA.fx / slant >= 10.0
    assert altitude >= LoopConfig().approach_min_altitude_m - 1e-6
    # Below the roof, the viewpoint is outside the apartment's clearance ring and
    # the descent from the current altitude there is clear.
    assert altitude < APARTMENT.height_m + 2.0
    assert _outside_apartment_ring(view)
    assert SOURCE.segment_clear((view[0], view[1], -SEEN_FROM[2]), (view[0], view[1], -altitude))
    # The nearest-side viewpoint would have descended into the apartment roof ring.
    toward = math.atan2(SEEN_FROM[1] - HYDRANT[1], SEEN_FROM[0] - HYDRANT[0])
    near_side = (
        HYDRANT[0] + record["view_range_m"] * math.cos(toward),
        HYDRANT[1] + record["view_range_m"] * math.sin(toward),
    )
    assert not SOURCE.segment_clear(
        (near_side[0], near_side[1], -SEEN_FROM[2]), (near_side[0], near_side[1], -altitude)
    )

    # Fly the look: every leg is checked against the map as a 3-D segment.
    loop = executor.active_loop
    for _ in range(40):
        look = loop._revisit
        if look is not None and look["phase"] == "look":
            break
        action, info, _ = _cycle(executor, sim, legs=legs)
    else:
        pytest.fail("the revisit never reached its viewpoint")
    assert all(SOURCE.segment_clear(a, b) for a, b in legs)
    assert math.dist(sim.ned[:2], view) <= 3.5
    assert abs(-sim.ned[2] - altitude) <= 0.6

    # From the viewpoint the box is large enough: judged, then counted as usual.
    side = HYDRANT_WIDTH_M * CAMERA.fx / math.hypot(math.dist(sim.ned[:2], HYDRANT), -sim.ned[2])
    box = (640 - side / 2, 600 - 2 * side, 640 + side / 2, 600 + 2 * side)
    asked_any = False
    for _ in range(12):
        _, info, asked = _cycle(
            executor, sim, [Detection(box, 0.8, "fire hydrant")], "confirm_target", legs
        )
        asked_any = asked_any or bool(asked)
        if executor.counts.get("count") == 1:
            break
    assert asked_any
    assert executor.counts["count"] == 1
    assert math.dist(executor.findings[0]["position_ne"], HYDRANT) < 0.5
    assert record["outcome"] == "judged_confirm"
    assert all(SOURCE.segment_clear(a, b) for a, b in legs)


@pytest.mark.parametrize(
    ("label", "config", "margin", "reason"),
    [
        # Not the queried class: no look and nothing pending.
        ("backpack", None, None, None),
        (None, None, None, None),
        # Relevant, but no look is affordable: recorded, and the count is a lower bound.
        ("fire hydrant", {"small_unjudged_revisit_attempts": 0}, None, None),
        ("fire hydrant", {"step_budget": 12}, None, "budget"),
        ("fire hydrant", None, 20.0, "energy_margin"),
    ],
)
def test_small_unjudged_revisit_skips_unrelated_and_unaffordable_places(
    label, config, margin, reason
):
    executor = _executor(ExecutorConfig(**{"step_budget": 140, **(config or {})}))
    sim = Sim(*SEEN_FROM, yaw=_seen_yaw())
    _see_once_then_finish_coverage(executor, sim, label=label)
    executor.energy_margin_s = margin
    loop = executor.active_loop
    _, info, _ = _cycle(executor, sim)
    assert "small_unjudged_revisit" not in info
    status = executor.history[0]
    assert status["reason"] == "coverage_complete"
    assert "small_unjudged_revisits" not in status
    relevant = label == "fire hydrant"
    assert bool(loop.small_unjudged_places()) is relevant
    assert status["count_is_lower_bound"] is relevant
    if reason is not None:
        assert [item["reason"] for item in status["small_unjudged_revisit_skipped"]] == [reason]
    else:
        assert "small_unjudged_revisit_skipped" not in status


def test_small_unjudged_places_exclude_counted_and_judged_places():
    loop = _make_loop("count", small_unjudged_query="fire hydrants", stop_on_confirm=False)
    pose = (*SEEN_FROM[:2], -SEEN_FROM[2])
    loop.update_tracks(
        [Detection(SEEN_BOX, 0.8, "fire hydrant")],
        pose_ned=pose,
        yaw_deg=_seen_yaw(),
        ground_point=lambda u, v: HYDRANT,
    )
    assert [place["track_ids"] for place in loop.small_unjudged_places()] == [["t1"]]
    # Already counted within the de-duplication radius: the same object.
    loop.confirmations.append(
        {"position_estimate_ne": [HYDRANT[0] + 1.0, HYDRANT[1]], "track_id": "t9", "step": 3}
    )
    assert loop.small_unjudged_places() == []
    loop.confirmations.clear()
    # Judged at that place under another track id (e.g. rejected when nearer).
    loop.step_index += 10
    loop.update_tracks(
        [Detection((600.0, 600.0, 640.0, 680.0), 0.8, "fire hydrant")],
        pose_ned=pose,
        yaw_deg=_seen_yaw(),
        ground_point=lambda u, v: (HYDRANT[0] + 2.0, HYDRANT[1]),
    )
    judged = next(t for t in loop.tracks.values() if t.track_id != "t1")
    loop.apply_verdict(judged.track_id, "reject_track")
    assert loop.small_unjudged_places() == []


def test_area_search_also_revisits_a_small_unjudged_place():
    from multidetect.autonomy.mission_executor import plan_for_find_task

    executor = MissionExecutor(
        plan_for_find_task("fire hydrant"),
        loop_factory=_make_loop,
        config=ExecutorConfig(step_budget=140),
        obstacle_source=SOURCE,
    )
    sim = Sim(*SEEN_FROM, yaw=_seen_yaw())
    _see_once_then_finish_coverage(executor, sim)
    _, info, _ = _cycle(executor, sim)
    assert math.dist(info["small_unjudged_revisit"]["centre"], HYDRANT) < 0.5
    assert executor.history == []


def test_count_lower_bound_tells_a_looked_at_small_place_from_an_unvisited_one():
    # Looked at again (nothing there to judge): no longer an open place.
    executor = _executor()
    sim = Sim(*SEEN_FROM, yaw=_seen_yaw())
    _see_once_then_finish_coverage(executor, sim)
    _, info, _ = _cycle(executor, sim)
    record = info["small_unjudged_revisit"]
    loop = executor.active_loop
    for _ in range(40):
        _cycle(executor, sim)
        if loop._revisit is None:
            break
    assert record["outcome"] == "not_seen"
    loop.state = "completed"
    _cycle(executor, sim)
    status = executor.history[0]
    assert status["small_unjudged_revisits"][0]["outcome"] == "not_seen"
    assert status["small_unjudged_revisit_skipped"] == [
        {"centre": record["centre"], "track_ids": ["t1"], "reason": "already_revisited"}
    ]
    assert status["count_is_lower_bound"] is False

    # Outside the step's zone: not part of the count, no look, no lower bound.
    outside = MissionExecutor(
        _plan(),
        loop_factory=_make_loop,
        refs={"zone:site": (0.0, 0.0, 30.0)},
        config=ExecutorConfig(step_budget=140),
        obstacle_source=SOURCE,
    )
    sim = Sim(*SEEN_FROM, yaw=_seen_yaw())
    _see_once_then_finish_coverage(outside, sim)
    _cycle(outside, sim)
    status = outside.history[0]
    assert [item["reason"] for item in status["small_unjudged_revisit_skipped"]] == [
        "outside_region"
    ]
    assert status["count_is_lower_bound"] is False
