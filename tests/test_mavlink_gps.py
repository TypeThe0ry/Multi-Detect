from __future__ import annotations

import pytest

from multidetect.mavlink_gps import (
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_GPS_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_SATELLITES_MIN,
    MAVLINK_SATELLITES_MAX,
    MAVLINK_SATELLITES_MIN,
    MAVLINK_SATELLITES_UNKNOWN,
    normalize_gps_fix_type,
    normalize_satellites_visible,
)


def test_mavlink_gps_wire_bounds_and_quality_thresholds_are_explicit() -> None:
    assert MAVLINK_GPS_FIX_TYPE_MIN == 0
    assert MAVLINK_GPS_FIX_TYPE_MAX == 8
    assert MAVLINK_QUALIFIED_FIX_TYPE_MIN == 3
    assert MAVLINK_SATELLITES_MIN == 0
    assert MAVLINK_SATELLITES_MAX == 254
    assert MAVLINK_SATELLITES_UNKNOWN == 255
    assert MAVLINK_QUALIFIED_SATELLITES_MIN == 6


@pytest.mark.parametrize("value", range(MAVLINK_GPS_FIX_TYPE_MIN, MAVLINK_GPS_FIX_TYPE_MAX + 1))
def test_normalize_gps_fix_type_accepts_every_wire_enum(value: int) -> None:
    assert normalize_gps_fix_type(value) == value


@pytest.mark.parametrize("value", [-1, 9, 3.0, True, "3", None])
def test_normalize_gps_fix_type_rejects_malformed_values(value: object) -> None:
    assert normalize_gps_fix_type(value) is None


@pytest.mark.parametrize("value", [MAVLINK_SATELLITES_MIN, 6, MAVLINK_SATELLITES_MAX])
def test_normalize_satellites_visible_accepts_known_wire_values(value: int) -> None:
    assert normalize_satellites_visible(value) == value


@pytest.mark.parametrize("value", [-1, MAVLINK_SATELLITES_UNKNOWN, 256, 6.0, True, "6", None])
def test_normalize_satellites_visible_maps_unknown_or_malformed_to_none(value: object) -> None:
    assert normalize_satellites_visible(value) is None
