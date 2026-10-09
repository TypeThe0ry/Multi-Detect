from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.autonomy import AutonomyContractError
from multidetect.autonomy.task_policy_runtime import TaskPolicyRuntimeBinding
from multidetect.domain import VehicleTelemetry
from multidetect.multimodal_ranging import RangeSolution, RangeValidity
from multidetect.operator_link import PolicyRuntimeAction, RuntimeBackendHealth, RuntimeMode
from multidetect.operator_status import (
    RangeSolutionFrameBinding,
    build_gnss_status_message,
    build_policy_runtime_status_message,
    build_range_status_message,
    build_runtime_status_from_descriptor,
    build_runtime_status_message,
    build_target_geolocation_status_message,
)
from multidetect.runtime import RuntimeDescriptor, RuntimeProfile

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def _qualified_gnss_telemetry(**overrides: object) -> VehicleTelemetry:
    values: dict[str, object] = {
        "altitude_agl_m": 10.0,
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "ground_speed_mps": 0.0,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "position_healthy": True,
        "link_healthy": True,
        "flight_mode_allows_deploy": False,
        "release_zone_clear": False,
        "latitude_deg": 1.3,
        "longitude_deg": 103.8,
        "gps_fix_type": 3,
        "satellites_visible": 12,
        "gps_horizontal_accuracy_m": 1.2,
        "gps_vertical_accuracy_m": 2.4,
        "gps_observed_at_s": 99.8,
    }
    values.update(overrides)
    return VehicleTelemetry(**values)


def test_gnss_status_builder_uses_canonical_policy_and_raw_quality() -> None:
    status = build_gnss_status_message(
        sequence=4,
        telemetry=_qualified_gnss_telemetry(),
        produced_at_s=100.0,
    )
    assert status.qualified is True
    assert status.reason == "gps_qualified"
    assert status.fix_type == 3
    assert status.satellites_visible == 12
    assert status.source_age_s == pytest.approx(0.2)


def test_gnss_status_builder_withholds_invalid_raw_fields_but_keeps_reason() -> None:
    status = build_gnss_status_message(
        sequence=5,
        telemetry=_qualified_gnss_telemetry(
            gps_fix_type=255,
            satellites_visible=255,
            gps_horizontal_accuracy_m=float("nan"),
            gps_vertical_accuracy_m=-1.0,
            gps_observed_at_s=float("nan"),
        ),
        produced_at_s=100.0,
    )
    assert status.qualified is False
    assert status.reason == "gps_fix_unqualified"
    assert status.fix_type is None
    assert status.satellites_visible is None
    assert status.horizontal_accuracy_m is None
    assert status.vertical_accuracy_m is None
    assert status.source_age_s is None


def _valid_range_solution(
    *, evaluated_at_s: float = 100.0, freshness_s: float = 0.1
) -> RangeSolution:
    return RangeSolution(
        target_id="target-a",
        frame_id="frame-a",
        calibration_id="camera-a",
        evaluated_at_s=evaluated_at_s,
        validity=RangeValidity.VALID,
        reasons=("multimodal_range_consistent",),
        sources=("camera_ground",),
        rejected_sources=(),
        slant_range_m=20.0,
        ground_range_m=18.0,
        data_freshness_s=freshness_s,
        sensor_consistency=1.0,
    )


def test_range_source_binding_has_exact_inclusive_freshness_boundary() -> None:
    solution = _valid_range_solution(freshness_s=1.5)
    binding = RangeSolutionFrameBinding.from_solution(
        solution,
        source_frame_id="frame-a",
        source_captured_at_s=98.5,
    )

    status = build_range_status_message(
        sequence=1,
        solution=solution,
        source_binding=binding,
    )
    assert status.data_freshness_s == pytest.approx(1.5)
    assert status.produced_at_s - status.source_captured_at_s == pytest.approx(1.5)

    with pytest.raises(ValueError, match="frame is stale"):
        RangeSolutionFrameBinding.from_solution(
            solution,
            source_frame_id="frame-a",
            source_captured_at_s=98.499999,
        )
    with pytest.raises(ValueError, match="data is stale"):
        RangeSolutionFrameBinding.from_solution(
            replace(solution, data_freshness_s=1.500001),
            source_frame_id="frame-a",
            source_captured_at_s=100.0,
        )
    with pytest.raises(ValueError, match="requires data_freshness_s"):
        RangeSolutionFrameBinding.from_solution(
            replace(solution, data_freshness_s=None),
            source_frame_id="frame-a",
            source_captured_at_s=100.0,
        )


def test_range_source_binding_rejects_wrong_frame_missing_time_and_old_solution_relabel() -> None:
    solution = _valid_range_solution(freshness_s=0.125)
    binding = RangeSolutionFrameBinding.from_solution(
        solution,
        source_frame_id="frame-a",
        source_captured_at_s=99.875,
    )

    with pytest.raises(ValueError, match="immutable source binding"):
        build_range_status_message(
            sequence=2,
            solution=replace(solution, evaluated_at_s=101.0),
            source_binding=binding,
            produced_at_s=101.0,
        )
    with pytest.raises(ValueError, match="immutable source binding"):
        build_range_status_message(
            sequence=3,
            solution=solution,
            source_frame_id="frame-new",
            source_captured_at_s=99.875,
        )
    with pytest.raises(ValueError, match="capture timestamp is required"):
        build_range_status_message(sequence=4, solution=solution)

    # Re-publishing the bound sample advances both source age and effective
    # data age; it may pass exactly at 1.5 s but never at 1.500001 s.
    boundary = build_range_status_message(
        sequence=5,
        solution=solution,
        source_binding=binding,
        produced_at_s=101.375,
    )
    assert boundary.data_freshness_s == pytest.approx(1.5)
    with pytest.raises(ValueError, match="source frame age|data freshness"):
        build_range_status_message(
            sequence=6,
            solution=solution,
            source_binding=binding,
            produced_at_s=101.375001,
        )


def test_target_geolocation_builder_uses_the_same_immutable_range_binding() -> None:
    solution = _valid_range_solution(freshness_s=0.125)
    binding = RangeSolutionFrameBinding.from_solution(
        solution,
        source_frame_id="frame-a",
        source_captured_at_s=99.875,
    )
    telemetry = VehicleTelemetry(
        altitude_agl_m=10.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=0.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=False,
        link_healthy=True,
        flight_mode_allows_deploy=False,
        release_zone_clear=False,
    )

    status = build_target_geolocation_status_message(
        sequence=7,
        solution=solution,
        telemetry=telemetry,
        source_binding=binding,
        produced_at_s=100.0,
    )
    assert status.source_frame_id == "frame-a"
    assert status.source_captured_at_s == pytest.approx(99.875)
    assert status.available is False

    with pytest.raises(ValueError, match="immutable source binding"):
        build_target_geolocation_status_message(
            sequence=8,
            solution=replace(solution, evaluated_at_s=101.0),
            telemetry=telemetry,
            source_binding=binding,
            produced_at_s=101.0,
        )
    with pytest.raises(ValueError, match="source frame is from the future"):
        build_target_geolocation_status_message(
            sequence=9,
            solution=solution,
            telemetry=telemetry,
            source_captured_at_s=100.5,
            produced_at_s=100.5,
        )


@pytest.mark.parametrize(
    "internal_reason",
    [
        "target_ray_uncertainty_unavailable",
        "target_ray_uncertainty_crosses_horizon",
        "camera_offset_uncertainty_unbounded",
        "terrain_intersection_unavailable",
        "terrain_uncertainty_unbounded",
    ],
)
def test_range_status_builder_maps_internal_geometry_failures_to_wire_reason(
    internal_reason: str,
) -> None:
    solution = RangeSolution(
        target_id="target-horizon",
        frame_id="frame-horizon",
        calibration_id="camera-main-v2",
        evaluated_at_s=100.1,
        validity=RangeValidity.INVALID,
        reasons=(internal_reason,),
        sources=(),
        rejected_sources=(),
    )

    status = build_range_status_message(
        sequence=1,
        solution=solution,
        source_captured_at_s=100.0,
    )

    assert solution.reasons == (internal_reason,)
    assert status.reasons == ("target_geometry_uncertainty_unbounded",)


def test_range_status_builder_deduplicates_collapsed_geometry_reasons() -> None:
    solution = RangeSolution(
        target_id="target-horizon",
        frame_id="frame-horizon",
        calibration_id="camera-main-v2",
        evaluated_at_s=100.1,
        validity=RangeValidity.INVALID,
        reasons=(
            "target_ray_uncertainty_crosses_horizon",
            "camera_offset_uncertainty_unbounded",
        ),
        sources=(),
        rejected_sources=(),
    )

    status = build_range_status_message(
        sequence=2,
        solution=solution,
        source_captured_at_s=100.0,
    )

    assert status.reasons == ("target_geometry_uncertainty_unbounded",)


@pytest.mark.parametrize(
    "internal_reason",
    ["absolute_range_uncertainty_inflated", "direct_range_uncertainty_inflated"],
)
def test_range_status_builder_maps_internal_uncertainty_inflation_to_wire_reason(
    internal_reason: str,
) -> None:
    solution = replace(
        _valid_range_solution(freshness_s=0.1),
        validity=RangeValidity.DEGRADED,
        reasons=(internal_reason,),
    )

    status = build_range_status_message(
        sequence=3,
        solution=solution,
        source_frame_id="frame-a",
        source_captured_at_s=99.9,
    )

    assert solution.reasons == (internal_reason,)
    assert status.reasons == ("direct_degraded_metric_range",)


def test_runtime_status_builder_binds_mode_health_scenario_profile_and_run() -> None:
    status = build_runtime_status_message(
        sequence=7,
        mode=RuntimeMode.REPLAY,
        backend_health=RuntimeBackendHealth.HEALTHY,
        scenario_seed=0,
        scenario_hash64=0x0102030405060708,
        airframe_profile_hash64=0x1112131415161718,
        run_hash64=0x2122232425262728,
        ground_truth_isolated=True,
        hardware_outputs_present=False,
        profile_valid=True,
        produced_at_s=20.0,
    )

    assert status.mode is RuntimeMode.REPLAY
    assert status.backend_health is RuntimeBackendHealth.HEALTHY
    assert status.scenario_present is True
    assert status.scenario_seed == 0
    assert status.ground_truth_isolated is True
    assert status.hardware_outputs_present is False
    assert status.profile_valid is True
    assert status.metadata_only is True
    assert status.direct_pixhawk_writes is False
    assert status.physical_release_enabled is False


def test_runtime_status_builder_rejects_partial_scenario_identity() -> None:
    with pytest.raises(ValueError, match="present together|inconsistent"):
        build_runtime_status_message(
            sequence=8,
            mode=RuntimeMode.SIM,
            backend_health=RuntimeBackendHealth.DEGRADED,
            scenario_seed=42,
            scenario_hash64=None,
            airframe_profile_hash64=1,
            run_hash64=2,
            produced_at_s=21.0,
        )


def test_policy_runtime_status_builder_projects_hash_bound_read_only_binding() -> None:
    binding = TaskPolicyRuntimeBinding(
        vehicle_class="vtol",
        context_sha256=HASH_A,
        airframe_profile_hash=HASH_B,
        action=PolicyRuntimeAction.SELECT.value,
        reason="planner_revalidated",
        selected_task_id="river-rescue-001",
        policy_artifact_sha256=HASH_C,
        ranker_sha256=HASH_D,
        plan_sha256=HASH_E,
        executable_mission_sha256=HASH_A,
        wire_hash=HASH_B,
        planner_revalidated=True,
        mission_bound=True,
        produced_at_s=21.0,
    )

    status = build_policy_runtime_status_message(sequence=18, binding=binding)

    assert status.action is PolicyRuntimeAction.SELECT
    assert status.selected_task_hash64 is not None
    assert status.context_hash64 == int(HASH_A[:16], 16)
    assert status.airframe_profile_hash64 == int(HASH_B[:16], 16)
    assert status.planner_revalidated is True
    assert status.mission_bound is True
    assert status.mission_started is False
    assert status.policy_applied_to_actuator is False
    assert status.truth_free is True


def test_runtime_status_builder_requires_nonzero_valid_profile_and_run_hashes() -> None:
    with pytest.raises(ValueError, match="profile"):
        build_runtime_status_message(
            sequence=9,
            mode=RuntimeMode.REAL,
            backend_health=RuntimeBackendHealth.UNHEALTHY,
            airframe_profile_hash64=0,
            run_hash64=2,
            profile_valid=True,
            hardware_outputs_present=True,
            produced_at_s=22.0,
        )
    with pytest.raises(ValueError, match="run hash"):
        build_runtime_status_message(
            sequence=10,
            mode=RuntimeMode.HIL,
            backend_health=RuntimeBackendHealth.UNKNOWN,
            airframe_profile_hash64=1,
            run_hash64=0,
            produced_at_s=23.0,
        )


@pytest.mark.parametrize(
    (
        "mode",
        "scenario_seed",
        "scenario_hash64",
        "ground_truth_isolated",
        "hardware_outputs_present",
        "message",
    ),
    [
        (RuntimeMode.SIM, 1, 2, False, False, "isolate ground truth"),
        (RuntimeMode.HIL, 1, 2, True, True, "hardware outputs"),
        (RuntimeMode.REAL, 1, 2, True, False, "cannot bind a scenario"),
    ],
)
def test_runtime_status_builder_rejects_cross_mode_semantic_mismatch(
    mode: RuntimeMode,
    scenario_seed: int | None,
    scenario_hash64: int | None,
    ground_truth_isolated: bool,
    hardware_outputs_present: bool,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_runtime_status_message(
            sequence=11,
            mode=mode,
            backend_health=RuntimeBackendHealth.HEALTHY,
            scenario_seed=scenario_seed,
            scenario_hash64=scenario_hash64,
            airframe_profile_hash64=3,
            run_hash64=4,
            ground_truth_isolated=ground_truth_isolated,
            hardware_outputs_present=hardware_outputs_present,
            profile_valid=True,
            produced_at_s=24.0,
        )


def test_real_runtime_status_profile_may_explicitly_disable_hardware_outputs() -> None:
    status = build_runtime_status_message(
        sequence=12,
        mode=RuntimeMode.REAL,
        backend_health=RuntimeBackendHealth.DEGRADED,
        airframe_profile_hash64=3,
        run_hash64=4,
        ground_truth_isolated=True,
        hardware_outputs_present=False,
        profile_valid=True,
        produced_at_s=25.0,
    )

    assert status.scenario_present is False
    assert status.hardware_outputs_present is False


def test_runtime_status_from_descriptor_derives_sim_identity_and_safety_flags() -> None:
    descriptor = RuntimeDescriptor(
        mode=RuntimeMode.SIM,
        profile=RuntimeProfile.sim(),
        backend_id="px4-gazebo:river",
        airframe_profile_hash=HASH_A,
        scenario_id="river_rescue_vtol_001",
        scenario_manifest_hash=HASH_B,
        scenario_seed=42017,
        deterministic=True,
        fault_injection_enabled=True,
    )

    status = build_runtime_status_from_descriptor(
        sequence=13,
        descriptor=descriptor,
        backend_health=RuntimeBackendHealth.HEALTHY,
        run_identity_hash=HASH_C,
        produced_at_s=26.0,
    )

    assert status.mode is RuntimeMode.SIM
    assert status.scenario_present is True
    assert status.scenario_seed == 42017
    assert status.scenario_hash64 == int(HASH_B[:16], 16)
    assert status.airframe_profile_hash64 == int(HASH_A[:16], 16)
    assert status.run_hash64 == int(HASH_C[:16], 16)
    assert status.ground_truth_isolated is True
    assert status.hardware_outputs_present is False
    assert status.profile_valid is True


def test_runtime_status_from_descriptor_derives_real_flight_capability() -> None:
    descriptor = RuntimeDescriptor(
        mode=RuntimeMode.REAL,
        profile=RuntimeProfile.flight(
            vehicle_identity_hash=HASH_A,
            airframe_profile_hash=HASH_B,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=HASH_D,
        ),
        backend_id="px4:vehicle-1",
        airframe_profile_hash=HASH_B,
    )

    status = build_runtime_status_from_descriptor(
        sequence=14,
        descriptor=descriptor,
        backend_health=RuntimeBackendHealth.DEGRADED,
        run_identity_hash=HASH_E,
        produced_at_s=27.0,
    )

    assert status.mode is RuntimeMode.REAL
    assert status.scenario_present is False
    assert status.scenario_seed is None
    assert status.scenario_hash64 is None
    assert status.airframe_profile_hash64 == int(HASH_B[:16], 16)
    assert status.run_hash64 == int(HASH_E[:16], 16)
    assert status.ground_truth_isolated is False
    assert status.hardware_outputs_present is True
    assert status.profile_valid is True


def test_runtime_status_from_descriptor_derives_observer_as_read_only() -> None:
    descriptor = RuntimeDescriptor(
        mode=RuntimeMode.REAL,
        profile=RuntimeProfile.observer(),
        backend_id="observer:vehicle-1",
        airframe_profile_hash=HASH_A,
    )

    status = build_runtime_status_from_descriptor(
        sequence=15,
        descriptor=descriptor,
        backend_health=RuntimeBackendHealth.HEALTHY,
        run_identity_hash=HASH_B,
        produced_at_s=28.0,
    )

    assert status.hardware_outputs_present is False
    assert status.ground_truth_isolated is False


def test_runtime_status_from_descriptor_requires_strict_run_sha256() -> None:
    descriptor = RuntimeDescriptor(
        mode=RuntimeMode.REAL,
        profile=RuntimeProfile.observer(),
        backend_id="observer:vehicle-1",
        airframe_profile_hash=HASH_A,
    )

    with pytest.raises(AutonomyContractError, match="run_identity_hash"):
        build_runtime_status_from_descriptor(
            sequence=16,
            descriptor=descriptor,
            backend_health=RuntimeBackendHealth.UNKNOWN,
            run_identity_hash="not-a-sha256",
            produced_at_s=29.0,
        )


def test_runtime_status_from_descriptor_rejects_zero_run_wire_prefix() -> None:
    descriptor = RuntimeDescriptor(
        mode=RuntimeMode.REAL,
        profile=RuntimeProfile.observer(),
        backend_id="observer:vehicle-1",
        airframe_profile_hash=HASH_A,
    )

    with pytest.raises(ValueError, match="64-bit wire prefix cannot be zero"):
        build_runtime_status_from_descriptor(
            sequence=17,
            descriptor=descriptor,
            backend_health=RuntimeBackendHealth.UNKNOWN,
            run_identity_hash=("0" * 16) + ("1" * 48),
            produced_at_s=30.0,
        )
