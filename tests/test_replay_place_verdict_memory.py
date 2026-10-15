from __future__ import annotations

import pytest

from multidetect.autonomy.onboard_loop import LoopConfig
from scripts import replay_place_verdict_memory as replay


def _row(decision="reject_track", *, point=(10.0, 0.0), pose=(0.0, 0.0, -10.0), **labels):
    return {
        "obs_before": {"pose": {"local_ned_m": list(pose)}},
        "labels": {
            "onboard": {
                "tracks": [{"id": "t1", "point": point}],
                "slow_calls": [{"track_id": "t1", "decision": decision, "latency_s": 0.44}],
                **labels,
            }
        },
    }


@pytest.fixture(autouse=True)
def ground_positions(monkeypatch):
    monkeypatch.setattr(replay, "_track_position", lambda row, track: track["point"])


def test_baseline_identity_consensus_differs_from_exact_decision():
    rows = [_row(d) for d in ("approach_track", "confirm_target", "reject_track")]
    result = replay.replay_rows(rows, LoopConfig())
    assert result["slow_calls"] == result["placed_calls"] == 3
    assert result["distinct_places"] == 1
    assert result["calls_per_place"] == 3
    assert result["identity_consensus_comparable"] == 2
    assert result["identity_consensus_agree"] == 1
    assert result["decision_consensus_comparable"] == 1
    assert result["decision_consensus_agree"] == 0
    assert result["skipped_calls"] == 0


def test_replay_does_not_leak_recorded_disagreement_into_memory():
    rows = [_row() for _ in range(3)] + [_row("confirm_target"), _row()]
    result = replay.replay_rows(rows, LoopConfig(memory_enabled=True))
    assert result["skipped_calls"] == 2
    assert len(result["disagreements"]) == 1
    assert result["disagreements"][0]["remembered"] == "reject_track"
    assert result["disagreements"][0]["recorded"] == "confirm_target"
    assert result["disagreements"][0]["identity_agrees"] is False
    assert result["skipped_latency_s"] == 0.88


def test_task_changes_reset_memory_but_integer_frame_steps_do_not():
    rows = [_row(step="s1") for _ in range(3)] + [_row(step="s2")]
    assert replay.replay_rows(rows, LoopConfig(memory_enabled=True))["skipped_calls"] == 0
    assert replay.replay_rows(rows, LoopConfig())["distinct_places"] == 2
    rows = [_row(step=i) for i in range(4)]
    assert replay.replay_rows(rows, LoopConfig(memory_enabled=True))["skipped_calls"] == 1


def test_transition_call_belongs_to_completed_task_before_memory_reset():
    rows = [_row(step="s1") for _ in range(3)]
    history = [{"step": "s1", "skill": "count", "state": "succeeded"}]
    rows += [
        _row(step="s2", skill="return_home", state_before="count", mission={"history": history}),
        _row(step="s2", mission={"history": history}),
    ]
    result = replay.replay_rows(rows, LoopConfig(memory_enabled=True))
    assert result["skipped_calls"] == 1
    assert result["skips"][0]["task"] == "s1"
    assert result["distinct_places"] == 2


def test_previous_focus_inspect_and_second_look_stay_fresh():
    for guard in ({"focus": "t1"}, {"skill": "inspect"}, {"second_look": {"phase": "fly"}}):
        rows = [_row("approach_track", **guard) for _ in range(4)]
        result = replay.replay_rows(rows, LoopConfig(memory_enabled=True))
        assert result["skipped_calls"] == 0


def test_unknown_and_unprojectable_calls_remain_explicit():
    rows = [_row(point=None), _row(decision=None), _row()]
    result = replay.replay_rows(rows, LoopConfig(memory_enabled=True))
    assert result["slow_calls"] == 3
    assert result["unplaced_calls"] == 1
    assert result["placed_calls"] == 2
    assert result["unknown_identity_calls"] == 1
    assert result["identity_consensus_comparable"] == 0


def test_already_remembered_labels_are_not_counted_as_real_calls():
    rows = [_row()]
    rows[0]["labels"]["onboard"]["slow_calls"][0]["remembered"] = True
    result = replay.replay_rows(rows, LoopConfig(memory_enabled=True))
    assert result["already_remembered_calls"] == 1
    assert result["slow_calls"] == result["skipped_calls"] == 0
