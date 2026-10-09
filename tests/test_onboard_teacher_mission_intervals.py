"""Small source-declared CPU fixtures, not actual Mission/teacher acceptance."""

from __future__ import annotations

import copy
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_executed_trajectory import (
    QUATERNION_CONVENTION,
    ActualObservation,
    EvidenceBinding,
    ObservedPose,
)
from scripts import onboard_teacher_mission_intervals as intervals

SHA = "a" * 64
CLOCK = "fixture-observer-clock"
TRANSACTION = "fixture-one-upload"


def binding(*, pointer="/receive/0", source_id="fixture-telemetry"):
    return EvidenceBinding("fixture/receive.json", 20, SHA, source_id, pointer)


def parent():
    return intervals.MissionParentSubmission(
        "fixture-mission",
        TRANSACTION,
        "fixture-x500",
        SHA,
        "b" * 64,
        5,
        0.5,
        CLOCK,
        1,
        1,
        "fixture-px4-local-origin",
        binding(),
    )


def message(kind, **fields):
    return {"message_type": kind, "source_system_id": 1, "source_component_id": 1, **fields}


def feedback(recorder, kind, seq, stamp, **kwargs):
    return recorder.record_feedback(
        message(kind, seq=seq),
        received_at_s=stamp,
        clock_id=CLOCK,
        transaction_id=TRANSACTION,
        source=binding(),
        **kwargs,
    )


def state(recorder, stamp, *, xyz=(0, 0, -3), velocity=(0, 0, 0), boot=None, **changes):
    raw = message(
        "LOCAL_POSITION_NED",
        time_boot_ms=int(stamp * 1000) if boot is None else boot,
        **dict(zip(("x", "y", "z", "vx", "vy", "vz"), (*xyz, *velocity), strict=True)),
    )
    raw.update(changes)
    return recorder.record_state(
        raw,
        received_at_s=stamp,
        clock_id=CLOCK,
        transaction_id=TRANSACTION,
        local_origin_id="fixture-px4-local-origin",
        source=binding(),
    )


def window(recorder, start=1.0, **kwargs):
    for index in range(5):
        state(recorder, start + index * 0.25, **kwargs)


def observation(sequence, stamp, *, clock_id=CLOCK, origin="fixture-gazebo-origin"):
    pose = ObservedPose(
        (0, 0, 3),
        (1, 0, 0, 0),
        "enu",
        origin,
        QUATERNION_CONVENTION,
        "simulated_observation",
        stamp,
        clock_id,
        binding(source_id="fixture-gazebo-odometry"),
    )
    return ActualObservation(
        f"fixture-camera-{sequence}",
        sequence,
        stamp,
        clock_id,
        EvidenceBinding(f"fixture/{sequence}.rgb", 30, SHA, "fixture-camera", None),
        pose,
        binding(source_id="fixture-frame-records"),
    )


def settled(*, attach=True):
    recorder = intervals.MissionIntervalRecorder(parent(), (1, 2))
    window(recorder)
    feedback(recorder, "MISSION_CURRENT", 1, 2.1)
    feedback(recorder, "MISSION_ITEM_REACHED", 1, 3.0)
    feedback(recorder, "MISSION_CURRENT", 1, 3.0)
    window(recorder, 3.0)
    if attach:
        recorder.attach_observation(1, "before", observation(1, 2.0))
        recorder.attach_observation(1, "after", observation(2, 4.0))
    return recorder


def test_one_upload_late_before_not_relabelled_as_per_leg_submission():
    result = settled().close()
    interval = result["intervals"][0]
    assert result["parent_submission"]["submitted_at_s"] == 0.5
    assert interval["before"]["at_s"] == 2.0 > 0.5
    assert interval["status"] == "settled_interval_observed"
    assert "submitted" not in interval and "teacher_target" not in interval
    assert result["events"][0]["message"]["message_type"] == "MISSION_CURRENT"
    assert interval["before_window"]["state_indices"] == [0, 1, 2, 3, 4]
    assert interval["after_window"]["state_indices"] == [5, 6, 7, 8, 9]
    assert result["counts"] == {
        "feedback_events": 3,
        "state_observations": 10,
        "settled_intervals": 1,
    }
    assert not any(result["claims"].values())
    assert interval["before"]["pose"]["world_origin_id"] != parent().local_origin_id


def test_reached_alone_does_not_make_settled_endpoint():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    feedback(recorder, "MISSION_CURRENT", 1, 1.0)
    feedback(recorder, "MISSION_ITEM_REACHED", 1, 2.0)
    result = recorder.close()
    assert result["valid"]
    assert result["intervals"][0]["status"] == "incomplete"
    assert result["intervals"][0]["after_window"] is None
    assert result["intervals"][0]["after"] is None


def test_fly_through_next_current_does_not_attach_previous_leg_after_window():
    recorder = intervals.MissionIntervalRecorder(parent(), (1, 2))
    window(recorder)
    feedback(recorder, "MISSION_CURRENT", 1, 2.1)
    feedback(recorder, "MISSION_ITEM_REACHED", 1, 2.2)
    feedback(recorder, "MISSION_CURRENT", 2, 2.3)
    window(recorder, 3.0)
    assert recorder.snapshot()["intervals"][0]["after_window"] is None


def test_duplicate_current_preserves_first_activation_and_before_window():
    recorder = settled(attach=False)
    result = recorder.snapshot()
    assert result["intervals"][0]["activation_event"] == 0
    assert result["events"][0]["received_at_s"] == 2.1
    assert result["events"][2]["received_at_s"] == 3.0


@pytest.mark.parametrize(
    "change",
    [
        {"expected_system_id": True},
        {"expected_component_id": False},
        {"expected_system_id": 0},
        {"expected_component_id": 256},
        {"item_count": True},
        {"submitted_at_s": float("nan")},
        {"mission_sha256": "A" * 64},
        {"clock_id": " "},
    ],
)
def test_parent_strict_types_and_identity(change):
    with pytest.raises((ValueError, TypeError)):
        replace(parent(), **change)


def test_parent_round_trip_extra_fields_and_source_pointer():
    raw = parent().to_mapping()
    assert intervals.MissionParentSubmission.from_mapping(raw).to_mapping() == raw
    with pytest.raises(ValueError):
        intervals.MissionParentSubmission.from_mapping({**raw, "authorized": True})
    with pytest.raises(TypeError):
        replace(parent(), source=binding(pointer=None))


@pytest.mark.parametrize("sequences", [(1, 1), (2, 1), (5,), (True,), (), [1]])
def test_tracked_sequences_strict(sequences):
    with pytest.raises((ValueError, TypeError)):
        intervals.MissionIntervalRecorder(parent(), sequences)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"clock_id": "px4_boot_ms"},
        {"transaction_id": "another-upload"},
        {"received_at_s": 0.4},
        {"received_at_s": True},
        {"received_at_s": float("inf")},
        {"packet_sha256": "invalid"},
        {"source": binding(pointer=None)},
    ],
)
def test_relevant_bad_envelope_latches_invalid(kwargs):
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    args = {
        "received_at_s": 1.0,
        "clock_id": CLOCK,
        "transaction_id": TRANSACTION,
        "source": binding(),
    }
    args.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        recorder.record_feedback(message("MISSION_CURRENT", seq=1), **args)
    assert not recorder.snapshot()["valid"]
    with pytest.raises(ValueError, match="closed or invalid"):
        feedback(recorder, "MISSION_CURRENT", 1, 2.0)


@pytest.mark.parametrize(
    "fields",
    [
        {"seq": True},
        {"seq": 5},
        {"source_system_id": 2},
        {"source_component_id": 2},
        {"source_component_id": True},
        {"extra_raw_field": float("nan")},
        {"extra_raw_field": "x" * 5000},
    ],
)
def test_raw_message_invalid_rejected(fields):
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    raw = message("MISSION_CURRENT", seq=1)
    raw.update(fields)
    with pytest.raises((ValueError, TypeError)):
        recorder.record_feedback(
            raw, received_at_s=1.0, clock_id=CLOCK, transaction_id=TRANSACTION, source=binding()
        )
    assert not recorder.snapshot()["valid"]


def test_unrelated_message_has_no_effect():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    assert not recorder.record_feedback(
        {"message_type": "HEARTBEAT"},
        received_at_s=-1,
        clock_id="other",
        transaction_id="other",
        source=None,
    )
    assert recorder.snapshot()["valid"]
    assert recorder.snapshot()["events"] == []


@pytest.mark.parametrize(
    "events",
    [
        [("MISSION_ITEM_REACHED", 1, 1.0)],
        [("MISSION_CURRENT", 1, 1.0), ("MISSION_ITEM_REACHED", 2, 1.1)],
        [("MISSION_CURRENT", 2, 1.0), ("MISSION_CURRENT", 1, 1.1)],
        [
            ("MISSION_CURRENT", 2, 1.0),
            ("MISSION_ITEM_REACHED", 2, 1.1),
            ("MISSION_ITEM_REACHED", 1, 1.2),
        ],
        [("MISSION_CURRENT", 1, 1.0), ("MISSION_CURRENT", 1, 0.9)],
    ],
)
def test_sequence_or_host_time_regression(events):
    recorder = intervals.MissionIntervalRecorder(parent(), (1, 2))
    with pytest.raises(ValueError):
        for kind, seq, stamp in events:
            feedback(recorder, kind, seq, stamp)
    assert not recorder.close()["valid"]


@pytest.mark.parametrize(
    "changes",
    [
        {"x": float("nan")},
        {"vy": float("inf")},
        {"vz": True},
        {"time_boot_ms": True},
        {"time_boot_ms": -1},
        {"message_type": "planned_position"},
    ],
)
def test_bad_state_not_observation(changes):
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    with pytest.raises((ValueError, TypeError)):
        state(recorder, 1.0, **changes)
    assert not recorder.snapshot()["valid"]


@pytest.mark.parametrize("reset", ["boot", "jump", "source", "origin"])
def test_source_boot_or_origin_reset_latched(reset):
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    state(recorder, 1.0)
    with pytest.raises(ValueError):
        if reset == "boot":
            state(recorder, 1.1, boot=999)
        elif reset == "jump":
            state(recorder, 1.1, xyz=(100, 0, -3))
        else:
            recorder.record_state(
                message("LOCAL_POSITION_NED", x=0, y=0, z=-3, vx=0, vy=0, vz=0, time_boot_ms=1100),
                received_at_s=1.1,
                clock_id=CLOCK,
                transaction_id=TRANSACTION,
                local_origin_id="other-origin" if reset == "origin" else parent().local_origin_id,
                source=binding(
                    source_id="other-receiver" if reset == "source" else "fixture-telemetry"
                ),
            )
    assert not recorder.snapshot()["valid"]


@pytest.mark.parametrize("condition", ["vertical_velocity", "position_span", "gap", "stale"])
def test_settlement_requires_position_and_full_3d_velocity_and_time(condition):
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    for index in range(5):
        stamp = 1.0 + index * (0.4 if condition == "gap" else 0.25)
        state(
            recorder,
            stamp,
            xyz=(index * 0.1 if condition == "position_span" else 0, 0, -3),
            velocity=(0, 0, 0.21 if condition == "vertical_velocity" else 0),
        )
    activation = 3.0 if condition in {"gap", "stale"} else 2.1
    feedback(recorder, "MISSION_CURRENT", 1, activation)
    assert recorder.snapshot()["intervals"][0]["before_window"] is None
    assert recorder.snapshot()["valid"]  # unstable is incomplete, not corrupt


def test_current_feedback_must_still_be_fresh_for_after_window():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    window(recorder)
    feedback(recorder, "MISSION_CURRENT", 1, 2.1)
    feedback(recorder, "MISSION_ITEM_REACHED", 1, 3.0)
    window(recorder, 3.0)
    assert recorder.snapshot()["intervals"][0]["after_window"] is None


def test_replayed_same_boot_sample_cannot_manufacture_stable_window_with_host_waits():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    with pytest.raises(ValueError, match="duplicate"):
        for index in range(5):
            state(recorder, 1.0 + index * 0.25, boot=1000)
    result = recorder.close()
    assert not result["valid"]
    assert len(result["states"]) == 1
    assert result["intervals"][0]["before_window"] is None


def test_short_source_duration_cannot_be_stretched_by_host_receive_clock():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    for index in range(5):
        state(recorder, 1.0 + index * 0.25, boot=1000 + index)
    feedback(recorder, "MISSION_CURRENT", 1, 2.1)
    assert recorder.snapshot()["intervals"][0]["before_window"] is None
    assert recorder.snapshot()["valid"]


def test_boot_duration_separate_from_host_time_no_epoch_alignment():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    for index in range(5):
        state(recorder, 1.0 + index * 0.25, boot=8_000_000 + index * 250)
    feedback(recorder, "MISSION_CURRENT", 1, 2.1)
    result = recorder.snapshot()
    assert result["intervals"][0]["before_window"]["px4_boot_duration_ms"] == 1000
    assert result["states"][0]["received_at_s"] == 1.0
    assert result["states"][0]["message"]["time_boot_ms"] == 8_000_000
    assert not result["claims"]["sensor_clock_mapping_verified"]


@pytest.mark.parametrize(
    "condition",
    [
        "no_window",
        "wrong_clock",
        "after_at_reached",
        "pose_skew",
        "future_pose",
        "duplicate_endpoint",
        "camera_order",
        "pose_origin",
    ],
)
def test_endpoint_attachment_cannot_invent_settlement(condition):
    recorder = settled(attach=False)
    before, after = observation(1, 2.0), observation(2, 4.0)
    if condition == "no_window":
        recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    elif condition == "wrong_clock":
        before = observation(1, 2.0, clock_id="sensor-clock-not-host")
    elif condition == "after_at_reached":
        after = observation(2, 3.0)  # Reached is not the later confirmed settled endpoint
    elif condition == "pose_skew":
        before = replace(before, pose=replace(before.pose, at_s=1.5))
    elif condition == "future_pose":
        before = replace(before, pose=replace(before.pose, at_s=2.05))
    elif condition == "camera_order":
        after = observation(1, 4.0)
    elif condition == "pose_origin":
        after = observation(2, 4.0, origin="different-gazebo-origin")
    with pytest.raises((ValueError, TypeError)):
        recorder.attach_observation(1, "before", before)
        if condition == "duplicate_endpoint":
            recorder.attach_observation(1, "before", before)
        else:
            recorder.attach_observation(1, "after", after)
    assert not recorder.snapshot()["valid"]


def test_snapshot_is_deep_copy_input_mapping_is_original_value_preserved():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    raw = message("MISSION_CURRENT", seq=1, actual_extra_raw_counter=2**63 + 123)
    recorder.record_feedback(
        raw,
        received_at_s=1.0,
        clock_id=CLOCK,
        transaction_id=TRANSACTION,
        source=binding(),
        packet_sha256=SHA,
    )
    raw["seq"] = 3
    first = recorder.snapshot()
    assert first["events"][0]["message"]["actual_extra_raw_counter"] == 2**63 + 123
    first["events"][0]["message"]["seq"] = 4
    assert recorder.snapshot()["events"][0]["message"]["seq"] == 1
    assert recorder.snapshot()["events"][0]["packet_sha256"] == SHA


def test_integer_receive_times_not_reserialized_as_projected_floats():
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    state(recorder, 1)
    feedback(recorder, "MISSION_CURRENT", 1, 2)
    result = recorder.snapshot()
    assert type(result["states"][0]["received_at_s"]) is int
    assert type(result["events"][0]["received_at_s"]) is int


@pytest.mark.parametrize("capacity", ["events", "states"])
def test_capacity_failure_not_silent_truncation(monkeypatch, capacity):
    recorder = intervals.MissionIntervalRecorder(parent(), (1,))
    monkeypatch.setattr(intervals, "MAX_EVENTS" if capacity == "events" else "MAX_STATES", 1)
    with pytest.raises(ValueError, match="capacity"):
        if capacity == "events":
            feedback(recorder, "MISSION_CURRENT", 1, 1.0)
            feedback(recorder, "MISSION_CURRENT", 1, 2.0)
        else:
            state(recorder, 1.0)
            state(recorder, 2.0)
    result = recorder.close()
    assert not result["valid"] and not any(result["claims"].values())


def test_closed_recorder_no_reopen_and_missing_endpoint_truthful():
    recorder = settled(attach=False)
    result = recorder.close()
    assert result["closed"]
    assert result["intervals"][0]["missing"] == ["before", "after"]
    with pytest.raises(ValueError, match="closed"):
        state(recorder, 5.0)


def test_import_quiet_stdlib_only_and_no_i_o_or_control_api():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        f"sys.path[:0]=[{str(root)!r},{str(root / 'src')!r}]; "
        "from scripts import onboard_teacher_mission_intervals as m; "
        "assert not any(n.split('.')[0] in {'torch','PIL','numpy','pymavlink','transformers'} "
        "for n in sys.modules); "
        "assert not any(hasattr(m,n) for n in ('send','arm','upload','build_actor','main'))"
    )
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert process.returncode == 0, process.stderr


def test_raw_snapshot_strict_json_and_policy_no_nan_bool():
    raw = settled().snapshot()
    encoded = intervals.canonical_json(raw)
    assert intervals.strict_json_loads(encoded) == raw
    assert copy.deepcopy(raw) == raw
    for change in (
        {"minimum_samples": True},
        {"maximum_speed_mps": float("nan")},
        {"minimum_duration_s": 0.01},
        {"maximum_pose_skew_s": -1},
    ):
        with pytest.raises((ValueError, TypeError)):
            replace(intervals.SettlementPolicy(), **change)
