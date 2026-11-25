"""Observe horizontal wind from PX4 kinematics; never infer wind in hover."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from statistics import median

_MS = 1_000_000
_INPUT_MAX_AGE = 1_000 * _MS
_MODE_MAX_AGE = 1_500 * _MS
_MAX_SKEW = 250 * _MS
_WINDOW = 2_000 * _MS
_MIN_SAMPLES = 5
_INLIER_RADIUS_MPS = 1.5
_DIFFERENTIAL_PRESSURE = 1 << 4  # MAV_SYS_STATUS_SENSOR_DIFFERENTIAL_PRESSURE
_VELOCITY_FIELDS = ("vx", "vy", "vz")
_WIND_FIELDS = ("north_mps", "east_mps", "down_mps")


def _numbers(row, fields):
    values = tuple(row.get(field) for field in fields)
    if all(type(x) in (int, float) and abs(x) <= 2**32 - 1 and math.isfinite(x) for x in values):
        return values
    return None


def _stamp(row, now_ns, max_age):
    stamp = row.get("receive_monotonic_ns")
    if type(stamp) is int and 0 <= stamp <= now_ns and now_ns - stamp <= max_age:
        return stamp
    return None


@dataclass(frozen=True)
class WindReading:
    ned_mps: tuple[float, float, float | None]
    stamp_ns: int
    support_since_ns: int
    confidence: float | None  # Inlier fraction, not a calibrated probability or covariance.

    @classmethod
    def from_wind_cov(cls, row: Mapping, now_ns: int) -> WindReading | None:
        stamp = _stamp(row, now_ns, _MODE_MAX_AGE)
        vector = _numbers(row, ("wind_x", "wind_y", "wind_z"))
        return cls(vector, stamp, stamp, None) if stamp is not None and vector is not None else None

    @property
    def method(self) -> str:
        return "wind_triangle" if self.ned_mps[2] is None else "wind_cov"

    def age_ms(self, now_ms: int) -> int:
        return now_ms - self.stamp_ns // _MS

    def telemetry(self, now_ms: int) -> dict:
        return {"age_ms": self.age_ms(now_ms), "source": "mavlink", **{
            field: round(value, 2) if value is not None else None
            for field, value in zip(_WIND_FIELDS, self.ned_mps, strict=True)
        }}


class WindEstimator:
    """Assume TAS and negligible sideslip and angle of attack (flow along FW body +X).

    ATTITUDE gives a continuous direction, unlike the rounded HUD heading. PX4's
    X1 tailsitter reports the MC FRD frame even in FW; its FW +X is MC -Z.
    Compute only the horizontal wind, with no vertical estimate or ground truth.
    """

    def __init__(self, *, tailsitter: bool = False, min_airspeed_mps: float = 12.0):
        self.tailsitter = tailsitter
        self.min_airspeed_mps = min_airspeed_mps
        self._samples: deque[tuple[int, tuple[float, float]]] = deque(maxlen=24)
        self._used_stamps = (-1, -1, -1)

    def _unknown(self):
        self._samples.clear()
        return None

    def update(self, rows: Mapping[str, Mapping], *, now_ns: int) -> WindReading | None:
        mode = rows.get("EXTENDED_SYS_STATE", {})
        hud = rows.get("VFR_HUD", {})
        attitude = rows.get("ATTITUDE", {})
        air = _numbers(hud, ("airspeed",))
        angles = _numbers(attitude, ("roll", "pitch", "yaw"))
        hud_stamp = _stamp(hud, now_ns, _INPUT_MAX_AGE)
        attitude_stamp = _stamp(attitude, now_ns, _INPUT_MAX_AGE)
        if (_stamp(mode, now_ns, _MODE_MAX_AGE) is None or type(mode.get("vtol_state")) is not int
                or mode["vtol_state"] != 4 or type(mode.get("landed_state")) is not int
                or mode["landed_state"] != 2 or air is None or air[0] < self.min_airspeed_mps
                or angles is None or hud_stamp is None or attitude_stamp is None):
            return self._unknown()

        status = rows.get("SYS_STATUS", {})
        present = status.get("onboard_control_sensors_present")
        healthy = status.get("onboard_control_sensors_health")
        if (_stamp(status, now_ns, _MODE_MAX_AGE) is not None
                and type(present) is int and type(healthy) is int
                and present & _DIFFERENTIAL_PRESSURE and not healthy & _DIFFERENTIAL_PRESSURE):
            return self._unknown()

        roll, pitch, yaw = angles
        cy, sy, cp = math.cos(yaw), math.sin(yaw), math.cos(pitch)
        if self.tailsitter:  # Horizontal components of R_body_to_NED @ (0, 0, -1).
            cr, sr, sp = math.cos(roll), math.sin(roll), math.sin(pitch)
            forward_n, forward_e = -cy * sp * cr - sy * sr, -sy * sp * cr + cy * sr
        else:
            forward_n, forward_e = cy * cp, sy * cp
        if math.hypot(forward_n, forward_e) < 0.5:
            return self._unknown()

        aligned = False
        for name, scale in (("LOCAL_POSITION_NED", 1.0), ("GLOBAL_POSITION_INT", 0.01)):
            row = rows.get(name, {})
            velocity = _numbers(row, _VELOCITY_FIELDS)
            stamp = _stamp(row, now_ns, _INPUT_MAX_AGE)
            if velocity is None or stamp is None:
                continue
            stamps = (hud_stamp, attitude_stamp, stamp)
            if max(stamps) - min(stamps) > _MAX_SKEW:
                continue
            aligned = True
            # A polling tick or a fast stream alone cannot manufacture samples.
            if all(current > used for current, used in zip(stamps, self._used_stamps, strict=True)):
                self._used_stamps = stamps
                self._samples.append((min(stamps), (
                    scale * velocity[0] - air[0] * forward_n,
                    scale * velocity[1] - air[0] * forward_e,
                )))
                break

        if not aligned:
            return None  # Temporary skew is not a mode change; keep but do not publish history.
        while self._samples and now_ns - self._samples[0][0] > _WINDOW:
            self._samples.popleft()
        if len(self._samples) < _MIN_SAMPLES:
            return None
        vectors = [vector for _, vector in self._samples]
        center = tuple(map(median, zip(*vectors, strict=True)))
        inliers = [(stamp, v) for stamp, v in self._samples
                   if math.dist(v, center) <= _INLIER_RADIUS_MPS]
        fraction = len(inliers) / len(self._samples)
        if (len(inliers) < _MIN_SAMPLES or fraction < 0.75
                or now_ns - inliers[-1][0] > _INPUT_MAX_AGE):
            return None
        axes = zip(*(v for _, v in inliers), strict=True)
        north, east = (sum(axis) / len(inliers) for axis in axes)
        return WindReading((north, east, None), inliers[-1][0], inliers[0][0], fraction)
