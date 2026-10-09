import pytest
from test_mission_timing import mission_for

from multidetect.simulation.mission_phase_observations import summarize_mission_receipts


def event(at, seq, kind="MISSION_CURRENT", accepted=True):
    return dict(
        received_at_s=at,
        accepted=accepted,
        message=dict(
            message_type=kind,
            seq=seq,
            source_system_id=1,
            source_component_id=1,
        ),
    )


def test_skipped_index_and_duplicate_reached_do_not_create_phase_duration():
    mission = mission_for("vtol")
    result = summarize_mission_receipts(
        mission,
        [
            event(1, 0),
            event(2, 0),
            event(3, 1, "MISSION_ITEM_REACHED"),
            event(4, 2),
            event(5, 1, "MISSION_ITEM_REACHED"),
            event(6, 2),
        ],
        observer_duration_s=10,
    )
    assert result["missing_current_sequences"] == [1, 3, 4, 5, 6]
    assert [r["receive_span_s"] for r in result["current_runs"]] == [1, 2]
    assert result["current_change_receipts"][0]["receive_gap_s"] == 2
    assert result["current_change_receipts"][0]["adjacent_forward"] is False
    assert result["reached_receipts"] == [
        dict(
            sequence=1,
            first_received_s=3,
            last_received_s=5,
            count=2,
        )
    ]
    assert result["training_phase_labels_eligible"] is False


def test_regression_and_rejected_events_remain_visible_without_merging_runs():
    result = summarize_mission_receipts(
        mission_for("vtol"),
        [
            event(1, 0),
            event(2, 2),
            event(3, 1, accepted=False),
            event(4, 0),
        ],
        observer_duration_s=5,
    )
    assert [r["sequence"] for r in result["current_runs"]] == [0, 2, 0]
    assert result["rejected_events_excluded"] == 1
    assert result["current_change_receipts"][-1]["adjacent_forward"] is False


@pytest.mark.parametrize(
    "events",
    [
        [event(2, 0), event(1, 1)],
        [event(-1, 0)],
        [event(11, 0)],
        [event(float("nan"), 0)],
        [event(True, 0)],
        [event(1, True)],
        [event(1, 100)],
        [event(1, -1)],
        [event(1, 0, accepted=1)],
    ],
)
def test_malformed_events_rejected(events):
    with pytest.raises(ValueError):
        summarize_mission_receipts(mission_for("vtol"), events, observer_duration_s=10)


def test_accepted_wrong_source_rejected():
    wrong = event(1, 0)
    wrong["message"]["source_system_id"] = 2
    with pytest.raises(ValueError, match="source mismatch"):
        summarize_mission_receipts(mission_for("vtol"), [wrong], observer_duration_s=10)


def test_empty_history_has_no_phase_coverage():
    result = summarize_mission_receipts(mission_for("vtol"), [], observer_duration_s=10)
    assert result["missing_current_sequences"] == list(range(7))
    assert result["current_runs"] == []


def extended(at, state):
    item = event(at, 0, "EXTENDED_SYS_STATE")
    del item["message"]["seq"]
    item["message"]["vtol_state"] = state
    return item


def test_vtol_state_runs_preserve_transitions_without_inventing_durations():
    result = summarize_mission_receipts(
        mission_for("vtol"),
        [
            extended(1, 3),
            extended(2, 3),
            extended(3, 1),
            extended(4, 4),
            extended(5, 2),
            extended(6, 3),
        ],
        observer_duration_s=10,
    )
    assert [r["vtol_state"] for r in result["vtol_state_runs"]] == [3, 1, 4, 2, 3]
    assert result["vtol_state_runs"][0]["receive_span_s"] == 1
    assert result["vtol_state_runs"][1]["sample_count"] == 1
    assert result["source_transition_bounds_proven"] is False


def test_legacy_missing_vtol_field_stays_unknown():
    item = extended(1, 3)
    del item["message"]["vtol_state"]
    result = summarize_mission_receipts(mission_for("vtol"), [item], observer_duration_s=10)
    assert result["vtol_state_runs"] == []
    assert result["extended_state_missing_vtol_field_count"] == 1


@pytest.mark.parametrize("bad", [True, -1, 5, None, "3"])
def test_invalid_vtol_state_rejected(bad):
    with pytest.raises(ValueError, match="MAV_VTOL_STATE"):
        summarize_mission_receipts(mission_for("vtol"), [extended(1, bad)], observer_duration_s=10)
