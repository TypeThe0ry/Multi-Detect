from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from multidetect.autonomy.mission_adapter import (
    ExternalRuntimeAttestation,
    MissionExecutionAdapter,
    build_external_px4_adapter,
    build_in_process_px4_adapter,
)
from multidetect.mission_planning import compile_planned_task_mission
from multidetect.px4_external_mission import (
    ExternalPx4MissionError,
    attest_owned_px4_container,
)
from scripts.run_px4_airframe_transaction_acceptance import (
    _PROFILE_HASH,
    _compile_fixture,
)


@pytest.mark.parametrize("vehicle_class", ("vtol", "fixed-wing", "multirotor"))
def test_in_process_adapter_reuses_shared_compiler_and_transaction(
    vehicle_class: str, tmp_path: Path
) -> None:
    context, _, geometry, policy = _compile_fixture(vehicle_class)
    compilation = compile_planned_task_mission(
        # Compile a task through the same public compiler seam used by the
        # transaction acceptance path; the adapter only executes the result.
        task=_task_for_vehicle(vehicle_class),
        context=context,
        vehicle_class=vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=_PROFILE_HASH,
        mission_id=f"adapter-{vehicle_class}",
    )
    assert compilation.plan.context_snapshot_hash == context.canonical_hash
    adapter = build_in_process_px4_adapter(vehicle_class)
    assert isinstance(adapter, MissionExecutionAdapter)
    result = adapter.execute(
        compilation,
        context=context,
        policy=policy,
        journal_path=tmp_path / f"{vehicle_class}.jsonl",
        now_utc=datetime(2026, 9, 27, tzinfo=timezone.utc),
        transaction_id=f"adapter-{vehicle_class}-transaction",
    )
    assert result["gate_passed"] is True
    assert result["vehicle_class"] == vehicle_class
    assert result["execution_adapter"] == "in_process_px4_mission"
    assert result["external_runtime"] is False
    assert result["actuator_output_enabled"] is False


def test_adapter_rejects_vehicle_profile_mismatch() -> None:
    with pytest.raises(ValueError, match="airframe_profile_id"):
        build_in_process_px4_adapter("fixed-wing", airframe_profile_id="px4_vtol_gazebo_v1")


class _Message:
    def __init__(self, message_type: str, **fields: object) -> None:
        self._message_type = message_type
        self.__dict__.update(fields)

    def get_type(self) -> str:
        return self._message_type

    def get_srcSystem(self) -> int:
        return 1

    def get_srcComponent(self) -> int:
        return 1


class _MavRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):
        def send(*args: object) -> None:
            self.calls.append((name, args))

        return send


class _ExternalConnection:
    def __init__(self, messages: list[_Message]) -> None:
        self.mav = _MavRecorder()
        self._messages = iter(messages)

    def recv_match(self, **_: object) -> _Message:
        return next(self._messages)


def _external_messages(compilation):
    target = {"target_system": 250, "target_component": 191}
    messages = [
        _Message("MISSION_REQUEST_INT", seq=item.sequence, **target)
        for item in compilation.compiled_mission.wire_items
    ]
    messages.append(_Message("MISSION_ACK", type=0, **target))
    messages.append(
        _Message("MISSION_COUNT", count=len(compilation.compiled_mission.wire_items), **target)
    )
    for item in compilation.compiled_mission.wire_items:
        item_fields = item.to_mapping()
        item_fields.update(target)
        item_fields["seq"] = item_fields.pop("sequence")
        messages.append(
            _Message(
                "MISSION_ITEM_INT",
                **item_fields,
            )
        )
    return messages


def test_external_adapter_proves_real_mission_transport_without_starting_flight(
    tmp_path: Path,
) -> None:
    vehicle_class = "fixed-wing"
    context, _, geometry, policy = _compile_fixture(vehicle_class)
    compilation = compile_planned_task_mission(
        task=_task_for_vehicle(vehicle_class),
        context=context,
        vehicle_class=vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=_PROFILE_HASH,
        mission_id="external-fixed-wing",
    )
    connection = _ExternalConnection(_external_messages(compilation))
    adapter = build_external_px4_adapter(vehicle_class, connection)
    result = adapter.execute(
        compilation,
        context=context,
        policy=policy,
        journal_path=tmp_path / "external-fixed-wing.jsonl",
        now_utc=datetime(2026, 9, 27, tzinfo=timezone.utc),
        transaction_id="external-fixed-wing-transaction",
    )
    assert result["gate_passed"] is True
    assert result["external_runtime"] is False
    assert result["external_runtime_attested"] is False
    assert result["transport_exercised"] is True
    assert result["runtime_mode"] == "SIM"
    assert result["flight_started"] is False
    assert result["checks"]["state_path_exact"] is True
    assert result["checks"]["readback_hash_match"] is True
    assert [name for name, _ in connection.mav.calls].count("mission_item_int_send") == len(
        compilation.compiled_mission.wire_items
    )


def test_external_adapter_ignores_one_stale_ack_before_replacement_requests(
    tmp_path: Path,
) -> None:
    vehicle_class = "multirotor"
    context, _, geometry, policy = _compile_fixture(vehicle_class)
    compilation = compile_planned_task_mission(
        task=_task_for_vehicle(vehicle_class),
        context=context,
        vehicle_class=vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=_PROFILE_HASH,
        mission_id="external-multirotor-stale-ack",
    )
    target = {"target_system": 250, "target_component": 191}
    messages = [_Message("MISSION_ACK", type=0, **target)]
    messages.extend(
        _Message("MISSION_REQUEST_INT", seq=item.sequence, **target)
        for item in compilation.compiled_mission.wire_items
    )
    messages.append(_Message("MISSION_ACK", type=0, **target))
    messages.append(
        _Message(
            "MISSION_COUNT",
            count=len(compilation.compiled_mission.wire_items),
            **target,
        )
    )
    for item in compilation.compiled_mission.wire_items:
        item_fields = item.to_mapping()
        item_fields.update(target)
        item_fields["seq"] = item_fields.pop("sequence")
        messages.append(_Message("MISSION_ITEM_INT", **item_fields))
    result = build_external_px4_adapter(vehicle_class, _ExternalConnection(messages)).execute(
        compilation,
        context=context,
        policy=policy,
        journal_path=tmp_path / "external-multirotor-stale-ack.jsonl",
        now_utc=datetime(2026, 9, 27, tzinfo=timezone.utc),
        transaction_id="external-multirotor-stale-ack-transaction",
    )
    assert result["gate_passed"] is True
    assert result["upload"]["premature_ack_count"] == 1


@pytest.mark.parametrize("live_current_sequence", (None, 0, 1, 2))
def test_external_readback_accepts_dynamic_execution_current_flags(
    tmp_path: Path, live_current_sequence: int | None
) -> None:
    vehicle_class = "multirotor"
    context, _, geometry, policy = _compile_fixture(vehicle_class)
    compilation = compile_planned_task_mission(
        task=_task_for_vehicle(vehicle_class),
        context=context,
        vehicle_class=vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=_PROFILE_HASH,
        mission_id="external-dynamic-cursor",
    )
    messages = _external_messages(compilation)
    expected_raw_flags = []
    for message in messages:
        if message.get_type() == "MISSION_ITEM_INT":
            message.current = int(message.seq == live_current_sequence)
            expected_raw_flags.append(message.current)
    result = build_external_px4_adapter(vehicle_class, _ExternalConnection(messages)).execute(
        compilation,
        context=context,
        policy=policy,
        journal_path=tmp_path / "external-dynamic-cursor.jsonl",
        now_utc=datetime(2026, 9, 27, tzinfo=timezone.utc),
        transaction_id="external-dynamic-cursor-transaction",
    )
    assert result["gate_passed"] is True
    assert result["readback"]["raw_execution_current_flags"] == expected_raw_flags
    assert result["readback"]["execution_current_normalized"] is True
    assert result["readback"]["mission_progress_source"] == "MISSION_CURRENT"
    assert result["readback"]["readback_protocol_sha256"] == (
        compilation.compiled_mission.protocol_hash
    )
    assert [item["current"] for item in result["readback"]["items"]] == [
        int(item.sequence == 0) for item in compilation.compiled_mission.wire_items
    ]


@pytest.mark.parametrize("invalid_current", (-1, 2, 0.5, True, "0"))
def test_external_readback_rejects_invalid_execution_current_flag(
    tmp_path: Path, invalid_current: object
) -> None:
    vehicle_class = "multirotor"
    context, _, geometry, policy = _compile_fixture(vehicle_class)
    compilation = compile_planned_task_mission(
        task=_task_for_vehicle(vehicle_class),
        context=context,
        vehicle_class=vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=_PROFILE_HASH,
        mission_id="external-invalid-cursor",
    )
    messages = _external_messages(compilation)
    for message in messages:
        if message.get_type() == "MISSION_ITEM_INT":
            message.current = invalid_current
            break
    with pytest.raises(ExternalPx4MissionError, match="current must be an integer 0 or 1"):
        build_external_px4_adapter(vehicle_class, _ExternalConnection(messages)).execute(
            compilation,
            context=context,
            policy=policy,
            journal_path=tmp_path / "external-invalid-cursor.jsonl",
            now_utc=datetime(2026, 9, 27, tzinfo=timezone.utc),
            transaction_id="external-invalid-cursor-transaction",
        )


def test_external_adapter_rejects_legacy_request_before_sending_item(tmp_path: Path) -> None:
    vehicle_class = "multirotor"
    context, _, geometry, policy = _compile_fixture(vehicle_class)
    compilation = compile_planned_task_mission(
        task=_task_for_vehicle(vehicle_class),
        context=context,
        vehicle_class=vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=_PROFILE_HASH,
        mission_id="external-multirotor-legacy",
    )
    connection = _ExternalConnection(
        [_Message("MISSION_REQUEST", seq=0, target_system=250, target_component=191)]
    )
    adapter = build_external_px4_adapter(vehicle_class, connection)
    with pytest.raises(Exception, match="legacy MISSION_REQUEST"):
        adapter.execute(
            compilation,
            context=context,
            policy=policy,
            journal_path=tmp_path / "external-multirotor-legacy.jsonl",
            now_utc=datetime(2026, 9, 27, tzinfo=timezone.utc),
            transaction_id="external-multirotor-legacy-transaction",
        )


def _runtime_attestation(vehicle_class: str) -> dict[str, object]:
    profile = {
        "vtol": "px4_vtol_gazebo_v1",
        "fixed-wing": "px4_fixed_wing_gazebo_v1",
        "multirotor": "px4_multirotor_gazebo_v1",
    }[vehicle_class]
    return {
        "container_id": "a" * 64,
        "image": "px4io/px4-sitl-gazebo@sha256:" + "b" * 64,
        "vehicle_class": vehicle_class,
        "airframe_profile_id": profile,
        "scenario_id": f"{vehicle_class}-scenario",
        "scenario_manifest_sha256": "c" * 64,
        "ownership_checks": {
            "running": True,
            "pinned_image": True,
            "purpose_label": True,
            "bridge_network": True,
            "not_privileged": True,
            "no_device_mapping": True,
            "no_host_mounts": True,
        },
        "telemetry_qualified": True,
        "truth_free": True,
        "hardware_control_enabled": False,
    }


def test_external_adapter_attestation_unlocks_only_external_runtime_label(tmp_path: Path) -> None:
    vehicle_class = "multirotor"
    context, _, geometry, policy = _compile_fixture(vehicle_class)
    compilation = compile_planned_task_mission(
        task=_task_for_vehicle(vehicle_class),
        context=context,
        vehicle_class=vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=_PROFILE_HASH,
        mission_id="external-attested-multirotor",
    )
    connection = _ExternalConnection(_external_messages(compilation))
    attestation = ExternalRuntimeAttestation.from_mapping(_runtime_attestation(vehicle_class))
    adapter = build_external_px4_adapter(
        vehicle_class,
        connection,
        runtime_attestation=attestation,
    )
    result = adapter.execute(
        compilation,
        context=context,
        policy=policy,
        journal_path=tmp_path / "external-attested-multirotor.jsonl",
        now_utc=datetime(2026, 9, 27, tzinfo=timezone.utc),
        transaction_id="external-attested-multirotor-transaction",
    )
    assert result["gate_passed"] is True
    assert result["runtime_mode"] == "SIM_EXTERNAL"
    assert result["external_runtime"] is True
    assert result["external_runtime_attested"] is True
    assert result["runtime_attestation"]["attestation_sha256"] == attestation.canonical_hash


def test_runtime_attestation_rejects_hardware_or_truth_claims() -> None:
    invalid = _runtime_attestation("fixed-wing")
    invalid["hardware_control_enabled"] = True
    with pytest.raises(Exception, match="hardware control"):
        ExternalRuntimeAttestation.from_mapping(invalid)


def test_docker_inspect_record_produces_bound_attestation() -> None:
    vehicle_class = "fixed-wing"
    image = "px4io/px4-sitl-gazebo@sha256:" + "d" * 64
    record = {
        "Id": "e" * 64,
        "Config": {
            "Image": image,
            "Labels": {"multidetect.purpose": "fixed-wing-mission-acceptance"},
            "Env": ["PX4_SIM_MODEL=rc_cessna_mono_cam"],
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "Devices": [],
            "PortBindings": {
                "14640/udp": [{"HostIp": "127.0.0.1", "HostPort": "14640"}]
            },
        },
        "State": {"Running": True},
        "Mounts": [],
    }
    attestation = attest_owned_px4_container(
        record,
        expected_container_id="e" * 64,
        expected_image=image,
        vehicle_class=vehicle_class,
        scenario_id="coastal_patrol_fixed_wing_001",
        scenario_manifest_sha256="f" * 64,
        expected_model="rc_cessna_mono_cam",
        expected_purpose_label="fixed-wing-mission-acceptance",
        control_port=14640,
        telemetry_qualified=True,
    )
    assert attestation.vehicle_class == vehicle_class
    assert attestation.to_mapping()["ownership_checks"]["exact_loopback_control_port"] is True


def test_docker_inspect_attestation_rejects_public_port_binding() -> None:
    image = "px4io/px4-sitl-gazebo@sha256:" + "d" * 64
    record = {
        "Id": "e" * 64,
        "Config": {
            "Image": image,
            "Labels": {"multidetect.purpose": "fixed-wing-mission-acceptance"},
            "Env": ["PX4_SIM_MODEL=rc_cessna_mono_cam"],
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "Devices": [],
            "PortBindings": {
                "14640/udp": [{"HostIp": "0.0.0.0", "HostPort": "14640"}]
            },
        },
        "State": {"Running": True},
        "Mounts": [],
    }
    with pytest.raises(Exception, match="loopback_control_port"):
        attest_owned_px4_container(
            record,
            expected_container_id="e" * 64,
            expected_image=image,
            vehicle_class="fixed-wing",
            scenario_id="coastal_patrol_fixed_wing_001",
            scenario_manifest_sha256="f" * 64,
            expected_model="rc_cessna_mono_cam",
            expected_purpose_label="fixed-wing-mission-acceptance",
            control_port=14640,
            telemetry_qualified=True,
        )


def _task_for_vehicle(vehicle_class: str):
    from multidetect.autonomy import (
        GeoPoint,
        PlanAction,
        ResolvedTaskSpec,
        TaskConstraints,
        TaskGeometry,
        TaskType,
    )

    return ResolvedTaskSpec(
        task_id=f"adapter-task-{vehicle_class}",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="corridor",
            resolved_feature_id=f"adapter-{vehicle_class}-feature",
            direction="north",
            length_m=150.0,
            buffer_m=30.0,
            centerline=(
                GeoPoint(1.3000, 103.8000),
                GeoPoint(1.3010, 103.8000, 30.0, "relative_home"),
            ),
            resolution_confidence=1.0,
        ),
        target_classes=("simulated_search_subject",),
        on_detection=(PlanAction.CAPTURE_EVIDENCE,),
        constraints=TaskConstraints(
            minimum_return_battery_pct=5.0,
            maximum_duration_s=600.0,
            remain_inside_geofence=True,
        ),
        completion_action=PlanAction.RETURN,
        unresolved_assumptions=(),
    )
