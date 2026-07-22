#!/usr/bin/env python3
"""Evaluate a fitted outdoor depth calibration against measured field samples.

Measured rows use ``sample_id,raw_depth_m,actual_distance_m``. Rows whose ID
does not occur in the calibration document are reported as independent holdout
evidence; the script never changes the deployed calibration.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.depth_calibration import load_calibration_document  # noqa: E402


@dataclass(frozen=True, slots=True)
class _MeasuredSample:
    sample_id: str
    raw_depth_m: float
    actual_distance_m: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="CSV or JSON field sample table")
    parser.add_argument("--calibration", type=Path, required=True, help="fitted calibration JSON")
    parser.add_argument("--output", type=Path, required=True, help="validation report JSON")
    return parser


def load_measured_samples(path: Path) -> tuple[_MeasuredSample, ...]:
    rows = _load_rows(path)
    samples = []
    ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        actual = row.get("actual_distance_m", row.get("ground_truth_distance_m"))
        if actual is None or not str(actual).strip():
            continue
        sample_id = str(row.get("sample_id") or f"sample-{index:03d}").strip()
        if not sample_id or sample_id in ids:
            raise ValueError(f"duplicate or empty sample_id at row {index}")
        raw = row.get("raw_depth_m", row.get("raw_slant_range_m"))
        samples.append(
            _MeasuredSample(
                sample_id=sample_id,
                raw_depth_m=_positive_float(raw, "raw_depth_m"),
                actual_distance_m=_positive_float(actual, "actual_distance_m"),
            )
        )
        ids.add(sample_id)
    if not samples:
        raise ValueError("samples contain no measured actual_distance_m values")
    return tuple(samples)


def validation_report(
    samples: Iterable[_MeasuredSample],
    *,
    calibration_path: Path,
) -> dict[str, object]:
    profile = load_calibration_document(calibration_path)
    document = json.loads(calibration_path.read_text(encoding="utf-8"))
    fitted_ids = {
        str(sample["sample_id"])
        for sample in document["samples"]
        if isinstance(sample, Mapping) and isinstance(sample.get("sample_id"), str)
    }
    values = tuple(samples)
    fitted = tuple(sample for sample in values if sample.sample_id in fitted_ids)
    all_residuals = _residuals(values, scale=profile.scale, offset_m=profile.offset_m)
    holdout = tuple(sample for sample in values if sample.sample_id not in fitted_ids)
    return {
        "schema_version": 1,
        "calibration": {
            "profile": profile.profile,
            "document_path": str(profile.document_path),
            "document_sha256": profile.document_sha256,
            "scale": profile.scale,
            "offset_m": profile.offset_m,
            "fit_sample_count": profile.sample_count,
            "fit_inlier_count": profile.inlier_count,
        },
        "measurement": {
            "sample_count": len(values),
            "fitted_sample_count": len(fitted),
            "holdout_sample_count": len(holdout),
            "independent_validation_ready": len(holdout) >= 3,
            "raw_depth_coverage_m": [
                min(sample.raw_depth_m for sample in values),
                max(sample.raw_depth_m for sample in values),
            ],
            "actual_distance_coverage_m": [
                min(sample.actual_distance_m for sample in values),
                max(sample.actual_distance_m for sample in values),
            ],
        },
        "all_measured": _metrics(all_residuals),
        "fitted_samples": _metrics(
            _residuals(fitted, scale=profile.scale, offset_m=profile.offset_m)
        ),
        "independent_holdout": _metrics(
            _residuals(holdout, scale=profile.scale, offset_m=profile.offset_m)
        ),
        "advisory_only": True,
        "automatic_calibration_update": False,
    }


def write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = validation_report(
        load_measured_samples(args.samples),
        calibration_path=args.calibration,
    )
    write_report(args.output, report)
    measurement = report["measurement"]
    metrics = report["independent_holdout"]
    assert isinstance(measurement, Mapping) and isinstance(metrics, Mapping)
    holdout = measurement["holdout_sample_count"]
    print(f"wrote {args.output}")
    print(f"holdout_samples={holdout}")
    print(f"holdout_mae_m={metrics['mean_absolute_error_m']}")
    print("automatic_calibration_update=false")
    return 0


def _load_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return tuple(csv.DictReader(handle))
    if path.suffix.lower() == ".json":
        decoded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(decoded, list) and all(isinstance(row, Mapping) for row in decoded):
            return tuple(decoded)
        raise ValueError("JSON samples must be a list of objects")
    raise ValueError("samples must be a .csv or .json file")


def _residuals(
    samples: Iterable[_MeasuredSample],
    *,
    scale: float,
    offset_m: float,
) -> tuple[float, ...]:
    return tuple(
        scale * sample.raw_depth_m + offset_m - sample.actual_distance_m
        for sample in samples
    )


def _metrics(residuals: Iterable[float]) -> dict[str, float | int | None]:
    values = tuple(residuals)
    if not values:
        return {
            "sample_count": 0,
            "mean_absolute_error_m": None,
            "root_mean_square_error_m": None,
            "signed_bias_m": None,
            "maximum_absolute_error_m": None,
        }
    absolute = tuple(abs(value) for value in values)
    return {
        "sample_count": len(values),
        "mean_absolute_error_m": math.fsum(absolute) / len(absolute),
        "root_mean_square_error_m": math.sqrt(
            math.fsum(value * value for value in values) / len(values)
        ),
        "signed_bias_m": math.fsum(values) / len(values),
        "maximum_absolute_error_m": max(absolute),
    }


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
