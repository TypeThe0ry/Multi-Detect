"""Exact reported boot-millisecond joins, not sensor synchronization proof."""

import math

from multidetect.simulation.position_trajectory import summarize_position_capture


def join_attitude_positions(flight: dict) -> dict:
    """Join streams from one caller-bound flight report without interpolation.

    Equal MAVLink boot timestamps are observable labels; they do not establish
    that different estimators sampled the physical state simultaneously.
    """
    positions = flight["phase_position_observations"]
    summarize_position_capture(positions)
    dynamics = flight["phase_dynamics_observations"]
    if dynamics.get("schema_version") != 1 or dynamics.get("clock") != positions["clock"]:
        raise ValueError("incompatible dynamics capture")
    units = dynamics.get("units", {}).get("ATTITUDE", {})
    if units.get("roll") != "rad" or units.get("time_boot_ms") != "source_boot_ms":
        raise ValueError("explicit attitude units required")
    stream = dynamics["streams"]["ATTITUDE"]
    rows = stream.get("samples")
    if not isinstance(rows, list) or not 2 <= len(rows) <= 32768:
        raise ValueError("2..32768 attitude samples required")
    if (
        stream.get("truncated") is not False
        or type(stream.get("samples_dropped")) is not int
        or stream["samples_dropped"] != 0
        or type(stream.get("samples_seen")) is not int
        or stream["samples_seen"] != len(rows)
    ):
        raise ValueError("incomplete attitude stream")
    previous_boot, previous_receipt = -1, -1
    for row in rows:
        if row.get("invalid_or_missing_fields") != []:
            raise ValueError("missing attitude fields")
        if any(
            type(row.get(k)) is not int or row[k] != 1
            for k in ("source_system_id", "source_component_id")
        ):
            raise ValueError("unexpected attitude source")
        boot, at = row.get("time_boot_ms"), row.get("received_at_s")
        if type(boot) is not int or not previous_boot < boot < 2**32:
            raise ValueError("invalid/reset/duplicate attitude boot timestamp")
        if type(at) not in (int, float) or not math.isfinite(at) or at < max(0, previous_receipt):
            raise ValueError("invalid/reordered attitude receipt")
        for name in ("roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed"):
            value = row.get(name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("finite attitude field required")
        if any(abs(row[name]) > math.pi for name in ("roll", "pitch", "yaw")):
            raise ValueError("attitude angle outside MAVLink range")
        previous_boot, previous_receipt = boot, at
    by_boot = {row["time_boot_ms"]: row for row in rows}
    matches = []
    for position in positions["samples"]:
        attitude = by_boot.get(position["time_boot_ms"])
        if attitude is None:
            continue
        matches.append(
            dict(
                time_boot_ms=position["time_boot_ms"],
                position_received_at_s=position["received_at_s"],
                attitude_received_at_s=attitude["received_at_s"],
                receipt_difference_s=position["received_at_s"] - attitude["received_at_s"],
                roll_deg=math.degrees(attitude["roll"]),
                latitude_deg=position["lat"] / 1e7,
                longitude_deg=position["lon"] / 1e7,
                relative_altitude_m=position["relative_alt"] / 1000,
                horizontal_groundspeed_mps=math.hypot(position["vx"], position["vy"]) / 100,
            )
        )
    return dict(
        join_key="exact_source_system_component_boot_millisecond",
        matches=matches,
        matched_count=len(matches),
        unmatched_positions=len(positions["samples"]) - len(matches),
        unmatched_attitudes=len(rows) - len(matches),
        physical_simultaneity_proven=False,
        source_host_clock_mapping_proven=False,
        flight_tracking_gate_passed=False,
        interpolated=False,
    )
