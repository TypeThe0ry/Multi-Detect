"""Source-boot and receive-time trajectory diagnostics, not physical calibration."""

import math
from bisect import bisect_left, bisect_right


def summarize_position_capture(capture: dict) -> dict:
    if capture.get("schema_version") != 1 or capture.get("message_type") != "GLOBAL_POSITION_INT":
        raise ValueError("unsupported position capture")
    if capture.get("clock") != "observer_relative_host_receive_time":
        raise ValueError("unknown receipt clock")
    rows = capture.get("samples")
    if not isinstance(rows, list) or not 2 <= len(rows) <= 32768:
        raise ValueError("2..32768 position samples required")
    if capture.get("truncated") is not False or capture.get("samples_dropped") != 0:
        raise ValueError("truncated position capture")
    if type(capture.get("samples_seen")) is not int or capture["samples_seen"] != len(rows):
        raise ValueError("position sample count mismatch")
    for row in rows:
        if row.get("missing_integer_fields") != []:
            raise ValueError("missing position fields")
        for name in ("time_boot_ms", "lat", "lon", "relative_alt", "vx", "vy", "vz"):
            if type(row.get(name)) is not int:
                raise ValueError("raw integer position field required")
        if not 0 <= row["time_boot_ms"] < 2**32:
            raise ValueError("invalid source boot time")
        if not -(2**31) <= row["relative_alt"] < 2**31 or any(
            not -(2**15) <= row[name] < 2**15 for name in ("vx", "vy", "vz")
        ):
            raise ValueError("position fields exceed MAVLink integer widths")
        if not -900000000 <= row["lat"] <= 900000000 or not -1800000000 <= row["lon"] <= 1800000000:
            raise ValueError("invalid coordinate")
        if any(
            type(row.get(k)) is not int or row[k] != 1
            for k in ("source_system_id", "source_component_id")
        ):
            raise ValueError("unexpected position source")
        at = row.get("received_at_s")
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            raise ValueError("invalid receipt time")
    receive_gaps, boot_gaps = [], []
    integrated_down_m = 0.0
    for left, right in zip(rows[:-1], rows[1:], strict=True):
        boot_gap = (right["time_boot_ms"] - left["time_boot_ms"]) / 1000
        receive_gap = right["received_at_s"] - left["received_at_s"]
        if boot_gap <= 0 or receive_gap < 0:
            raise ValueError("source boot reset/duplicate or receipt reordering")
        receive_gaps.append(receive_gap)
        boot_gaps.append(boot_gap)
        integrated_down_m += (left["vz"] + right["vz"]) / 200 * boot_gap
    observed_down = (rows[0]["relative_alt"] - rows[-1]["relative_alt"]) / 1000
    return dict(
        schema_version=1,
        samples=len(rows),
        first_sample=dict(rows[0]),
        last_sample=dict(rows[-1]),
        source_boot_span_s=(rows[-1]["time_boot_ms"] - rows[0]["time_boot_ms"]) / 1000,
        receive_span_s=rows[-1]["received_at_s"] - rows[0]["received_at_s"],
        maximum_source_gap_s=max(boot_gaps),
        maximum_receive_gap_s=max(receive_gaps),
        altitude_min_m=min(row["relative_alt"] for row in rows) / 1000,
        altitude_max_m=max(row["relative_alt"] for row in rows) / 1000,
        observed_net_down_m=observed_down,
        source_time_trapezoidal_vz_integral_m=integrated_down_m,
        net_down_minus_vz_integral_m=observed_down - integrated_down_m,
        physical_accuracy_proven=False,
        continuous_coverage_proven=False,
        source_host_clock_mapping_proven=False,
        training_label_eligible=False,
    )


def bracket_position_receipt(capture: dict, received_at_s: float) -> dict:
    """Return strict receive-time neighbors; never interpolate a source event pose.

    The caller must bind the capture and event to the same observer clock and
    Mission. Exact-time samples are reported separately, not treated as proof
    that separately received messages describe simultaneous aircraft state.
    """
    summarize_position_capture(capture)
    if (
        type(received_at_s) not in (float, int)
        or not math.isfinite(received_at_s)
        or received_at_s < 0
    ):
        raise ValueError("finite nonnegative event receipt required")
    rows = capture["samples"]
    times = [row["received_at_s"] for row in rows]
    left = bisect_left(times, received_at_s)
    right = bisect_right(times, received_at_s)
    before = dict(rows[left - 1]) if left > 0 else None
    after = dict(rows[right]) if right < len(rows) else None
    return dict(
        event_received_at_s=received_at_s,
        before=before,
        after=after,
        equal_receipt_sample_count=right - left,
        strictly_bracketed=before is not None and after is not None,
        receive_gap_s=after["received_at_s"] - before["received_at_s"]
        if before and after
        else None,
        source_event_position_bounds_proven=False,
    )
