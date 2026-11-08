"""Pure geometry helpers for delta tailsitter fixed-wing smoke judgements."""

from __future__ import annotations

import math
from collections.abc import Sequence


def tailsitter_flight_angles_deg(quaternion: Sequence[float]) -> tuple[float, float]:
    """Return nose climb angle and absolute wing bank from a body-to-NED quaternion.

    MAVLink ATTITUDE_QUATERNION uses scalar-first ``(w, x, y, z)`` ordering.
    A tailsitter's nose is body -z and its lateral axis is body +y; Euler roll
    is singular when the nose is nearly vertical and is intentionally unused.
    The returned bank is signed, with magnitude equal to the body-y axis angle
    from the local horizontal plane.
    """
    if len(quaternion) != 4:
        raise ValueError("attitude quaternion must contain four values")
    values = tuple(float(value) for value in quaternion)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("attitude quaternion values must be finite")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        raise ValueError("attitude quaternion must be non-zero")
    w, x, y, z = (value / norm for value in values)

    def rotate(vector: tuple[float, float, float]) -> tuple[float, float, float]:
        vx, vy, vz = vector
        # q * v * conjugate(q), expressed without allocating another quaternion.
        cx, cy, cz = y * vz - z * vy, z * vx - x * vz, x * vy - y * vx
        ccx, ccy, ccz = y * cz - z * cy, z * cx - x * cz, x * cy - y * cx
        return (
            vx + 2.0 * (w * cx + ccx),
            vy + 2.0 * (w * cy + ccy),
            vz + 2.0 * (w * cz + ccz),
        )

    nose_ned = rotate((0.0, 0.0, -1.0))
    body_y_ned = rotate((0.0, 1.0, 0.0))
    nose_horizontal = math.hypot(nose_ned[0], nose_ned[1])
    body_y_horizontal = math.hypot(body_y_ned[0], body_y_ned[1])
    pitch_up_deg = math.degrees(math.atan2(-nose_ned[2], nose_horizontal))
    bank_deg = math.degrees(math.atan2(body_y_ned[2], body_y_horizontal))
    return pitch_up_deg, bank_deg
