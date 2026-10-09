"""Packet-first v2 through the frozen actual context bridge, CPU fixtures only."""

from __future__ import annotations

import array
import hashlib
import json
import math
from pathlib import Path

import pytest
from test_onboard_teacher_observation_context_integration import (
    Message,
    read_context,
)
from test_onboard_teacher_observation_context_integration import (
    integration as integration,
)

from scripts import run_onboard_teacher_gazebo_observation_v2 as runner
from scripts.onboard_teacher_telemetry_capture_v2 import TAG, decode_typed_value


def execute(tmp_path, integration, factory=None):
    plan, _, original_factory, _ = integration
    return runner.observe_teacher(
        plan,
        tmp_path / "observation-v2",
        execute=True,
        context_timeout_s=1,
        session_factory=factory or original_factory,
        artifact_root=tmp_path,
    )


def read_bound(binding):
    raw = Path(binding["path"]).read_bytes()
    assert len(raw) == binding["bytes"]
    assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    return json.loads(raw)


@pytest.mark.parametrize("sequence_type", [tuple, lambda values: array.array("H", values)])
def test_numeric_sequence_roundtrips_and_context_keeps_finite_values(
    tmp_path, integration, sequence_type
):
    original = sequence_type(integration[1]["BATTERY_STATUS"]["voltages"])
    integration[1]["BATTERY_STATUS"]["voltages"] = original
    report = execute(tmp_path, integration)
    assert report["contract"] == "multidetect.onboard-teacher-gazebo-observation-v2"
    assert report["status"] == "observed-context-qualified", report
    assert report["external_SIM_observed_here"] is False
    assert read_context(report)["context"]["estimated_flight_time_s"] > 1198
    records = report["raw_telemetry"]["records"]
    record = next(row for row in records if row["message_type"] == "BATTERY_STATUS")
    typed = decode_typed_value(read_bound(record["typed_projection"])["value"])
    assert type(typed["voltages"]) is type(original)
    assert list(typed["voltages"]) == list(original)
    assert read_bound(record["envelope"])["message"]["voltages"] == list(original)
    assert report["raw_telemetry"]["orphan_packet_count"] == 0
    assert all(value is False for value in report["claims"].values())


@pytest.mark.parametrize("bad_accuracy", [math.nan, math.inf, -math.inf])
def test_nonfinite_accuracy_stays_tagged_and_cannot_qualify(tmp_path, integration, bad_accuracy):
    integration[1]["GPS_RAW_INT"]["h_acc"] = bad_accuracy
    report = execute(tmp_path, integration)
    assert report["status"] == "context-refused", report
    assert report["context_qualified"] is False
    assert "context" not in report
    assert report["raw_telemetry"]["status"] == "recorded"
    projected = report["diagnostic_rows"]["GPS_RAW_INT"]["message"]["h_acc"]
    assert set(projected) == {TAG}
    decoded = decode_typed_value(projected[TAG])
    assert math.isnan(decoded) if math.isnan(bad_accuracy) else decoded == bad_accuracy
    assert report["session"]["closed"] is True
    assert all(value is False for value in report["claims"].values())


@pytest.mark.parametrize("binary_type", [bytes, bytearray])
def test_foreign_binary_packet_preserved_without_renewing_accepted_context(
    tmp_path, integration, binary_type
):
    class Binary(Message):
        def get_srcSystem(self):
            return 0

        def get_srcComponent(self):
            return 0

    class Session(integration[2]):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.foreign = Binary("UNKNOWN_255", {"data": binary_type(b"\x00\xff")}, 0)

        def raw_receive(self, timeout):
            if self.foreign is not None:
                packet, self.foreign = self.foreign, None
                return packet
            return super().raw_receive(timeout)

    report = execute(tmp_path, integration, Session)
    assert report["status"] == "observed-context-qualified", report
    raw = report["raw_telemetry"]
    assert raw["foreign_source_count"] == 1
    record = raw["records"][0]
    assert record["source_accepted"] is False and record["message_type"] == "UNKNOWN_255"
    original = decode_typed_value(read_bound(record["typed_projection"])["value"])
    assert type(original["data"]) is binary_type and original["data"] == b"\x00\xff"
    projected = read_bound(record["envelope"])["message"]["data"]
    assert set(projected) == {TAG}
    assert report["external_SIM_observed_here"] is False
    assert all(value is False for value in report["claims"].values())


def test_projection_error_keeps_first_original_packet_and_sends_no_request(tmp_path, integration):
    class Unsupported(Message):
        def to_dict(self):
            return {"mavpackettype": self.kind, "data": memoryview(b"unsupported")}

    class Session(integration[2]):
        def raw_receive(self, timeout):
            return Unsupported("UNKNOWN_255", {}, 0)

    report = execute(tmp_path, integration, Session)
    assert report["status"] == "observation-failed"
    assert "Unsupported typed projection type: memoryview" in report["error"]
    raw = report["raw_telemetry"]
    assert raw["record_count"] == 0 and raw["orphan_packet_count"] == 1
    attempt = raw["packet_attempts"][0]
    assert attempt["packet_durable"] is True and attempt["status"] == "orphan-packet"
    packet = Path(attempt["packet"]["path"]).read_bytes()
    assert packet == b"CPU-context-integration-packet:0:UNKNOWN_255"
    assert hashlib.sha256(packet).hexdigest() == attempt["packet"]["sha256"]
    assert read_bound(attempt["pending"])["projection_complete"] is False
    assert report["camera_frames_recorded"] == report["telemetry_stream_request_API_calls"] == 0
    assert integration[3][0].sent == [] and report["session"]["closed"] is True


def test_v2_default_plan_remains_nonexecuting(tmp_path, integration):
    def forbidden(*args, **kwargs):
        raise AssertionError("Plan-only must not create a session")

    report = runner.observe_teacher(
        integration[0],
        tmp_path / "planned-v2",
        session_factory=forbidden,
        artifact_root=tmp_path,
    )
    assert report["status"] == "planned"
    assert report["raw_telemetry"] is None
    assert all(value is False for value in report["claims"].values())
