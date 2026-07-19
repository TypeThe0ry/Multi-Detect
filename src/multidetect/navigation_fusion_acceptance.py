from __future__ import annotations

import json

from .navigation_fusion import (
    AirspeedMeasurement,
    GpsVelocityMeasurement,
    NavigationFusionValidity,
    NavigationVelocityFusionEngine,
    VisualOdometryVelocityMeasurement,
    WindVelocityMeasurement,
)


def run_navigation_fusion_acceptance() -> dict[str, object]:
    """Run deterministic scale, consistency, freshness, and outlier scenarios."""

    engine = NavigationVelocityFusionEngine()
    gps = GpsVelocityMeasurement(15.0, 2.0, 0.5, 10.0, 3, 12)
    vio = VisualOdometryVelocityMeasurement(15.2, 1.8, 0.4, 10.02, True)
    airspeed = AirspeedMeasurement(14.0, 0.0, 0.35, 1.0, 10.01)
    wind = WindVelocityMeasurement(1.0, 2.0, 0.4, 10.0)

    consistent = engine.solve(
        now_s=10.05,
        gps=gps,
        visual_odometry=vio,
        airspeed=airspeed,
        wind=wind,
    )
    unscaled_vio = engine.solve(
        now_s=10.05,
        gps=gps,
        visual_odometry=VisualOdometryVelocityMeasurement(
            15.2,
            1.8,
            0.4,
            10.02,
            False,
        ),
    )
    conflicting = engine.solve(
        now_s=10.05,
        gps=GpsVelocityMeasurement(15.0, 0.0, 0.2, 10.0, 3, 12),
        visual_odometry=VisualOdometryVelocityMeasurement(
            -15.0,
            0.0,
            0.2,
            10.02,
            True,
        ),
    )
    outlier = engine.solve(
        now_s=10.05,
        gps=GpsVelocityMeasurement(10.0, 0.0, 0.3, 10.0, 3, 12),
        visual_odometry=VisualOdometryVelocityMeasurement(10.1, 0.1, 0.3, 10.02, True),
        airspeed=AirspeedMeasurement(35.0, 0.0, 0.35, 1.0, 10.01),
        wind=WindVelocityMeasurement(0.0, 0.0, 0.4, 10.0),
    )

    if consistent.validity is not NavigationFusionValidity.VALID:
        raise RuntimeError("consistent three-source navigation fusion did not pass")
    if unscaled_vio.validity is not NavigationFusionValidity.DEGRADED:
        raise RuntimeError("unscaled VIO was not excluded")
    if conflicting.validity is not NavigationFusionValidity.INVALID:
        raise RuntimeError("conflicting metric sources did not fail closed")
    if outlier.validity is not NavigationFusionValidity.DEGRADED:
        raise RuntimeError("single navigation outlier was not rejected")

    return {
        "event": "navigation_fusion_acceptance",
        "consistent_three_source": consistent.to_document(),
        "unscaled_vio_gate": unscaled_vio.to_document(),
        "conflicting_pair_gate": conflicting.to_document(),
        "outlier_rejection": outlier.to_document(),
        "synthetic_measurements": True,
        "camera_opened": False,
        "pixhawk_opened": False,
        "messages_transmitted": 0,
        "flight_control_enabled": False,
        "passed": True,
    }


def main() -> int:
    print(
        json.dumps(
            run_navigation_fusion_acceptance(),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_navigation_fusion_acceptance"]
