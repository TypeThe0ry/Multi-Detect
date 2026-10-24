"""Count: a lone small "no" after positive views, closer looks cut short, clipped "no".

SITL gen_cnt2_w0001 (r18 identity model, camera 1280x960, fx 540, pitched
0.18 rad), mission-suite-hydrant10-20261024 and -lookgeometry-20261024:

* f517d76c: hydrant h2 (N -35.44, E -31.80) judged "approach" at 15 and 11 px
  from its closest view above the apartment block, then "no" at 11 px (0.96);
  the track was reset and the search moved on (2/3).  Across 84 recent count
  episodes 14 target tracks had two positive views followed by a small-box
  "no" (7 of those objects were never counted); no non-target track had two
  positive views.
* 7166ed2b: hydrant h0 judged "approach" at 12 and 11 px from the start
  point, then "no" at 11 px (0.98): never approached, never counted.
* 2435e7b9: the rejection look at h2 (planned for 15.6 px from 9.1 m) ended
  on the way, at the cruise altitude 24 m out, when a 12 px box was judged
  "approach"; the plain approach from there was blocked by the apartment
  block and one "no" at 10 px dropped it.
* 3121ac38: a 31x20 px box clipped by the image bottom was rejected (0.74)
  and outvoted a 23 px unclipped "confirm" into a contradiction relook; h2 was
  never counted.
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
F = 539.936
PITCH = CAMERA.pitch_down_rad
HYDRANT_WORLD = StaticMapSource(
    load_sdf_obstacles(ROOT / "simulation/gazebo/worlds/gen_cnt2_w0001.sdf")
)
ZONE = (0.0, 0.0, 55.0)
H0 = (8.76, -15.02)
H2 = (-35.44, -31.80)
# The onboard loop settings of the SITL suite (scripts/onboard_policy.py).
SITL_LOOP = {"confirm_max_range_m": 40.0, "contradiction_relook_enabled": True}

# (step, pose NED, yaw deg, box, onboard ground estimate, decision, confidence)
F517_H2 = [
    (
        51,
        (-27.24, -16.85, -17.12),
        -87.9,
        (347.7, 918.5, 362.8, 947.6),
        (-35.59, -31.68),
        "approach_track",
        0.8273,
    ),
    (
        52,
        (-27.23, -16.91, -17.13),
        -121.7,
        (657.8, 842.2, 669.3, 866.9),
        (-35.55, -31.72),
        "approach_track",
        0.9488,
    ),
    (
        53,
        (-27.16, -16.93, -17.10),
        -120.3,
        (634.5, 835.6, 645.6, 859.5),
        (-35.57, -31.72),
        "reject_track",
        0.9626,
    ),
    # The next logged frame (13.4 px); the replayed r18 model says "approach" (0.958).
    (
        54,
        (-27.19, -16.96, -17.13),
        -95.3,
        (420.4, 877.5, 433.7, 903.9),
        (-35.63, -31.67),
        "approach_track",
        0.958,
    ),
]
F7166_H0 = [
    (
        3,
        (-0.01, 0.04, -17.06),
        -84.6,
        (834.2, 884.0, 846.7, 910.5),
        (8.77, -15.02),
        "approach_track",
        0.9703,
    ),
    (
        4,
        (-0.02, -0.01, -17.08),
        -69.7,
        (716.3, 837.8, 727.3, 862.6),
        (8.74, -14.99),
        "approach_track",
        0.8312,
    ),
    (
        5,
        (0.04, -0.01, -17.05),
        -61.1,
        (643.0, 833.9, 654.0, 858.2),
        (8.70, -15.00),
        "reject_track",
        0.9837,
    ),
]


def _plan(skill="count", params=None):
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "count hydrants",
            "steps": [
                {
                    "id": "s1",
                    "skill": skill,
                    "params": params or {"region": "zone:site", "query": "fire hydrant"},
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


def _executor(plan=None, **config):
    def make_loop(task_text, region=None, obstacle_source=None, **overrides):
        return OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
            config=LoopConfig(**{**SITL_LOOP, **overrides}),
            obstacle_source=obstacle_source,
        )

    return MissionExecutor(
        plan or _plan(),
        loop_factory=make_loop,
        refs={"zone:site": ZONE},
        config=ExecutorConfig(step_budget=140, **config),
        obstacle_source=HYDRANT_WORLD,
    )


def _replay(executor, frames):
    """Feed logged frames and verdicts; return (action, info) after each frame."""
    out = []
    for _, pose, yaw, box, ground, decision, confidence in frames:
        for track in executor.observe(
            [Detection(box, 0.9, "fire hydrant")],
            pose_ned=pose,
            yaw_deg=yaw,
            ground_point=lambda u, v, ground=ground: ground,
        ):
            executor.apply_verdict(track.track_id, decision, confidence)
        out.append(
            executor.next_action(
                pose_ned=pose, yaw_deg=yaw, ground_point=lambda u, v, ground=ground: ground
            )
        )
    return out


def _track_near(loop, point):
    (track,) = [t for t in loop.tracks.values() if t.ground and math.dist(t.ground, point) < 1.0]
    return track


# -- one small "no" after two positive views -------------------------------------


def test_lone_small_no_at_the_closest_view_cancels_one_positive_and_h2_is_counted():
    executor = _executor()
    results = _replay(executor, F517_H2[:3])
    loop = executor.active_loop
    track = _track_near(loop, H2)
    # The 11 px "no" leaves the track positive with one view left.
    assert track.verdict == "approach"
    assert len(track.match_steps) == 1
    assert loop.netted_rejections[0]["min_side_px"] == pytest.approx(11.1, abs=0.1)
    assert not loop.rejected_far_small_places(15.0)
    _, info = results[-1]
    assert info.get("focus") == track.track_id and loop.state == "approaching"
    # The focus is asked again from the next view; one more "approach" counts it.
    assert track in loop.tracks_needing_verdict([track])
    _replay(executor, F517_H2[3:])
    assert executor.counts.get("s1") == 1
    (record,) = loop.confirmations
    assert record["rule"] == "identity_at_closest_view"
    assert math.dist(record["position_estimate_ne"], H2) < 1.0


def test_without_net_votes_the_same_frames_drop_h2_as_in_sitl():
    executor = _executor(count_small_reject_net_votes=0)
    _replay(executor, F517_H2[:3])
    loop = executor.active_loop
    track = _track_near(loop, H2)
    assert track.verdict == "no_match" and not track.match_steps
    _replay(executor, F517_H2[3:])
    assert not executor.counts.get("s1")


def test_lone_small_no_at_the_start_keeps_closing_in_on_h0():
    executor = _executor()
    action, info = _replay(executor, F7166_H0)[-1]
    loop = executor.active_loop
    track = _track_near(loop, H0)
    assert track.verdict == "approach"
    assert info.get("focus") == track.track_id
    # SITL turned away to resume the search; the approach goes on toward h0.
    assert action.kind == "translate" and action.forward_m > 0
    baseline = _executor(count_small_reject_net_votes=0)
    _replay(baseline, F7166_H0)
    assert _track_near(baseline.active_loop, H0).verdict == "no_match"
    assert baseline.active_loop.state == "searching"


def _loop(**overrides):
    return OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
        config=LoopConfig(**{**SITL_LOOP, **overrides}),
        obstacle_source=HYDRANT_WORLD,
    )


def _judge(loop, verdicts, *, side_px=11.0):
    """One h2-like track at the closest-view pose, judged once per frame."""
    pose = (-27.2, -16.9, -17.1)
    track = None
    for decision, side in verdicts:
        side = side_px if side is None else side
        box = (640.0, 900.0 - 2.0 * side, 640.0 + side, 900.0)
        loop.step_index += 1
        (track,) = loop.update_tracks(
            [Detection(box, 0.9, "fire hydrant")],
            pose_ned=pose,
            yaw_deg=-120.0,
            ground_point=lambda u, v: H2,
        )
        loop.apply_verdict(track.track_id, decision, 0.96)
    return track


@pytest.mark.parametrize(
    ("verdicts", "netted"),
    [
        # Two positive views, then a small "no": one view is cancelled.
        ([("approach_track", None), ("approach_track", None), ("reject_track", None)], True),
        # One positive view is not enough to outweigh a "no".
        ([("approach_track", None), ("reject_track", None)], False),
        # A "no" at the confirmation size stands.
        ([("approach_track", None), ("approach_track", None), ("reject_track", 22.0)], False),
    ],
)
def test_only_a_small_no_after_two_positive_views_is_netted(verdicts, netted):
    loop = _loop(small_reject_net_votes=1)
    track = _judge(loop, verdicts)
    if netted:
        assert track.verdict == "approach" and len(track.match_steps) == 1
        assert len(loop.netted_rejections) == 1
    else:
        assert track.verdict == "no_match" and not track.match_steps
        assert not loop.netted_rejections


def test_a_second_small_no_resets_the_track():
    loop = _loop(small_reject_net_votes=1)
    track = _judge(
        loop,
        [
            ("approach_track", None),
            ("approach_track", None),
            ("reject_track", None),
            ("approach_track", None),
            ("reject_track", None),
        ],
    )
    assert track.verdict == "no_match" and not track.match_steps
    assert len(loop.netted_rejections) == 1
    assert track.identity_verdict_history == [
        "approach",
        "approach",
        "no_match",
        "approach",
        "no_match",
    ]


def test_net_votes_are_off_by_default_and_on_only_for_count_steps():
    loop = _loop(small_reject_net_votes=0)
    track = _judge(
        loop, [("approach_track", None), ("approach_track", None), ("reject_track", None)]
    )
    assert track.verdict == "no_match"
    assert LoopConfig().small_reject_net_votes == 0
    assert _executor().active_loop.config.small_reject_net_votes == 1
    for skill, params in (
        ("area_search", {"region": "zone:site", "query": "fire hydrant"}),
        ("monitor", {"region": "zone:site", "query": "backpack", "duration_s": 60}),
    ):
        assert _executor(_plan(skill, params)).active_loop.config.small_reject_net_votes == 0


@pytest.mark.parametrize("value", [-1, True, 1.0])
def test_net_vote_limits_are_validated(value):
    with pytest.raises(ValueError):
        LoopConfig(small_reject_net_votes=value)
    with pytest.raises(ValueError):
        ExecutorConfig(count_small_reject_net_votes=value)


def test_finished_count_reports_the_netted_rejections():
    executor = _executor()
    _replay(executor, F517_H2[:3])
    executor.active_loop.state = "completed"
    for _ in range(40):  # any closer look runs out; the step then finishes
        if executor.history:
            break
        executor.next_action(
            pose_ned=(-27.2, -16.9, -17.1), yaw_deg=-120.0, ground_point=lambda u, v: None
        )
    (netted,) = executor.history[0]["small_rejects_netted"]
    assert netted["min_side_px"] == pytest.approx(11.1, abs=0.1)
    assert netted["positive_views_left"] == 1


# -- a count closer look is not ended by a smaller box on the way -----------------

# 2435e7b9 step 48: h2 rejected at 15.6 px (box at the image bottom, 0.998).
H2_REJECTED = (48, (-27.39, -17.25, -17.20), -89.4, (363.9, 931.2, 379.5, 959.7), (-35.56, -31.70))
# Step 71: the coverage pass is over and the rejection look starts from here.
LOOK_START = ((14.13, 11.19, -16.76), 134.4)
# Step 75, on the way at the cruise altitude: 12.0 px, judged "approach" (0.94).
H2_ON_THE_WAY = ((-12.11, -24.79, -17.17), (264.1, 805.5, 276.1, 828.5))


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


def _size_m(pose, box, ground):
    """Object size from a logged box: side x optical-axis depth / f."""
    u, v = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    slant = math.hypot(math.dist(ground, pose[:2]), -pose[2])
    depth = slant / math.hypot(1.0, (u - CAMERA.cx) / F, (v - CAMERA.cy) / F)
    return min(box[2] - box[0], box[3] - box[1]) * depth / F


def _render(sim, obj, size_m):
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
    box = (u - side / 2, v - 2.0 * side, u + side / 2, v)
    if box[0] < 0 or box[2] > CAMERA.width or box[1] < 0 or box[3] > CAMERA.height:
        return None
    return Detection(box, 0.9, "fire hydrant")


def _model(side):
    """As the r18 model judged h2 in SITL: "no" below 11.5 px, yes above, confirm from 20 px."""
    if side >= 20.0:
        return "confirm_target"
    return "approach_track" if side >= 11.5 else "reject_track"


def _fly_rejection_look(executor):
    _, pose, yaw, box, ground = H2_REJECTED
    for track in executor.observe(
        [Detection(box, 0.9, "fire hydrant")],
        pose_ned=pose,
        yaw_deg=yaw,
        ground_point=lambda u, v: ground,
    ):
        executor.apply_verdict(track.track_id, "reject_track", 0.9982)
    executor.next_action(pose_ned=pose, yaw_deg=yaw, ground_point=lambda u, v: ground)
    loop = executor.active_loop
    loop.step_index += 23  # the coverage pass ends at step 71
    loop.state = "completed"
    sim = Sim(*LOOK_START)
    size = _size_m(H2_ON_THE_WAY[0], H2_ON_THE_WAY[1], H2)
    judged, resolved_at = [], None
    for _ in range(80):
        detection = _render(sim, H2, size)
        for track in executor.observe(
            [] if detection is None else [detection],
            pose_ned=sim.ned,
            yaw_deg=sim.yaw,
            ground_point=lambda u, v: H2,
        ):
            side = min(track.bbox[2] - track.bbox[0], track.bbox[3] - track.bbox[1])
            judged.append((round(side, 1), round(-sim.pose[2], 1)))
            executor.apply_verdict(track.track_id, _model(side), 0.9)
        action, info = executor.next_action(
            pose_ned=sim.ned, yaw_deg=sim.yaw, ground_point=lambda u, v: H2
        )
        if resolved_at is None and "small_unjudged_revisit_outcome" in info:
            resolved_at = (sim.ned, info["small_unjudged_revisit_outcome"])
        if executor.counts.get("s1") == 1:
            break
        sim.apply(action)
    return executor, judged, resolved_at


def test_rejection_look_flies_on_past_a_smaller_positive_box_and_counts_h2():
    executor, judged, resolved_at = _fly_rejection_look(_executor())
    (record,) = executor._run.rejection_revisits
    assert record["target_side_px"] == pytest.approx(15.6, abs=0.1)
    # A box below the look's target was judged "approach" on the way at the
    # cruise altitude (as in SITL), and the look went on to its viewpoint.
    assert any(11.5 <= side < 15.6 and alt > 16.0 for side, alt in judged)
    assert resolved_at is not None
    pose, outcome = resolved_at
    assert outcome in {"judged_approach", "judged_confirm"}
    assert -pose[2] < record["view_altitude_m"] + 1.0
    assert math.dist(pose[:2], record["viewpoint"]) < 4.0
    assert executor.counts.get("s1") == 1
    (confirmed,) = executor.active_loop.confirmations
    assert math.dist(confirmed["position_estimate_ne"], H2) < 1.0


def _start_look(loop, look_kind):
    """A small unjudged / alert look at h2 started from the SITL look start."""
    place = {
        "centre": H2,
        "extent_m": 0.49,
        "min_side_px": 8.0,
        "slant_m": 33.0,
        "target_side_px": 15.6,
        "track_ids": ["t1"],
        "track": None,
    }
    look, _ = loop.plan_small_unjudged_revisit(place, LOOK_START[0])
    if look_kind == "alert":
        look["alert_look"] = True
    loop.begin_place_revisit(look, LOOK_START[0], LOOK_START[1], {})
    return look


@pytest.mark.parametrize(("kind", "resolved"), [("count", None), ("alert", "judged_approach")])
def test_only_count_looks_ignore_a_smaller_box_judged_on_the_way(kind, resolved):
    """The 12 px "approach" 24 m out ends a patrol/monitor look (its alert), not a count look."""
    loop = _loop()
    look = _start_look(loop, kind)
    loop.step_index += 1
    (track,) = loop.update_tracks(
        [Detection(H2_ON_THE_WAY[1], 0.9, "fire hydrant")],
        pose_ned=H2_ON_THE_WAY[0],
        yaw_deg=-126.1,
        ground_point=lambda u, v: H2,
    )
    loop.apply_verdict(track.track_id, "approach_track", 0.94)
    assert look["phase"] == "fly"
    assert loop._small_unjudged_resolution(look) == resolved


# -- a clipped "no" is no usable vote ---------------------------------------------

# 3121ac38: h2 clipped at the image bottom (31.4 x 20.5 px), rejected at 0.74 ...
CLIPPED_NO = ((-18.84, -30.34, -17.15), -142.3, (333.8, 939.4, 365.2, 959.9), (-35.68, -31.92))
# ... then the same track, unclipped at 22.8 px from 9.5 m, "confirm" (0.57).
UNCLIPPED_CONFIRM = (
    (-26.81, -31.69, -9.47),
    -173.3,
    (574.5, 896.3, 597.3, 949.3),
    (-35.48, -31.75),
)


def _vote(loop, frame, decision, confidence):
    pose, yaw, box, ground = frame
    loop.step_index += 1
    (track,) = loop.update_tracks(
        [Detection(box, 0.9, "fire hydrant")],
        pose_ned=pose,
        yaw_deg=yaw,
        ground_point=lambda u, v: ground,
    )
    loop.apply_verdict(track.track_id, decision, confidence)
    return track


def test_a_no_on_a_box_cut_by_the_image_border_does_not_contradict_a_confirm():
    loop = _loop()
    track = _vote(loop, CLIPPED_NO, "reject_track", 0.7363)
    assert loop._place_votes[-1]["usable"] is False
    track = _vote(loop, UNCLIPPED_CONFIRM, "confirm_target", 0.5664)
    assert not track.contradiction_active
    assert track.verdict == "confirm" and track.confirm_steps == [loop.step_index]


def test_an_unclipped_usable_no_still_starts_a_contradiction_relook():
    pose, yaw, box, ground = CLIPPED_NO
    lifted = (box[0], box[1] - 40.0, box[2], box[3] - 40.0)
    loop = _loop()
    _vote(loop, (pose, yaw, lifted, ground), "reject_track", 0.7363)
    assert loop._place_votes[-1]["usable"] is True
    track = _vote(loop, UNCLIPPED_CONFIRM, "confirm_target", 0.5664)
    assert track.contradiction_active
