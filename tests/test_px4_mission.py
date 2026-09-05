from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from multidetect.autonomy import (
    AutonomyContractError,
    ContextSnapshot,
    ExecutableMission,
    GeoPoint,
    MissionCommand,
    MissionFrame,
    MissionItem,
    OperatorGrant,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
    ValidationFinding,
    ValidationReport,
    ValidationVerdict,
)
from multidetect.px4_mission import (
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    MAV_FRAME_MISSION,
    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
    MAV_VTOL_STATE_FW,
    MAV_VTOL_STATE_MC,
    PX4_AUTO_MISSION_CUSTOM_MODE,
    PX4_SYSTEM_ID,
    PX4_VTOL_ADAPTER_HASH,
    PX4_VTOL_ADAPTER_ID,
    MissionTransactionBindings,
    MissionTransactionState,
    Px4MissionError,
    Px4MissionSafetyPolicy,
    Px4MissionTransaction,
    VtolMissionGeometry,
    compile_executable_mission,
    compile_px4_mission_items,
    compile_vtol_executable_mission,
    normalize_px4_vehicle_class,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
HASH_1 = "1" * 64
HASH_2 = "2" * 64


def _context(
    *, geofence_valid: bool = True, profile: str = "px4_vtol_gazebo_v1"
) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="context-vtol-001",
        captured_at_s=10.0,
        captured_at_utc="2026-08-26T00:00:00Z",
        aircraft_position=GeoPoint(1.3000, 103.8000),
        home_position=GeoPoint(1.3000, 103.8000),
        airframe_profile_id=profile,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1800.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=2.0,
        wind_east_mps=-1.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=0.8,
        geofence_valid=geofence_valid,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _plan(context: ContextSnapshot) -> PlatformNeutralPlan:
    return PlatformNeutralPlan(
        plan_id="river-vtol-001",
        task_spec_hash=HASH_A,
        context_snapshot_hash=context.canonical_hash,
        steps=(
            PlanStep(0, PlanAction.TAKEOFF),
            PlanStep(1, PlanAction.TRANSIT),
            PlanStep(2, PlanAction.SEARCH_CORRIDOR),
            PlanStep(3, PlanAction.LAND),
        ),
    )


def _runtime_geometry() -> dict[str, object]:
    return {
        "scenario_id": "runtime-only-vtol",
        "home_position": {"latitude_deg": 1.3000, "longitude_deg": 103.8000},
        "mission_geometry": {
            "waypoints": [
                {
                    "latitude_deg": 1.3004,
                    "longitude_deg": 103.8002,
                    "altitude_m": 45.0,
                },
                {
                    "latitude_deg": 1.3008,
                    "longitude_deg": 103.8004,
                    "altitude_m": 50.0,
                },
            ],
            "takeoff_altitude_m": 30.0,
            "loiter_time_s": 20.0,
            "loiter_radius_m": 35.0,
            "acceptance_radius_m": 5.0,
            "landing": {
                "latitude_deg": 1.3000,
                "longitude_deg": 103.8000,
                "altitude_m": 0.0,
            },
        },
    }


def _policy() -> Px4MissionSafetyPolicy:
    return Px4MissionSafetyPolicy(
        geofence_center=GeoPoint(1.3000, 103.8000),
        geofence_radius_m=2_000.0,
        allowed_airframe_profile_ids=("px4_vtol_gazebo_v1",),
        minimum_relative_altitude_m=10.0,
        maximum_relative_altitude_m=120.0,
        maximum_mission_radius_m=1_000.0,
        maximum_loiter_radius_m=100.0,
        maximum_landing_offset_m=10.0,
    )


def _compiled():
    context = _context()
    plan = _plan(context)
    geometry = VtolMissionGeometry.from_runtime_mapping(_runtime_geometry())
    compiled = compile_vtol_executable_mission(
        platform_plan=plan,
        context_snapshot=context,
        geometry=geometry,
        policy=_policy(),
        airframe_profile_hash=HASH_B,
    )
    return context, plan, compiled


def _transaction_documents(compiled, context, plan):
    report = ValidationReport(
        report_id="validation-vtol-001",
        executable_mission_hash=compiled.executable_mission.canonical_hash,
        verdict=ValidationVerdict.PASS,
        findings=(
            ValidationFinding("px4-wire", ValidationVerdict.PASS, "hash-bound"),
            ValidationFinding("geofence", ValidationVerdict.PASS, "inside"),
        ),
    )
    bindings = MissionTransactionBindings(
        prompt_hash=HASH_C,
        task_spec_hash=plan.task_spec_hash,
        plan_hash=plan.canonical_hash,
        context_snapshot_hash=context.canonical_hash,
        airframe_profile_hash=HASH_B,
        geofence_hash=_policy().geofence_hash,
        vehicle_identity_hash=HASH_D,
        runtime_descriptor_hash=HASH_E,
        runtime_provenance_hash=HASH_F,
    )
    grant = OperatorGrant(
        grant_id="grant-vtol-001",
        transaction_id="transaction-vtol-001",
        operator_id="test-operator",
        prompt_hash=bindings.prompt_hash,
        task_spec_hash=bindings.task_spec_hash,
        plan_hash=bindings.plan_hash,
        executable_mission_hash=compiled.executable_mission.canonical_hash,
        validation_report_hash=report.canonical_hash,
        context_snapshot_hash=bindings.context_snapshot_hash,
        airframe_profile_hash=bindings.airframe_profile_hash,
        geofence_hash=bindings.geofence_hash,
        vehicle_identity_hash=bindings.vehicle_identity_hash,
        runtime_descriptor_hash=bindings.runtime_descriptor_hash,
        runtime_provenance_hash=bindings.runtime_provenance_hash,
        expires_at="2027-01-01T00:00:00Z",
    )
    return report, bindings, grant


def test_runtime_geometry_compiles_complete_vtol_mission_and_int_wire_contract() -> None:
    _context_value, plan, compiled = _compiled()
    mission = compiled.executable_mission

    assert mission.platform_plan_hash == plan.canonical_hash
    assert mission.airframe_adapter_hash == PX4_VTOL_ADAPTER_HASH
    assert [item.command for item in mission.items] == [
        MissionCommand.VTOL_TAKEOFF,
        MissionCommand.VTOL_TRANSITION,
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.LOITER_TIME,
        MissionCommand.VTOL_TRANSITION,
        MissionCommand.VTOL_LAND,
    ]
    assert [item.current for item in mission.items] == [1, 0, 0, 0, 0, 0, 0]
    assert all(item.autocontinue and item.mission_type == 0 for item in mission.items)
    assert mission.items[1].param1 == MAV_VTOL_STATE_FW
    assert mission.items[2].param2 == 5.0
    assert mission.items[4].param1 == 20.0
    assert mission.items[4].param3 == 35.0
    assert mission.items[-2].param1 == MAV_VTOL_STATE_MC

    wire = compiled.wire_items
    assert wire[0].frame == MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    assert wire[1].frame == MAV_FRAME_MISSION
    assert wire[0].to_send_args()[:7] == (1, 1, 0, 6, 84, 1, 1)
    assert wire[-1].command == 85
    assert compiled.wire_hash == compiled.protocol_hash
    assert compiled.wire_items == compile_px4_mission_items(mission)
    assert len(compiled.wire_hash) == 64


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("vtol", "vtol"),
        ("v-tol", "vtol"),
        ("vertical takeoff and landing", "vtol"),
        ("vertical_takeoff_landing", "vtol"),
    ),
)
def test_px4_vehicle_dispatcher_normalizes_vtol_aliases(value: str, expected: str) -> None:
    assert normalize_px4_vehicle_class(value) == expected
    context, plan, direct = _compiled()
    geometry = VtolMissionGeometry.from_runtime_mapping(_runtime_geometry())
    compiled = compile_executable_mission(
        vehicle_class=value,
        platform_plan=plan,
        context_snapshot=context,
        geometry=geometry,
        policy=_policy(),
        airframe_profile_hash=HASH_B,
    )
    assert compiled.executable_mission.airframe_adapter_id == PX4_VTOL_ADAPTER_ID
    assert compiled.wire_hash == direct.wire_hash


def test_executable_mission_hash_binds_all_px4_parameters() -> None:
    _context_value, _plan_value, compiled = _compiled()
    original = compiled.executable_mission
    changed_item = replace(original.items[2], param2=6.0)
    changed = replace(
        original, items=(original.items[0], original.items[1], changed_item, *original.items[3:])
    )

    assert changed.canonical_hash != original.canonical_hash
    assert compile_px4_mission_items(changed)[2].param2 == 6.0
    mapping = original.items[2].to_mapping()
    assert {
        "param1",
        "param2",
        "param3",
        "param4",
        "current",
        "autocontinue",
        "mission_type",
    }.issubset(mapping)


def test_runtime_geometry_rejects_truth_channel() -> None:
    raw = _runtime_geometry()
    raw["truth"] = {"target_position": [1.3, 103.8]}
    with pytest.raises(Px4MissionError, match="evaluator-only"):
        VtolMissionGeometry.from_runtime_mapping(raw)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("context_hash", "context hash"),
        ("geofence_invalid", "invalid geofence"),
        ("airframe", "airframe profile"),
        ("altitude", "altitude"),
        ("loiter_radius", "loiter radius"),
        ("outside_geofence", "outside the geofence"),
        ("landing", "landing terminal altitude"),
    ],
)
def test_compile_validation_fails_closed(mutation: str, message: str) -> None:
    context = _context()
    plan = _plan(context)
    geometry = VtolMissionGeometry.from_runtime_mapping(_runtime_geometry())
    policy = _policy()
    if mutation == "context_hash":
        plan = replace(plan, context_snapshot_hash=HASH_1)
    elif mutation == "geofence_invalid":
        context = _context(geofence_valid=False)
        plan = _plan(context)
    elif mutation == "airframe":
        context = _context(profile="unexpected_fixed_wing")
        plan = _plan(context)
    elif mutation == "altitude":
        geometry = replace(geometry, takeoff_altitude_m=200.0)
    elif mutation == "loiter_radius":
        geometry = replace(geometry, loiter_radius_m=120.0)
    elif mutation == "outside_geofence":
        outside = GeoPoint(2.0, 104.0, 50.0, "relative_home")
        geometry = replace(geometry, waypoints=(outside,))
    elif mutation == "landing":
        geometry = replace(
            geometry,
            landing=GeoPoint(1.3, 103.8, 1.0, "relative_home"),
        )

    with pytest.raises(Px4MissionError, match=message):
        compile_vtol_executable_mission(
            platform_plan=plan,
            context_snapshot=context,
            geometry=geometry,
            policy=policy,
            airframe_profile_hash=HASH_B,
        )


def test_operator_grant_upload_readback_commit_and_set_mode_gate() -> None:
    context, plan, compiled = _compiled()
    report, bindings, grant = _transaction_documents(compiled, context, plan)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    transaction = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=now,
    )

    upload = transaction.begin_upload(now_utc=now)
    assert upload.count_send_args() == (1, 1, len(compiled.wire_items), 0)
    for sequence in reversed(range(upload.item_count)):
        assert transaction.mission_item_for_request(sequence) == upload.items[sequence]
    transaction.accept_upload_ack(now_utc=now)
    assert transaction.verify_readback(compiled.wire_items, now_utc=now) == compiled.wire_hash
    assert transaction.commit(now_utc=now) == compiled.wire_hash
    permit = transaction.permit_start(now_utc=now)

    assert transaction.state is MissionTransactionState.START_PERMITTED
    assert permit.to_set_mode_args() == (
        PX4_SYSTEM_ID,
        MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        PX4_AUTO_MISSION_CUSTOM_MODE,
    )


def test_transaction_rejects_expiry_and_every_grant_binding_mismatch() -> None:
    context, plan, compiled = _compiled()
    report, bindings, grant = _transaction_documents(compiled, context, plan)
    with pytest.raises(Px4MissionError, match="expired"):
        Px4MissionTransaction(
            transaction_id=grant.transaction_id,
            compiled=compiled,
            validation_report=report,
            operator_grant=replace(grant, expires_at="2026-01-01T00:00:00Z"),
            bindings=bindings,
            now_utc=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )

    grant_fields = (
        "prompt_hash",
        "task_spec_hash",
        "plan_hash",
        "executable_mission_hash",
        "validation_report_hash",
        "context_snapshot_hash",
        "airframe_profile_hash",
        "geofence_hash",
        "vehicle_identity_hash",
        "runtime_descriptor_hash",
        "runtime_provenance_hash",
    )
    for field in grant_fields:
        broken = replace(grant, **{field: HASH_2})
        with pytest.raises(Px4MissionError, match=f"{field} binding mismatch"):
            Px4MissionTransaction(
                transaction_id=grant.transaction_id,
                compiled=compiled,
                validation_report=report,
                operator_grant=broken,
                bindings=bindings,
                now_utc=datetime(2026, 8, 26, tzinfo=timezone.utc),
            )


def test_transaction_requires_pass_full_int_upload_exact_readback_and_commit() -> None:
    context, plan, compiled = _compiled()
    report, bindings, grant = _transaction_documents(compiled, context, plan)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)

    denied_report = ValidationReport(
        report_id="deny",
        executable_mission_hash=compiled.executable_mission.canonical_hash,
        verdict=ValidationVerdict.DENY,
        findings=(ValidationFinding("geofence", ValidationVerdict.DENY, "outside"),),
    )
    with pytest.raises(Px4MissionError, match="verdict must be PASS"):
        Px4MissionTransaction(
            transaction_id=grant.transaction_id,
            compiled=compiled,
            validation_report=denied_report,
            operator_grant=replace(grant, validation_report_hash=denied_report.canonical_hash),
            bindings=bindings,
            now_utc=now,
        )

    early = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=now,
    )
    with pytest.raises(Px4MissionError, match="does not permit"):
        early.permit_start(now_utc=now)
    assert early.state is MissionTransactionState.DENIED

    legacy = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=now,
    )
    legacy.begin_upload(now_utc=now)
    with pytest.raises(Px4MissionError, match="MISSION_REQUEST_INT"):
        legacy.mission_item_for_request(0, request_message_type="MISSION_REQUEST")

    mismatch = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=now,
    )
    mismatch.begin_upload(now_utc=now)
    for sequence in range(len(compiled.wire_items)):
        mismatch.mission_item_for_request(sequence)
    mismatch.accept_upload_ack(now_utc=now)
    changed = (*compiled.wire_items[:-1], replace(compiled.wire_items[-1], z=1.0))
    with pytest.raises(Px4MissionError, match="protocol hash mismatch"):
        mismatch.verify_readback(changed, now_utc=now)
    assert mismatch.state is MissionTransactionState.DENIED


def test_mission_item_contract_rejects_noncanonical_transition_and_current_flags() -> None:
    with pytest.raises(AutonomyContractError, match="current item"):
        ExecutableMission(
            mission_id="bad-current",
            platform_plan_hash=HASH_A,
            airframe_profile_hash=HASH_B,
            airframe_adapter_id="test",
            airframe_adapter_hash=HASH_C,
            items=(
                MissionItem(
                    0,
                    MissionCommand.VTOL_TAKEOFF,
                    MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                    1.3,
                    103.8,
                    30.0,
                    current=0,
                ),
            ),
        )
    with pytest.raises(AutonomyContractError, match="param1"):
        MissionItem(
            sequence=0,
            command=MissionCommand.VTOL_TRANSITION,
            frame=MissionFrame.MISSION,
            param1=2.0,
        )
