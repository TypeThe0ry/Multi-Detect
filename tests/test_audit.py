from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType

import pytest

from multidetect.audit import AuditLog
from multidetect.config import MissionConfig
from multidetect.mission import MissionController

ROOT = Path(__file__).resolve().parents[1]


def test_sequence_is_monotonic_under_concurrent_append() -> None:
    log = AuditLog()

    with ThreadPoolExecutor(max_workers=8) as executor:
        appended = tuple(
            executor.map(
                lambda index: log.append("worker_event", float(index), {"index": index}),
                range(64),
            )
        )

    assert len(appended) == 64
    assert tuple(event.sequence for event in log.events()) == tuple(range(1, 65))


def test_details_and_events_are_immutable_detached_snapshots() -> None:
    log = AuditLog()
    original = {"nested": {"items": [1, 2]}, "mode": "simulation"}

    log.append("decision", 1.25, original)
    original["nested"]["items"].append(3)
    snapshot = log.events()

    assert isinstance(snapshot, tuple)
    assert isinstance(snapshot[0].details, MappingProxyType)
    assert snapshot[0].details["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        snapshot[0].details["new"] = "value"


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timestamp_is_rejected(timestamp: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        AuditLog().append("invalid_time", timestamp)


def test_write_jsonl_atomically_replaces_utf8_file(tmp_path) -> None:
    destination = tmp_path / "audit.jsonl"
    destination.write_text("stale data", encoding="utf-8")
    log = AuditLog()
    log.append("启动", 1.0, {"status": "正常", "values": (1, 2)})
    log.append("completed", 2.0, {"ok": True})

    log.write_jsonl(destination)

    records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {
            "sequence": 1,
            "timestamp_s": 1.0,
            "event_type": "启动",
            "details": {"status": "正常", "values": [1, 2]},
        },
        {
            "sequence": 2,
            "timestamp_s": 2.0,
            "event_type": "completed",
            "details": {"ok": True},
        },
    ]
    assert not tuple(tmp_path.glob(".audit.jsonl.*.tmp"))


def test_streaming_audit_bounds_memory_and_persists_every_event(tmp_path) -> None:
    destination = tmp_path / "live.audit.jsonl"
    log = AuditLog(stream_path=destination, max_in_memory_events=2)
    log.append("one", 1.0)
    log.append("two", 2.0)
    log.append("three", 3.0)

    log.write_jsonl(destination)
    retained = log.events()
    records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    log.close()

    assert len(log) == 3
    assert tuple(event.sequence for event in retained) == (2, 3)
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert len({record["session_id"] for record in records}) == 1

    with pytest.raises(RuntimeError, match="closed"):
        log.append("late", 4.0)


def test_streaming_append_preserves_previous_sessions(tmp_path) -> None:
    destination = tmp_path / "live.audit.jsonl"
    first = AuditLog(stream_path=destination, stream_append=True)
    first.append("first-run", 1.0)
    first.close()
    second = AuditLog(stream_path=destination, stream_append=True)
    second.append("second-run", 2.0)
    second.close()

    records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]

    assert [record["event_type"] for record in records] == ["first-run", "second-run"]
    assert len({record["session_id"] for record in records}) == 2


def test_streaming_append_discards_only_incomplete_tail_record(tmp_path) -> None:
    destination = tmp_path / "live.audit.jsonl"
    destination.write_bytes(b'{"event_type":"complete"}\n{"event_type":"partial"')

    log = AuditLog(stream_path=destination, stream_append=True)
    log.append("new-session", 3.0)
    log.close()

    records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert [record["event_type"] for record in records] == ["complete", "new-session"]


def test_mission_preserves_an_empty_injected_streaming_audit_log(tmp_path) -> None:
    destination = tmp_path / "mission.audit.jsonl"
    log = AuditLog(stream_path=destination)
    mission = MissionController(
        MissionConfig.from_json(ROOT / "configs/missions/fire_patrol.demo.json"),
        audit_log=log,
    )

    mission.launch(now_s=1.0)
    mission.arrive_task_area(now_s=2.0)
    log.close()

    records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert mission.audit is log
    assert [record["event_type"] for record in records] == [
        "payload.inventory_evaluated",
        "mission.transition",
        "mission.transition",
    ]
