"""Small source-bound CPU fixtures; no live vehicle/context or flight acceptance."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import canonical_json
from multidetect.simulator.stack import airframe_profile_hash
from scripts import onboard_teacher_observed_context as observed
from scripts.onboard_teacher_telemetry_capture import TeacherTelemetryCapture


class Message:
    def __init__(self, kind, fields):
        self.kind, self.fields = kind, fields
        self.packet = b"CPU-packet-fixture-" + kind.encode()

    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return 1

    def get_srcComponent(self):
        return 1

    def get_seq(self):
        return 17

    def get_msgbuf(self):
        return self.packet

    def to_dict(self):
        return {"mavpackettype": self.kind, **self.fields}


def write_source(root, name, data, pointer):
    raw = (canonical_json(data) + "\n").encode()
    path = root / name
    path.write_bytes(raw)
    return EvidenceBinding(
        name, len(raw), hashlib.sha256(raw).hexdigest(), "CPU-fixture-source", pointer
    )


@pytest.fixture
def fixture(tmp_path):
    bits = observed.REQUIRED_SENSOR_BITS
    fields = {
        "HEARTBEAT": {"type": 2, "autopilot": 12, "base_mode": 81, "system_status": 3},
        "EXTENDED_SYS_STATE": {"landed_state": 1, "vtol_state": 0},
        "HOME_POSITION": {
            "latitude": 13000000,
            "longitude": 1038000000,
            "altitude": 18000,
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "q": [1, 0, 0, 0],
            "time_usec": 12345000,
        },
        "GPS_RAW_INT": {
            "time_usec": 2**63 + 123,
            "fix_type": 3,
            "satellites_visible": 12,
            "lat": 13000000,
            "lon": 1038000000,
            "alt": 18000,
            "h_acc": 1000,
            "eph": 120,
        },
        "GLOBAL_POSITION_INT": {
            "time_boot_ms": 12345,
            "lat": 13000000,
            "lon": 1038000000,
            "alt": 18000,
            "relative_alt": 0,
            "vx": 0,
            "vy": 0,
            "vz": 0,
        },
        "LOCAL_POSITION_NED": {
            "time_boot_ms": 12345,
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "vx": 0.0,
            "vy": 0.0,
            "vz": 0.0,
        },
        "BATTERY_STATUS": {
            "id": 0,
            "battery_function": 1,
            "charge_state": 1,
            "fault_bitmask": 0,
            "battery_remaining": 80,
            "time_remaining": 1200,
            "current_battery": 0,
            "voltages": [16000] + [65535] * 9,
        },
        "SYS_STATUS": {
            "onboard_control_sensors_present": bits,
            "onboard_control_sensors_enabled": bits,
            "onboard_control_sensors_health": bits,
            "errors_comm": 0,
            "errors_count1": 0,
            "errors_count2": 0,
            "errors_count3": 0,
            "errors_count4": 0,
            "battery_remaining": 80,
        },
        "VFR_HUD": {"groundspeed": 0.0, "airspeed": 0.0},
    }
    queue = [Message(k, fields[k]) for k in observed.REQUIRED_MESSAGES]
    clock_time = [10.0]

    def clock():
        clock_time[0] += 0.001
        return clock_time[0]

    capture = TeacherTelemetryCapture(
        tmp_path / "raw",
        reader=lambda _: queue.pop(0),
        clock=clock,
        clock_id="same-observer-QPC",
        transaction_id="fixture-parent",
        local_origin_id="declared-PX4-origin",
    )
    rows = {kind: capture.receive(1)[1] for kind in observed.REQUIRED_MESSAGES}
    capture.seal()
    profile = {
        "schema_version": 1,
        "airframe_profile_id": "px4_multirotor_gazebo_v1",
        "vehicle_model": "px4_multirotor_standard",
        "model_name": "x500_mono_cam",
        "stack_plan_sha256": "a" * 64,
    }
    resources = {
        "clock_id": capture.clock_id,
        "transaction_id": capture.transaction_id,
        "observed_at_s": 10.15,
        "airframe_profile_hash": airframe_profile_hash(profile),
        "geofence_valid": True,
        "camera_available": True,
        "perception_available": True,
        "map_cache_available": True,
        "dem_available": True,
        "wind_north_mps": 1.0,
        "wind_east_mps": -0.4,
        "wind_source": "software-SIM-configured",
    }
    return {
        "rows": rows,
        "artifact_root": tmp_path,
        "now_s": 10.2,
        "captured_at_utc": "2026-10-08T02:00:10.200000+00:00",
        "clock_id": capture.clock_id,
        "transaction_id": capture.transaction_id,
        "local_origin_id": capture.local_origin_id,
        "profile_source": write_source(tmp_path, "profile.json", {"profile": profile}, "/profile"),
        "profile_id": profile["airframe_profile_id"],
        "resource_source": write_source(
            tmp_path, "resources.json", {"resources": resources}, "/resources"
        ),
    }


def rewrite_row(fixture, kind, field, value, *, envelope_field=False):
    row = fixture["rows"][kind]
    if envelope_field:
        row[field] = value
    else:
        row["message"][field] = value
    saved = {k: v for k, v in row.items() if k not in {"dispatch_ready_at_s", "envelope", "packet"}}
    raw = (canonical_json(saved) + "\n").encode()
    Path(row["envelope"]["path"]).write_bytes(raw)
    row["envelope"]["bytes"] = len(raw)
    row["envelope"]["sha256"] = hashlib.sha256(raw).hexdigest()


def rewrite_source(fixture, kind, field, value):
    source = fixture[kind + "_source"]
    path = fixture["artifact_root"] / source.path
    data = json.loads(path.read_bytes())
    data[kind if kind == "profile" else "resources"][field] = value
    fixture[kind + "_source"] = write_source(
        fixture["artifact_root"], source.path, data, source.json_pointer
    )


def test_positive_qualified_context_original_units_sources_and_oldest_time(fixture):
    result = observed.build_observed_teacher_context(**fixture)
    context = result.context
    oldest = min(r["received_at_s"] for r in fixture["rows"].values())
    assert context.captured_at_s == oldest and context.captured_at_s != fixture["now_s"]
    expected_utc = datetime.fromisoformat(fixture["captured_at_utc"]) - timedelta(
        seconds=10.2 - oldest
    )
    assert datetime.fromisoformat(context.captured_at_utc) == expected_utc
    assert context.aircraft_position.latitude_deg == context.home_position.latitude_deg == 1.3
    assert context.aircraft_position.longitude_deg == 103.8
    assert context.aircraft_position.altitude_m == 0
    assert context.aircraft_position.altitude_reference == "relative_home"
    assert context.gps_horizontal_accuracy_m == 1.0
    assert context.battery_remaining_pct == 80
    battery_age = 10.2 - fixture["rows"]["BATTERY_STATUS"]["received_at_s"]
    assert context.estimated_flight_time_s == 1200 - battery_age
    assert context.wind_north_mps == 1 and context.wind_east_mps == -0.4
    assert len(result.source_references) == 20
    assert result.qualification_details["qualified_plain_MC_disarmed_on_ground"]
    assert result.qualification_details["endurance_reserve_already_deducted"] is False
    assert not any(result.to_mapping()["claims"].values())
    assert fixture["rows"]["GPS_RAW_INT"]["message"]["time_usec"] == 2**63 + 123
    assert (
        context.airframe_profile_hash
        == "5bd121275d285e27adef2a8f4b4242b481dffb8368ec4dd231089eeac8732d47"
    )


def test_false_resources_are_preserved_not_defaulted_or_promoted(fixture):
    rewrite_source(fixture, "resource", "geofence_valid", False)
    rewrite_source(fixture, "resource", "perception_available", False)
    result = observed.build_observed_teacher_context(**fixture)
    assert result.context.geofence_valid is False and result.context.perception_available is False
    assert result.context.camera_available is True
    assert not result.to_mapping()["claims"]["mission_approved"]


@pytest.mark.parametrize("kind", observed.REQUIRED_MESSAGES)
def test_no_missing_message_defaults(fixture, kind):
    fixture["rows"].pop(kind)
    with pytest.raises(ValueError, match="exact required"):
        observed.build_observed_teacher_context(**fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("system_id", 2),
        ("system_id", True),
        ("component_id", 2),
        ("source_accepted", False),
        ("transaction_id", "other"),
        ("clock_id", "other"),
        ("local_origin_id", "other"),
        ("received_at_s", 8.0),
        ("received_at_s", 10.3),
        ("dispatch_ready_at_s", 10.3),
    ],
)
def test_source_clock_namespace_and_freshness_gates(fixture, field, value):
    if field == "dispatch_ready_at_s":
        fixture["rows"]["HEARTBEAT"][field] = value
    else:
        rewrite_row(fixture, "HEARTBEAT", field, value, envelope_field=True)
    with pytest.raises(ValueError):
        observed.build_observed_teacher_context(**fixture)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("HEARTBEAT", "type", 21),
        ("HEARTBEAT", "autopilot", 3),
        ("HEARTBEAT", "base_mode", 209),
        ("HEARTBEAT", "system_status", 4),
        ("EXTENDED_SYS_STATE", "landed_state", 2),
        ("EXTENDED_SYS_STATE", "vtol_state", 3),
        ("GPS_RAW_INT", "fix_type", 2),
        ("GPS_RAW_INT", "fix_type", True),
        ("GPS_RAW_INT", "satellites_visible", 255),
        ("GPS_RAW_INT", "h_acc", 0),
        ("GPS_RAW_INT", "h_acc", 3000),
        ("GPS_RAW_INT", "h_acc", 2**32 - 1),
        ("GPS_RAW_INT", "lon", 1038010000),
        ("GLOBAL_POSITION_INT", "relative_alt", 1000),
        ("GLOBAL_POSITION_INT", "alt", 20000),
        ("GPS_RAW_INT", "alt", 22000),
        ("LOCAL_POSITION_NED", "time_boot_ms", 13000),
        ("LOCAL_POSITION_NED", "x", 3.0),
        ("LOCAL_POSITION_NED", "z", 2.0),
        ("LOCAL_POSITION_NED", "vx", 0.31),
        ("GLOBAL_POSITION_INT", "vz", 31),
        ("VFR_HUD", "groundspeed", -0.01),
        ("VFR_HUD", "airspeed", -0.01),
        ("BATTERY_STATUS", "time_remaining", 0),
        ("BATTERY_STATUS", "time_remaining", -1),
        ("BATTERY_STATUS", "time_remaining", 604801),
        ("BATTERY_STATUS", "battery_remaining", -1),
        ("BATTERY_STATUS", "charge_state", 0),
        ("BATTERY_STATUS", "fault_bitmask", 1),
        ("BATTERY_STATUS", "battery_function", 0),
        ("BATTERY_STATUS", "id", 1),
        ("BATTERY_STATUS", "current_battery", -1),
        ("BATTERY_STATUS", "voltages", [65535] * 10),
        ("BATTERY_STATUS", "voltages", [16000]),
        ("SYS_STATUS", "battery_remaining", 60),
        ("SYS_STATUS", "errors_comm", 1),
        ("SYS_STATUS", "onboard_control_sensors_health", 3),
    ],
)
def test_actual_telemetry_qualifications_fail_closed(fixture, kind, field, value):
    rewrite_row(fixture, kind, field, value)
    with pytest.raises(ValueError):
        observed.build_observed_teacher_context(**fixture)


def test_eph_is_not_metric_accuracy_fallback(fixture):
    row = fixture["rows"]["GPS_RAW_INT"]
    row["message"].pop("h_acc")
    rewrite_row(fixture, "GPS_RAW_INT", "eph", 100)
    with pytest.raises(ValueError, match="h_acc"):
        observed.build_observed_teacher_context(**fixture)


def test_telemetry_skew_is_not_refreshed(fixture):
    rewrite_row(fixture, "HEARTBEAT", "received_at_s", 8.5, envelope_field=True)
    with pytest.raises(ValueError, match="skew"):
        observed.build_observed_teacher_context(**fixture)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("profile", "schema_version", True),
        ("profile", "airframe_profile_id", "other"),
        ("profile", "vehicle_model", "px4_vtol_standard"),
        ("profile", "model_name", "x500"),
        ("profile", "stack_plan_sha256", "bad"),
        ("resource", "clock_id", "other"),
        ("resource", "transaction_id", "other"),
        ("resource", "observed_at_s", 8.0),
        ("resource", "observed_at_s", 10.3),
        ("resource", "airframe_profile_hash", "c" * 64),
        ("resource", "wind_source", "default-zero"),
        ("resource", "geofence_valid", 1),
    ],
)
def test_profile_and_resource_projection_gates(fixture, kind, field, value):
    rewrite_source(fixture, kind, field, value)
    with pytest.raises(ValueError):
        observed.build_observed_teacher_context(**fixture)


@pytest.mark.parametrize(
    "failure",
    [
        "packet",
        "envelope",
        "claims",
        "projection",
        "missingfile",
        "escape",
        "pointer",
        "extra",
        "boot_epoch",
        "duplicate_record",
        "unready",
    ],
)
def test_bound_original_sources_no_caller_mutation_or_unverified_projection(fixture, failure):
    row = fixture["rows"]["HEARTBEAT"]
    if failure == "packet":
        Path(row["packet"]["path"]).write_bytes(b"bad")
    elif failure == "envelope":
        Path(row["envelope"]["path"]).write_bytes(b"bad")
    elif failure == "claims":
        row["claims"]["telemetry_qualified_for_flight"] = True
    elif failure == "projection":
        row["message"]["base_mode"] = 0
    elif failure == "missingfile":
        row["packet"]["path"] = str(fixture["artifact_root"] / "absent.bin")
    elif failure == "escape":
        row["packet"]["path"] = str(fixture["artifact_root"].parent / "outside.bin")
    elif failure == "pointer":
        source = fixture["profile_source"]
        fixture["profile_source"] = EvidenceBinding(
            source.path, source.bytes, source.sha256, source.source_id, "/missing"
        )
    elif failure == "extra":
        fixture["rows"]["ATTITUDE_QUATERNION"] = row
    elif failure == "boot_epoch":
        rewrite_row(fixture, "LOCAL_POSITION_NED", "time_boot_ms", 2**63)
    elif failure == "duplicate_record":
        rewrite_row(fixture, "HEARTBEAT", "record_index", 1, envelope_field=True)
    else:
        row["dispatch_ready_at_s"] = row["received_at_s"] - 0.1
    with pytest.raises((ValueError, FileNotFoundError)):
        observed.build_observed_teacher_context(**fixture)


def test_source_drift_after_derivation_prevents_return(fixture, monkeypatch):
    original = observed._source_bytes
    counts = {}

    def mutate_on_second_read(root, source, cap, observations):
        counts[source.path] = counts.get(source.path, 0) + 1
        if source.source_id == "teacher-raw-HEARTBEAT-packet" and counts[source.path] == 2:
            path = root / source.path
            before = path.read_bytes()
            path.write_bytes(b"X" + before[1:])
        return original(root, source, cap, observations)

    monkeypatch.setattr(observed, "_source_bytes", mutate_on_second_read)
    with pytest.raises(ValueError, match="identity/bytes/SHA"):
        observed.build_observed_teacher_context(**fixture)


def test_no_endurance_override_and_no_mutable_result_alias(fixture):
    with pytest.raises(TypeError):
        observed.build_observed_teacher_context(**fixture, estimated_flight_time_s=120)
    result = observed.build_observed_teacher_context(**fixture)
    identity = result.canonical_hash
    result.qualification_details["endurance_source"] = "fake-timeout"
    result.to_mapping()["claims"]["mission_approved"] = True
    assert result.canonical_hash == identity
    assert not hasattr(result, "permit") and not hasattr(result, "operator_grant")


@pytest.mark.parametrize(
    "key,value",
    [
        ("now_s", True),
        ("now_s", float("nan")),
        ("maximum_age_s", True),
        ("maximum_age_s", 3),
        ("captured_at_utc", "2026-10-08T00:00:00"),
    ],
)
def test_clock_and_bounded_freshness_input_types(fixture, key, value):
    fixture[key] = value
    with pytest.raises(ValueError):
        observed.build_observed_teacher_context(**fixture)


def test_nonfinite_source_document_is_rejected_without_repair(fixture):
    row = fixture["rows"]["LOCAL_POSITION_NED"]
    saved = {k: v for k, v in row.items() if k not in {"dispatch_ready_at_s", "envelope", "packet"}}
    saved["message"]["x"] = float("nan")
    raw = json.dumps(saved).encode()
    Path(row["envelope"]["path"]).write_bytes(raw)
    row["envelope"]["bytes"] = len(raw)
    row["envelope"]["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="non-finite"):
        observed.build_observed_teacher_context(**fixture)


def test_profile_per_run_wiring_retained_in_source_not_stable_aircraft_hash(fixture):
    result = observed.build_observed_teacher_context(**fixture)
    rewrite_source(fixture, "profile", "stack_plan_sha256", "b" * 64)
    later = observed.build_observed_teacher_context(**fixture)
    assert later.context.airframe_profile_hash == result.context.airframe_profile_hash
    assert later.source_references[-2].sha256 != result.source_references[-2].sha256


def test_source_bound_result_does_not_write_or_change_any_source(fixture):
    paths = [
        Path(row[role]["path"])
        for row in fixture["rows"].values()
        for role in ("envelope", "packet")
    ]
    before = {p: p.read_bytes() for p in paths}
    keys = set(fixture["artifact_root"].rglob("*"))
    original_rows = copy.deepcopy(fixture["rows"])
    observed.build_observed_teacher_context(**fixture)
    assert fixture["rows"] == original_rows
    assert set(fixture["artifact_root"].rglob("*")) == keys
    assert {p: p.read_bytes() for p in paths} == before
