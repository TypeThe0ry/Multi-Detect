"""Bracket diagnostic receipt windows with mode observations, not source phases."""

import math
from bisect import bisect_left, bisect_right


def select_mode_bracketed_samples(
    events, samples, *, required_state, maximum_gap_s, include_actual_receipt=True
):
    if type(include_actual_receipt) is not bool:
        raise ValueError("explicit receipt selection required")
    if type(required_state) is not int or required_state not in range(5):
        raise ValueError("valid VTOL state required")
    if (
        type(maximum_gap_s) not in (int, float)
        or not math.isfinite(maximum_gap_s)
        or maximum_gap_s <= 0
    ):
        raise ValueError("positive finite receive gap required")
    if not isinstance(events, list) or len(events) > 100000:
        raise ValueError("bounded events required")
    if not isinstance(samples, list) or len(samples) > 32768:
        raise ValueError("bounded samples required")
    modes, previous = [], -1
    for event in events:
        at = event.get("received_at_s")
        if type(at) not in (int, float) or not math.isfinite(at) or at < max(0, previous):
            raise ValueError("ordered finite events required")
        previous = at
        if type(event.get("accepted")) is not bool:
            raise ValueError("explicit accepted flag required")
        if not event["accepted"] or event["message"]["message_type"] != "EXTENDED_SYS_STATE":
            continue
        msg = event["message"]
        if any(
            type(msg.get(k)) is not int or msg[k] != 1
            for k in ("source_system_id", "source_component_id")
        ):
            raise ValueError("unexpected accepted source")
        state = msg.get("vtol_state")
        # Missing state invalidates coverage; it must not be skipped across.
        state = state if type(state) is int and state in range(5) else None
        modes.append((at, state))
    times = [m[0] for m in modes]
    selected, rejected = [], 0
    for sample in samples:
        keys = ("history_start_received_at_s", "target_received_at_s")
        if include_actual_receipt:
            keys += ("actual_received_at_s",)
        receipts = [sample.get(k) for k in keys]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in receipts):
            raise ValueError("finite sample/history receipts required")
        left = bisect_left(times, min(receipts)) - 1
        right = bisect_right(times, max(receipts))
        bracket = modes[max(0, left) : right + 1]
        valid = left >= 0 and right < len(modes) and all(s == required_state for _, s in bracket)
        valid = valid and all(
            0 < b[0] - a[0] <= maximum_gap_s for a, b in zip(bracket, bracket[1:], strict=False)
        )
        if valid:
            selected.append(dict(sample))
        else:
            rejected += 1
    return dict(
        samples=selected,
        rejected_samples=rejected,
        required_state=required_state,
        maximum_gap_s=maximum_gap_s,
        source_phase_boundaries_proven=False,
        physical_mode_continuity_proven=False,
    )
