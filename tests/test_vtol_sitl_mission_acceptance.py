from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from multidetect.px4_mission import Px4MissionError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vtol_sitl_mission_acceptance.py"
MANIFEST = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
CONTAINER_ID = "d" * 64


def _module() -> ModuleType:
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    name = "vtol_sitl_mission_acceptance_test_module"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Message:
    def __init__(
        self,
        message_type: str,
        *,
        source_system: int = 1,
        source_component: int = 1,
        **fields: Any,
    ) -> None:
        self._message_type = message_type
        self._source_system = source_system
        self._source_component = source_component
        for name, value in fields.items():
            setattr(self, name, value)

    def get_type(self) -> str:
        return self._message_type

    def get_srcSystem(self) -> int:
        return self._source_system

    def get_srcComponent(self) -> int:
        return self._source_component


class _MavRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def mission_count_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_COUNT", args))

    def mission_item_int_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_ITEM_INT", args))

    def mission_request_list_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_REQUEST_LIST", args))

    def mission_request_int_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_REQUEST_INT", args))


class _Connection:
    def __init__(self, messages: list[_Message | None]) -> None:
        self.mav = _MavRecorder()
        self._messages = iter(messages)

    def recv_match(self, **_kwargs: Any) -> _Message | None:
        return next(self._messages)


def _live_context() -> dict[str, Any]:
    return {
        "captured_at_s": 12.5,
        "latitude_deg": 1.3000,
        "longitude_deg": 103.8000,
        "gps_fix_type": 3,
        "satellites_visible": 12,
        "gps_horizontal_accuracy_m": 0.8,
        "gps_accuracy_source": "GPS_RAW_INT.h_acc",
        "gps_estimator_disagreement_m": 0.2,
        "battery_remaining_pct": 93.0,
        "ground_speed_mps": 0.0,
        "airspeed_mps": 0.0,
        "source": "live MAVLink telemetry only",
    }


def _documents(module: ModuleType):
    runtime_input = module.load_runtime_scenario(MANIFEST, mode=module.RuntimeMode.SIM)
    now = datetime.now(timezone.utc)
    return module.build_acceptance_documents(
        runtime_input=runtime_input,
        live_telemetry=_live_context(),
        container_id=CONTAINER_ID,
        airframe_profile_hash=HASH_A,
        runtime_descriptor_hash=HASH_B,
        runtime_provenance_hash=HASH_C,
        now_utc=now,
        maximum_duration_s=240.0,
    )


def _transaction(module: ModuleType):
    documents = _documents(module)
    now = datetime.now(timezone.utc)
    transaction = module.Px4MissionTransaction(
        transaction_id=documents.operator_grant.transaction_id,
        compiled=documents.compiled,
        validation_report=documents.validation_report,
        operator_grant=documents.operator_grant,
        bindings=documents.bindings,
        now_utc=now,
    )
    upload_plan = transaction.begin_upload(now_utc=now)
    return documents, transaction, upload_plan


def _upload_messages(upload_plan: Any, *, legacy_sequence: int | None = None) -> list[_Message]:
    result: list[_Message] = []
    for sequence in range(upload_plan.item_count):
        message_type = "MISSION_REQUEST" if legacy_sequence == sequence else "MISSION_REQUEST_INT"
        result.append(
            _Message(
                message_type,
                seq=sequence,
                mission_type=0,
                target_system=250,
                target_component=191,
            )
        )
    result.append(
        _Message(
            "MISSION_ACK",
            type=0,
            mission_type=0,
            target_system=250,
            target_component=191,
        )
    )
    return result


def _readback_messages(upload_plan: Any, *, change_sequence: int | None = None) -> list[_Message]:
    messages = [
        _Message(
            "MISSION_COUNT",
            count=upload_plan.item_count,
            mission_type=0,
            target_system=250,
            target_component=191,
        )
    ]
    for item in upload_plan.items:
        mapping = item.to_mapping()
        x = int(mapping["x"]) + (1 if item.sequence == change_sequence else 0)
        messages.append(
            _Message(
                "MISSION_ITEM_INT",
                seq=item.sequence,
                frame=item.frame,
                command=item.command,
                current=item.current,
                autocontinue=item.autocontinue,
                param1=item.param1,
                param2=item.param2,
                param3=item.param3,
                param4=item.param4,
                x=x,
                y=item.y,
                z=item.z,
                mission_type=item.mission_type,
                target_system=250,
                target_component=191,
            )
        )
    return messages


def test_live_geometry_and_documents_are_runtime_bound_complete_vtol() -> None:
    module = _module()
    documents = _documents(module)
    mission = documents.compiled.executable_mission

    assert documents.context.home_position.latitude_deg == _live_context()["latitude_deg"]
    assert documents.context.home_position.longitude_deg == _live_context()["longitude_deg"]
    assert documents.context.gps_fix_type == 3
    assert documents.context.map_cache_available is True
    assert documents.context.dem_available is True
    assert documents.bindings.prompt_hash == documents.prompt.canonical_hash
    assert documents.bindings.task_spec_hash == documents.task_spec.canonical_hash
    assert documents.bindings.plan_hash == documents.plan.canonical_hash
    assert documents.operator_grant.operator_id == (
        "test-only-acceptance-fixture-not-human-approval"
    )
    assert [item.command.value for item in mission.items] == [
        "MAV_CMD_NAV_VTOL_TAKEOFF",
        "MAV_CMD_DO_VTOL_TRANSITION",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_LOITER_TIME",
        "MAV_CMD_DO_VTOL_TRANSITION",
        "MAV_CMD_NAV_VTOL_LAND",
    ]
    assert all(item.mission_type == 0 for item in mission.items)
    assert documents.compiled.wire_hash == module.px4_wire_hash(documents.compiled.wire_items)
    evidence = module._document_evidence(documents)
    assert evidence["operator_grant"]["human_operator_approval_claimed"] is False
    assert evidence["mission_dispatcher"] == {
        "name": "compile_executable_mission",
        "vehicle_class_input": "vtol",
        "vehicle_class_normalized": "vtol",
    }


def test_live_context_clamps_negative_sitl_speed_without_fabricating_position() -> None:
    module = _module()

    class _Collector:
        elapsed_s = 4.2
        last_position = {
            "latitude_deg": 1.3,
            "longitude_deg": 103.8,
            "relative_altitude_m": 0.0,
        }
        last_battery_remaining = 97.0

        def __init__(self) -> None:
            self._messages = iter(
                [
                    _Message(
                        "VFR_HUD",
                        groundspeed=-0.04,
                        airspeed=-0.78,
                    ),
                    _Message(
                        "GPS_RAW_INT",
                        fix_type=3,
                        lat=13_000_000,
                        lon=1_038_000_000,
                        satellites_visible=10,
                        h_acc=900,
                    ),
                ]
            )

        def pump(self, _timeout_s: float) -> _Message | None:
            try:
                return next(self._messages)
            except StopIteration:
                return None

    context = module._collect_live_context(_Collector(), timeout_s=0.1)
    assert context["latitude_deg"] == 1.3
    assert context["longitude_deg"] == 103.8
    assert context["ground_speed_mps"] == 0.0
    assert context["airspeed_mps"] == 0.0
    assert context["raw_ground_speed_mps"] == -0.04
    assert context["raw_airspeed_mps"] == -0.78
    assert context["nonnegative_speed_clamp_applied"] is True


def test_telemetry_collector_records_monotonic_freshness_for_flight_state() -> None:
    module = _module()
    collector = module.TelemetryCollector(object())

    collector._record(
        _Message(
            "HEARTBEAT",
            base_mode=module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
            custom_mode=module.PX4_AUTO_MISSION_CUSTOM_MODE,
            system_status=4,
            type=2,
        )
    )
    collector._record(
        _Message(
            "GLOBAL_POSITION_INT",
            lat=13_000_000,
            lon=1_038_000_000,
            relative_alt=20_000,
            hdg=9_000,
        )
    )
    collector._record(
        _Message(
            "EXTENDED_SYS_STATE",
            vtol_state=module.MAV_VTOL_STATE_FW,
            landed_state=module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
        )
    )

    observed = (
        collector.last_heartbeat_observed_at_monotonic_s,
        collector.last_position_observed_at_monotonic_s,
        collector.last_extended_state_observed_at_monotonic_s,
    )
    assert all(isinstance(value, float) and value > 0.0 for value in observed)
    assert observed == tuple(sorted(observed))


def test_fake_int_upload_readback_commit_and_permit_exact_auto_mission() -> None:
    module = _module()
    _docs, transaction, upload_plan = _transaction(module)
    foreign_qgc_request = _Message(
        "MISSION_REQUEST_INT",
        seq=0,
        mission_type=0,
        target_system=255,
        target_component=190,
    )
    upload_connection = _Connection([foreign_qgc_request, *_upload_messages(upload_plan)])

    upload = module.upload_int_only_mission(upload_connection, transaction, upload_plan)
    assert upload["acknowledged"] is True
    assert upload["int_only"] is True
    assert upload["ignored_foreign_target_messages"] == 1
    assert transaction.state is module.MissionTransactionState.UPLOADED
    assert [name for name, _args in upload_connection.mav.calls] == [
        "MISSION_COUNT",
        *("MISSION_ITEM_INT" for _ in range(upload_plan.item_count)),
    ]

    foreign_qgc_count = _Message(
        "MISSION_COUNT",
        count=0,
        mission_type=0,
        target_system=255,
        target_component=190,
    )
    readback_connection = _Connection([foreign_qgc_count, *_readback_messages(upload_plan)])
    readback = module.readback_int_only_mission(readback_connection, transaction, upload_plan)
    assert readback["hash_match"] is True
    assert readback["ignored_foreign_target_messages"] == 1
    assert transaction.state is module.MissionTransactionState.READBACK_VERIFIED
    assert transaction.commit() == upload_plan.protocol_hash
    permit = transaction.permit_start()
    assert transaction.state is module.MissionTransactionState.START_PERMITTED
    assert permit.to_set_mode_args() == (
        1,
        module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        module.PX4_AUTO_MISSION_CUSTOM_MODE,
    )
    assert [name for name, _args in readback_connection.mav.calls] == [
        "MISSION_REQUEST_LIST",
        *("MISSION_REQUEST_INT" for _ in range(upload_plan.item_count)),
    ]


def test_legacy_mission_request_denies_transaction_before_any_item_send() -> None:
    module = _module()
    _docs, transaction, upload_plan = _transaction(module)
    connection = _Connection(_upload_messages(upload_plan, legacy_sequence=0))

    with pytest.raises(Px4MissionError, match="MISSION_REQUEST_INT"):
        module.upload_int_only_mission(connection, transaction, upload_plan)
    assert transaction.state is module.MissionTransactionState.DENIED
    assert [name for name, _args in connection.mav.calls] == ["MISSION_COUNT"]


def test_readback_coordinate_mismatch_denies_before_commit_or_permit() -> None:
    module = _module()
    _docs, transaction, upload_plan = _transaction(module)
    module.upload_int_only_mission(
        _Connection(_upload_messages(upload_plan)), transaction, upload_plan
    )

    with pytest.raises(Px4MissionError, match="protocol hash mismatch"):
        module.readback_int_only_mission(
            _Connection(_readback_messages(upload_plan, change_sequence=2)),
            transaction,
            upload_plan,
        )
    assert transaction.state is module.MissionTransactionState.DENIED
    with pytest.raises(Px4MissionError, match="does not permit"):
        transaction.commit()


def _container_record(module: ModuleType) -> dict[str, Any]:
    return {
        "Id": CONTAINER_ID,
        "Name": "/combined-sim-test",
        "Config": {
            "Image": module.PINNED_IMAGE,
            "Labels": {"multidetect.purpose": module.EXPECTED_CONTAINER_LABEL},
            "Env": [
                "PX4_SIM_MODEL=gz_standard_vtol_mono_cam",
                "PX4_SYS_AUTOSTART=4004",
            ],
            "Cmd": ["-c", "mavlink start -x -u 18570 -o 18774"],
        },
        "HostConfig": {
            "Privileged": False,
            "NetworkMode": "bridge",
            "Devices": [],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {"18570/udp": [{"HostIp": "127.0.0.1", "HostPort": "18774"}]},
        },
        "State": {"Running": True},
    }


def test_container_gate_requires_exact_id_label_and_loopback_without_14550() -> None:
    module = _module()
    record = _container_record(module)
    checks = module.validate_owned_container_record(
        record,
        container_name="combined-sim-test",
        expected_container_id=CONTAINER_ID,
        expected_label=module.EXPECTED_CONTAINER_LABEL,
    )
    assert all(checks.values())

    record["HostConfig"]["PortBindings"]["18570/udp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(module.VtolMissionAcceptanceError, match="loopback_only"):
        module.validate_owned_container_record(
            record,
            container_name="combined-sim-test",
            expected_container_id=CONTAINER_ID,
            expected_label=module.EXPECTED_CONTAINER_LABEL,
        )


def test_ready_json_is_atomic_refuses_stale_file_and_keeps_visual_boundary(
    tmp_path: Path,
) -> None:
    module = _module()
    ready = tmp_path / "mission-active.ready.json"
    payload = {
        "transaction_state": "start_permitted",
        "auto_mission_confirmed": True,
        "armed_after_permit": True,
        "mission_activity": {"message_type": "MISSION_CURRENT", "sequence": 0},
        "operator_visual_acceptance": "not_claimed",
    }
    module._atomic_write_json(ready, payload, overwrite=False)
    assert ready.is_file()
    assert not list(tmp_path.glob("*.tmp"))
    assert module.json.loads(ready.read_text(encoding="utf-8")) == payload
    with pytest.raises(module.VtolMissionAcceptanceError, match="stale ready"):
        module._atomic_write_json(ready, payload, overwrite=False)


def test_observer_writes_ready_only_after_armed_activity_and_proves_full_lifecycle(
    tmp_path: Path,
) -> None:
    module = _module()

    class FakeFlightCollector:
        def __init__(self) -> None:
            observed_at = module.time.monotonic()
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self.last_heartbeat_observed_at_monotonic_s = observed_at
            self.last_position_observed_at_monotonic_s = observed_at
            self.last_extended_state_observed_at_monotonic_s = observed_at
            self._steps = iter(
                [
                    (
                        {},
                        _Message("MISSION_CURRENT", seq=0),
                    ),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
                            "last_vtol_state": module.MAV_VTOL_STATE_FW,
                            "last_position": {
                                "latitude_deg": 1.30063,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 20.0,
                            },
                        },
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    (
                        {"last_vtol_state": module.MAV_VTOL_STATE_MC},
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    ({}, _Message("MISSION_CURRENT", seq=6)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND,
                            "last_heartbeat": {"base_mode": 0},
                        },
                        _Message("MISSION_ITEM_REACHED", seq=6),
                    ),
                ]
            )

        def pump(self, _timeout_s: float) -> _Message | None:
            try:
                changes, message = next(self._steps)
            except StopIteration:
                return None
            for name, value in changes.items():
                setattr(self, name, value)
            return message

    ready = tmp_path / "active.json"
    events: list[dict[str, Any]] = []
    result = module.observe_mission_flight(
        FakeFlightCollector(),
        item_count=7,
        home_latitude_deg=1.3,
        home_longitude_deg=103.8,
        mission_timeout_s=10.0,
        mission_active_ready_file=ready,
        container_id=CONTAINER_ID,
        transaction_state="start_permitted",
        control_command_events=events,
    )

    assert result["mission_current_sequences"] == [0, 6]
    assert result["mission_item_reached_sequences"] == [6]
    assert result["maximum_distance_from_home_m"] > 35.0
    assert result["maximum_relative_altitude_m"] == 20.0
    assert result["in_air_observed"] is True
    assert result["fixed_wing_observed"] is True
    assert result["multicopter_observed"] is True
    assert result["landed_observed"] is True
    assert result["terminal_disarmed"] is True
    assert events == []
    ready_payload = module.json.loads(ready.read_text(encoding="utf-8"))
    assert ready_payload["transaction_state"] == "start_permitted"
    assert ready_payload["auto_mission_confirmed"] is True
    assert ready_payload["auto_mission_active"] is True
    assert ready_payload["auto_mission_custom_mode"] == module.PX4_AUTO_MISSION_CUSTOM_MODE
    assert ready_payload["armed_after_permit"] is True
    assert ready_payload["in_air_observed"] is True
    assert ready_payload["in_air_at_ready"] is True
    assert ready_payload["mission_activity"]["message_type"] == "MISSION_CURRENT"
    assert ready_payload["mission_activity"]["sequence"] == 0
    assert ready_payload["mission_activity"]["at_s"] >= 0.0
    assert ready_payload["mission_progress"]["observed"] is True
    assert ready_payload["mission_progress"]["current_sequences"] == [0]
    assert ready_payload["operator_visual_acceptance"] == "not_claimed"


def test_observer_never_writes_active_ready_for_ground_arm_and_activity(
    tmp_path: Path,
) -> None:
    module = _module()

    class GroundOnlyCollector:
        def __init__(self) -> None:
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self._activity_sent = False

        def pump(self, _timeout_s: float) -> _Message | None:
            if self._activity_sent:
                return None
            self._activity_sent = True
            return _Message("MISSION_CURRENT", seq=0)

    ready = tmp_path / "ground-must-not-be-ready.json"
    with pytest.raises(module.VtolMissionAcceptanceError, match="did not prove progress"):
        module.observe_mission_flight(
            GroundOnlyCollector(),
            item_count=7,
            home_latitude_deg=1.3,
            home_longitude_deg=103.8,
            mission_timeout_s=0.01,
            mission_active_ready_file=ready,
            container_id=CONTAINER_ID,
            transaction_state="start_permitted",
            control_command_events=[],
        )
    assert not ready.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transaction_state", "committed"),
        ("mission_progress_observed", False),
        ("auto_mission_active", False),
        ("armed", False),
        ("in_air_at_ready", False),
        ("fixed_wing_observed", False),
        ("current_vtol_state", None),
        ("maximum_distance_m", 34.999),
        ("maximum_altitude_m", 9.999),
        ("telemetry_fresh", False),
    ],
)
def test_flight_capture_ready_gate_requires_every_strict_flight_fact(
    field: str,
    value: Any,
) -> None:
    module = _module()
    facts: dict[str, Any] = {
        "transaction_state": "start_permitted",
        "mission_progress_observed": True,
        "auto_mission_active": True,
        "armed": True,
        "in_air_at_ready": True,
        "fixed_wing_observed": True,
        "current_vtol_state": module.MAV_VTOL_STATE_FW,
        "maximum_distance_m": 35.0,
        "maximum_altitude_m": 10.0,
        "telemetry_fresh": True,
    }
    assert module._flight_capture_ready_gate(**facts) is True

    facts[field] = value
    assert module._flight_capture_ready_gate(**facts) is False


def test_flight_capture_ready_rejects_stale_or_aliased_target_before_flight(
    tmp_path: Path,
) -> None:
    module = _module()
    stale = tmp_path / "stale-flight-capture.json"
    stale.write_text('{"sentinel":"preserve"}\n', encoding="utf-8")

    with pytest.raises(module.VtolMissionAcceptanceError, match="stale flight-capture"):
        module._validate_ready_file_targets(None, stale)
    assert stale.read_text(encoding="utf-8") == '{"sentinel":"preserve"}\n'

    target = tmp_path / "same-target.json"
    with pytest.raises(module.VtolMissionAcceptanceError, match="must be distinct"):
        module._validate_ready_file_targets(target, target)
    assert not target.exists()

    complete = tmp_path / "stale-complete.json"
    complete.write_text('{"sentinel":"preserve"}\n', encoding="utf-8")
    with pytest.raises(module.VtolMissionAcceptanceError, match="stale flight-capture complete"):
        module._validate_ready_file_targets(None, tmp_path / "ready.json", complete)
    assert complete.read_text(encoding="utf-8") == '{"sentinel":"preserve"}\n'


@pytest.mark.parametrize(
    ("ready", "complete"),
    (
        ("ready.json", None),
        (None, "complete.json"),
    ),
)
def test_flight_capture_ready_and_complete_targets_are_required_as_a_pair(
    tmp_path: Path,
    ready: str | None,
    complete: str | None,
) -> None:
    module = _module()

    with pytest.raises(module.VtolMissionAcceptanceError, match="supplied together"):
        module._validate_ready_file_targets(
            None,
            tmp_path / ready if ready is not None else None,
            tmp_path / complete if complete is not None else None,
        )


def test_observer_writes_strict_flight_capture_ready_once_with_bound_facts(
    tmp_path: Path,
) -> None:
    module = _module()
    mission_active_ready = tmp_path / "mission-active.json"
    flight_capture_ready = tmp_path / "flight-capture.json"
    flight_capture_complete = tmp_path / "flight-capture-complete.json"

    class StrictFlightCollector:
        def __init__(self) -> None:
            observed_at = module.time.monotonic()
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self.last_heartbeat_observed_at_monotonic_s = observed_at
            self.last_position_observed_at_monotonic_s = observed_at
            self.last_extended_state_observed_at_monotonic_s = observed_at
            self.calls = 0
            self.flight_ready_bytes: bytes | None = None
            self._steps = iter(
                [
                    ({}, _Message("MISSION_CURRENT", seq=0)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
                            "last_vtol_state": module.MAV_VTOL_STATE_FW,
                            "last_position": {
                                "latitude_deg": 1.3002,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 10.0,
                            },
                        },
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    (
                        {
                            "last_position": {
                                "latitude_deg": 1.3004,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 10.0,
                            }
                        },
                        _Message("GLOBAL_POSITION_INT"),
                    ),
                    (
                        {
                            "last_position": {
                                "latitude_deg": 1.30063,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 20.0,
                            }
                        },
                        _Message("GLOBAL_POSITION_INT"),
                    ),
                    (
                        {"last_vtol_state": module.MAV_VTOL_STATE_MC},
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    ({}, _Message("MISSION_CURRENT", seq=6)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND,
                            "last_heartbeat": {"base_mode": 0},
                        },
                        _Message("MISSION_ITEM_REACHED", seq=6),
                    ),
                ]
            )

        def pump(self, _timeout_s: float) -> _Message | None:
            self.calls += 1
            if self.calls == 3:
                assert mission_active_ready.is_file()
                assert not flight_capture_ready.exists()
            if self.calls == 4:
                assert flight_capture_ready.is_file()
                self.flight_ready_bytes = flight_capture_ready.read_bytes()
                ready_payload = module.json.loads(flight_capture_ready.read_text(encoding="utf-8"))
                ready_monotonic_s = float(ready_payload["captured_at_monotonic_s"])
                capture_completed_monotonic_s = module.time.monotonic()
                module._atomic_write_json(
                    flight_capture_complete,
                    {
                        "schema": "multidetect.vtol-sensor-capture-complete",
                        "schema_version": 1,
                        "generated_at_utc": "2026-09-05T00:00:00Z",
                        "clock": "host_monotonic",
                        "capture_started_at_monotonic_s": ready_monotonic_s,
                        "capture_completed_at_monotonic_s": capture_completed_monotonic_s,
                        "container_id": CONTAINER_ID,
                        "transaction_id": "tx-v56-flight-capture",
                        "committed_protocol_sha256": HASH_A,
                        "scenario_id": "river_rescue_vtol_001",
                        "scenario_manifest_hash": HASH_B,
                        "frame_count": 3,
                        "sensor_binding_hash": HASH_B,
                        "frame_set_manifest_sha256": HASH_C,
                        "ground_truth_used_for_runtime_or_capture": False,
                    },
                    overwrite=False,
                )
            if self.calls > 4 and self.flight_ready_bytes is not None:
                assert flight_capture_ready.read_bytes() == self.flight_ready_bytes
            try:
                changes, message = next(self._steps)
            except StopIteration:
                return None
            for name, value in changes.items():
                setattr(self, name, value)
            return message

    collector = StrictFlightCollector()
    transaction_id = "tx-v56-flight-capture"
    result = module.observe_mission_flight(
        collector,
        item_count=7,
        home_latitude_deg=1.3,
        home_longitude_deg=103.8,
        mission_timeout_s=10.0,
        mission_active_ready_file=mission_active_ready,
        container_id=CONTAINER_ID,
        transaction_state="start_permitted",
        control_command_events=[],
        flight_capture_ready_file=flight_capture_ready,
        flight_capture_complete_file=flight_capture_complete,
        transaction_id=transaction_id,
        committed_protocol_sha256=HASH_A,
        scenario_id="river_rescue_vtol_001",
        scenario_manifest_hash=HASH_B,
    )

    assert result["mission_active_ready_written"] is True
    assert result["flight_capture_ready_written"] is True
    assert result["flight_capture_complete_observed"] is True
    assert result["flight_capture_complete_file"] == str(flight_capture_complete)
    assert (
        result["flight_capture_complete_file_sha256"]
        == hashlib.sha256(flight_capture_complete.read_bytes()).hexdigest()
    )
    assert all(result["flight_capture_complete_checks"].values())
    assert result["flight_capture_interval_sample_count"] > 0
    assert result["flight_capture_interval_violation"] is False
    assert result["flight_capture_interval_qualified"] is True
    assert all(
        sample["maximum_state_age_s"] == module.MAXIMUM_FLIGHT_STATE_AGE_S
        and 0.0 <= sample["heartbeat_age_s"] <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        and 0.0 <= sample["position_age_s"] <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        and 0.0 <= sample["extended_system_state_age_s"] <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        and sample["telemetry_fresh"] is True
        and sample["valid"] is True
        for sample in result["flight_capture_interval_samples"]
    )
    assert collector.flight_ready_bytes == flight_capture_ready.read_bytes()
    payload = module.json.loads(flight_capture_ready.read_text(encoding="utf-8"))
    assert payload["schema"] == "multidetect.px4-vtol-flight-capture-ready"
    assert payload["container"] == {"exact_id_bound": True, "id": CONTAINER_ID}
    assert payload["transaction"] == {
        "committed_protocol_sha256": HASH_A,
        "id": transaction_id,
        "start_permitted": True,
        "state": "start_permitted",
    }
    assert payload["flight"]["auto_mission_active"] is True
    assert payload["flight"]["auto_mission_custom_mode"] == (module.PX4_AUTO_MISSION_CUSTOM_MODE)
    assert payload["flight"]["armed"] is True
    assert payload["flight"]["in_air_at_ready"] is True
    assert payload["flight"]["fixed_wing_observed"] is True
    assert payload["flight"]["maximum_distance_from_home_m"] >= 35.0
    assert payload["flight"]["maximum_relative_altitude_m"] >= 10.0
    assert payload["flight"]["mission_progress"]["observed"] is True
    assert payload["flight"]["mission_progress"]["current_sequences"] == [0]
    assert payload["flight"]["mission_progress"]["first_activity"]["at_s"] >= 0.0
    assert "captured_at_s" not in payload["flight"]["mission_progress"]["first_activity"]
    assert payload["flight"]["telemetry_fresh"] is True
    assert payload["flight"]["maximum_telemetry_age_s"] == module.MAXIMUM_FLIGHT_STATE_AGE_S
    assert all(
        0.0 <= age <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        for age in payload["flight"]["telemetry_ages_s"].values()
    )
    assert (
        payload["flight"]["maximum_distance_from_home_m"] < (result["maximum_distance_from_home_m"])
    )
    assert payload["operator_visual_acceptance"] == "not_claimed"


def test_observer_rejects_valid_complete_marker_after_frozen_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    flight_capture_ready = tmp_path / "flight-capture-ready.json"
    flight_capture_complete = tmp_path / "flight-capture-complete.json"

    class FakeClock:
        def __init__(self) -> None:
            self.value = 100.0

        def monotonic(self) -> float:
            return self.value

    clock = FakeClock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)

    class FrozenCollector:
        def __init__(self) -> None:
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self.last_heartbeat_observed_at_monotonic_s = clock.value
            self.last_position_observed_at_monotonic_s = clock.value
            self.last_extended_state_observed_at_monotonic_s = clock.value
            self.calls = 0

        def pump(self, _timeout_s: float) -> _Message | None:
            self.calls += 1
            if self.calls == 1:
                return _Message("MISSION_CURRENT", seq=0)
            if self.calls == 2:
                self.last_position = {
                    "latitude_deg": 1.30063,
                    "longitude_deg": 103.8,
                    "relative_altitude_m": 20.0,
                }
                self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR
                self.last_vtol_state = module.MAV_VTOL_STATE_FW
                self.last_extended_state_observed_at_monotonic_s = clock.value
                return _Message("EXTENDED_SYS_STATE")
            if self.calls == 3:
                ready_payload = module.json.loads(flight_capture_ready.read_text(encoding="utf-8"))
                ready_time = float(ready_payload["captured_at_monotonic_s"])
                module._atomic_write_json(
                    flight_capture_complete,
                    {
                        "schema": "multidetect.vtol-sensor-capture-complete",
                        "schema_version": 1,
                        "generated_at_utc": "2026-09-05T00:00:00Z",
                        "clock": "host_monotonic",
                        "capture_started_at_monotonic_s": ready_time,
                        "capture_completed_at_monotonic_s": ready_time + 1.0,
                        "container_id": CONTAINER_ID,
                        "transaction_id": "tx-v56-frozen-telemetry",
                        "committed_protocol_sha256": HASH_A,
                        "scenario_id": "river_rescue_vtol_001",
                        "scenario_manifest_hash": HASH_B,
                        "frame_count": 3,
                        "sensor_binding_hash": HASH_B,
                        "frame_set_manifest_sha256": HASH_C,
                        "ground_truth_used_for_runtime_or_capture": False,
                    },
                    overwrite=False,
                )
                clock.value = ready_time + module.MAXIMUM_FLIGHT_STATE_AGE_S + 0.1
                return None
            if self.calls == 4:
                self.last_position_observed_at_monotonic_s = clock.value
                return _Message("GLOBAL_POSITION_INT")
            if self.calls == 5:
                self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
                self.last_vtol_state = module.MAV_VTOL_STATE_MC
                self.last_extended_state_observed_at_monotonic_s = clock.value
                return _Message("EXTENDED_SYS_STATE")
            if self.calls == 6:
                self.last_heartbeat = {"base_mode": 0, "custom_mode": 0}
                self.last_heartbeat_observed_at_monotonic_s = clock.value
                return _Message("HEARTBEAT")
            if self.calls == 7:
                return _Message("MISSION_CURRENT", seq=6)
            if self.calls == 8:
                return _Message("MISSION_ITEM_REACHED", seq=6)
            clock.value += 10.0
            return None

    with pytest.raises(
        module.VtolMissionAcceptanceError,
        match="valid flight-capture completion marker.*left current FW/armed/in-air/AUTO.MISSION",
    ):
        module.observe_mission_flight(
            FrozenCollector(),
            item_count=7,
            home_latitude_deg=1.3,
            home_longitude_deg=103.8,
            mission_timeout_s=10.0,
            mission_active_ready_file=None,
            container_id=CONTAINER_ID,
            transaction_state="start_permitted",
            control_command_events=[],
            flight_capture_ready_file=flight_capture_ready,
            flight_capture_complete_file=flight_capture_complete,
            transaction_id="tx-v56-frozen-telemetry",
            committed_protocol_sha256=HASH_A,
            scenario_id="river_rescue_vtol_001",
            scenario_manifest_hash=HASH_B,
        )
    assert flight_capture_ready.is_file()
    assert flight_capture_complete.is_file()


def test_observer_fails_closed_when_requested_flight_capture_ready_never_occurs(
    tmp_path: Path,
) -> None:
    module = _module()

    class NeverExactAutoCollector:
        def __init__(self) -> None:
            observed_at = module.time.monotonic()
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE + 1,
            }
            self.last_heartbeat_observed_at_monotonic_s = observed_at
            self.last_position_observed_at_monotonic_s = observed_at
            self.last_extended_state_observed_at_monotonic_s = observed_at
            self._steps = iter(
                [
                    ({}, _Message("MISSION_CURRENT", seq=0)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
                            "last_vtol_state": module.MAV_VTOL_STATE_FW,
                            "last_position": {
                                "latitude_deg": 1.30063,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 20.0,
                            },
                        },
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    (
                        {"last_vtol_state": module.MAV_VTOL_STATE_MC},
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    ({}, _Message("MISSION_CURRENT", seq=6)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND,
                            "last_heartbeat": {"base_mode": 0},
                        },
                        _Message("MISSION_ITEM_REACHED", seq=6),
                    ),
                ]
            )

        def pump(self, _timeout_s: float) -> _Message | None:
            try:
                changes, message = next(self._steps)
            except StopIteration:
                return None
            for name, value in changes.items():
                setattr(self, name, value)
            return message

    ready = tmp_path / "must-not-exist.json"
    complete = tmp_path / "complete-must-not-exist.json"
    with pytest.raises(
        module.VtolMissionAcceptanceError,
        match="without the flight-capture ready barrier",
    ):
        module.observe_mission_flight(
            NeverExactAutoCollector(),
            item_count=7,
            home_latitude_deg=1.3,
            home_longitude_deg=103.8,
            mission_timeout_s=10.0,
            mission_active_ready_file=None,
            container_id=CONTAINER_ID,
            transaction_state="start_permitted",
            control_command_events=[],
            flight_capture_ready_file=ready,
            flight_capture_complete_file=complete,
            transaction_id="tx-v56-never-ready",
            committed_protocol_sha256=HASH_A,
            scenario_id="river_rescue_vtol_001",
            scenario_manifest_hash=HASH_B,
        )
    assert not ready.exists()
    assert not complete.exists()


def test_cli_forwards_paired_flight_capture_ready_and_complete_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    observed: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return {"gate_passed": True}

    monkeypatch.setattr(module, "_resolve_docker_cli", lambda value: f"resolved:{value}")
    monkeypatch.setattr(module, "run_mission_acceptance", fake_run)
    ready = tmp_path / "ready.json"
    complete = tmp_path / "complete.json"
    code = module.main(
        [
            "--scenario-manifest",
            str(MANIFEST),
            "--evidence",
            str(tmp_path / "mission-evidence.json"),
            "--container-name",
            "multidetect-vtol-cli-test",
            "--expected-container-id",
            CONTAINER_ID,
            "--evaluator-port",
            "51005",
            "--airframe-profile-sha256",
            HASH_A,
            "--runtime-descriptor-sha256",
            HASH_B,
            "--runtime-provenance-sha256",
            HASH_C,
            "--flight-capture-ready-file",
            str(ready),
            "--flight-capture-complete-file",
            str(complete),
            "--docker",
            "docker-test",
            "--acknowledge-owned-disposable-sitl",
        ]
    )

    assert code == 0
    assert observed["kwargs"]["docker"] == "resolved:docker-test"
    assert observed["kwargs"]["flight_capture_ready_file"] == ready.resolve()
    assert observed["kwargs"]["flight_capture_complete_file"] == complete.resolve()
    assert observed["kwargs"]["acknowledge_owned_disposable_sitl"] is True


def test_source_has_no_container_lifecycle_or_evaluator_truth_loader() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    lower = text.lower()

    assert "from multidetect.simulation import load_runtime_scenario" in text
    assert "load_scenario_manifest" not in text
    assert '"created_by_this_helper": False' in text
    assert '"stopped_by_this_helper": False' in text
    assert '"operator_visual_acceptance": "not_claimed"' in text
    assert '"real_pixhawk_contacted": False' in text
    assert "docker run" not in lower
    assert "docker stop" not in lower
    assert "docker rm" not in lower
    assert "MISSION_REQUEST_INT" in text
    assert "MISSION_ITEM_INT" in text
    assert "permit_start" in text
    assert "mission_journal.resume_plan().to_mapping()" in text
    assert "PX4_AUTO_MISSION_CUSTOM_MODE" in text
    assert "--flight-capture-ready-file" in text
    assert "--flight-capture-complete-file" in text
    assert "--acknowledge-owned-disposable-sitl" in text
    assert "_container_inspect(docker, expected_container_id)" in text
    assert '"exec",\n                    expected_container_id,' in text
    assert '"id": (documents.operator_grant.transaction_id' in text
