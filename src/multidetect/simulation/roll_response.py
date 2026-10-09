"""Sampled target-history diagnostics; no inferred continuous command hold."""

import math
from bisect import bisect_left, bisect_right

from multidetect.simulation.attitude_target import decode_attitude_target


def _angle_difference(a, b):
    return (a - b + 180) % 360 - 180


def _stream(capture, name):
    stream = capture["streams"][name]
    rows = stream["samples"]
    if not isinstance(rows, list) or not 2 <= len(rows) <= 32768:
        raise ValueError("bounded stream with at least two samples required")
    if (
        stream.get("truncated") is not False
        or type(stream.get("samples_dropped")) is not int
        or stream["samples_dropped"] != 0
        or type(stream.get("samples_seen")) is not int
        or stream["samples_seen"] != len(rows)
    ):
        raise ValueError("incomplete stream")
    previous_boot, previous_receipt = -1, -1
    for row in rows:
        boot, receipt = row.get("time_boot_ms"), row.get("received_at_s")
        if type(boot) is not int or not previous_boot < boot < 2**32:
            raise ValueError("invalid, duplicate or reset source clock")
        if (
            type(receipt) not in (int, float)
            or not math.isfinite(receipt)
            or receipt < max(0, previous_receipt)
        ):
            raise ValueError("invalid receipt clock")
        if any(
            type(row.get(k)) is not int or row[k] != 1
            for k in ("source_system_id", "source_component_id")
        ):
            raise ValueError("unexpected source")
        previous_boot, previous_receipt = boot, receipt
    return rows


def analyze_roll_response(
    capture, *, lookback_ms, maximum_gap_ms, maximum_target_deviation_deg, quaternion_norm_tolerance
):
    """Classify exact-time pairs by observed past targets, never future targets.

    A predecessor at/before the window start is required. Even a quiet sampled
    history does not prove that commands between messages were constant.
    """
    for value in (lookback_ms, maximum_gap_ms):
        if type(value) is not int or not 0 < value <= 60000:
            raise ValueError("explicit time window/gap in 1..60000ms required")
    if (
        type(maximum_target_deviation_deg) not in (int, float)
        or not math.isfinite(maximum_target_deviation_deg)
        or not 0 <= maximum_target_deviation_deg < 180
    ):
        raise ValueError("explicit angular deviation in [0,180) required")
    if (
        capture.get("schema_version") != 1
        or capture.get("clock") != "observer_relative_host_receive_time"
    ):
        raise ValueError("unsupported capture")
    units = capture.get("units", {})
    if (
        units.get("ATTITUDE", {}).get("roll") != "rad"
        or units.get("ATTITUDE_TARGET", {}).get("q") != "quaternion_wxyz"
    ):
        raise ValueError("explicit attitude units required")
    targets = _stream(capture, "ATTITUDE_TARGET")
    attitudes = _stream(capture, "ATTITUDE")
    for row in attitudes:
        roll = row.get("roll")
        if type(roll) not in (int, float) or not math.isfinite(roll) or abs(roll) > math.pi:
            raise ValueError("valid measured roll required")
    decoded = [
        decode_attitude_target(r, quaternion_norm_tolerance=quaternion_norm_tolerance)
        for r in targets
    ]
    target_rolls = [r.get("roll_deg") for r in decoded]
    by_time = {r["time_boot_ms"]: r for r in attitudes}
    times = [r["time_boot_ms"] for r in targets]
    results, target_samples = [], []
    for index, target in enumerate(targets):
        actual = by_time.get(times[index])
        start = bisect_right(times, times[index] - lookback_ms) - 1
        history = target_rolls[max(0, start) : index + 1]
        gaps = [
            b - a
            for a, b in zip(
                times[max(0, start) : index], times[max(0, start) + 1 : index + 1], strict=True
            )
        ]
        adequate = start >= 0 and bool(gaps) and max(gaps) <= maximum_gap_ms and None not in history
        deviation = (
            max(abs(_angle_difference(r, target_rolls[index])) for r in history)
            if adequate
            else None
        )
        classification = (
            "insufficient_history"
            if not adequate
            else (
                "sampled_near_current_target"
                if deviation <= maximum_target_deviation_deg
                else "sampled_changing_target"
            )
        )
        target_samples.append(
            dict(
                time_boot_ms=times[index],
                target_roll_deg=target_rolls[index],
                classification=(
                    classification
                    if target_rolls[index] is not None
                    else "unusable_target_attitude"
                ),
                exact_actual_source_label_present=actual is not None,
                maximum_observed_target_deviation_deg=deviation,
                target_received_at_s=target["received_at_s"],
                history_start_received_at_s=targets[max(0, start)]["received_at_s"],
            )
        )
        if actual is None or target_rolls[index] is None:
            continue
        results.append(
            dict(
                time_boot_ms=times[index],
                target_roll_deg=target_rolls[index],
                actual_roll_deg=math.degrees(actual["roll"]),
                roll_error_deg=_angle_difference(math.degrees(actual["roll"]), target_rolls[index]),
                classification=classification,
                maximum_observed_target_deviation_deg=deviation,
                target_received_at_s=target["received_at_s"],
                actual_received_at_s=actual["received_at_s"],
                history_start_received_at_s=targets[max(0, start)]["received_at_s"],
            )
        )
    return dict(
        samples=results,
        target_samples=target_samples,
        lookback_ms=lookback_ms,
        maximum_gap_ms=maximum_gap_ms,
        maximum_target_deviation_deg=maximum_target_deviation_deg,
        continuous_target_hold_proven=False,
        physical_simultaneity_proven=False,
        controller_acceptance_proven=False,
    )


def inspect_attitude_neighbors(capture):
    """Retain measured endpoints around target labels; never estimate between them.

    Endpoint spread is observed change, not a bound on an unobserved trajectory.
    Host receipt differences and source label offsets are kept separate.
    """
    if (
        capture.get("schema_version") != 1
        or capture.get("clock") != "observer_relative_host_receive_time"
    ):
        raise ValueError("unsupported capture")
    units = capture.get("units", {})
    if units.get("ATTITUDE", {}).get("roll") != "rad" or any(
        units.get(name, {}).get("time_boot_ms") != "source_boot_ms"
        for name in ("ATTITUDE", "ATTITUDE_TARGET")
    ):
        raise ValueError("explicit roll and source time units required")
    attitudes = _stream(capture, "ATTITUDE")
    targets = _stream(capture, "ATTITUDE_TARGET")
    for row in attitudes:
        roll = row.get("roll")
        if type(roll) not in (int, float) or not math.isfinite(roll) or abs(roll) > math.pi:
            raise ValueError("valid measured roll required")
    times = [row["time_boot_ms"] for row in attitudes]
    result = []
    for target in targets:
        time = target["time_boot_ms"]
        index = bisect_left(times, time)
        exact = index < len(times) and times[index] == time
        indices = [index] if exact else [index - 1, index]
        endpoints = [
            dict(
                time_boot_ms=times[i],
                source_offset_ms=times[i] - time,
                roll_deg=math.degrees(attitudes[i]["roll"]),
                received_at_s=attitudes[i]["received_at_s"],
                receipt_offset_s=attitudes[i]["received_at_s"] - target["received_at_s"],
            )
            for i in indices
            if 0 <= i < len(times)
        ]
        bracketed = not exact and len(endpoints) == 2
        result.append(
            dict(
                time_boot_ms=time,
                target_received_at_s=target["received_at_s"],
                status="exact_label"
                if exact
                else "bracketed_labels"
                if bracketed
                else "unbracketed",
                endpoints=endpoints,
                bracket_width_ms=endpoints[-1]["time_boot_ms"] - endpoints[0]["time_boot_ms"]
                if bracketed
                else None,
                observed_endpoint_roll_change_deg=_angle_difference(
                    endpoints[-1]["roll_deg"], endpoints[0]["roll_deg"]
                )
                if bracketed
                else None,
            )
        )
    return dict(
        samples=result,
        interpolation_performed=False,
        physical_simultaneity_proven=False,
        intermediate_error_bound_proven=False,
        controller_acceptance_proven=False,
    )
