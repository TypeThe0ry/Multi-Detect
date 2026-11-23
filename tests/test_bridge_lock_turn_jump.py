"""Python reference of the C11 bridge lock/follow solve (gz-gimbal-horizon-stabilization.patch).

Regression for X1 R3b (rw_farm_iowa__clear_noon/249f1fa2c6e502ebc3d3): after the FW legs the yaw joint
is wound near the -2*pi stop; in the FW->MC back-transition an exact lock pose reappears whose nearest
winding lies past the stop, so nearestTurnWithin() moves it a full turn and the bridge slews ~330 deg of
yaw while the yaw axis is near horizontal (camera rolled ~96 deg in a recorded frame). The fixed rule
keeps a continuous horizon solution while one exists. Mirrors the C++ helpers; no PX4 build here.
"""

import math

TURN = 2. * math.pi
ROLL = (-math.pi / 4, math.pi / 4)
PITCH = (math.radians(-135.), math.radians(45.))
YAW = (-TURN, TURN)
PREVIEW_S, PREVIEW_MAX = .20, math.radians(12.)

# R3b episode-0000-871fa518, ATTITUDE_QUATERNION at 682.784 s (vtol_state 2, back-transition) and the
# bridge's previous joint command (replayed from the hand-back at 680.184 s), radians.
R3B_Q = (-0.2618114948272705, 0.5310530066490173, 0.33314594626426697, 0.7337923645973206)
R3B_RATES = (-0.0033992293756455183, 0.11918197572231293, 0.11163870245218277)
R3B_PREVIOUS = tuple(map(math.radians, (39.6602, 16.2706, -337.0906)))


def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def tr(a):
    return [list(r) for r in zip(*a)]


def rx(a):
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def ry(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def rz(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def zxy(roll, pitch, yaw):
    return mul(mul(rz(yaw), rx(roll)), ry(pitch))


def dcm(q):
    w, x, y, z = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


def qmul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return (w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2)


def nearest_turn(angle, previous):
    return previous + math.remainder(angle - previous, TURN)


def nearest_turn_within(angle, previous, lo=YAW[0], hi=YAW[1]):
    y = nearest_turn(angle, previous)
    while y > hi:
        y -= TURN
    while y < lo:
        y += TURN
    return y


def clamp(v, lo, hi):
    return min(hi, max(lo, v))


def preview_body(q, rates):
    speed = math.sqrt(sum(v * v for v in rates))
    half = .5 * min(speed * PREVIEW_S, PREVIEW_MAX)
    scale = math.sin(half) / speed
    return dcm(qmul(q, (math.cos(half), *(v * scale for v in rates))))


def bounded_joints(rel, prev):
    roll = math.asin(clamp(rel[2][1], -1., 1.))
    yaw = math.atan2(-rel[0][1], rel[1][1])
    pitch = math.atan2(-rel[2][0], rel[2][2])
    best = None
    for branch in (0, 1):
        r = nearest_turn(math.pi - roll if branch else roll, prev[0])
        offset = math.pi if branch else 0.
        p = nearest_turn(pitch + offset, prev[1])
        joint = (clamp(r, *ROLL), clamp(p, *PITCH), nearest_turn_within(yaw + offset, prev[2]))
        exact = abs(joint[0] - r) < 1e-6 and abs(joint[1] - p) < 1e-6
        key = (not exact, sum((a - b) ** 2 for a, b in zip(joint, prev)))
        best = min(best, (key, joint, exact)) if best else (key, joint, exact)
    return best[1], best[2]


def horizon_joints(body, desired, prev):
    def entry(row, col):
        return (body[0][row] * desired[0][col] + body[1][row] * desired[1][col],
                body[1][row] * desired[0][col] - body[0][row] * desired[1][col], body[2][row] * desired[2][col])
    sr, cp = entry(2, 1), entry(2, 2)
    sp = tuple(-v for v in entry(2, 0))
    cuts = [0., TURN]

    def roots(v, value):
        amplitude = math.hypot(v[0], v[1])
        if amplitude > 1e-7 and abs(value - v[2]) <= amplitude + 1e-6:
            phase = math.atan2(v[1], v[0])
            delta = math.acos(clamp((value - v[2]) / amplitude, -1., 1.))
            cuts.extend(((phase + delta) % TURN, (phase - delta) % TURN))
    roots(sr, math.sin(ROLL[0]))
    roots(sr, math.sin(ROLL[1]))
    for bound in PITCH:
        roots(tuple(a * math.cos(bound) - b * math.sin(bound) for a, b in zip(sp, cp)), 0.)
    cuts.sort()
    best = [math.inf, None]

    def evaluate(h):
        hc = (math.cos(h), math.sin(h), 1.)
        r = math.asin(clamp(sum(a * b for a, b in zip(sr, hc)), -1., 1.))
        p = math.atan2(sum(a * b for a, b in zip(sp, hc)), sum(a * b for a, b in zip(cp, hc)))
        if not (ROLL[0] - 1e-6 <= r <= ROLL[1] + 1e-6 and PITCH[0] - 1e-6 <= p <= PITCH[1] + 1e-6):
            return math.inf
        rel = mul(mul(tr(body), rz(h)), desired)
        joint = (clamp(r, *ROLL), clamp(p, *PITCH), nearest_turn_within(math.atan2(-rel[0][1], rel[1][1]), prev[2]))
        travel = sum((a - b) ** 2 for a, b in zip(joint, prev))
        if travel < best[0]:
            best[:] = [travel, joint]
        return travel
    for i, cut in enumerate(cuts):
        evaluate(cut)
        if i + 1 < len(cuts) and math.isfinite(evaluate((cut + cuts[i + 1]) / 2)):
            lo, hi = cut, cuts[i + 1]
            for _ in range(14):
                left, right = (2 * lo + hi) / 3, (lo + 2 * hi) / 3
                if evaluate(left) < evaluate(right):
                    hi = right
                else:
                    lo = left
    return best[1]


def turn_jump(joint, prev):
    return abs(joint[2] - nearest_turn(joint[2], prev[2])) > 1e-3


def lock_follow_solve(q, rates, prev, *, fixed):
    """publishStabilizedPose() for transport flags 44 (roll+pitch lock, yaw in vehicle frame), pitch -56."""
    body = dcm(q)
    desired = mul(rz(math.atan2(body[1][0], body[0][0])), ry(math.radians(-56.)))
    solve_body = preview_body(q, rates)
    joint, exact = bounded_joints(mul(tr(solve_body), desired), prev)
    if not exact:
        joint = horizon_joints(solve_body, desired, prev) or joint
    elif fixed and turn_jump(joint, prev):
        continuous = horizon_joints(solve_body, desired, prev)
        if continuous is not None and not turn_jump(continuous, prev):
            joint = continuous
    return joint, solve_body


def earth_roll_pitch_deg(rotation):
    return (math.degrees(math.atan2(rotation[2][1], rotation[2][2])),
            math.degrees(math.asin(clamp(-rotation[2][0], -1., 1.))))


def test_r3b_back_transition_exact_lock_pose_costs_a_full_yaw_turn():
    joint, solve_body = lock_follow_solve(R3B_Q, R3B_RATES, R3B_PREVIOUS, fixed=False)
    assert turn_jump(joint, R3B_PREVIOUS)
    assert math.degrees(joint[2] - R3B_PREVIOUS[2]) > 300.  # shipped bridge: slew +334 deg through the horizon
    assert dcm(R3B_Q)[2][2] < math.cos(math.radians(60.))  # yaw axis > 60 deg off vertical


def test_fixed_lock_rule_keeps_a_continuous_exact_horizon():
    joint, solve_body = lock_follow_solve(R3B_Q, R3B_RATES, R3B_PREVIOUS, fixed=True)
    assert not turn_jump(joint, R3B_PREVIOUS)
    assert abs(math.degrees(joint[2] - R3B_PREVIOUS[2])) < 5.
    roll, pitch = earth_roll_pitch_deg(mul(solve_body, zxy(*joint)))
    assert abs(roll) < 1e-3 and abs(pitch + 56.) < 1e-3


LEVEL = (math.cos(math.radians(15.)), 0., 0., math.sin(math.radians(15.)))  # MC hover, heading 30 deg


def test_fixed_lock_rule_is_unchanged_without_a_turn_jump():
    # Lock-only flights keep the yaw joint within about +-37 deg (5 recorded Iowa routes): no turn jump.
    for previous in ((0., math.radians(-56.), 0.), (0., math.radians(-56.), math.radians(-100.))):
        shipped, _ = lock_follow_solve(LEVEL, (0., 0., .05), previous, fixed=False)
        fixed, _ = lock_follow_solve(LEVEL, (0., 0., .05), previous, fixed=True)
        assert not turn_jump(shipped, previous) and fixed == shipped


def test_fixed_lock_rule_leaves_a_wound_joint_unwind_to_track_mode():
    # Behaviour change, by design: a joint wound near the stop is no longer unwound by lock/follow even in a
    # level hover (where the shipped full turn is only a pan). Earth-yaw track keeps the +-2*pi window rule.
    previous = (0., math.radians(-56.), math.radians(-300.))
    shipped, _ = lock_follow_solve(LEVEL, (0., 0., .05), previous, fixed=False)
    fixed, solve_body = lock_follow_solve(LEVEL, (0., 0., .05), previous, fixed=True)
    assert turn_jump(shipped, previous) and not turn_jump(fixed, previous)
    roll, pitch = earth_roll_pitch_deg(mul(solve_body, zxy(*fixed)))
    assert abs(roll) < 1e-3 and abs(pitch + 56.) < 1e-3
