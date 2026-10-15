from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def _cursor_sequence(log):
    if not log.cursor_path.exists():
        return 0
    return json.loads(log.cursor_path.read_text(encoding="utf-8"))["sequence"]


def _wait_for_ack(queue, sequence):
    _wait_until(lambda: queue.acknowledged_sequence == sequence)
    assert _cursor_sequence(queue.log) == sequence


def test_results_and_summary_survive_offline_reopen(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    queue = DownlinkQueue(log, None)
    try:
        first = log.append("finding", 10.0, query="red car", position_ne=[20, 30])
        prefix = log.path.read_bytes()
        log.append("decision", 11.0, action="continue", reason="operator_policy")
        final = log.finish(12.0, {"findings": 1, "state": "complete"})
        assert log.path.read_bytes().startswith(prefix)
        records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        assert records[0] == first
        assert records[-1] == final
        assert json.loads(log.summary_path.read_text(encoding="utf-8")) == final
        assert not log.cursor_path.exists()
    finally:
        queue.close()

    reopened = OnboardMissionLog(tmp_path, "mission-1")
    assert reopened.append("recovered", 13.0)["sequence"] == 4
    with pytest.raises(ValueError, match="mission_id"):
        OnboardMissionLog(tmp_path, "other-mission")


def test_reconnect_drains_and_restart_resumes_after_durable_ack(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    log.append("finding", 1.0, index=1)
    log.append("finding", 2.0, index=2)
    received = []
    queue = DownlinkQueue(log, received.append)
    try:
        assert received == []
        queue.set_connected(True)
        _wait_for_ack(queue, 2)
        queue.set_connected(False)
        log.append("finding", 3.0, index=3)
        queue.notify()
        assert [record["sequence"] for record in received] == [1, 2]
    finally:
        queue.close()

    queue = DownlinkQueue(OnboardMissionLog(tmp_path, "mission-1"), received.append)
    try:
        queue.set_connected(True)
        _wait_for_ack(queue, 3)
        assert [record["sequence"] for record in received] == [1, 2, 3]
        queue.set_connected(False)
        log.append("decision", 4.0, action="return_home")
        queue.notify()
        queue.set_connected(True)
        _wait_for_ack(queue, 4)
        assert [record["sequence"] for record in received] == [1, 2, 3, 4]
    finally:
        queue.close()


@pytest.mark.parametrize("failure", [False, OSError("link disconnected")])
def test_failed_sender_retains_record_until_transport_recovers(tmp_path: Path, failure):
    log = OnboardMissionLog(tmp_path, "mission-1")
    log.append("finding", 1.0)
    called = threading.Event()

    def unavailable(record):
        called.set()
        if isinstance(failure, Exception):
            raise failure
        return failure

    queue = DownlinkQueue(log, unavailable)
    try:
        queue.set_connected(True)
        assert called.wait(2.0)
        _wait_until(lambda: queue.last_error is not None)
        assert not log.cursor_path.exists()
        received = []
        queue.set_sender(received.append)
        _wait_for_ack(queue, 1)
        assert [record["sequence"] for record in received] == [1]
        assert queue.last_error is None
    finally:
        queue.close()


def test_slow_transport_never_blocks_flight_append_or_shutdown(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    log.append("finding", 1.0)
    entered = threading.Event()
    release = threading.Event()

    def slow_sender(record):
        entered.set()
        release.wait(3.0)

    queue = DownlinkQueue(log, slow_sender)
    queue.set_connected(True)
    try:
        assert entered.wait(2.0)
        started = time.monotonic()
        log.append("decision", 2.0, action="continue")
        queue.notify()
        queue.set_connected(False)
        queue.close()
        assert time.monotonic() - started < 0.5
        assert not log.cursor_path.exists()
        assert len(log.path.read_text(encoding="utf-8").splitlines()) == 2
    finally:
        release.set()
        queue.close()


def test_append_after_empty_online_queue_is_notified(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    received = []
    queue = DownlinkQueue(log, None)
    try:
        queue.set_connected(True)
        queue.set_sender(received.append)
        log.append("finding", 1.0)
        queue.notify()
        _wait_for_ack(queue, 1)
        assert received[0]["mission_id"] == "mission-1"
    finally:
        queue.close()


def test_invalid_cursor_replays_instead_of_skipping_results(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    log.append("finding", 1.0)
    log.cursor_path.write_text(
        json.dumps({"mission_id": "mission-1", "sequence": 100, "offset": 1}),
        encoding="utf-8",
    )
    received = []
    queue = DownlinkQueue(log, received.append)
    try:
        queue.set_connected(True)
        _wait_for_ack(queue, 1)
        assert len(received) == 1
    finally:
        queue.close()


def test_incomplete_log_tail_is_preserved_and_rejected(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    log.append("finding", 1.0)
    with log.path.open("ab") as handle:
        handle.write(b'{"partial":')
    original = log.path.read_bytes()
    with pytest.raises(ValueError, match="incomplete tail"):
        OnboardMissionLog(tmp_path, "mission-1")
    assert log.path.read_bytes() == original
