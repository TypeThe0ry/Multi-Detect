"""Summarize received Mission state without inventing source-time phase labels."""

import math

from multidetect.autonomy.contracts import ExecutableMission


def summarize_mission_receipts(
    mission: ExecutableMission,
    events: list[dict],
    *,
    observer_duration_s: float,
    source_system: int = 1,
    source_component: int = 1,
) -> dict:
    """Use observer-relative receive times, not aircraft execution timestamps.

    A skipped CURRENT index is missing, not a zero-duration phase. A REACHED
    event is retained independently; it is not used to fill a CURRENT gap.
    """
    if not isinstance(mission, ExecutableMission):
        raise ValueError("ExecutableMission required")
    if (
        type(observer_duration_s) not in (int, float)
        or not math.isfinite(observer_duration_s)
        or observer_duration_s < 0
    ):
        raise ValueError("finite nonnegative observer duration required")
    if not isinstance(events, list) or len(events) > 100_000:
        raise ValueError("bounded event list required")
    for value in (source_system, source_component):
        if type(value) is not int or not 1 <= value <= 255:
            raise ValueError("source ids must be integers in 1..255")
    runs, reached, changes = [], {}, []
    vtol_runs = []
    missing_vtol_field = 0
    previous_time = 0.0
    rejected_count = 0
    for event in events:
        at = event["received_at_s"]
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or not previous_time <= at <= observer_duration_s
        ):
            raise ValueError("event times must be finite, ordered and inside observer window")
        previous_time = at
        if type(event["accepted"]) is not bool:
            raise ValueError("accepted must be boolean")
        if not event["accepted"]:
            rejected_count += 1
            continue
        message = event["message"]
        kind = message["message_type"]
        if kind not in {"MISSION_CURRENT", "MISSION_ITEM_REACHED", "EXTENDED_SYS_STATE"}:
            continue
        if (
            type(message.get("source_system_id")) is not int
            or type(message.get("source_component_id")) is not int
            or message.get("source_system_id") != source_system
            or message.get("source_component_id") != source_component
        ):
            raise ValueError("accepted mission event source mismatch")
        if kind == "EXTENDED_SYS_STATE":
            if "vtol_state" not in message:
                missing_vtol_field += 1
                continue
            state = message["vtol_state"]
            if type(state) is not int or not 0 <= state <= 4:
                raise ValueError("invalid MAV_VTOL_STATE")
            if not vtol_runs or vtol_runs[-1]["vtol_state"] != state:
                vtol_runs.append(
                    dict(vtol_state=state, first_received_s=at, last_received_s=at, sample_count=0)
                )
            vtol_runs[-1]["last_received_s"] = at
            vtol_runs[-1]["sample_count"] += 1
            continue
        seq = message["seq"]
        if type(seq) is not int or not 0 <= seq < len(mission.items):
            raise ValueError("mission sequence outside bound document")
        if kind == "MISSION_ITEM_REACHED":
            if seq not in reached:
                reached[seq] = dict(sequence=seq, first_received_s=at, last_received_s=at, count=0)
            reached[seq]["last_received_s"] = at
            reached[seq]["count"] += 1
            continue
        if not runs or runs[-1]["sequence"] != seq:
            if runs:
                old = runs[-1]
                changes.append(
                    dict(
                        from_sequence=old["sequence"],
                        to_sequence=seq,
                        previous_last_received_s=old["last_received_s"],
                        next_first_received_s=at,
                        receive_gap_s=at - old["last_received_s"],
                        adjacent_forward=seq == old["sequence"] + 1,
                    )
                )
            runs.append(
                dict(
                    sequence=seq,
                    command=mission.items[seq].command.value,
                    first_received_s=at,
                    last_received_s=at,
                    sample_count=0,
                )
            )
        runs[-1]["last_received_s"] = at
        runs[-1]["sample_count"] += 1
    for run in runs + vtol_runs:
        run["receive_span_s"] = run["last_received_s"] - run["first_received_s"]
    seen = {run["sequence"] for run in runs}
    return dict(
        schema_version=1,
        executable_mission_hash=mission.canonical_hash,
        clock_semantics="observer_relative_host_receive_time",
        observer_duration_s=observer_duration_s,
        current_runs=runs,
        vtol_state_runs=vtol_runs,
        extended_state_missing_vtol_field_count=missing_vtol_field,
        current_change_receipts=changes,
        reached_receipts=list(reached.values()),
        missing_current_sequences=sorted(set(range(len(mission.items))) - seen),
        rejected_events_excluded=rejected_count,
        source_transition_bounds_proven=False,
        execution_phase_durations_proven=False,
        training_phase_labels_eligible=False,
    )
