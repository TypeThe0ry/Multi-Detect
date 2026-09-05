from __future__ import annotations

import pytest

from multidetect.adaptive_ranging import (
    AdaptiveRangingConfig,
    AdaptiveRangingPolicy,
    MotionRegime,
    NavigationState,
    VehicleProfile,
    assess_gps_quality,
)
from multidetect.domain import VehicleTelemetry


def _telemetry(**updates: object) -> VehicleTelemetry:
    values: dict[str, object] = {
        "altitude_agl_m": 80.0,
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "ground_speed_mps": 0.0,
        "in_allowed_zone": None,
        "geofence_healthy": None,
        "position_healthy": False,
        "link_healthy": True,
        "flight_mode_allows_deploy": None,
        "release_zone_clear": None,
        "latitude_deg": float("nan"),
        "longitude_deg": float("nan"),
        "satellites_visible": None,
        "gps_fix_type": None,
        "gps_horizontal_accuracy_m": float("nan"),
        "gps_vertical_accuracy_m": float("nan"),
        "gps_observed_at_s": float("nan"),
        "armed": True,
        "velocity_north_mps": 0.0,
        "velocity_east_mps": 0.0,
        "velocity_observed_at_s": 100.0,
        "airspeed_mps": 0.0,
        "airspeed_observed_at_s": 100.0,
        "local_north_m": 2.0,
        "local_east_m": 3.0,
        "local_down_m": -80.0,
        "local_position_observed_at_s": 100.0,
    }
    values.update(updates)
    return VehicleTelemetry(**values)


def test_fixed_wing_gps_cruise_prioritizes_temporal_geometry() -> None:
    policy = AdaptiveRangingPolicy(AdaptiveRangingConfig(vehicle_profile=VehicleProfile.FIXED_WING))
    decision = policy.decide(
        _telemetry(
            ground_speed_mps=18.0,
            airspeed_mps=20.0,
            position_healthy=True,
            latitude_deg=1.2,
            longitude_deg=103.8,
            satellites_visible=12,
            gps_fix_type=3,
            gps_horizontal_accuracy_m=0.8,
            gps_vertical_accuracy_m=1.2,
            gps_observed_at_s=100.0,
        ),
        now_s=100.2,
    )

    assert decision.vehicle_profile is VehicleProfile.FIXED_WING
    assert decision.navigation_state is NavigationState.GPS_AIDED
    assert decision.motion_regime is MotionRegime.HIGH_SPEED
    assert decision.source_weight_priors["vio"] > decision.source_weight_priors["monocular_metric"]
    assert (
        decision.source_weight_priors["rgb_slam"]
        > decision.source_weight_priors["monocular_metric"]
    )


def test_local_position_cannot_promote_an_invalid_gps_fix_to_gps_aided() -> None:
    decision = AdaptiveRangingPolicy().decide(
        _telemetry(
            position_healthy=True,
            latitude_deg=1.2,
            longitude_deg=103.8,
            satellites_visible=12,
            gps_fix_type=0,
            gps_horizontal_accuracy_m=0.8,
            gps_vertical_accuracy_m=1.2,
            gps_observed_at_s=100.0,
        ),
        now_s=100.2,
    )

    assert decision.navigation_state is NavigationState.LOCAL_NED


def test_stale_or_low_accuracy_gps_remains_outside_gps_aided_mode() -> None:
    policy = AdaptiveRangingPolicy()
    common = {
        "position_healthy": True,
        "latitude_deg": 1.2,
        "longitude_deg": 103.8,
        "satellites_visible": 12,
        "gps_fix_type": 3,
        "gps_vertical_accuracy_m": 1.2,
    }

    stale = policy.decide(
        _telemetry(**common, gps_horizontal_accuracy_m=0.8, gps_observed_at_s=97.0),
        now_s=100.2,
    )
    inaccurate = policy.decide(
        _telemetry(**common, gps_horizontal_accuracy_m=25.0, gps_observed_at_s=100.0),
        now_s=100.2,
    )

    assert stale.navigation_state is NavigationState.LOCAL_NED
    assert inaccurate.navigation_state is NavigationState.LOCAL_NED


def test_future_gps_timestamp_is_rejected_instead_of_looking_fresh() -> None:
    decision = AdaptiveRangingPolicy().decide(
        _telemetry(
            position_healthy=True,
            latitude_deg=1.2,
            longitude_deg=103.8,
            satellites_visible=12,
            gps_fix_type=3,
            gps_horizontal_accuracy_m=0.8,
            gps_vertical_accuracy_m=1.2,
            gps_observed_at_s=101.0,
        ),
        now_s=100.2,
    )

    # The local NED sample is current, so it is the explicit fallback.  A
    # future GPS sample must never promote the target to GPS-aided mapping.
    assert decision.navigation_state is NavigationState.LOCAL_NED


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("maximum_gps_horizontal_accuracy_m", 6.01),
        ("maximum_gps_vertical_accuracy_m", 10.01),
        ("maximum_gps_age_s", 1.51),
    ),
)
def test_adaptive_config_cannot_relax_canonical_gps_quality_gate(
    field: str, value: float
) -> None:
    with pytest.raises(ValueError, match="canonical"):
        AdaptiveRangingConfig(**{field: value})


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("gps_fix_type", 9, "gps_fix_unqualified"),
        ("gps_fix_type", 255, "gps_fix_unqualified"),
        ("satellites_visible", 255, "gps_satellites_insufficient"),
    ],
)
def test_mavlink_unknown_or_out_of_range_gps_values_never_qualify(
    field: str,
    value: int,
    reason: str,
) -> None:
    updates: dict[str, object] = {
        "position_healthy": True,
        "latitude_deg": 1.2,
        "longitude_deg": 103.8,
        "satellites_visible": 12,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 0.8,
        "gps_vertical_accuracy_m": 1.2,
        "gps_observed_at_s": 100.0,
    }
    updates[field] = value
    telemetry = _telemetry(**updates)

    quality = assess_gps_quality(telemetry, now_s=100.2)

    assert quality.qualified is False
    assert quality.reason == reason


@pytest.mark.parametrize("fix_type", [7, 8])
def test_mavlink_static_and_ppp_fix_types_remain_valid(fix_type: int) -> None:
    quality = assess_gps_quality(
        _telemetry(
            position_healthy=True,
            latitude_deg=1.2,
            longitude_deg=103.8,
            satellites_visible=12,
            gps_fix_type=fix_type,
            gps_horizontal_accuracy_m=0.8,
            gps_vertical_accuracy_m=1.2,
            gps_observed_at_s=100.0,
        ),
        now_s=100.2,
    )

    assert quality.qualified is True
    assert quality.reason == "gps_qualified"


def test_future_local_and_airspeed_samples_do_not_enter_dead_reckoning() -> None:
    decision = AdaptiveRangingPolicy().decide(
        _telemetry(
            local_north_m=float("nan"),
            local_east_m=float("nan"),
            local_down_m=float("nan"),
            local_position_observed_at_s=101.0,
            ground_speed_mps=float("nan"),
            velocity_north_mps=float("nan"),
            velocity_east_mps=float("nan"),
            velocity_observed_at_s=101.0,
            airspeed_mps=14.0,
            airspeed_observed_at_s=101.0,
        ),
        now_s=100.2,
    )

    assert decision.navigation_state is NavigationState.VISION_ONLY


def test_multirotor_static_gps_denied_prioritizes_current_metric_depth() -> None:
    policy = AdaptiveRangingPolicy(AdaptiveRangingConfig(vehicle_profile=VehicleProfile.MULTIROTOR))
    decision = policy.decide(_telemetry(), now_s=100.2)

    assert decision.vehicle_profile is VehicleProfile.MULTIROTOR
    assert decision.navigation_state is NavigationState.LOCAL_NED
    assert decision.motion_regime is MotionRegime.STATIC
    assert (
        decision.source_weight_priors["monocular_metric"]
        > decision.source_weight_priors["rgb_slam"]
    )


def test_gps_denied_airspeed_dead_reckoning_remains_a_distinct_state() -> None:
    decision = AdaptiveRangingPolicy().decide(
        _telemetry(
            ground_speed_mps=float("nan"),
            velocity_north_mps=float("nan"),
            velocity_east_mps=float("nan"),
            local_north_m=float("nan"),
            local_east_m=float("nan"),
            local_down_m=float("nan"),
            airspeed_mps=14.0,
            airspeed_observed_at_s=100.0,
        ),
        now_s=100.2,
    )

    assert decision.navigation_state is NavigationState.AIRSPEED_DR
    assert decision.motion_regime is MotionRegime.HIGH_SPEED
    assert decision.source_weight_priors["vio"] > 1.0
