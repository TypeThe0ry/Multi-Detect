import hashlib
import io
import json

import pytest

from scripts import mission_clock_capture as module


def records():
    return [
        {
            "schema_version": 1,
            "source": "gz.msgs.Clock",
            "topic": "/world/default/clock",
            "sequence": i,
            "sim_ns": i * 100,
            "real_ns": i * 200,
            "system_ns": i * 200,
            "container_receive_monotonic_ns": i * 201,
            "host_px4_alignment_claimed": False,
        }
        for i in range(2)
    ]


class Process:
    def __init__(self, text):
        self.stdout = io.StringIO(text)
        self.stdin = io.StringIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.returncode = 0
        return 0


def start(monkeypatch, tmp_path, text):
    monkeypatch.setattr(module, "wait_clock_publisher", lambda *args: None)
    calls_at = iter([0, 100, 200])

    def probes(*args):
        base = next(calls_at)
        return [
            {
                "host_before_ns": base + i * 10,
                "host_after_ns": base + i * 10 + 2,
                "container_ns": base + i * 10 + 1,
                "container_id": "a" * 64,
            }
            for i in range(3)
        ]

    monkeypatch.setattr(module, "sample_probes", probes)
    process = Process(text)
    calls = []
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda args, **kw: calls.append(args) or process
    )
    capture = module.MissionClockCapture("docker", "a" * 64, tmp_path)
    capture.reader.join(timeout=2)
    assert calls[0][:4] == ["docker", "exec", "-i", "a" * 64]
    assert "--stop-on-stdin" in calls[0]
    return capture


def test_capture_finish_binds_raw_host_and_container_records(monkeypatch, tmp_path):
    rows = records()
    capture = start(monkeypatch, tmp_path, "".join(json.dumps(r) + "\n" for r in rows))
    capture.wait_after(0, timeout=0.1)
    result = capture.finish()
    capture.cleanup()
    parsed = [json.loads(line) for line in capture.path.read_text().splitlines()]
    assert [r["clock"] for r in parsed] == rows
    assert all(type(r["host_receive_monotonic_ns"]) is int for r in parsed)
    assert result["sha256"] == hashlib.sha256(capture.path.read_bytes()).hexdigest()
    assert result["summary"]["observed_sim_per_gazebo_real"] == 0.5
    assert not result["host_px4_alignment_claimed"]
    assert len(result["host_container_probes"]) == 6
    assert result["host_container_offset"]["constant_offset_compatible"]
    assert not result["host_container_offset"]["training_label_eligible"]


def test_capture_malformed_stream_fails_and_cleans_up(monkeypatch, tmp_path):
    capture = start(monkeypatch, tmp_path, "invalid-json\n")
    with pytest.raises(ValueError, match="stopped early"):
        capture.wait_after(0, timeout=0.1)
    capture.cleanup()
    assert capture.output.closed
    assert capture.stderr.closed


def test_holdout_is_separate_from_endpoint_fit(monkeypatch, tmp_path):
    capture = start(monkeypatch, tmp_path, "".join(json.dumps(r) + "\n" for r in records()))
    capture.sample_holdout()
    persisted = json.loads(capture.holdout_path.read_text())
    assert persisted["holdout_probes"] == capture.holdout_probes
    assert persisted["start_probes"] == capture.start_probes
    with pytest.raises(ValueError, match="already attempted"):
        capture.sample_holdout()
    result = capture.finish()
    capture.cleanup()
    assert len(result["host_container_probes"]) == 6
    holdout = result["host_container_holdout"]
    assert len(holdout["probes"]) == 3
    assert holdout["probes"][0]["host_before_ns"] == 100
    assert result["host_container_probes"][3]["host_before_ns"] == 200
    assert not holdout["used_for_endpoint_fit"]
    assert not holdout["mission_window_membership_verified"]


def test_holdout_survives_cleanup_without_successful_mission(monkeypatch, tmp_path):
    capture = start(monkeypatch, tmp_path, "".join(json.dumps(r) + "\n" for r in records()))
    capture.sample_holdout()
    capture.cleanup()
    persisted = json.loads(capture.holdout_path.read_text())
    assert len(persisted["holdout_probes"]) == 3
    assert not persisted["mission_window_membership_verified"]


def test_failed_holdout_cannot_be_silently_omitted(monkeypatch, tmp_path):
    capture = start(monkeypatch, tmp_path, "".join(json.dumps(r) + "\n" for r in records()))

    def fail(*args, **kwargs):
        raise ValueError("probe failed")

    monkeypatch.setattr(module, "sample_probes", fail)
    with pytest.raises(ValueError, match="probe failed"):
        capture.sample_holdout()
    with pytest.raises(ValueError, match="attempt failed"):
        capture.finish()
    capture.cleanup()
    assert capture.output.closed


def test_capture_reset_fails_final_validation(monkeypatch, tmp_path):
    rows = records()
    rows[0]["sim_ns"] = 500
    capture = start(monkeypatch, tmp_path, "".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(ValueError, match="reset"):
        capture.finish()
    capture.cleanup()


def test_only_exact_container_ids_accepted(tmp_path):
    with pytest.raises(ValueError, match="exact"):
        module.MissionClockCapture("docker", "some-name", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_wait_requires_publisher_not_subscriber(monkeypatch):
    from types import SimpleNamespace

    replies = iter(
        [
            "No publishers on topic\nSubscribers [Address, Message Type]: gz.msgs.Clock",
            "Publishers [Address, Message Type]: tcp://test, gz.msgs.Clock\nSubscribers [none]",
        ]
    )
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=next(replies))

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.time, "sleep", lambda delay: None)
    module.wait_clock_publisher("docker", "a" * 64)
    assert len(calls) == 2
    assert all(args[1:3] == ["exec", "a" * 64] for args in calls)
