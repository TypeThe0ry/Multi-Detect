from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.domain import VehicleTelemetry
from multidetect.telemetry import with_observed_flight_mode_permission


def _telemetry(**changes: object) -> VehicleTelemetry:
    base = VehicleTelemetry(
        altitude_agl_m=20.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=1.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=True,
        armed=True,
        flight_mode="AUTO",
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"flight_mode": "MANUAL"}, False),
        ({"armed": False}, False),
        ({"link_healthy": False}, False),
        ({"armed": None}, None),
        ({"flight_mode": None}, None),
    ],
)
def test_observed_flight_mode_permission_is_fail_closed(
    changes: dict[str, object],
    expected: bool | None,
) -> None:
    observed = with_observed_flight_mode_permission(
        _telemetry(**changes),
        allowed_modes=("AUTO", "AUTO_MISSION"),
    )

    assert observed.flight_mode_allows_deploy is expected


def test_observed_flight_mode_permission_requires_policy() -> None:
    with pytest.raises(ValueError, match="requires allowed modes"):
        with_observed_flight_mode_permission(_telemetry(), allowed_modes=())
