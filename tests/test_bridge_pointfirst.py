"""Wire-pose regressions: the C11 bridge solves a static endpoint, then slews to it."""

import math

import pytest
from test_horizon_stabilization import _multiply, _quaternion_rotation, _rotation

from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget

AXES = ("roll", "pitch", "yaw")
ZYX = ("yaw", "pitch", "roll")  # FRD body -> NED
ZXY = ("yaw", "roll", "pitch")  # physical C11 chain
ZERO = (0., 0., 0.)

# R2 rw_farm_iowa__clear_noon/533e0e74f6fe34f37de6, episode-0000-4f06a3d3.
# collector_gimbal_eval_sample at sim_stamp_ns: body RPY (deg), unit NED ray
# from camera center to the leg target, measured joint RPY (deg). No NAS at test time.
R2_130 = ((-19.008456, -83.089531, -62.379672), (-.6871839742, .7240886287, .0589393192),
          (-35.149448, -85.367900, -177.805580))
R2_144 = ((69.400728, -71.458543, -45.792781), (-.6911704875, .7208006355, .0522474980),
          (44.999963, -72.238765, -180.536448))
R2_433_5 = ((-90.225914, -53.463987, -55.757139), (.8872853321, -.3493561263, .3011229589),
            (44.999991, -134.998920, -46.147960))
MC = ((3., 2., 27.), (.8, .6, .4), ZERO)


def _rpy(values):
    return dict(zip(AXES, values, strict=True))


def _transpose(m):
    return m[0::3] + m[1::3] + m[2::3]


def _level(ray):
    n, e, d = ray
    return _rotation(_rpy((0., math.degrees(math.atan2(-d, math.hypot(n, e))),
                           math.degrees(math.atan2(e, n)))), ZYX)


def _bounded_joints(relative, previous):
    """Independent GZGimbal::boundedJoints oracle (degrees, before actuator slew)."""
    r = math.asin(max(-1., min(1., relative[7])))
    if abs(math.cos(r)) < 1e-6:
        y = previous[2]
        base = _rotation(_rpy((math.degrees(r), 0., y)), ZXY)
        residual = _multiply(_transpose(base), relative)
        p = math.atan2(residual[2], residual[0])
    else:
        y = math.degrees(math.atan2(-relative[1], relative[4]))
        p = math.atan2(-relative[6], relative[8])
    r, p = math.degrees(r), math.degrees(p)
    candidates = []
    for branch in ((r, p, y), (180. - r, p + 180., y + 180.)):
        roll, pitch, yaw = (prev + math.remainder(a - prev, 360.)
                            for a, prev in zip(branch, previous, strict=True))
        # nearestTurnWithin: use an equivalent yaw inside the cable window; don't clip it.
        if yaw > 360.:
            yaw -= 360.
        elif yaw < -360.:
            yaw += 360.
        joints = (max(-45., min(45., roll)), max(-135., min(45., pitch)), yaw)
        exact = max(abs(joints[0] - roll), abs(joints[1] - pitch)) < math.degrees(1e-6)
        candidates.append((not exact, math.dist(joints, previous), joints))
    clipped, _, joints = min(candidates, key=lambda c: c[:2])
    return joints, not clipped


def _track_pose(controller, body, ray, *, t=1., rates=None):
    result = controller.update(_rpy(body), now_s=t, attitude_time_s=t,
                               track_direction_ned=ray, body_rates_rad_s=rates)
    assert result["transport_flags"] == 92  # roll/pitch lock, earth yaw
    q = result["quaternion"]
    assert math.hypot(*q) == pytest.approx(1., abs=1e-10)
    return _quaternion_rotation(q)


def _endpoint(body, command, previous):
    joints, exact = _bounded_joints(_multiply(_transpose(body), command), previous)
    assert exact, "transmitted earth pose requires bridge clipping"
    relative = _rotation(_rpy(joints), ZXY)
    assert _multiply(body, relative) == pytest.approx(command, abs=1e-8, rel=0)
    return relative


def _error_deg(a, b):
    # Chord length is well-conditioned for the near-zero pointing errors of interest.
    a, b = (tuple(x / math.hypot(*v) for x in v) for v in (a, b))
    return math.degrees(2. * math.asin(min(1., math.dist(a, b) / 2.)))


@pytest.mark.parametrize("case", [MC, R2_130], ids=["mc-hover", "fw-r2-130s"])
def test_feasible_track_preserves_level_earth_quaternion(case):
    body, ray, previous = case
    track = BridgeHorizonTarget(mode="track", track_preview_s=0.)
    track.last_joint.update(_rpy(previous))
    command = _track_pose(track, body, ray)
    assert command == pytest.approx(_level(ray), abs=1e-8, rel=0)
    _endpoint(_rotation(_rpy(body), ZYX), command, previous)


@pytest.mark.parametrize("case", [R2_144, R2_433_5], ids=["leg0-144s", "leg4-433.5s"])
def test_unpreviewed_r2_turn_preserves_pointing_without_bridge_clipping(case):
    body, ray, previous = case
    b = _rotation(_rpy(body), ZYX)
    # Negative control: sending the old level quaternion alone misses after bridge FK.
    legacy, exact = _bounded_joints(_multiply(_transpose(b), _level(ray)), previous)
    assert not exact
    assert _error_deg(_multiply(b, _rotation(_rpy(legacy), ZXY))[0::3], ray) > 10.

    track = BridgeHorizonTarget(mode="track", track_preview_s=0.)
    track.last_joint.update(_rpy(previous))
    command = _track_pose(track, body, ray)
    relative = _endpoint(b, command, previous)
    assert _error_deg(_multiply(b, relative)[0::3], ray) < 1.


@pytest.mark.parametrize("case,rates", [
    (MC, (0., 0., math.radians(60.))),
    (R2_144, (.186043218, .056092773, .017982649)),  # ATTITUDE at 143.992 s, rad/s
], ids=["mc-level-preview", "fw-r2-roll-stop"])
def test_preview_uses_bounded_endpoint_in_current_bridge_frame(case, rates):
    body, ray, previous = case
    seconds, cap = .3, math.radians(30.)  # explicit configuration, not an FW default
    b = _rotation(_rpy(body), ZYX)
    half = min(seconds * math.hypot(*rates), cap) / 2.
    delta = (math.cos(half), *(v * math.sin(half) / math.hypot(*rates) for v in rates))
    anticipated = _multiply(b, _quaternion_rotation(delta))
    track = BridgeHorizonTarget(mode="track", track_preview_s=seconds, track_preview_max_rad=cap)
    track.last_joint.update(_rpy(previous))
    command = _track_pose(track, body, ray, rates=rates)
    relative = _endpoint(b, command, previous)
    at_preview = _multiply(anticipated, relative)
    assert _error_deg(at_preview[0::3], ray) < 1e-6
    if case == MC:
        assert at_preview == pytest.approx(_level(ray), abs=1e-8, rel=0)


def test_only_track_continuity_state_slews_and_replay_is_deterministic():
    def replay():
        previous = (0., 0., 165.)
        body = (25., 0., 0.)
        b = _rotation(_rpy(body), ZYX)
        track = BridgeHorizonTarget(mode="track", track_preview_s=0.)
        track.last_joint.update(_rpy(previous))
        initial = _rotation(_rpy(previous), ZXY)
        _track_pose(track, ZERO, initial[0::3], t=0.)  # known aligned initial state
        history = []
        last_t = 0.
        for t, yaw in ((.02, -160.), (.05, -158.), (.06, -162.)):
            level = _rotation(_rpy((0., -30., yaw)), ZYX)
            endpoint, exact = _bounded_joints(_multiply(_transpose(b), level), previous)
            assert exact and endpoint[2] > 180.  # continue positive yaw winding
            command = _track_pose(track, body, level[0::3], t=t)
            assert command == pytest.approx(level, abs=1e-8, rel=0)  # no second slew on the wire
            step = 180. * (t - last_t)
            expected = tuple(p + max(-step, min(step, e - p))
                             for p, e in zip(previous, endpoint, strict=True))
            state = tuple(track.last_joint[a] for a in AXES)
            assert state == pytest.approx(expected, abs=1e-8, rel=0)
            history.append((command, state))
            last_t, previous = t, state
        return history

    assert replay() == replay()


@pytest.mark.parametrize("mode,yaw_mode,flags", [
    ("lock", "lock", 92), ("lock", "follow", 44), ("follow", "follow", 32),
])
def test_switch_from_track_preserves_lock_and_follow_wire_contract(mode, yaw_mode, flags):
    target = _rpy((13., -24., 33.))
    control = BridgeHorizonTarget(target, mode="track", yaw_mode=yaw_mode, track_preview_s=.3)
    body, ray, _ = R2_144
    _track_pose(control, body, ray)
    control.set_mode(mode)
    result = control.update(_rpy(body), now_s=1.02, attitude_time_s=1.02,
                            body_rates_rad_s=(1, 2, 3))
    assert result["transport_flags"] == flags
    expected = _rotation(target, ZXY if mode == "follow" else ZYX)
    assert _quaternion_rotation(result["quaternion"]) == pytest.approx(expected, abs=1e-8, rel=0)


@pytest.mark.parametrize("body,pole", [((12., 15., 37.), -90.), ((20., 70., 40.), 90.)])
def test_earth_pitch_pole_keeps_existing_joint_pose(body, pole):
    # Exact vertical LOS leaves image twist free. This zero-travel witness cancels
    # body roll and retains body heading; checking only the ray would miss lost twist.
    r, p, y = body
    previous = (-r, pole - p, 0.)
    track = BridgeHorizonTarget(mode="track", track_preview_s=0.)
    track.last_joint.update(_rpy(previous))
    command = _track_pose(track, body, (0., 0., -math.copysign(1., pole)))
    assert command == pytest.approx(_rotation(_rpy((0., pole, y)), ZYX), abs=1e-8, rel=0)
    _endpoint(_rotation(_rpy(body), ZYX), command, previous)
