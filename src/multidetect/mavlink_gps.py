"""MAVLink GPS wire-domain constants and normalizers.

MAVLink's ``GPS_FIX_TYPE`` enum currently defines values 0 through 8.  The
satellite-count field is an unsigned byte where 255 means *unknown* rather
than 255 usable satellites.  Keeping these rules in one small dependency-free
module prevents the REAL, HIL and SIM telemetry adapters from silently
drifting apart.
"""

from __future__ import annotations

from typing import Final

MAVLINK_GPS_FIX_TYPE_MIN: Final[int] = 0
MAVLINK_GPS_FIX_TYPE_MAX: Final[int] = 8
# A GPS sample is eligible for the outdoor/global-position quality gate only
# after it has a 3-D fix and a known, usable satellite count.  Keep these
# thresholds in the wire-domain module so SIM, HIL, REAL and offline replay do
# not grow subtly different literals.
MAVLINK_QUALIFIED_FIX_TYPE_MIN: Final[int] = 3
MAVLINK_SATELLITES_MIN: Final[int] = 0
MAVLINK_SATELLITES_MAX: Final[int] = 254
MAVLINK_SATELLITES_UNKNOWN: Final[int] = 255
MAVLINK_QUALIFIED_SATELLITES_MIN: Final[int] = 6
# Outdoor target geolocation uses one quality policy in SIM, HIL, REAL and
# offline evidence.  Individual diagnostics may tighten these limits, but a
# caller must never relax them and silently promote a weak fix to GPS-aided
# mapping.
GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M: Final[float] = 6.0
GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M: Final[float] = 10.0
GNSS_CANONICAL_MAX_GPS_AGE_S: Final[float] = 1.5


def normalize_gps_fix_type(value: object) -> int | None:
    """Return a legal MAVLink fix enum, or ``None`` for malformed input."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not MAVLINK_GPS_FIX_TYPE_MIN <= value <= MAVLINK_GPS_FIX_TYPE_MAX:
        return None
    return int(value)


def normalize_satellites_visible(value: object) -> int | None:
    """Return a usable satellite count; 255/invalid values become ``None``."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not MAVLINK_SATELLITES_MIN <= value <= MAVLINK_SATELLITES_MAX:
        return None
    return int(value)


__all__ = [
    "GNSS_CANONICAL_MAX_GPS_AGE_S",
    "GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M",
    "GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M",
    "MAVLINK_GPS_FIX_TYPE_MAX",
    "MAVLINK_GPS_FIX_TYPE_MIN",
    "MAVLINK_QUALIFIED_FIX_TYPE_MIN",
    "MAVLINK_QUALIFIED_SATELLITES_MIN",
    "MAVLINK_SATELLITES_MAX",
    "MAVLINK_SATELLITES_MIN",
    "MAVLINK_SATELLITES_UNKNOWN",
    "normalize_gps_fix_type",
    "normalize_satellites_visible",
]
