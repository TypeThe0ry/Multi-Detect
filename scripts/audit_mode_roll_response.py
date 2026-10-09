"""Read-only, hash-bound mode-window diagnostics. Never a release gate."""

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
from multidetect.simulation.roll_response import analyze_roll_response  # noqa: E402


def audit_file(path, expected_sha256, *, lookbacks_ms, include_target_coverage=False, **options):
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("source SHA256 mismatch")
    report = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(lookbacks_ms, list) or not 1 <= len(lookbacks_ms) <= 16:
        raise ValueError("one to sixteen explicit lookbacks required")
    windows = []
    for window in lookbacks_ms:
        response = analyze_roll_response(
            report["flight"]["phase_dynamics_observations"],
            lookback_ms=window,
            maximum_gap_ms=options["maximum_gap_ms"],
            maximum_target_deviation_deg=options["maximum_target_deviation_deg"],
            quaternion_norm_tolerance=options["quaternion_norm_tolerance"],
        )
        selection = select_mode_bracketed_samples(
            report["shared_outcome_shadow"]["events"],
            response["samples"],
            required_state=options["required_state"],
            maximum_gap_s=options["maximum_gap_s"],
        )
        rows = selection.pop("samples")
        groups = {}
        for name in (
            "insufficient_history",
            "sampled_near_current_target",
            "sampled_changing_target",
        ):
            errors = [r["roll_error_deg"] for r in rows if r["classification"] == name]
            groups[name] = dict(
                count=len(errors),
                max_error_deg=max(map(abs, errors)) if errors else None,
                rms_error_deg=math.sqrt(math.fsum(e * e for e in errors) / len(errors))
                if errors
                else None,
            )
        windows.append(
            dict(lookback_ms=window, selection=selection, retained_count=len(rows), groups=groups)
        )
        if include_target_coverage:
            target_selection = select_mode_bracketed_samples(
                report["shared_outcome_shadow"]["events"],
                response["target_samples"],
                required_state=options["required_state"],
                maximum_gap_s=options["maximum_gap_s"],
                include_actual_receipt=False,
            )
            target_rows = target_selection.pop("samples")
            windows[-1]["target_coverage"] = dict(
                scope="target_history_receipts_only_not_actual_response",
                total_targets=len(response["target_samples"]),
                retained_targets=len(target_rows),
                selection=target_selection,
                groups={
                    name: {
                        label: sum(
                            r["classification"] == name
                            and r["exact_actual_source_label_present"] is matched
                            for r in target_rows
                        )
                        for label, matched in (
                            ("exact_label_present", True),
                            ("exact_label_absent", False),
                        )
                    }
                    for name in (*groups, "unusable_target_attitude")
                },
            )
    return dict(
        source_path=str(path),
        source_sha256=digest,
        windows=windows,
        options=options,
        selection_scope=(
            "strict EXTENDED_SYS_STATE receipt brackets around history and both pair receipts"
        ),
        diagnostic_only=True,
        controller_acceptance_proven=False,
        final_release_allowed=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--lookbacks-ms", required=True, nargs="+", type=int)
    parser.add_argument("--maximum-gap-ms", required=True, type=int)
    parser.add_argument("--maximum-target-deviation-deg", required=True, type=float)
    parser.add_argument("--quaternion-norm-tolerance", required=True, type=float)
    parser.add_argument("--required-state", required=True, type=int)
    parser.add_argument("--maximum-gap-s", required=True, type=float)
    parser.add_argument("--include-target-coverage", action="store_true")
    args = vars(parser.parse_args())
    source, digest = args.pop("source"), args.pop("source_sha256")
    try:
        result = audit_file(source, digest, **args)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"audit rejected: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
