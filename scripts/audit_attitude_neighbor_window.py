"""Hash-bound endpoint diagnostics around unpaired attitude targets.

This command reports observed measurements before and after a target source
label. It deliberately does not interpolate, estimate an error bound, or
promote a controller result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from multidetect.simulation.mode_window import select_mode_bracketed_samples  # noqa: E402
from multidetect.simulation.roll_response import (  # noqa: E402
    analyze_roll_response,
    inspect_attitude_neighbors,
)


def audit_file(
    path: Path,
    expected_sha256: str,
    *,
    lookback_ms: int,
    maximum_gap_ms: int,
    maximum_target_deviation_deg: float,
    quaternion_norm_tolerance: float,
    required_state: int,
    maximum_gap_s: float,
) -> dict:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("source SHA256 mismatch")
    report = strict_json_loads(raw.decode("utf-8"))
    capture = report["flight"]["phase_dynamics_observations"]
    response = analyze_roll_response(
        capture,
        lookback_ms=lookback_ms,
        maximum_gap_ms=maximum_gap_ms,
        maximum_target_deviation_deg=maximum_target_deviation_deg,
        quaternion_norm_tolerance=quaternion_norm_tolerance,
    )
    selected = select_mode_bracketed_samples(
        report["shared_outcome_shadow"]["events"],
        response["target_samples"],
        required_state=required_state,
        maximum_gap_s=maximum_gap_s,
        include_actual_receipt=False,
    )["samples"]
    selected = [
        row
        for row in selected
        if row["classification"] == "sampled_near_current_target"
        and not row["exact_actual_source_label_present"]
    ]
    neighbors = {row["time_boot_ms"]: row for row in inspect_attitude_neighbors(capture)["samples"]}
    rows = []
    for target in selected:
        neighbor = neighbors[target["time_boot_ms"]]
        rows.append(
            {
                "time_boot_ms": target["time_boot_ms"],
                "target_received_at_s": target["target_received_at_s"],
                "history_start_received_at_s": target["history_start_received_at_s"],
                "status": neighbor["status"],
                "endpoints": neighbor["endpoints"],
                "bracket_width_ms": neighbor["bracket_width_ms"],
                "observed_endpoint_roll_change_deg": neighbor["observed_endpoint_roll_change_deg"],
            }
        )
    changes = [
        abs(row["observed_endpoint_roll_change_deg"]) for row in rows if row["bracket_width_ms"]
    ]
    widths = [row["bracket_width_ms"] for row in rows if row["bracket_width_ms"]]
    source_offsets = [
        abs(endpoint["source_offset_ms"]) for row in rows for endpoint in row["endpoints"]
    ]
    receipt_offsets = [
        abs(endpoint["receipt_offset_s"]) for row in rows for endpoint in row["endpoints"]
    ]
    return {
        "source_path": str(path),
        "source_sha256": digest,
        "selection": {
            "lookback_ms": lookback_ms,
            "maximum_gap_ms": maximum_gap_ms,
            "maximum_target_deviation_deg": maximum_target_deviation_deg,
            "quaternion_norm_tolerance": quaternion_norm_tolerance,
            "required_state": required_state,
            "maximum_gap_s": maximum_gap_s,
            "scope": "fixed-wing target-history receipts only, then unpaired quiet targets",
        },
        "quiet_unpaired_count": len(rows),
        "summary": {
            "bracket_width_ms_min": min(widths) if widths else None,
            "bracket_width_ms_max": max(widths) if widths else None,
            "source_offset_ms_abs_max": max(source_offsets) if source_offsets else None,
            "receipt_offset_s_abs_max": max(receipt_offsets) if receipt_offsets else None,
            "observed_endpoint_roll_change_abs_max_deg": max(changes) if changes else None,
            "observed_endpoint_roll_change_rms_deg": (
                math.sqrt(math.fsum(value * value for value in changes) / len(changes))
                if changes
                else None
            ),
        },
        "rows": rows,
        "interpolation_performed": False,
        "intermediate_error_bound_proven": False,
        "physical_simultaneity_proven": False,
        "controller_acceptance_proven": False,
        "final_release_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--lookback-ms", required=True, type=int)
    parser.add_argument("--maximum-gap-ms", required=True, type=int)
    parser.add_argument("--maximum-target-deviation-deg", required=True, type=float)
    parser.add_argument("--quaternion-norm-tolerance", required=True, type=float)
    parser.add_argument("--required-state", required=True, type=int)
    parser.add_argument("--maximum-gap-s", required=True, type=float)
    args = parser.parse_args()
    try:
        result = audit_file(
            args.source,
            args.source_sha256,
            lookback_ms=args.lookback_ms,
            maximum_gap_ms=args.maximum_gap_ms,
            maximum_target_deviation_deg=args.maximum_target_deviation_deg,
            quaternion_norm_tolerance=args.quaternion_norm_tolerance,
            required_state=args.required_state,
            maximum_gap_s=args.maximum_gap_s,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(2, f"audit rejected: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
