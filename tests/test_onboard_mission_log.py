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


def test_a_count_record_with_sets_and_dataclasses_is_logged() -> None:
    # A supervised count crashed on its first counted cone: the record carries
    # the co-visible track ids as a set and an appearance dataclass.
    from multidetect.autonomy.onboard_loop import AppearanceSignature
    from multidetect.autonomy.onboard_mission_log import _encoded

    record = {
        "co_visible_with": {"t3", "t1"},
        "appearance": AppearanceSignature(size_range=0.02, aspect_ratio=0.5),
    }
    decoded = json.loads(_encoded({"counted": record}))
    assert decoded["counted"]["co_visible_with"] == ["t1", "t3"]
    assert decoded["counted"]["appearance"]["aspect_ratio"] == 0.5
    with pytest.raises(TypeError):
        _encoded({"bad": object()})


def test_selector_keeps_routine_records_local_but_passes_the_cursor(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    log.append("mission_started", 0.0)
    for second in range(1, 70):
        log.append("ground_event", float(second), event_type="heartbeat")
        log.append("estimates", float(second), remaining_energy_wh=80)
        log.append("execution", float(second), action={"kind": "hold"})
        log.append("decision", float(second), action="continue", reason="ok")
    log.append("ground_event", 70.0, event_type="pause")
    log.append("ground_event_rejected", 70.5, reason="rate", event_type="heartbeat")
    log.append("decision", 71.0, action="hold", reason="operator pause")
    log.append("decision", 72.0, action="hold", reason="operator pause")
    received = []
    queue = DownlinkQueue(log, received.append)
    try:
        queue.set_connected(True)
        _wait_for_ack(queue, log._sequence)
    finally:
        queue.close()
    sent = [(r["kind"], r["timestamp"]) for r in received]
    # One decision per change, plus a keepalive every 30 s of record time.
    assert sent == [
        ("mission_started", 0.0),
        ("decision", 1.0),
        ("decision", 31.0),
        ("decision", 61.0),
        ("ground_event", 70.0),
        ("decision", 71.0),
    ]


def test_priority_record_overtakes_the_routine_backlog(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    for second in range(5):
        log.append("decision", float(second), action="continue", reason=f"r{second}")
    log.append("finding", 5.0, label="cone")
    log.append("final_summary", 6.0, summary={})
    received = []
    queue = DownlinkQueue(log, received.append)
    try:
        queue.set_connected(True)
        _wait_for_ack(queue, 7)
    finally:
        queue.close()
    assert [r["sequence"] for r in received] == [6, 7, 1, 2, 3, 4, 5]


def test_failed_decision_send_is_retried_not_treated_as_a_repeat(tmp_path: Path):
    log = OnboardMissionLog(tmp_path, "mission-1")
    log.append("decision", 1.0, action="continue", reason="ok")
    attempts = []

    def flaky(record):
        attempts.append(record["sequence"])
        return len(attempts) > 1

    queue = DownlinkQueue(log, flaky)
    try:
        queue.set_connected(True)
        _wait_until(lambda: queue.last_error is not None)
        queue.notify()
        _wait_for_ack(queue, 1)
    finally:
        queue.close()
    assert attempts == [1, 1]


def test_tick_rate_appends_stay_fast_on_a_slow_disk(tmp_path: Path, monkeypatch):
    """SITL run8: a per-record fsync under the log lock stalled the control loop
    and the link thread for ~5.6 s.  Routine records are no longer fsynced one
    by one and the downlink reader takes no append lock."""
    import multidetect.autonomy.onboard_mission_log as module

    synced = []
    real_fsync = module.os.fsync

    def slow_fsync(fd):
        # Count the control loop's own syncs; the downlink cursor syncs in its worker.
        if threading.current_thread() is threading.main_thread():
            synced.append(fd)
        time.sleep(0.2)
        real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", slow_fsync)
    log = OnboardMissionLog(tmp_path, "mission-1")
    in_flight = threading.Event()

    def slow_sender(record):
        in_flight.set()
        time.sleep(0.05)

    queue = DownlinkQueue(log, slow_sender)
    queue.set_connected(True)
    try:
        latencies = []
        # run7 rates: per second one heartbeat, ~2 decisions, estimates, execution.
        for second in range(60):
            for kind, payload in (
                ("ground_event", {"event_type": "heartbeat"}),
                ("decision", {"action": "continue", "reason": f"r{second % 3}"}),
                ("estimates", {"remaining_energy_wh": 80}),
                ("execution", {"action": {"kind": "hold"}, "history": list(range(50))}),
                ("decision", {"action": "continue", "reason": "ok"}),
            ):
                started = time.perf_counter()
                log.append(kind, float(second), **payload)
                latencies.append(time.perf_counter() - started)
            queue.notify()
        assert in_flight.is_set()
        assert max(latencies) < 0.1, max(latencies)
        assert synced == []
        started = time.perf_counter()
        log.append("finding", 61.0, label="cone")
        assert time.perf_counter() - started >= 0.2 and len(synced) == 1  # results stay durable
    finally:
        queue.close()
