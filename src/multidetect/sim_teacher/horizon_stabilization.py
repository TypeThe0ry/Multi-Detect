"""Host compensation for the pinned C11 bridge (NED/FRD, degrees, pitch up).

The bridge decodes a ZYX quaternion as three joint angles, while the physical
chain is ZXY. Send a *ZYX encoding of solved joint numbers*, not the desired
camera quaternion. This compatibility controller must not run with a corrected
bridge or a hardware gimbal that implements stabilization flags itself.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Mapping

from multidetect.camera_mount import _mat_mul, _mat_transpose, rpy_rotation

AXES = ("roll", "pitch", "yaw")
C11_LIMITS = {"roll": (-45.0, 45.0), "pitch": (-135.0, 45.0), "yaw": (-360.0, 360.0)}
LOG = logging.getLogger(__name__)


def wrap_deg(value):
    return (value + 180.0) % 360.0 - 180.0


def rotation_deg(angles):
    return rpy_rotation(**{axis + "_rad": math.radians(angles[axis]) for axis in AXES})


def c11_rotation_deg(angles):
    result = rotation_deg(dict.fromkeys(AXES, 0.0))
    for axis in ("yaw", "roll", "pitch"):
        result = _mat_mul(result, rotation_deg({a: angles[a] if a == axis else 0.0 for a in AXES}))
    return result


def earth_angles_deg(rotation):
    return dict(
        zip(
            AXES,
            map(
                math.degrees,
                (
                    math.atan2(rotation[7], rotation[8]),
                    math.atan2(-rotation[6], math.hypot(rotation[0], rotation[3])),
                    math.atan2(rotation[3], rotation[0]),
                ),
            ),
            strict=True,
        )
    )


def horizon_error_deg(desired_rotation, measured_rotation):
    desired, measured = map(earth_angles_deg, (desired_rotation, measured_rotation))
    return max(abs(wrap_deg(measured[a] - desired[a])) for a in ("roll", "pitch"))


def _continuous(value, previous):
    return value + 360.0 * round((previous - value) / 360.0)


def _zxy_branches(relative, previous):
    roll = math.asin(max(-1.0, min(1.0, relative[7])))
    singular = abs(math.cos(roll)) < 1e-7
    if singular:
        yaw = math.radians(previous["yaw"])
        residual = _mat_mul(
            _mat_transpose(c11_rotation_deg({
                "roll": math.degrees(roll), "pitch": 0.0, "yaw": previous["yaw"],
            })), relative,
        )
        pitch = math.atan2(residual[2], residual[0])
    else:
        yaw = math.atan2(-relative[1], relative[4])
        pitch = math.atan2(-relative[6], relative[8])
    principal = dict(zip(AXES, map(math.degrees, (roll, pitch, yaw)), strict=True))
    other = {"roll": 180.0 - principal["roll"],
             "pitch": principal["pitch"] + 180.0, "yaw": principal["yaw"] + 180.0}
    return (principal, other), singular


def solve_joint_target(relative, previous, limits):
    """Select ZXY branch *after* stops, avoiding a clipped Euler branch flip.

    At joint roll +/-90 the two exact solutions exchange. Although neither
    roll is mechanically reachable on C11, their clipped pitch/yaw need not be
    equivalent. Prefer bounded rotation error when reachable, and continuity
    when unreachable. Rate limiting never changes static feasibility.
    """
    branches, singular = _zxy_branches(relative, previous)
    candidates = []
    feasible = False
    for branch in branches:
        continuous = {a: _continuous(branch[a], previous[a]) for a in AXES}
        bounded = {a: min(limits[a][1], max(limits[a][0], continuous[a])) for a in AXES}
        # Test existence independently of the previous winding/cable stop.
        feasible |= all(any(limits[a][0] - 1e-7 <= branch[a] + 360*k <= limits[a][1] + 1e-7
                            for k in range(-2, 3)) for a in AXES)
        rotation = c11_rotation_deg(bounded)
        distance = sum((x-y)**2 for x, y in zip(rotation, relative, strict=True))
        travel = sum((bounded[a] - previous[a])**2 for a in AXES)
        candidates.append((distance, travel, continuous, bounded))
    if not feasible:
        # Both exact branches are beyond a stop: preserve the outer-axis
        # branch through the unreachable roll +/-90 region. A greedy nearest
        # rotation would swap pitch/yaw by 180 for a tiny residual improvement.
        chosen = min(candidates, key=lambda c: c[1])
        return chosen[2], chosen[3], feasible, singular
    best_distance = min(c[0] for c in candidates)
    # A tiny matrix tolerance prevents numerical noise at the singular crossing
    # from forcing a 180-degree pitch/yaw excursion.
    chosen = min((c for c in candidates if c[0] <= best_distance + 1e-10), key=lambda c: c[1])
    return chosen[2], chosen[3], feasible, singular


def _horizon_joint_target(body_rotation, desired, previous, limits):
    """Solve the exact horizon over all headings for the C11 principal branch.

    For earth yaw offset h, each entry of B^T Rz(h) D is A cos(h) + B sin(h)
    + C. The roll stops and pitch/yaw angle rays therefore partition the circle
    into intervals of constant feasibility. C11's roll stops exclude the second
    ZXY branch and the singularity. Test the endpoints too (tangent solutions).
    """
    if not -90 < limits["roll"][0] <= limits["roll"][1] < 90:
        return None, None, None

    def entry(row, col):
        bx, by, bz = body_rotation[row::3]
        dx, dy, dz = desired[col::3]
        return (bx * dx + by * dy, by * dx - bx * dy, bz * dz)

    roll_sin = entry(2, 1)
    pitch_sin = tuple(-v for v in entry(2, 0))
    pitch_cos = entry(2, 2)
    yaw_sin = tuple(-v for v in entry(0, 1))
    yaw_cos = entry(1, 1)
    entries = (roll_sin, pitch_sin, pitch_cos, yaw_sin, yaw_cos)

    def yaw_offsets(coefficients, value):
        a, b, c = coefficients
        amplitude = math.hypot(a, b)
        if amplitude < 1e-14 or abs(value - c) > amplitude + 1e-13:
            return ()
        phase = math.atan2(b, a)
        delta = math.acos(max(-1.0, min(1.0, (value - c) / amplitude)))
        return ((phase + delta) % math.tau, (phase - delta) % math.tau)

    cuts = {0.0, math.tau}
    for bound in limits["roll"]:
        cuts.update(yaw_offsets(roll_sin, math.sin(math.radians(bound))))
    for axis, sine, cosine in (("pitch", pitch_sin, pitch_cos), ("yaw", yaw_sin, yaw_cos)):
        # Include the wrap of the nearest winding as a minimization boundary.
        for bound in (*limits[axis], previous[axis] + 180):
            angle = math.radians(bound)
            coefficients = tuple(x * math.cos(angle) - y * math.sin(angle)
                                 for x, y in zip(sine, cosine, strict=True))
            cuts.update(yaw_offsets(coefficients, 0.0))
    cuts = sorted(cuts)

    def candidate(heading):
        cos_h, sin_h = math.cos(heading), math.sin(heading)
        sr, sp, cp, sy, cy = (a * cos_h + b * sin_h + c for a, b, c in entries)
        roll = math.degrees(math.asin(max(-1.0, min(1.0, sr))))
        pitch, yaw = map(math.degrees, (math.atan2(sp, cp), math.atan2(sy, cy)))
        joints = {}
        for axis, principal in zip(AXES, (roll, pitch, yaw), strict=True):
            low, high = limits[axis]
            first = math.ceil((low - principal - 1e-8) / 360)
            last = math.floor((high - principal + 1e-8) / 360)
            if first > last:
                return math.inf, None
            winding = min(last, max(first, round((previous[axis] - principal) / 360)))
            joints[axis] = min(high, max(low, principal + 360 * winding))
        return sum((joints[a] - previous[a]) ** 2 for a in AXES), joints

    best = min(map(candidate, cuts), key=lambda item: item[0])
    for low, high in zip(cuts, cuts[1:]):
        middle = candidate((low + high) / 2)
        if middle[1] is None:
            continue
        # Refine the nearest solution in each feasible interval. Include the
        # endpoints above so an active joint stop remains a possible optimum.
        best = min(best, middle, key=lambda item: item[0])
        ratio = (math.sqrt(5) - 1) / 2
        left, right = high - ratio * (high - low), low + ratio * (high - low)
        left_fit, right_fit = candidate(left), candidate(right)
        for _ in range(18):
            if left_fit[0] <= right_fit[0]:
                high, right, right_fit = right, left, left_fit
                left = high - ratio * (high - low)
                left_fit = candidate(left)
            else:
                low, left, left_fit = left, right, right_fit
                right = low + ratio * (high - low)
                right_fit = candidate(right)
        best = min(best, left_fit, right_fit, key=lambda item: item[0])

    joints = best[1]
    if joints is not None:
        measured = _mat_mul(body_rotation, c11_rotation_deg(joints))
        if horizon_error_deg(desired, measured) < 1e-6:
            return joints, True, "joint_limits_and_fk_witness"
        return None, None, None
    # The feasibility is constant between consecutive stop intersections and
    # every intersection was also tested. No heading admits an exact solution.
    return None, False, "no_joint_solution_on_heading_intervals"


def _solve_pose(body_rotation, desired, previous, limits):
    relative = _mat_mul(_mat_transpose(body_rotation), desired)
    target, _, exact, singular = solve_joint_target(relative, previous, limits)
    # Same physical stops when the bridge does not narrow the range.
    mechanical = exact if limits == C11_LIMITS else solve_joint_target(
        relative, previous, C11_LIMITS
    )[2]
    if exact:
        witness, possible, proof = None, True, "full_target_joint_solution"
    else:
        witness, possible, proof = _horizon_joint_target(body_rotation, desired, previous, limits)
    return (witness if witness is not None else target), {
        "target_pose_feasible": exact and mechanical,
        "mechanical_feasible": mechanical,
        "command_feasible": exact,
        "horizon_feasible": possible,
        "horizon_witness_joint_deg": witness,
        "horizon_feasibility_evidence": proof,
        "zxy_singular": singular,
    }


def _closest_level_pitch(body_rotation, desired, previous, limits):
    """Keep earth roll level at the pitch stop if exact tilt is impossible.

    For level earth roll and camera pitch alpha, the body down vector equals
    Rz(yaw) [sin(beta), -sin(roll)*cos(beta), cos(roll)*cos(beta)],
    where beta = joint_pitch - alpha. At the pitch stop, aim for the
    attainable alpha instead of falling back to exact-yaw Euler clipping.
    """
    angles = earth_angles_deg(desired)
    if abs(angles["roll"]) > 1e-7:
        return None
    x, y, z = body_rotation[6:9]
    zero_roll = min(limits["roll"][1], max(limits["roll"][0], 0.0))
    candidates = []
    for roll in (previous["roll"], zero_roll):
        r = math.radians(roll)
        if math.cos(r) < abs(z) - 1e-12 or math.cos(r) <= 0:
            continue
        beta = math.acos(max(-1.0, min(1.0, z / math.cos(r))))
        for b in (beta, -beta):
            requested_pitch = _continuous(angles["pitch"] + math.degrees(b), previous["pitch"])
            pitch = min(limits["pitch"][1], max(limits["pitch"][0], requested_pitch))
            if math.hypot(x, y) < 1e-12:
                yaw = previous["yaw"]
            else:
                yaw = _continuous(
                    math.degrees(math.atan2(y, x) - math.atan2(-math.sin(r) * math.cos(b), math.sin(b))),
                    previous["yaw"],
                )
            yaw = min(limits["yaw"][1], max(limits["yaw"][0], yaw))
            joint = {"roll": roll, "pitch": pitch, "yaw": yaw}
            measured = _mat_mul(body_rotation, c11_rotation_deg(joint))
            error = horizon_error_deg(desired, measured)
            travel = sum((joint[a] - previous[a]) ** 2 for a in AXES)
            candidates.append((error, travel, joint))
    if not candidates:
        return None
    best_error = min(c[0] for c in candidates)
    return min((c for c in candidates if c[0] <= best_error + 1e-6), key=lambda c: c[1])[2]


def _saturation(joints, effective):
    def saturated(limits):
        return [a for a in AXES if joints is not None and
                min(abs(joints[a] - v) for v in limits[a]) <= 0.05]

    physical = saturated(C11_LIMITS)
    return {
        "feasibility_definition": "target_pose_includes_yaw; horizon_allows_yaw_relaxation",
        "saturated_axes": physical,
        "saturated": bool(physical),
        "command_saturated_axes": saturated(effective),
        "limits_deg": {a: list(effective[a]) for a in AXES},
    }


def frame_feasibility(body, desired, joints=None, *, limits=None):
    """Static reachability; frame feedback and rate limits are separate."""
    previous = joints if joints is not None else dict.fromkeys(AXES, 0.0)
    effective = C11_LIMITS if limits is None else limits
    _, geometry = _solve_pose(rotation_deg(body), desired, previous, effective)
    return {**geometry, **_saturation(joints, effective)}


def _yaw_error_deg(desired, measured):
    return abs(wrap_deg(earth_angles_deg(measured)["yaw"] - earth_angles_deg(desired)["yaw"]))


def joint_command_quaternion(joints):
    """ZYX carrier; bridge Eulerf(q) must recover the joint numbers.

    Eulerf cannot carry pitch outside [-90, 90]. Reject rather than silently
    execute an equivalent orientation with different physical joint angles.
    """
    if not all(math.isfinite(joints[a]) for a in AXES):
        raise ValueError("nonfinite joint command")
    if not -90.0 < joints["pitch"] < 90.0:
        raise ValueError("bridge ZYX carrier pitch is singular or unrepresentable")
    if not -180.0 < joints["yaw"] < 180.0:
        raise ValueError("bridge ZYX carrier cannot retain yaw winding")
    r, p, y = (math.radians(joints[a]) / 2 for a in AXES)
    cr, cp, cy, sr, sp, sy = (
        math.cos(r),
        math.cos(p),
        math.cos(y),
        math.sin(r),
        math.sin(p),
        math.sin(y),
    )
    return [
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    ]


class HorizonStabilizer:
    def __init__(
        self,
        target_deg=None,
        *,
        mode="lock",
        yaw_mode="follow",
        limits_deg=None,
        max_rate_deg_s=180.0,
        max_age_s=0.2,
    ):
        if mode not in {"lock", "follow"} or yaw_mode not in {"lock", "follow"}:
            raise ValueError("mode/yaw_mode must be lock or follow")
        self.target_deg = dict(target_deg or {"roll": 0.0, "pitch": -45.0, "yaw": 0.0})
        self.mode, self.yaw_mode = mode, yaw_mode
        self.limits_deg = dict(C11_LIMITS if limits_deg is None else limits_deg)
        self.rates = (
            {a: float(max_rate_deg_s[a]) for a in AXES}
            if isinstance(max_rate_deg_s, Mapping)
            else dict.fromkeys(AXES, float(max_rate_deg_s))
        )
        if (
            not all(math.isfinite(self.target_deg[a]) for a in AXES)
            or not math.isfinite(max_age_s)
            or max_age_s <= 0
            or any(not math.isfinite(v) or v <= 0 for v in self.rates.values())
            or any(
                len(self.limits_deg[a]) != 2
                or not all(map(math.isfinite, self.limits_deg[a]))
                or self.limits_deg[a][0] >= self.limits_deg[a][1]
                for a in AXES
            )
        ):
            raise ValueError("invalid target, limits, rate or freshness budget")
        self.max_age_s = max_age_s
        self.last_joint = {
            a: min(self.limits_deg[a][1], max(self.limits_deg[a][0], 0.0)) for a in AXES
        }
        self.last_time = None
        self.last_result = None
        self._warned = ()
        self._heading = None
        self._command_history = deque(maxlen=512)
        self._command_history_lock = threading.Lock()

    def desired_rotation(self, body_deg, *, heading=None):
        if self.mode == "follow":
            return _mat_mul(rotation_deg(body_deg), c11_rotation_deg(self.target_deg))
        desired = dict(self.target_deg)
        if self.yaw_mode == "follow":
            # ATTITUDE yaw provides heading even at tailsitter pitch +/-90.
            desired["yaw"] += body_deg["yaw"] if heading is None else heading
        return rotation_deg(desired)

    def update(self, body_deg, *, now_s, attitude_time_s):
        reasons = []
        valid = (
            body_deg is not None
            and all(a in body_deg and math.isfinite(body_deg[a]) for a in AXES)
            and math.isfinite(now_s)
            and attitude_time_s is not None
            and math.isfinite(attitude_time_s)
            and 0 <= now_s - attitude_time_s <= self.max_age_s
        )
        dt = (
            0.0 if self.last_time is None else min(self.max_age_s, max(0.0, now_s - self.last_time))
        )
        if self.last_time is not None and now_s < self.last_time:
            valid = False
            reasons.append("clock_regression")
        if valid:
            heading = body_deg["yaw"]
            if abs(math.cos(math.radians(body_deg["pitch"]))) < 1e-3 and self._heading is not None:
                heading = self._heading
            self._heading = heading
        desired = self.desired_rotation(body_deg, heading=self._heading) if valid else None
        target = dict(self.last_joint)
        geometry = {"target_pose_feasible": None, "horizon_feasible": None}
        yaw_degraded = False
        bounded_horizon_target = False
        body_rotation = rotation_deg(body_deg) if valid else None
        if not valid:
            reasons.append("attitude_stale_or_invalid_hold_last")
        elif self.mode == "follow":
            target = dict(self.target_deg)
            _, geometry = _solve_pose(body_rotation, desired, self.last_joint, self.limits_deg)
        else:
            target, geometry = _solve_pose(body_rotation, desired, self.last_joint, self.limits_deg)
            yaw_degraded = geometry["horizon_witness_joint_deg"] is not None
            if yaw_degraded:
                reasons.append("horizon_first_yaw")
                bounded_horizon_target = True
            elif geometry["horizon_feasible"] is not True:
                reasons.append("horizon_unreachable" if geometry["horizon_feasible"] is False
                               else "horizon_feasibility_unknown")
                projected = _closest_level_pitch(
                    body_rotation, desired, self.last_joint, self.limits_deg
                )
                if projected is not None:
                    target = projected
                    bounded_horizon_target = True
            if geometry["zxy_singular"]:
                reasons.append("zxy_singular")
        rate_limited = False
        for axis in AXES:
            low, high = self.limits_deg[axis]
            # The horizon solver already chose the closest legal winding.
            # For full yaw lock, preserve the nearest winding at a cable stop.
            continuous = target[axis] if bounded_horizon_target else _continuous(
                target[axis], self.last_joint[axis]
            )
            bounded = min(high, max(low, continuous))
            if abs(bounded - continuous) > 1e-8:
                reasons.append(axis + "_limit")
            previous = self.last_joint[axis]
            step = self.rates[axis] * dt
            value = min(previous + step, max(previous - step, bounded))
            if abs(value - bounded) > 1e-8:
                reasons.append(axis + "_rate_limit")
                rate_limited = True
            assert low - 1e-8 <= value <= high + 1e-8
            assert abs(value - previous) <= step + 1e-8, f"C11 {axis} joint discontinuity"
            self.last_joint[axis] = value
        self.last_time = now_s
        measured = _mat_mul(body_rotation, c11_rotation_deg(self.last_joint)) if valid else None
        result = {
            "mode": self.mode,
            "yaw_mode": self.yaw_mode,
            "joint_deg": dict(self.last_joint),
            "desired_earth_deg": earth_angles_deg(desired) if desired else None,
            "degraded": bool(reasons),
            "horizon_degraded": not valid or horizon_error_deg(desired, measured) > 1e-6,
            "yaw_degraded": yaw_degraded,
            "yaw_error_deg": _yaw_error_deg(desired, measured) if valid else None,
            "rate_limited": rate_limited,
            "reasons": reasons,
            "time_s": now_s,
            "attitude_age_s": now_s - attitude_time_s if valid else None,
            **geometry,
            **_saturation(self.last_joint if valid else None, self.limits_deg),
        }
        warnings = tuple(r for r in reasons if "rate_limit" not in r and r != "horizon_first_yaw")
        if warnings and warnings != self._warned:
            LOG.warning("C11 horizon stabilization: %s", ", ".join(warnings))
        self._warned = warnings
        self.last_result = result
        return result

    def record_command(self, result, source_time_s):
        """Associate a queued host command with the PX4 clock at sampling."""
        if source_time_s is not None and math.isfinite(source_time_s):
            with self._command_history_lock:
                if self._command_history:
                    # PX4 streams can deliver older messages out of order.
                    # This history belongs to a single episode and boot clock.
                    source_time_s = max(source_time_s, self._command_history[-1][0])
                self._command_history.append((source_time_s, result))

    def _command_at(self, frame_stamp_ns):
        if frame_stamp_ns is None:
            return None, None
        frame_time_s = frame_stamp_ns / 1e9
        with self._command_history_lock:
            for stamp, result in reversed(self._command_history):
                age = frame_time_s - stamp
                if 0 <= age <= self.max_age_s:
                    return result, age * 1000
                if age > self.max_age_s:
                    break
        return None, None

    def annotate(self, observation):
        """Frame-aligned measured FK only; never substitute a command for feedback."""
        state = observation.get("gimbal_state")
        if not isinstance(state, dict):
            return
        body = {a: observation["attitude"][a + "_deg"] for a in AXES}
        frame_command, command_age_ms = self._command_at(observation.get("frame_sensor_stamp_ns"))
        heading = None
        if (self.mode == "lock" and self.yaw_mode == "follow"
                and abs(math.cos(math.radians(body["pitch"]))) < 1e-3
                and frame_command is not None and frame_command["desired_earth_deg"] is not None):
            heading = frame_command["desired_earth_deg"]["yaw"] - self.target_deg["yaw"]
        desired = self.desired_rotation(body, heading=heading)
        camera = observation.get("camera_pose")
        fresh = (
            camera is not None
            and state.get("fresh") is True
            and state.get("source") == "gazebo_joint_state"
            and isinstance(camera.get("age_ms"), (float, int))
            and 0 <= camera["age_ms"] <= self.max_age_s * 1000
        )
        measured = camera.get("optical_to_world") if fresh else None
        feasibility = frame_feasibility(
            body, desired,
            {a: math.degrees(state["joint_position_rad"][a]) for a in AXES}
            if state.get("joint_position_rad") else None,
            limits=self.limits_deg,
        )
        frame_reasons = (["measurement_stale_or_missing"] if not fresh else [])
        if not feasibility["target_pose_feasible"]:
            frame_reasons.append("target_pose_unreachable")
        frame_reasons.extend(a + "_saturated" for a in feasibility["saturated_axes"])
        yaw_degraded = (self.mode == "lock" and feasibility["target_pose_feasible"] is False
                        and feasibility["horizon_feasible"] is True)
        state["stabilization"] = {
            "mode": self.mode,
            "yaw_mode": self.yaw_mode,
            "desired_earth_deg": earth_angles_deg(desired),
            "measured_earth_deg": earth_angles_deg(measured) if measured else None,
            "horizon_error_deg": horizon_error_deg(desired, measured) if measured else None,
            "control": self.last_result,
            "control_alignment": "latest_host_snapshot_not_frame_aligned",
            "frame_control_age_ms": command_age_ms,
            "frame_control_alignment": "latest_queued_host_command_on_px4_clock_at_or_before_frame",
            "horizon_degraded": (
                True if not fresh or feasibility["horizon_feasible"] is not True
                else None if frame_command is None else frame_command["horizon_degraded"]
            ),
            "yaw_degraded": yaw_degraded,
            "yaw_error_deg": _yaw_error_deg(desired, measured) if measured is not None else None,
            "measurement_source": state.get("source"),
            "measurement_fresh": fresh,
            **feasibility,
            "frame_geometric_degraded": bool(frame_reasons),
            "frame_geometric_reasons": frame_reasons,
        }


class HostHorizonLoop:
    """Independent 50 Hz worker: video reads and blocking flight waits cannot stall it.

    Send callbacks must return the existing I/O gate ticket. At most one send
    may be outstanding; a blocked transport never accumulates stale commands.
    Session polling belongs to this worker while active.
    """

    def __init__(
        self, stabilizer, sample, send, *, session=None, record=None, clock=time.monotonic
    ):
        self.stabilizer, self.sample, self.send = stabilizer, sample, send
        self.session, self.clock, self.record = session, clock, record
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.sent_count = 0
        self.ticket = None

    def step(self):
        if self.session is not None:
            self.session.poll()
            if not self.session.owned:
                raise RuntimeError("horizon stabilization lost gimbal manager ownership")
        if self.ticket is not None:
            if not self.ticket.done.is_set():
                return
            if self.ticket.error is not None:
                raise RuntimeError("horizon transport failed") from self.ticket.error
        body, timestamp, *source = self.sample()
        result = self.stabilizer.update(body, now_s=self.clock(), attitude_time_s=timestamp)
        self.ticket = self.send(joint_command_quaternion(result["joint_deg"]))
        if source:
            self.stabilizer.record_command(result, source[0])
        self.sent_count += 1
        if self.record is not None and self.sent_count % 10 == 1:
            self.record({
                "message_type": "host_horizon_command",
                "sequence": self.sent_count,
                **result,
            })

    def start(self):
        def run():
            while not self.stop_event.is_set():
                started = self.clock()
                try:
                    self.step()
                except Exception as exc:
                    self.error = exc
                    return
                self.stop_event.wait(max(0, 0.02 - (self.clock() - started)))

        self.thread = threading.Thread(target=run, name="c11-horizon", daemon=False)
        self.thread.start()
        return self

    def check(self):
        if self.error is not None:
            raise RuntimeError("host horizon stabilization failed") from self.error

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
