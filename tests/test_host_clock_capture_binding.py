import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock, HostClockStamp
from scripts import mission_clock_capture as capture_module
from scripts.audit_mission_clock_receipts import audit_files, audit_receipts
from scripts.clock_offset_probe import describe_probes, host_clock_binding, validate_host_identity
from scripts.gazebo_clock_capture import describe_clock_progress


def test_qpc_capture_probes_receipts_and_threshold_share_identity(monkeypatch, tmp_path):
    from multidetect import clock as module

    ticks = iter([1000, 1001])
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: next(ticks))
    clock = HighResolutionSystemClock(session_id="capture-session")
    identity = clock.identity()
    raw = [
        {
            "schema_version": 1,
            "source": "gz.msgs.Clock",
            "topic": "/world/default/clock",
            "sequence": i,
            "sim_ns": i * 10,
            "real_ns": i * 20,
            "system_ns": i * 20,
            "container_receive_monotonic_ns": i * 20,
            "host_px4_alignment_claimed": False,
        }
        for i in range(2)
    ]
    calls = []
    bases = iter([0, 10000])

    def probes(*args, **kwargs):
        assert kwargs["clock"] is clock
        base = next(bases)
        calls.append(base)
        return [
            {
                "host_before_ns": base + i * 10,
                "host_after_ns": base + i * 10 + 2,
                "container_ns": base + i * 10 + 1,
                "container_id": "a" * 64,
                "host_resolution_ns": identity["resolution_ns"],
                "host_clock_identity": identity,
            }
            for i in range(3)
        ]

    process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO("".join(json.dumps(r) + "\n" for r in raw)),
        poll=lambda: 0,
        wait=lambda **kwargs: 0,
    )
    monkeypatch.setattr(capture_module, "wait_clock_publisher", lambda *args: None)
    monkeypatch.setattr(capture_module, "sample_probes", probes)
    monkeypatch.setattr(capture_module.subprocess, "Popen", lambda *args, **kwargs: process)
    capture = capture_module.MissionClockCapture("docker", "a" * 64, tmp_path, clock=clock)
    capture.reader.join(timeout=2)
    for wrong in (
        0,
        HostClockStamp(0, "python.monotonic", "capture-session"),
        HostClockStamp(0, "python.perf_counter", "other"),
    ):
        with pytest.raises(ValueError, match="matching"):
            capture.wait_after(wrong)
    capture.wait_after(HostClockStamp(0, "python.perf_counter", "capture-session"))
    result = capture.finish()
    capture.cleanup()
    receipts = [json.loads(line) for line in capture.path.read_text().splitlines()]
    assert [r["host_receive_monotonic_ns"] for r in receipts] == [1000, 1001]
    assert all(r["host_clock_identity"] == identity for r in receipts)
    assert result["host_clock_identity"] == result["host_container_offset"]["host_clock_identity"]
    assert calls == [0, 10000]
    audit_receipts(receipts, 0, 0.01, host_clock_identity=identity)
    with pytest.raises(ValueError):
        audit_receipts(receipts, 0, 0.01)
    with pytest.raises(ValueError):
        audit_receipts(receipts, 0, 0.01, host_clock_identity=dict(identity, session_id="other"))


@pytest.mark.parametrize("fault", ["mixed_session", "missing", "null", "resolution"])
def test_probe_identity_mixing_rejected(fault):
    identity = HighResolutionSystemClock(session_id="same").identity()
    probes = [
        {
            "host_before_ns": i * 100,
            "host_after_ns": i * 100 + 20,
            "container_ns": i * 100 + 1,
            "container_id": "a" * 64,
            "host_resolution_ns": identity["resolution_ns"],
            "host_clock_identity": identity,
        }
        for i in range(2)
    ]
    if fault == "mixed_session":
        probes[1]["host_clock_identity"] = dict(identity, session_id="other")
    elif fault == "missing":
        del probes[1]["host_clock_identity"]
    elif fault == "null":
        for row in probes:
            row["host_clock_identity"] = None
    else:
        probes[1]["host_resolution_ns"] += 1
    with pytest.raises(ValueError):
        describe_probes(probes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "python.monotonic"),
        ("session_id", ""),
        ("resolution_ns", True),
        ("accuracy_verified", True),
    ],
)
def test_invalid_identity_rejected(field, value):
    identity = HighResolutionSystemClock().identity()
    identity[field] = value
    with pytest.raises(ValueError):
        validate_host_identity(identity)


def test_unknown_clock_cannot_be_silently_bound():
    with pytest.raises(TypeError):
        host_clock_binding(object())


def test_hash_bound_qpc_report_requires_matching_mission_and_probes(tmp_path):
    identity = HighResolutionSystemClock(session_id="one-campaign").identity()
    clocks = [
        {
            "schema_version": 1,
            "source": "gz.msgs.Clock",
            "topic": "/world/default/clock",
            "sequence": i,
            "sim_ns": i,
            "real_ns": i,
            "system_ns": i,
            "container_receive_monotonic_ns": i,
            "host_px4_alignment_claimed": False,
        }
        for i in range(3)
    ]
    receipts = [
        {
            "host_receive_monotonic_ns": (i + 1) * 10**9,
            "clock": row,
            "host_clock_identity": identity,
        }
        for i, row in enumerate(clocks)
    ]
    probes = [
        {
            "host_before_ns": i * 10**9,
            "host_after_ns": i * 10**9 + 1000,
            "container_ns": i * 10**9,
            "container_id": "a" * 64,
            "host_resolution_ns": identity["resolution_ns"],
            "host_clock_identity": identity,
        }
        for i in range(2)
    ]
    clock_path, mission_path, outer_path = [
        tmp_path / name for name in ("clock", "mission", "outer")
    ]
    clock_path.write_text("\n".join(json.dumps(row) for row in receipts), encoding="utf-8")
    mission = {
        "gate_passed": True,
        "failure": None,
        "container": {"id": "a" * 64},
        "flight": {"host_clock_identity": identity, "completed_host_monotonic_s": 2.5},
        "mission_receive_window": {
            "host_clock_identity": identity,
            "clock": "host_perf_counter_since_collector_start",
            "arm_request_host_monotonic_s": 1.5,
            "completed_host_monotonic_s": 2.5,
        },
    }
    mission_path.write_text(json.dumps(mission), encoding="utf-8")
    capture = {
        "exit_code": 0,
        "host_px4_alignment_claimed": False,
        "bytes": clock_path.stat().st_size,
        "sha256": hashlib.sha256(clock_path.read_bytes()).hexdigest(),
        "container_id": "a" * 64,
        "summary": describe_clock_progress(clocks),
        "host_clock_identity": identity,
        "host_container_probes": probes,
        "host_container_offset": describe_probes(probes),
    }
    outer = {
        "gate_passed": True,
        "gates": {"clock_capture": True},
        "clock_capture": capture,
        "campaign": {
            "verified_sorties": [
                {"report_sha256": hashlib.sha256(mission_path.read_bytes()).hexdigest()}
            ]
        },
    }

    def run():
        outer_path.write_text(json.dumps(outer), encoding="utf-8")
        return audit_files(
            outer_path,
            hashlib.sha256(outer_path.read_bytes()).hexdigest(),
            clock_path,
            mission_path,
        )

    assert run()["receipt_window_covered"]
    capture["host_container_offset"] = dict(capture["host_container_offset"], sample_count=99)
    with pytest.raises(ValueError, match="probe"):
        run()
    capture["host_container_offset"] = describe_probes(probes)
    capture["host_clock_identity"] = dict(identity, session_id="another-campaign")
    with pytest.raises(ValueError, match="domains"):
        run()
