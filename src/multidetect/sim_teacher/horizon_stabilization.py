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
        self.last_joint = dict.fromkeys(AXES, 0.0)
        self.last_time = None
        self.last_result = None
        self._warned = ()
        self._heading = None

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
        if not valid:
            reasons.append("attitude_stale_or_invalid_hold_last")
        elif self.mode == "follow":
            target = dict(self.target_deg)
        else:
            relative = _mat_mul(_mat_transpose(rotation_deg(body_deg)), desired)
            roll = math.asin(max(-1.0, min(1.0, relative[7])))
            if abs(math.cos(roll)) < 1e-7:
                # ZXY singularity is JOINT roll +/-90, not BODY pitch +/-90.
                # Retain previous yaw and solve coupled pitch continuously.
                yaw = math.radians(self.last_joint["yaw"])
                residual = _mat_mul(
                    _mat_transpose(
                        rotation_deg(
                            {"roll": math.degrees(roll), "pitch": 0.0, "yaw": math.degrees(yaw)}
                        )
                    ),
                    relative,
                )
                pitch = math.atan2(residual[2], residual[0])
                reasons.append("zxy_singular")
            else:
                yaw, pitch = (
                    math.atan2(-relative[1], relative[4]),
                    math.atan2(-relative[6], relative[8]),
                )
            target = dict(zip(AXES, map(math.degrees, (roll, pitch, yaw)), strict=True))
        # Choose a realizable turn nearest the last joint; respect cable limits.
        for axis in AXES:
            low, high = self.limits_deg[axis]
            # Keep the nearest continuous turn even when it falls outside the
            # cable stop. Selecting a distant feasible turn would silently unwind
            # almost 360 degrees when an Euler heading wraps.
            continuous = min(
                (target[axis] + 360 * k for k in range(-2, 3)),
                key=lambda x: abs(x - self.last_joint[axis]),
            )
            target[axis] = min(high, max(low, continuous))
            if abs(target[axis] - continuous) > 1e-8:
                reasons.append(axis + "_limit")
            step = self.rates[axis] * dt
            value = min(
                self.last_joint[axis] + step, max(self.last_joint[axis] - step, target[axis])
            )
            if abs(value - target[axis]) > 1e-8:
                reasons.append(axis + "_rate_limit")
            self.last_joint[axis] = min(high, max(low, value))
        self.last_time = now_s
        result = {
            "mode": self.mode,
            "yaw_mode": self.yaw_mode,
            "joint_deg": dict(self.last_joint),
            "desired_earth_deg": earth_angles_deg(desired) if desired else None,
            "degraded": bool(reasons),
            "reasons": reasons,
            "time_s": now_s,
        }
        warnings = tuple(r for r in reasons if "rate_limit" not in r)
        if warnings and warnings != self._warned:
            LOG.warning("C11 horizon stabilization: %s", ", ".join(warnings))
        self._warned = warnings
        self.last_result = result
        return result

    def annotate(self, observation):
        """Frame-aligned measured FK only; never substitute a command for feedback."""
        state = observation.get("gimbal_state")
        if not isinstance(state, dict):
            return
        body = {a: observation["attitude"][a + "_deg"] for a in AXES}
        heading = self._heading if abs(math.cos(math.radians(body["pitch"]))) < 1e-3 else None
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
        state["stabilization"] = {
            "mode": self.mode,
            "yaw_mode": self.yaw_mode,
            "desired_earth_deg": earth_angles_deg(desired),
            "measured_earth_deg": earth_angles_deg(measured) if measured else None,
            "horizon_error_deg": horizon_error_deg(desired, measured) if measured else None,
            "control": self.last_result,
            "measurement_source": state.get("source"),
            "measurement_fresh": fresh,
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
        body, timestamp = self.sample()
        result = self.stabilizer.update(body, now_s=self.clock(), attitude_time_s=timestamp)
        self.ticket = self.send(joint_command_quaternion(result["joint_deg"]))
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
