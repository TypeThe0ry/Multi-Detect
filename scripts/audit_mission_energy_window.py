"""Read-only receive-window audit; does not assert physical clock synchronization."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from copy import deepcopy
from pathlib import Path

if __package__ in (None, ""):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))

from multidetect.clock import validate_host_identity
from multidetect.simulation.energy_replay import replay_energy_inputs
from multidetect.simulator.flight_model import SimulationEnergyProfile


def _receive_identity(report, history, window):
    """Bind every receive-time producer before subtracting absolute timestamps."""
    flight = report["flight"]
    observation = flight["post_mission_observation"]
    producers = (window, flight, observation, history)
    label = window.get("clock")
    if history.get("receive_clock") != label:
        raise ValueError("receive clock label mismatch")
    if label == "host_perf_counter_since_collector_start":
        identity = validate_host_identity(window.get("host_clock_identity"))
        for producer in producers[1:]:
            other = validate_host_identity(producer.get("host_clock_identity"))
            if other != identity:
                raise ValueError("receive clock identity mismatch")
        return deepcopy(identity)
    if label != "host_monotonic_since_collector_start":
        raise ValueError("unsupported receive clock")
    # Old reports omitted identity. Explicit contradictory/null identity is not
    # absence, and a QPC identity must never be downgraded by changing its label.
    legacy = {"source": "python.monotonic", "session_id": None}
    for producer in producers:
        if "host_clock_identity" in producer and producer["host_clock_identity"] != legacy:
            raise ValueError("legacy receive clock identity mismatch")
    return legacy


def _window_energy_envelope(samples, covered, start_s, end_s, profile):
    """Account whole boot intervals; never interpolate between clock domains.

    Touching either receive boundary is ambiguous, including equal timestamps.
    This envelope is conditional on the replay load model, not a physical bound.
    """
    first_boot, last_boot = covered[0]["value"], covered[-1]["value"]
    held = {}
    previous = None
    inner_wh = boundary_wh = inner_s = boundary_s = 0.0
    inner_count = boundary_count = 0
    prefix_wh = 0.0
    for sample in samples:
        kind = sample["type"]
        if kind in ("HEARTBEAT", "VFR_HUD"):
            held[kind] = sample["value"]
            continue
        if kind != "GLOBAL_POSITION_INT" or sample["value"] < first_boot:
            continue
        if previous is not None:
            left, heartbeat, throttle = previous
            dt = (sample["value"] - left["value"]) / 1000
            wh = profile.consumed_wh(dt, armed=bool(heartbeat & 128), throttle=throttle / 100)
            prefix_wh += wh
            if left["at_s"] > start_s and sample["at_s"] < end_s:
                inner_wh += wh
                inner_s += dt
                inner_count += 1
            elif left["at_s"] <= end_s and sample["at_s"] >= start_s:
                boundary_wh += wh
                boundary_s += dt
                boundary_count += 1
        if sample["value"] == last_boot:
            break
        previous = (sample, held["HEARTBEAT"], held["VFR_HUD"])
    return {
        "lower_wh": inner_wh,
        "upper_wh": inner_wh + boundary_wh,
        "boundary_uncertainty_wh": boundary_wh,
        "strict_interior_intervals": inner_count,
        "boundary_intervals": boundary_count,
        "strict_interior_boot_s": inner_s,
        "boundary_boot_s": boundary_s,
        "checked_prefix_wh": prefix_wh,
        "boundary_policy": "strict_interior_lower_inclusive_touching_upper_no_interpolation",
        "assumption": "same causal left-load profile as replay; receive time attribution only",
        "physical_error_bound_claimed": False,
    }


def audit_window(report: dict, profile: SimulationEnergyProfile) -> dict:
    history = report["flight"]["post_mission_observation"]["energy_input_history"]
    window = report["mission_receive_window"]
    identity = _receive_identity(report, history, window)
    origin = history["receive_origin_host_monotonic_s"]
    arm = window["arm_request_host_monotonic_s"]
    end = window["completed_host_monotonic_s"]
    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in (origin, arm, end)):
        raise ValueError("finite host clock boundaries required")
    if not origin <= arm < end:
        raise ValueError("ordered nonempty mission window required")
    if window.get("receive_origin_host_monotonic_s") != origin:
        raise ValueError("receive clock origin mismatch")
    # Derive boundaries from recorded absolute values, not embedded status flags.
    start_s, end_s = arm - origin, end - origin
    replay = replay_energy_inputs(history, profile)
    first_boot = replay["first_covered_boot_ms"]
    positions = [s for s in history["samples"] if s["type"] == "GLOBAL_POSITION_INT"]
    first_index = next((i for i, s in enumerate(positions) if s["value"] == first_boot), None)
    covered = (
        positions[first_index : first_index + replay["segments"] + 1]
        if first_index is not None and replay["segments"] > 0
        else []
    )
    before = [s for s in covered if s["at_s"] <= start_s]
    after = [s for s in covered if s["at_s"] >= end_s]
    left, right = (before[-1] if before else None), (after[0] if after else None)
    bracketed = left is not None and right is not None
    envelope = (
        _window_energy_envelope(history["samples"], covered, start_s, end_s, profile)
        if bracketed
        else None
    )
    if envelope is not None and not math.isclose(
        envelope["checked_prefix_wh"], replay["consumed_wh"], rel_tol=1e-10, abs_tol=1e-12
    ):
        raise ValueError("interval energy accounting differs from canonical replay")
    complete = bool(
        bracketed
        and envelope is not None
        and replay["reason"] == "covered_prefix_complete"
        and replay["segments"] > 0
    )
    # The midpoint is a deterministic model estimate between the strict
    # interior lower bound and the boundary-inclusive upper bound. It is
    # deliberately not presented as physical energy or as a calibrated
    # measurement; the envelope remains the evidence needed to audit the
    # receive-time attribution.
    window_energy_wh = (
        (envelope["lower_wh"] + envelope["upper_wh"]) / 2.0
        if complete and envelope is not None
        else None
    )
    return {
        "schema_version": 1,
        "receive_clock": window["clock"],
        "host_clock_identity": identity,
        "receive_window_start_s": start_s,
        "receive_window_end_s": end_s,
        "left_sample": left,
        "right_sample": right,
        "receive_window_bracketed_by_valid_energy_prefix": bracketed,
        "missing_boundaries": [name for name, s in (("start", left), ("end", right)) if s is None],
        "replay": replay,
        "window_energy_wh": window_energy_wh,
        "window_energy_method": (
            "causal_envelope_midpoint_model_v1" if complete else None
        ),
        "window_energy_envelope": envelope,
        "physical_clock_synchronization_claimed": False,
        "full_mission_energy_claimed": complete,
        "full_mission_energy_semantics": (
            "complete_receive_window_model_only" if complete else None
        ),
        "authorizes_next_task": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--capacity-wh", type=float, required=True)
    parser.add_argument("--avionics-w", type=float, required=True)
    parser.add_argument("--propulsion-w", type=float, required=True)
    parser.add_argument("--provenance", required=True)
    args = parser.parse_args()
    raw = args.report.read_bytes()
    result = audit_window(
        json.loads(raw),
        SimulationEnergyProfile(
            args.capacity_wh, args.avionics_w, args.propulsion_w, args.provenance
        ),
    )
    result["input"] = {
        "path": str(args.report),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
