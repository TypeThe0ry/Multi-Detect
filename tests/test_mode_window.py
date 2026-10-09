import pytest

from multidetect.simulation.mode_window import select_mode_bracketed_samples


def event(at, state=4):
    return dict(
        received_at_s=at,
        accepted=True,
        message=dict(
            message_type="EXTENDED_SYS_STATE",
            source_system_id=1,
            source_component_id=1,
            vtol_state=state,
        ),
    )


def sample(start=0.1, target=0.5, actual=0.51):
    return dict(
        history_start_received_at_s=start, target_received_at_s=target, actual_received_at_s=actual
    )


def select(events, row=None):
    return select_mode_bracketed_samples(
        events, [row or sample()], required_state=4, maximum_gap_s=0.5
    )


def test_history_and_both_receipts_need_strict_brackets():
    events = [event(0), event(0.25), event(0.5), event(0.75)]
    assert len(select(events)["samples"]) == 1
    assert select(events, sample(start=0))["samples"] == []
    assert select(events, sample(actual=0.75))["samples"] == []


@pytest.mark.parametrize("state", [1, 2, 3, None, True])
def test_mode_change_or_missing_state_in_history_rejects(state):
    assert select([event(0), event(0.25, state), event(0.5), event(0.75)])["samples"] == []


def test_gaps_and_duplicate_receipts_reject_coverage():
    assert select([event(0), event(0.75)])["samples"] == []
    assert select([event(0), event(0.25), event(0.25), event(0.5), event(0.75)])["samples"] == []


def test_invalid_order_and_foreign_source_rejected():
    with pytest.raises(ValueError):
        select([event(0.5), event(0)])
    foreign = event(0)
    foreign["message"]["source_system_id"] = 2
    with pytest.raises(ValueError):
        select([foreign, event(0.25)])


def test_wrong_sample_clock_rejected():
    with pytest.raises(ValueError):
        select([event(0), event(0.25)], sample(start=True))


def test_target_only_scope_does_not_invent_actual_receipt():
    row = sample()
    del row["actual_received_at_s"]
    result = select_mode_bracketed_samples(
        [event(0), event(0.25), event(0.5), event(0.75)],
        [row],
        required_state=4,
        maximum_gap_s=0.5,
        include_actual_receipt=False,
    )
    assert result["samples"] == [row]
    with pytest.raises(ValueError):
        select([event(0), event(0.75)], row)
