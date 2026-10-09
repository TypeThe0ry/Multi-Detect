"""Actual raw-capture/context code with CPU message/session stand-ins, not SIM."""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.serialization import strict_json_loads
from scripts import run_onboard_teacher_gazebo_observation as runner
from scripts.onboard_teacher_observed_context import REQUIRED_SENSOR_BITS


def message_fields():
    """Real common-message field names/units; synthetic CPU values only."""
    return {
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
            "onboard_control_sensors_present": REQUIRED_SENSOR_BITS,
            "onboard_control_sensors_enabled": REQUIRED_SENSOR_BITS,
            "onboard_control_sensors_health": REQUIRED_SENSOR_BITS,
            "errors_comm": 0,
            "errors_count1": 0,
            "errors_count2": 0,
            "errors_count3": 0,
            "errors_count4": 0,
            "battery_remaining": 80,
        },
        "VFR_HUD": {"groundspeed": 0.0, "airspeed": 0.0},
    }


class Clock:
    session_id = "context-integration-CPU-QPC-stand-in"

    def __init__(self):
        self.value = 10.0

    def monotonic(self):
        self.value += 0.001
        return self.value

    def utc_now(self):
        return datetime(2026, 10, 8, tzinfo=timezone.utc) + timedelta(seconds=self.value)

    def identity(self):
        return {"session_id": self.session_id, "source": "CPU-stand-in-not-real-QPC"}


class Message:
    def __init__(self, kind, fields, sequence):
        self.kind, self.fields, self.sequence = kind, fields, sequence

    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return 1

    def get_srcComponent(self):
        return 1

    def get_seq(self):
        return self.sequence % 256

    def get_msgbuf(self):
        return f"CPU-context-integration-packet:{self.sequence}:{self.kind}".encode()

    def to_dict(self):
        return {"mavpackettype": self.kind, **self.fields}


class CameraObservation:
    rgb_bgr24 = b"\x01\x02\x03"

    def __init__(self, clock):
        self.depth = SimpleNamespace(payload=b"\x00\x00\x80\x3f")
        self.host_return_stamp = SimpleNamespace(value_ns=int(clock.monotonic() * 1e9))

    def to_mapping(self):
        return {
            "rgb_bgr24_sha256": hashlib.sha256(self.rgb_bgr24).hexdigest(),
            "depth": {"payload_sha256": hashlib.sha256(self.depth.payload).hexdigest()},
            "CPU_fixture": True,
        }


@pytest.fixture
def integration(tmp_path, monkeypatch):
    source = tmp_path / "fixture.scenario.json"
    source.write_text("{}", encoding="utf-8")
    source.with_name("fixture.runtime.json").write_text(
        json.dumps({"world": {"wind_north_mps": 1.0, "wind_east_mps": -0.4}}),
        encoding="utf-8",
    )
    assets = SimpleNamespace(
        map=SimpleNamespace(path=source),
        dem=SimpleNamespace(path=source),
    )
    plan = SimpleNamespace(
        source_manifest_path=source,
        frame_count=2,
        docker_plan=SimpleNamespace(
            airframe_profile_id="px4_multirotor_gazebo_v1",
            vehicle_model="px4_multirotor_standard",
            model_name="x500_mono_cam",
            stack_plan_sha256="a" * 64,
            assets=assets,
        ),
        to_mapping=lambda: {"CPU_fixture": True, "flight_authority": False},
    )
    fields = message_fields()
    ids = {
        f"MAVLINK_MSG_ID_{kind}": index
        for index, kind in enumerate((*runner.REQUIRED_MESSAGES, *runner.EXTRA_MESSAGES))
    }
    monkeypatch.setitem(
        sys.modules,
        "pymavlink",
        SimpleNamespace(mavutil=SimpleNamespace(mavlink=SimpleNamespace(**ids))),
    )
    sessions = []

    class Session:
        def __init__(self, selected_plan, *, execute):
            assert selected_plan is plan and execute is True
            self.clock, self.closed, self.sent = Clock(), False, []
            self.connection = SimpleNamespace(
                mav=SimpleNamespace(command_long_send=lambda *args: self.sent.append(args))
            )
            self.camera = SimpleNamespace(read=lambda: CameraObservation(self.clock))
            self.kinds = itertools.cycle(runner.REQUIRED_MESSAGES)
            self.sequence = 0
            sessions.append(self)

        def raw_receive(self, timeout):
            self.clock.value += min(timeout, 0.01)
            kind = next(self.kinds)
            message = Message(kind, fields[kind], self.sequence)
            self.sequence += 1
            return message

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True
            return False

        def attest_qualified(self, *, telemetry_qualified):
            assert telemetry_qualified is True
            return SimpleNamespace(to_mapping=lambda: {"CPU_fixture": True, "not_authority": True})

        def to_mapping(self):
            return {"CPU_fixture": True, "closed": self.closed}

    return plan, fields, Session, sessions


def execute_fixture(tmp_path, integration):
    plan, _, factory, _ = integration
    return runner.observe_teacher(
        plan,
        tmp_path / "observation",
        execute=True,
        context_timeout_s=1,
        session_factory=factory,
        artifact_root=tmp_path,
    )  # default REAL context builder and REAL raw capture, injected session only


def read_context(report):
    source = report["context"]
    raw = Path(source["path"]).read_bytes()
    assert len(raw) == source["bytes"] and hashlib.sha256(raw).hexdigest() == source["sha256"]
    return strict_json_loads(raw)


def test_actual_raw_to_actual_context_positive_CPU_only(tmp_path, integration):
    report = execute_fixture(tmp_path, integration)
    assert report["status"] == "observed-context-qualified", report
    assert report["context_qualified"] is True and report["external_SIM_observed_here"] is False
    assert report["injected_dependencies_used"] is True
    context = read_context(report)
    assert len(context["source_references"]) == 20
    assert context["qualification_details"]["source_file_bytes_SHA_verified"] is True
    assert context["context"]["estimated_flight_time_s"] > 1198
    assert context["context"]["gps_horizontal_accuracy_m"] == 1
    assert all(value is False for value in context["claims"].values())
    assert all(value is False for value in report["claims"].values())
    assert report["raw_telemetry"]["status"] == "recorded"
    assert report["session"]["closed"] is True and integration[3][0].closed is True


def test_actual_context_unknown_remaining_time_refuses_and_preserves_raw(tmp_path, integration):
    integration[1]["BATTERY_STATUS"]["time_remaining"] = 0
    report = execute_fixture(tmp_path, integration)
    assert report["status"] == "context-refused", report
    assert report["context_qualified"] is False and "context" not in report
    assert "time_remaining" in report["context_error"] and "no override" in report["context_error"]
    assert report["raw_telemetry"]["status"] == "recorded"
    assert report["raw_telemetry"]["accepted_source_count"] >= 9
    assert report["diagnostic_rows"]["BATTERY_STATUS"]["message"]["time_remaining"] == 0
    assert not list((tmp_path / "observation").glob("observed-context*.json"))
    assert report["session"]["closed"] is True
    assert all(value is False for value in report["claims"].values())


def test_actual_context_false_resources_preserved_not_flight_approval(tmp_path, integration):
    plan = integration[0]
    missing = tmp_path / "no-map-or-DEM"
    plan.docker_plan.assets.map.path = plan.docker_plan.assets.dem.path = missing
    report = execute_fixture(tmp_path, integration)
    assert report["status"] == "observed-context-qualified", report
    context = read_context(report)
    values = context["context"]
    for key in ("geofence_valid", "perception_available", "map_cache_available", "dem_available"):
        assert values[key] is False
    assert values["camera_available"] is True
    assert values["wind_north_mps"] == 1.0 and values["wind_east_mps"] == -0.4
    assert context["qualification_details"]["wind_source"] == "software-SIM-configured"
    assert context["claims"]["mission_approved"] is context["claims"]["train_admitted"] is False
