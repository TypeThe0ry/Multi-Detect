"""CPU-only regressions for the opt-in X1 FW route and reachability gate."""

import math
from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.gimbal_recording import x1_eval_yaw_unwind
from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget, rotation_deg
from multidetect.sim_teacher.target_tracking import (
    WorldTarget, c11_optical_axis_reachable, level_target_rotation, world_target_direction,
)
from multidetect.terrain_dem import LocalDemGrid
from scripts.realworld import long_range_flight as flight
from scripts.realworld.long_range_flight import gimbal_eval_fw_targets
from scripts.realworld.x1_gimbal_eval import TRACK_FW_MODE, TRACK_SAMPLE, fw_legs


def test_fw_yaw_replay_stays_tracking_but_hover_preaim_still_unwinds():
    # R1 repeatedly crossed -150 on approach to -180; azimuth can also wrap at +/-180.
    yaw_deg = [-144.4, -152.2, -179.5, 179.8, 176.0, -345.3, -16.4]
    action = SimpleNamespace(reason="hover_return_to_home")
    for fw_leg, expected in ((True, [None] * 7), (False, [None, "lock", None, None, None, None, "track"])):
        unwind = x1_eval_yaw_unwind(4, 3, action, fw_leg=fw_leg)
        assert (unwind is None) is fw_leg
        decisions = [unwind.step(math.radians(yaw), i * 0.1) if unwind else None
                     for i, yaw in enumerate(yaw_deg)]
        assert decisions == expected
    assert x1_eval_yaw_unwind(4, 0, SimpleNamespace(reason="reposition"), fw_leg=True) is None


def test_bridge_track_selection_uses_explicit_leg_target_and_preserves_state():
    origin = WorldTarget(42.0, -93.0, 400.0)
    hover = WorldTarget(42.0001, -93.0, 320.0)
    selected = WorldTarget(41.99, -93.015, 321.0)
    bridge = BridgeHorizonTarget(mode="track", track_targets=(hover,))
    state = bridge.last_joint, bridge._command_history
    angles = dict(roll=0., pitch=-83., yaw=0.)
    assert bridge.update(angles, now_s=1., attitude_time_s=1., position=origin)["target_ref"] == hover.reference()
    bridge.select_track_target(selected)
    cmd = bridge.update(angles, now_s=2., attitude_time_s=2., position=origin)
    assert cmd["mode"] == "track" and cmd["target_ref"] == selected.reference()
    assert bridge.last_joint is state[0] and bridge._command_history is state[1]
    bridge.select_track_target(hover)
    assert bridge.update(angles, now_s=3., attitude_time_s=3., position=origin)["target_ref"] == hover.reference()


def test_ground_targets_remain_in_envelope_at_r1_return_pitch():
    home = SimpleNamespace(latitude_deg=42., longitude_deg=-93.)
    dem = LocalDemGrid("flat", 42., -93., 320., -3000., -3000., 6000., 6000.,
                       ((320., 320.), (320., 320.)), 1., 0., "a" * 64)
    legs = [(1600., 0., -85.), (0., 600., -85.)]
    mission = {"terrain": dem, "geometry": SimpleNamespace(home=home, takeoff_altitude_m=65.),
               "source": {"sim_params": {"FW_R_LIM": 35.}}, "targets": legs}
    targets = gimbal_eval_fw_targets(mission)
    assert len(set(targets)) == 2 and all(p.alt_m == 320 for p in targets)
    radius = 6_378_137.
    for start, end, point in zip([(0., 0., -65.), legs[0]], legs, targets, strict=True):
        dn, de, dd = (b - a for a, b in zip(start, end, strict=True))
        yaw = math.degrees(math.atan2(de, dn))
        for f in (i / 20 for i in range(21)):
            pos = WorldTarget(42 + math.degrees((start[0] + f*dn) / radius),
                              -93 + math.degrees((start[1] + f*de) / (radius * math.cos(math.radians(42)))),
                              320 - start[2] - f*dd)
            for roll in (-35., 0., 35.):
                body = rotation_deg(dict(roll=roll, pitch=-83., yaw=yaw))
                assert c11_optical_axis_reachable(world_target_direction(pos, point, body), margin_deg=5.)
    old_point = WorldTarget(42.0009, -93., 325.)
    returning = rotation_deg(dict(roll=0., pitch=-83., yaw=180.))
    assert not c11_optical_axis_reachable(world_target_direction(WorldTarget(42.03, -93., 405.), old_point, returning))


def _mission(legs, *, takeoff=85., sloped=False):
    heights = ((312., 314.), (326., 328.)) if sloped else ((320., 320.), (320., 320.))
    dem = LocalDemGrid("fixture", 42., -93., 320., -3000., -3000., 6000., 6000.,
                       heights, 1., 0., "b" * 64)
    home = SimpleNamespace(latitude_deg=42., longitude_deg=-93.)
    return {"terrain": dem, "geometry": SimpleNamespace(home=home, takeoff_altitude_m=takeoff),
            "source": {"sim_params": {"FW_R_LIM": 35.}}, "targets": legs}


def _world_point(dem, north, east, down):
    return WorldTarget(42. + math.degrees(north / 6378137.),
                       -93. + math.degrees(east / (6378137. * math.cos(math.radians(42.)))),
                       dem.origin_elevation_m - down)


@pytest.mark.parametrize("closest,over_limit", [(-1., False), (-90., True)])
def test_fw_minimizes_switch_angle_before_static_quality(monkeypatch, caplog, closest, over_limit):
    mission = _mission([(20., 0., -85.), (60., 0., -85.)], sloped=True)
    dem = mission["terrain"]
    # Keep the measured-origin, candidate filtering and ranking paths real. Fix the
    # switch location to isolate angular ranking from updates to the pose model.
    monkeypatch.setattr(flight, "waypoint_turn", lambda end, *_args, **_kw: SimpleNamespace(
        switch_ned=(end[0]+5., end[1], end[2]), arriving=(), departing=()))
    # First leg has one choice. Second leg offers the static winner, an exact
    # repeat of the first anchor, then the closest fresh reachable point.
    def offsets(length, radius):
        return [(20., 0.)] if length == 20. else [(-300., 0.), (0., 0.), (closest, 0.)]
    monkeypatch.setattr(flight, "_fw_ground_offsets", offsets)
    static_only, = gimbal_eval_fw_targets({**mission, "targets": [(40., 0., -85.)]})
    assert dem.local_from_wgs84(static_only.lat_deg, static_only.lon_deg)[0] == pytest.approx(-300., abs=1.)
    diag = []
    prev, target = gimbal_eval_fw_targets(mission, diagnostics=diag)
    for point, north in ((prev, 20.), (target, 20.+closest)):
        # A sampled height from a nonflat DEM must be preserved in the AMSL ref.
        ne = dem.local_from_wgs84(point.lat_deg, point.lon_deg)
        assert ne == pytest.approx((north, 0.), abs=.5)
        assert point.reference()["altitude_reference"] == "AMSL"
        assert point.alt_m == pytest.approx(dem.sample_local(*ne).elevation_m)
    assert target != prev
    assert diag[0]["switch_los_deg"] is None
    assert diag[1]["switch_ned_m"] == pytest.approx((25., 0., -85.))
    origin = _world_point(dem, *diag[1]["switch_ned_m"])
    u, v = (world_target_direction(origin, p) for p in (prev, target))
    angle = math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(u, v))))))
    assert diag[1]["switch_los_deg"] == pytest.approx(angle, abs=1e-6)
    assert diag[1]["over_limit"] is over_limit and (angle > 30.) is over_limit
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("30" in msg and "1" in msg for msg in warnings) is over_limit


def test_iowa_switch_pose_matches_recorded_wing_direction_and_body_attitude():
    # First guided loiter handoff in Iowa 04caae4291f6e47848bd. This compares
    # full MC-FRD rotations. Matching Euler yaw to the wing course fails here.
    waypoint = (850., -850., -82.11)
    turn = flight.waypoint_turn(waypoint, -45., -40.156, 80., None,
                                plane_pass=False, short_approach=False)
    observation = (912.411, -894.243, -82.513)
    assert math.dist(turn.switch_ned, observation) < 20.
    actual = rotation_deg(dict(roll=69.898, pitch=-71.033, yaw=-14.486))
    # The trace of A^T B is the elementwise inner product of two rotations.
    # Planning stores world-to-body matrices, while rotation_deg returns body-to-world.
    expected = [actual[3*j+i] for i in range(3) for j in range(3)]
    for poses in (turn.arriving, turn.departing):
        alignments = [sum(a*b for a, b in zip(expected, body)) for pos, bodies in poses
                      if math.dist(pos, observation) < 20. for body in bodies]
        assert alignments and max(alignments) >= 1. + 2.*math.cos(math.radians(15.))


def test_iowa_waypoint_rejects_only_candidate_at_measured_loiter_pose(monkeypatch):
    # Iowa 04caae4291f6e47848bd, episode-0000-88387bae, track switch into step 1.
    # Local NED and MC-FRD ATTITUDE share the simulation clock. The wing heading
    # was +56.42 deg at this instant while MC Euler yaw was -14.49 deg.
    end = (850., -850., -82.11)
    mission = _mission([end], takeoff=65.)
    dem = mission["terrain"]
    # 1.5 km to the right of the inbound leg. The old straight envelope admits it.
    target = _world_point(dem, 1500./math.sqrt(2), 1500./math.sqrt(2), 0.)
    for j in range(9):
        f = j/8
        pos = _world_point(dem, f*end[0], f*end[1], -65. + f*(end[2]+65.))
        for pitch in (-90., -83., -65.):
            for roll in (-35., 0., 35.):
                body = rotation_deg(dict(roll=roll, pitch=pitch, yaw=-45.))
                assert c11_optical_axis_reachable(world_target_direction(pos, target, body), margin_deg=5.)
    observed = _world_point(dem, 912.411, -894.243, -82.513)
    actual = rotation_deg(dict(roll=69.898, pitch=-71.033, yaw=-14.486))
    assert not c11_optical_axis_reachable(world_target_direction(observed, target, actual), margin_deg=5.)
    monkeypatch.setattr(flight, "_fw_ground_offsets", lambda *_: [(0., 1500.)])
    with pytest.raises(ValueError, match="leg 0"):
        gimbal_eval_fw_targets(mission)


ORIGIN = WorldTarget(42., -93., 400.)
TARGETS = [WorldTarget(41.99, -93., 320.), WorldTarget(42., -93.02, 320.)]
REFS = [point.reference() for point in TARGETS]
META = {"horizon_stabilization": {"eval_fw_legs": 2, "eval_fw_target_refs": REFS}}
MODES = [{"message_type": TRACK_FW_MODE, "step_index": i, "mode": "track", "target_ref": ref}
         for i, ref in enumerate(REFS)]


def sample(i, step, phase, *, pitch=-83., yaw=0., optical=None, mode="track"):
    return {"message_type": TRACK_SAMPLE, "step_index": step, "fw_phase": phase, "sim_stamp_ns": i * 10**8,
            "track_mode": mode, "sim_skew_ms": 0., "attitude_skew_ms": 0.,
            "attitude": {"roll_deg": 0., "pitch_deg": pitch, "yaw_deg": yaw},
            "pose": {"lat_deg": ORIGIN.lat_deg, "lon_deg": ORIGIN.lon_deg, "alt_amsl_m": ORIGIN.alt_m,
                     "local_ned_m": (0., 0., 0.), "global_position_skew_ms": 0.},
            "gimbal_state": {"source": "gazebo_joint_state", "fresh": True},
            "camera_pose": {"age_ms": 0., "center_ned": (0., 0., 0.), "optical_to_world":
                            level_target_rotation(world_target_direction(ORIGIN, TARGETS[step]))
                            if optical is None else optical},
            "camera_info": {"fx": 100, "fy": 100, "cx": 50, "cy": 40, "width": 100, "height": 80}}


def good_rows():
    return [sample(i, i % 2, "cruise" if i < 120 else "turn") for i in range(150)]


def test_fw_gate_uses_each_legs_point_and_checks_plan_binding():
    rows = good_rows()
    report = fw_legs(META, [*MODES, *rows, rows[-1]])
    assert report["passed"] and report["reachable_coverage"] == 1.
    assert report["labeled_samples"] == 150
    assert report["cruise"]["pointing_error_deg"]["p95"] == pytest.approx(0., abs=1e-5)
    assert report["turn"]["lock_rate"] == 1.
    assert report["gate"]["min_reachable_coverage"] == .8
    assert [row["target_ref"] for row in report["by_leg"]] == REFS
    for invalid in (MODES[:1], [MODES[0], {**MODES[1], "target_ref": REFS[0]}]):
        assert fw_legs(META, [*invalid, *rows])["status"] == "target_ref_unavailable"


def test_unreachable_and_unknown_reduce_coverage_even_when_pointing_is_perfect():
    rows = good_rows() + [sample(200+i, 0, "cruise", yaw=180.) for i in range(50)]
    invalid_pose = {**sample(300, 0, "cruise"), "attitude": None}
    missing_phase = {**sample(301, 0, None), "vtol_state": 4, "attitude": None}
    outside_fw = {**sample(302, 0, None), "vtol_state": 3}
    report = fw_legs(META, [*MODES, *rows, invalid_pose, missing_phase, outside_fw])
    assert report["failed_checks"] == ["reachable_coverage"]
    assert report["reachable_coverage"] == pytest.approx(150 / 202)
    assert report["unreachable_samples"] == 50 and report["reachability_unknown_samples"] == 2
    assert report["unlabeled_samples"] == 1


def test_reachable_misses_and_unresolved_optics_do_not_leave_lock_denominator():
    opposite = rotation_deg(dict(roll=0., pitch=0., yaw=0.))
    locked_misses = [sample(i, 0, "cruise", mode="lock", optical=opposite) for i in range(12)]
    unresolved = sample(12, 0, "cruise")
    del unresolved["camera_pose"]["optical_to_world"]
    report = fw_legs(META, [*MODES, *locked_misses, unresolved, *good_rows()[13:]])
    assert report["reachable_coverage"] == 1. and report["unwind_fraction"] == .08
    assert report["cruise"]["sample_count"] == 120
    assert report["cruise"]["lock_rate"] == pytest.approx(107 / 120)
    assert report["failed_checks"] == ["cruise_p95", "cruise_lock_rate"]


def test_turn_gate_scores_immediate_post_switch_samples():
    # Three misses in the first 0.3 s exceed the full-turn P95 budget.
    modes = [{**MODES[0], "sim_stamp_ns": 0}, {**MODES[1], "sim_stamp_ns": 12_000_000_000}]
    rows = [sample(i, 0, "cruise") for i in range(120)]
    away = rotation_deg(dict(roll=0., pitch=0., yaw=0.))
    rows += [sample(120+i, 1, "turn", optical=away if i < 3 else None) for i in range(40)]
    report = fw_legs(META, [*modes, *rows])
    assert report["gate"]["turn"] == {"max_p95_deg": 10., "min_samples": 20}
    assert report["gate"]["cruise"] == {"max_p95_deg": 5., "min_samples": 100}
    assert report["gate"]["min_lock_rate"] == .9 and report["gate"]["min_reachable_coverage"] == .8
    assert report["turn"]["sample_count"] == 40
    assert report["turn"]["lock_rate"] == pytest.approx(37/40)
    assert report["failed_checks"] == ["turn_p95"]
