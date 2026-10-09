"""CPU actual-clock/committed-wire freshness seams, not external SIM proof."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import collect_teacher_live_navigation_checkpoint_v2 as provider

_SPEC = importlib.util.spec_from_file_location(
    "_provider_v2_cpu_helpers",
    Path(__file__).with_name("test_collect_teacher_live_navigation_checkpoint_v1.py"),
)
helpers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helpers)


@pytest.fixture
def collection_fixture(tmp_path, monkeypatch):
    generator = helpers.collection_fixture.__wrapped__(tmp_path, monkeypatch)
    try:
        yield next(generator)
    finally:
        generator.close()


def test_v2_original_packet_freshness_to_frozen_typed_CP(collection_fixture):
    session, receiver, capture, plan, args, inputs = collection_fixture
    checkpoint = provider.collect_live_checkpoint(
        session,
        receiver,
        capture,
        plan,
        args["artifact_root"],
        args["artifact_root"] / "collected",
        **inputs,
    )
    assert checkpoint.observed_context.captured_at_s == 100.36
    assert not capture._closed and receiver.to_mapping()["thread_alive"]
    report = helpers.receipt(collection_fixture)
    assert report["freshness_wait"]["latest"]["ready"]
    assert not report["freshness_wait"]["telemetry_requests_sent"]
    assert not any(report["claims"].values())


def test_wait_low_rate_event_uses_new_actual_receive_not_renewal(collection_fixture, monkeypatch):
    session, receiver, capture, _, _, _ = collection_fixture
    # Change only the retained committed row metadata to exercise the wait
    # predicate, NEVER pass this modified metadata to a qualifier.
    capture._latest["HEARTBEAT"]["received_at_s"] = 98.465
    original_battery_at = capture._latest["BATTERY_STATUS"]["received_at_s"]
    report = {}
    worker = SimpleNamespace(
        camera_updates=[{"host_return_at_s": 100.2}, {"host_return_at_s": 100.3}],
        rows={
            "navigation-after": {"started_at_s": 100.4},
            "owned-inspect-after": {"started_at_s": 100.45},
        },
    )
    calls = []

    def deliver(timeout):
        calls.append(timeout)
        session.clock.value += 0.1
        # This is a predicate seam, not a fabricated wire fixture PASS.
        capture._latest["HEARTBEAT"]["received_at_s"] = session.clock.value
        return []

    monkeypatch.setattr(receiver, "read_batch", deliver)
    provider._wait_for_current_packets(session, receiver, capture, worker, 120, report)
    wait = report["freshness_wait"]
    assert len(calls) == 1 and wait["latest"]["ready"]
    assert wait["latest"]["original_received_at_s"]["HEARTBEAT"] == 100.61
    assert wait["latest"]["original_received_at_s"]["BATTERY_STATUS"] == original_battery_at
    assert wait["latest"]["received_times_renewed"] is False
    assert wait["finished_at_s"] == 100.61


def test_wait_refuses_when_real_packet_never_arrives_before_original_camera_deadline(
    collection_fixture,
    monkeypatch,
):
    session, receiver, capture, _, _, _ = collection_fixture
    capture._latest["HEARTBEAT"]["received_at_s"] = 98
    worker = SimpleNamespace(
        camera_updates=[{"host_return_at_s": 99.1}, {"host_return_at_s": 99.2}],
        rows={
            "navigation-after": {"started_at_s": 100.4},
            "owned-inspect-after": {"started_at_s": 100.45},
        },
    )
    monkeypatch.setattr(
        receiver, "read_batch", lambda timeout: setattr(session.clock, "value", 100.8) or []
    )
    report = {}
    with pytest.raises(ValueError, match="headroom"):
        provider._wait_for_current_packets(session, receiver, capture, worker, 120, report)
    assert report["freshness_wait"]["deadline_s"] == pytest.approx(100.7)
    assert not capture._closed and receiver.to_mapping()["thread_alive"]


def test_context_wait_does_not_consume_raw_partial_or_missing_row(collection_fixture):
    capture = collection_fixture[2]
    del capture._latest["HOME_POSITION"]
    state = provider._latest_context_state(capture, 100.51)
    assert not state["ready"] and state["missing"] == ["HOME_POSITION"]


def test_context_wait_checks_original_skew(collection_fixture):
    capture = collection_fixture[2]
    capture._latest["HEARTBEAT"]["received_at_s"] = 99.7
    state = provider._latest_context_state(capture, 100.51)
    assert not state["ready"] and state["ages_s"]["HEARTBEAT"] > 0.75
