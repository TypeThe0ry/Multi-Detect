from copy import deepcopy

import pytest

from multidetect.clock import HighResolutionSystemClock
from scripts.audit_telemetry_receipts import correlate


def inputs():
    identity = HighResolutionSystemClock(session_id="receipt-test").identity()
    router = [
        dict(
            frame_sha256=str(i) * 64,
            message_id=74,
            source_system=1,
            source_component=1,
            router_decoded_host_s=100 + i,
            router_forwarded_host_s=100.01 + i,
        )
        for i in (1, 2)
    ]
    samples = [
        dict(
            frame_sha256=str(i) * 64,
            type="VFR_HUD",
            source_system=1,
            source_component=1,
            at_s=i + 0.125,
            pump_started_host_s=100 + i,
            previous_pump_finished_host_s=99.5 + i,
        )
        for i in (1, 2)
    ]
    return (
        dict(
            host_clock_identity=deepcopy(identity),
            samples=router,
            samples_seen=2,
            samples_dropped=0,
        ),
        dict(
            host_clock_identity=deepcopy(identity),
            samples=samples,
            samples_seen=2,
            samples_dropped=0,
            receive_clock="host_perf_counter_since_collector_start",
            receive_origin_host_monotonic_s=100,
        ),
    )


def test_unique_wire_match_reports_userspace_intervals_without_mutation():
    trace, history = inputs()
    original = deepcopy((trace, history))
    result = correlate(trace, history)
    assert result["unique_matches"] == 2
    assert result["largest_decode_to_collect"][0]["router_decode_to_collect_s"] == 0.125
    assert result["largest_decode_to_collect"][0]["between_pumps_s"] == 0.5
    assert result["largest_load_message_gaps"][0]["router_decode_gap_s"] == 1
    assert result["gap_root_cause_proven"] is False
    assert (trace, history) == original


@pytest.mark.parametrize("side", [0, 1])
def test_repeated_wire_hash_is_ambiguous_not_guessed(side):
    records = inputs()
    records[side]["samples"].append(deepcopy(records[side]["samples"][-1]))
    records[side]["samples_seen"] += 1
    result = correlate(*records)
    assert result["unique_matches"] == 1
    assert result["ambiguous_records"] == (1 if side == 0 else 2)
    actual_gaps = result["largest_consecutive_collector_load_gaps"]
    assert actual_gaps[0]["collector_gap_s"] == 1
    assert len(actual_gaps) == (1 if side == 0 else 2)


@pytest.mark.parametrize("fault", ["identity", "truncated", "count", "metadata", "future", "pump"])
def test_invalid_or_cross_domain_input_rejected(fault):
    trace, history = inputs()
    if fault == "identity":
        history["host_clock_identity"]["session_id"] = "other"
    elif fault == "truncated":
        trace["samples_dropped"] = 1
    elif fault == "count":
        history["samples_seen"] = 3
    elif fault == "metadata":
        history["samples"][0]["source_system"] = 2
    elif fault == "future":
        history["samples"][0]["at_s"] = 0
    else:
        history["samples"][0]["pump_started_host_s"] = 200
    with pytest.raises(ValueError):
        correlate(trace, history)


def test_missing_frame_bytes_do_not_create_a_match():
    trace, history = inputs()
    history["samples"][0]["frame_sha256"] = None
    result = correlate(trace, history)
    assert result["missing_or_unavailable_records"] == 1
    assert result["unique_matches"] == 1
