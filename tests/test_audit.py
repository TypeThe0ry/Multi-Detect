from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType

import pytest

from multidetect.audit import AuditLog


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
