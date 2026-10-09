"""Test a positive affine host/container clock hypothesis; never calibrate sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from scripts.audit_mission_clock_receipts import read_bound  # noqa: E402
from scripts.clock_offset_probe import describe_probes  # noqa: E402


def ratio(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def describe_affine_probes(rows):
    baseline = describe_probes(rows)
    lower, upper = Fraction(0), None
    lower_pair = upper_pair = None
    # For H = rate*C + offset, every offset interval must intersect.
    # Pairwise interval intersection on the real line is sufficient. Since
    # C strictly increases, all resulting constraints are bounds on rate.
    for i, left in enumerate(rows):
        lr = left.get("host_resolution_ns", 0)
        left_low = left["host_before_ns"] - lr
        left_high = left["host_after_ns"] + lr
        for j in range(i + 1, len(rows)):
            right = rows[j]
            rr = right.get("host_resolution_ns", 0)
            delta = right["container_ns"] - left["container_ns"]
            low = Fraction(right["host_before_ns"] - rr - left_high, delta)
            high = Fraction(right["host_after_ns"] + rr - left_low, delta)
            if low > lower:
                lower, lower_pair = low, [i, j]
            if upper is None or high < upper:
                upper, upper_pair = high, [i, j]
    compatible = upper > 0 and lower <= upper
    return {
        "schema_version": 1,
        "container_id": baseline["container_id"],
        "host_clock_identity": baseline.get("host_clock_identity"),
        "sample_count": len(rows),
        "assumption": (
            "host_ns = positive_rate * container_ns + offset; rate constant over probes; "
            "no resets; causal probe brackets; host read error <= declared resolution"
        ),
        "constant_offset_compatible": baseline["constant_offset_compatible"],
        "affine_compatible": compatible,
        "rate_constraints": {
            "lower": ratio(lower),
            "upper": ratio(upper),
            "lower_inclusive": lower > 0,
            "upper_inclusive": True,
            "lower_probe_pair": lower_pair,
            "upper_probe_pair": upper_pair,
        },
        "conditional_rate_interval": [ratio(lower), ratio(upper)] if compatible else None,
        "rate_direction": "host_ns_per_container_ns",
        "probe_rates_verified": False,
        "timestamp_accuracy_verified": False,
        "source_clock_alignment_claimed": False,
        "extrapolation_allowed": False,
        "training_label_eligible": False,
    }


def assess_holdout(endpoint_rows, holdout_rows):
    if len(endpoint_rows) != 6 or len(holdout_rows) != 3:
        raise ValueError("six endpoint and three holdout probes required")
    endpoint = describe_affine_probes(endpoint_rows)
    # The original fit remains unchanged; this is a separate consistency test.
    combined = endpoint_rows[:3] + holdout_rows + endpoint_rows[3:]
    joint = describe_affine_probes(combined)
    return {
        "status": "compatible" if joint["affine_compatible"] else "incompatible",
        "endpoint_conditional_rate_interval": endpoint["conditional_rate_interval"],
        "endpoint_fit_modified": False,
        "holdout_count": 3,
        "scope": "joint consistency with endpoint constraints, not calibration proof",
        "mission_window_membership_verified": False,
        "training_label_eligible": False,
    }


def audit_outer(path, digest):
    outer = strict_json_loads(read_bound(path, digest).decode("utf-8"))
    capture = outer["clock_capture"]
    if (
        outer.get("gate_passed") is not True
        or outer["gates"].get("clock_capture") is not True
        or type(capture.get("exit_code")) is not int
        or capture["exit_code"] != 0
    ):
        raise ValueError("successful clock capture required")
    rows = capture["host_container_probes"]
    baseline = describe_probes(rows)
    if (
        baseline != capture["host_container_offset"]
        or baseline["container_id"] != capture["container_id"]
        or baseline.get("host_clock_identity") != capture.get("host_clock_identity")
    ):
        raise ValueError("capture/probe identity or summary mismatch")
    result = describe_affine_probes(rows)
    result["holdout_validation"] = {"status": "missing", "training_label_eligible": False}
    if "host_container_holdout" in capture:
        holdout = capture["host_container_holdout"]
        if (
            not isinstance(holdout, dict)
            or set(holdout)
            != {"phase", "probes", "used_for_endpoint_fit", "mission_window_membership_verified"}
            or holdout["phase"] != "after_sensor_complete_before_mission_wait"
            or holdout["used_for_endpoint_fit"] is not False
            or holdout["mission_window_membership_verified"] is not False
            or not isinstance(holdout["probes"], list)
        ):
            raise ValueError("invalid holdout declaration")
        result["holdout_validation"] = assess_holdout(rows, holdout["probes"])
    result["outer_sha256"] = digest
    result["mission_or_clock_stream_revalidated"] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outer", type=Path, required=True)
    parser.add_argument("--outer-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit_outer(args.outer, args.outer_sha256)
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
