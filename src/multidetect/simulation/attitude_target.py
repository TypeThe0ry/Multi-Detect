"""Decode recorded attitude targets without interpreting ignored rate fields."""

import math


def decode_attitude_target(sample: dict, *, quaternion_norm_tolerance: float) -> dict:
    """Validate wxyz and report any small numerical normalization explicitly."""
    tolerance = quaternion_norm_tolerance
    if (
        type(tolerance) not in (int, float)
        or not math.isfinite(tolerance)
        or not 0 <= tolerance < 1
    ):
        raise ValueError("explicit finite quaternion norm tolerance in [0,1) required")
    mask = sample.get("type_mask")
    if type(mask) is not int or not 0 <= mask <= 255:
        raise ValueError("valid attitude target mask required")
    if mask & 128:
        return {"attitude_used": False, "reason": "attitude_ignored_by_mask"}
    q = sample.get("q")
    if (
        not isinstance(q, (list, tuple))
        or len(q) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in q)
    ):
        raise ValueError("finite four-element wxyz quaternion required")
    norm = math.hypot(*q)
    if not math.isfinite(norm) or norm <= 0 or abs(norm - 1) > tolerance:
        raise ValueError("quaternion norm outside explicit tolerance")
    w, x, y, z = (v / norm for v in q)
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    singular = abs(math.cos(pitch)) < 1e-6
    return dict(
        attitude_used=True,
        original_quaternion_norm=norm,
        quaternion_norm_tolerance=tolerance,
        numerical_normalization_applied=norm != 1.0,
        roll_deg=None
        if singular
        else math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))),
        pitch_deg=math.degrees(pitch),
        yaw_deg=None
        if singular
        else math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))),
        euler_roll_yaw_ambiguous=singular,
        physical_accuracy_proven=False,
    )
