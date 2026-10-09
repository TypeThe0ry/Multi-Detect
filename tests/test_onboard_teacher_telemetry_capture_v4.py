"""Actual local CPU journals, not flight, authority or measured packet-rate proof."""

from dataclasses import replace
from pathlib import Path

import pytest
from test_onboard_teacher_sortie_budget_v1 import budget as budget_fixture
from test_onboard_teacher_telemetry_capture import Message

from scripts import onboard_teacher_telemetry_capture_v3 as old
from scripts.onboard_teacher_telemetry_capture_v4 import TeacherTelemetryCaptureV4

budget = budget_fixture


class Clock:
    def __init__(self, value=100.1):
        self.value = value

    def __call__(self):
        return self.value


def writer(tmp_path, budget, clock=None):
    clock = clock or Clock()
    cap = TeacherTelemetryCaptureV4(
        tmp_path / "writer",
        budget=budget,
        clock=clock,
        clock_id=budget.clock_id,
        transaction_id="owned-cpu",
        local_origin_id="cpu-origin",
    )
    return cap, clock


def test_original_raw_first_dual_commit_and_source_filter(tmp_path, budget):
    cap, _ = writer(tmp_path, budget)

    class Original(Message):
        def to_dict(self):
            assert cap._commits["raw"]["verified"] == 1
            assert cap._commits["projection"]["attempted"] == 0
            return super().to_dict()

    rows = cap.append_batch([(Original(extra=-0.0), 100), (Message(system=0), 100.01)])
    assert [r["record_index"] for r in rows] == [0, 1]
    assert [r["source_accepted"] for r in rows] == [True, False]
    assert (
        cap.latest_message("LOCAL_POSITION_NED", now_s=100.1, maximum_age_s=2)["record_index"] == 0
    )
    receipt = cap.seal()
    assert receipt["resource_budget"] == budget.to_mapping()
    assert receipt["journal_wire_contract"] == old.CONTRACT
    assert receipt["receipt"]["bytes"] <= budget.final_receipt_byte_limit
    assert not any(receipt["claims"].values())
    assert all(s.closed for s in cap._streams.values())


def test_new_writer_really_crosses_8192_without_reindex_or_drop(tmp_path, budget):
    cap, clock = writer(tmp_path, budget)
    for number in range(513):
        clock.value = 100.1 + number * 0.1
        rows = cap.append_batch([(Message(), clock.value)] * 16)
        assert rows[0]["record_index"] == number * 16
    assert len(cap._records) == 8208
    receipt = cap.seal()
    assert receipt["records"][-1]["record_index"] == 8207
    assert receipt["packet_attempt_count"] == receipt["record_count"] == 8208
    assert len(receipt["batches"]) == 513
    assert all(x["verified_prefix_matches"] for x in receipt["journal_sources"].values())
    with pytest.raises(ValueError, match="max_records"):
        old.TeacherTelemetryCaptureV3(
            tmp_path / "legacy",
            clock=clock,
            clock_id="old",
            transaction_id="old",
            local_origin_id="old",
            max_records=8193,
        )


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("maximum_raw_record_bytes", 512, "raw bound"),
        ("maximum_projection_record_bytes", 512, "projection bound"),
        ("maximum_receipt_record_bytes", 1024, "receipt record bound"),
    ],
)
def test_record_resources_poison_before_publication(tmp_path, budget, field, value, match):
    cap, _ = writer(tmp_path, replace(budget, **{field: value}))
    message = Message(extra="x" * 1200)
    if field == "maximum_raw_record_bytes":
        message.payload = b"x" * 500
    with pytest.raises(ValueError, match=match):
        cap.append_batch([(message, 100)])
    assert not cap._records and not cap._latest and cap._failure
    receipt = cap.abort("resource refusal")
    assert receipt["status"] == "aborted" and receipt["record_count"] == 0


def test_dual_fsync_delay_poison_before_cache(tmp_path, budget, monkeypatch):
    cap, clock = writer(tmp_path, budget)
    fsync = old.os.fsync

    def slow(fd):
        fsync(fd)
        clock.value += 0.3

    monkeypatch.setattr(old.os, "fsync", slow)
    with pytest.raises(ValueError, match="dual-commit latency"):
        cap.append_batch([(Message(), 100)])
    assert cap._commits["raw"]["verified"] == cap._commits["projection"]["verified"] == 1
    assert not cap._records and not cap._latest
    assert cap.abort("slow fsync")["orphan_packet_count"] == 1


def test_original_receive_rate_not_delayed_main_drain(tmp_path, budget):
    cap, clock = writer(tmp_path, budget)
    clock.value = 120
    for _ in range(8):
        cap.append_batch([(Message(), 100.1)] * 16)
    with pytest.raises(ValueError, match="packet-rate/burst"):
        cap.append_batch([(Message(), 100.1)])
    assert len(cap._records) == 128
    cap.abort("declared receive burst exceeded")


def test_idle_cannot_accumulate_unlimited_burst(tmp_path, budget):
    cap, clock = writer(tmp_path, budget)
    clock.value = 140
    for _ in range(8):
        cap.append_batch([(Message(), 140)] * 16)
    with pytest.raises(ValueError, match="packet-rate/burst"):
        cap.append_batch([(Message(), 140)])
    cap.abort("rate refusal")


@pytest.mark.parametrize("received", [99, 101, True])
def test_bad_original_receive_time_cannot_publish(tmp_path, budget, received):
    cap, _ = writer(tmp_path, budget)
    with pytest.raises(ValueError):
        cap.append_batch([(Message(), received)])
    assert not cap._records
    cap.abort("bad source time")


def test_cleanup_reserve_is_real_capture_stop_not_renewal(tmp_path, budget):
    cap, clock = writer(tmp_path, budget)
    clock.value = budget.phase_deadline_s("rtl_land")
    with pytest.raises(ValueError, match="phase deadline"):
        cap.append_batch([(Message(), clock.value)])
    # Closing files remains possible inside cleanup; it does not extend time.
    receipt = cap.seal()
    assert receipt["resource_budget"]["started_at_s"] == 100
    assert not cap._records


def test_lifecycle_binding_change_and_physical_tail_refuse(tmp_path, budget):
    cap, _ = writer(tmp_path, budget)
    cap.append_batch([(Message(), 100)])
    path = Path(cap._streams["raw"].name)
    with path.open("ab") as stream:
        stream.write(b"uncommitted")
    with pytest.raises(RuntimeError, match="Capture close refused"):
        cap.seal()
    assert all(s.closed for s in cap._streams.values())


def test_no_arbitrary_record_override_or_renewed_clock(tmp_path, budget):
    with pytest.raises(ValueError, match="original bound budget"):
        TeacherTelemetryCaptureV4(
            tmp_path / "bad",
            budget=budget,
            clock=Clock(),
            clock_id=budget.clock_id,
            max_records=8192,
        )
    cap, _ = writer(tmp_path, budget)
    cap.max_records += 1
    with pytest.raises(ValueError, match="binding changed"):
        cap.append_batch([(Message(), 100)])
    cap.abort("original budget altered")
