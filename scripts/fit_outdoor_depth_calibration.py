#!/usr/bin/env python3
"""Create a validated outdoor depth-calibration document from measured samples.

Input CSV needs ``sample_id,raw_depth_m,actual_distance_m``. The raw value is
the ``raw_slant_range_m`` field from a geometry-accepted
``ranging.metric_depth_updated`` audit event; actual distance is a tape, laser,
or surveyed slant range. Empty true-distance rows are ignored so the candidate
CSV can retain unmeasured evidence for later field sessions.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.depth_calibration import (  # noqa: E402
    FieldDepthSample,
    calibration_document,
    fit_outdoor_depth_calibration,
    write_calibration_document,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="CSV or JSON list of measured field samples")
    parser.add_argument(
        "--output", type=Path, required=True, help="output .json calibration document"
    )
    parser.add_argument(
        "--profile", required=True, help="immutable human-readable calibration profile"
    )
    return parser


def load_samples(path: Path) -> tuple[FieldDepthSample, ...]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows: Iterable[Mapping[str, object]] = tuple(csv.DictReader(handle))
    elif path.suffix.lower() == ".json":
        decoded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(decoded, list) or not all(isinstance(row, Mapping) for row in decoded):
            raise ValueError("JSON samples must be a list of objects")
        rows = decoded
    else:
        raise ValueError("samples must be a .csv or .json file")
    values = []
    for index, row in enumerate(rows, start=1):
        raw = row.get("raw_depth_m", row.get("raw_slant_range_m"))
        actual = row.get("actual_distance_m", row.get("ground_truth_distance_m"))
        if actual is None or not str(actual).strip():
            continue
        if raw is None or not str(raw).strip():
            raise ValueError(f"sample {index} has true distance but no raw depth")
        sample_id = str(row.get("sample_id") or f"sample-{index:03d}")
        values.append(FieldDepthSample(float(raw), float(actual), sample_id))
    if not values:
        raise ValueError("samples contain no measured actual_distance_m values")
    return tuple(values)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    samples = load_samples(args.samples)
    fit = fit_outdoor_depth_calibration(samples)
    document = calibration_document(profile=args.profile, samples=samples, fit=fit)
    write_calibration_document(args.output, document)
    print(f"wrote {args.output}")
    print(f"scale={fit.scale:.9g}")
    print(f"offset_m={fit.offset_m:.9g}")
    print(f"inliers={fit.inlier_count}/{fit.sample_count}")
    print(f"mae_m={fit.mean_absolute_error_m:.3f}")
    print(f"rmse_m={fit.root_mean_square_error_m:.3f}")
    print(f"METRIC_DEPTH_CALIBRATION_DOCUMENT={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
