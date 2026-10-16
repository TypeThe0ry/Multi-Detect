#!/usr/bin/env python3
"""Evaluate a fitted outdoor depth calibration against measured field samples.

Measured rows use ``sample_id,raw_depth_m,actual_distance_m``. Rows whose ID
does not occur in the calibration document are reported as independent holdout
evidence; the script never changes the deployed calibration.  The optional
strict gate turns holdout count and supplied error thresholds into an explicit
fail-closed deployment-readiness check, while the default remains advisory.
Both raw and actual depths are optical-axis Z. Prefer ``raw_optical_axis_depth_m``
when reading new metric-depth events; an off-axis surveyed slant range must be
multiplied by cos(theta) before supplying ``actual_distance_m``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.autonomy.serialization import (  # noqa: E402
    AutonomyContractError,
    strict_json_loads,
)
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
    parser.add_argument(
        "--require-independent-holdout",
        action="store_true",
        help=(
            "fail unless the calibration has the required number of independent holdout "
            "samples and all supplied error thresholds pass"
        ),
    )
    parser.add_argument(
        "--minimum-independent-holdout",
        type=int,
        default=3,
        help="minimum independent holdout samples required by the strict gate (default: 3)",
    )
    parser.add_argument(
        "--maximum-holdout-mae-m",
        type=float,
        help="optional maximum independent-holdout mean absolute error in metres",
    )
    parser.add_argument(
        "--maximum-holdout-rmse-m",
        type=float,
        help="optional maximum independent-holdout root mean square error in metres",
    )
    parser.add_argument(
        "--maximum-holdout-error-m",
        type=float,
        help="optional maximum absolute independent-holdout error in metres",
    )
    parser.add_argument(
        "--maximum-holdout-relative-mae",
        type=float,
        help="optional maximum independent-holdout mean absolute relative error",
    )
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
        raw = row.get("raw_optical_axis_depth_m")
        if raw is None:
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
    require_independent_holdout: bool = False,
    minimum_independent_holdout: int = 3,
    maximum_holdout_mae_m: float | None = None,
    maximum_holdout_rmse_m: float | None = None,
    maximum_holdout_error_m: float | None = None,
    maximum_holdout_relative_mae: float | None = None,
) -> dict[str, object]:
    _validate_quality_gate_arguments(
        minimum_independent_holdout=minimum_independent_holdout,
        maximum_holdout_mae_m=maximum_holdout_mae_m,
        maximum_holdout_rmse_m=maximum_holdout_rmse_m,
        maximum_holdout_error_m=maximum_holdout_error_m,
        maximum_holdout_relative_mae=maximum_holdout_relative_mae,
    )
    profile = load_calibration_document(calibration_path)
    try:
        document = strict_json_loads(
            calibration_path.read_bytes(),
            name="calibration document",
        )
    except (OSError, AutonomyContractError, TypeError) as exc:
        raise ValueError(f"calibration document is not valid strict JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ValueError("calibration document root must be an object")
    fitted_ids = {
        str(sample["sample_id"])
        for sample in document["samples"]
        if isinstance(sample, Mapping) and isinstance(sample.get("sample_id"), str)
    }
    values = tuple(samples)
    fitted = tuple(sample for sample in values if sample.sample_id in fitted_ids)
    all_residuals = _residuals(values, mapper=profile.map_depth)
    holdout = tuple(sample for sample in values if sample.sample_id not in fitted_ids)
    holdout_residuals = _residuals(holdout, mapper=profile.map_depth)
    holdout_metrics = _metrics(holdout_residuals)
    holdout_relative_metrics = _relative_metrics(holdout, mapper=profile.map_depth)
    quality_gate = _quality_gate(
        holdout_count=len(holdout),
        holdout_metrics=holdout_metrics,
        holdout_relative_metrics=holdout_relative_metrics,
        required=require_independent_holdout,
        minimum_independent_holdout=minimum_independent_holdout,
        maximum_holdout_mae_m=maximum_holdout_mae_m,
        maximum_holdout_rmse_m=maximum_holdout_rmse_m,
        maximum_holdout_error_m=maximum_holdout_error_m,
        maximum_holdout_relative_mae=maximum_holdout_relative_mae,
    )
    return {
        "schema_version": 1,
        "calibration": {
            "profile": profile.profile,
            "mode": profile.calibration_mode,
            "document_path": str(profile.document_path),
            "document_sha256": profile.document_sha256,
            "scale": profile.scale,
            "offset_m": profile.offset_m,
            "raw_breakpoints_m": list(profile.raw_breakpoints_m),
            "actual_breakpoints_m": list(profile.actual_breakpoints_m),
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
        "fitted_samples": _metrics(_residuals(fitted, mapper=profile.map_depth)),
        "independent_holdout": holdout_metrics,
        "independent_holdout_relative": holdout_relative_metrics,
        "quality_gate": quality_gate,
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
        require_independent_holdout=args.require_independent_holdout,
        minimum_independent_holdout=args.minimum_independent_holdout,
        maximum_holdout_mae_m=args.maximum_holdout_mae_m,
        maximum_holdout_rmse_m=args.maximum_holdout_rmse_m,
        maximum_holdout_error_m=args.maximum_holdout_error_m,
        maximum_holdout_relative_mae=args.maximum_holdout_relative_mae,
    )
    write_report(args.output, report)
    measurement = report["measurement"]
    metrics = report["independent_holdout"]
    assert isinstance(measurement, Mapping) and isinstance(metrics, Mapping)
    holdout = measurement["holdout_sample_count"]
    print(f"wrote {args.output}")
    print(f"holdout_samples={holdout}")
    print(f"holdout_mae_m={metrics['mean_absolute_error_m']}")
    relative = report["independent_holdout_relative"]
    assert isinstance(relative, Mapping)
    print(f"holdout_relative_mae={relative['mean_absolute_relative_error']}")
    quality_gate = report["quality_gate"]
    assert isinstance(quality_gate, Mapping)
    print(f"quality_gate_status={quality_gate['status']}")
    print("automatic_calibration_update=false")
    return 0 if quality_gate["passed"] is not False else 2


def _load_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return tuple(csv.DictReader(handle))
    if path.suffix.lower() == ".json":
        try:
            decoded = strict_json_loads(
                path.read_bytes(),
                name="outdoor depth calibration samples",
            )
        except (OSError, AutonomyContractError, TypeError) as exc:
            raise ValueError(f"JSON samples are not valid strict JSON: {exc}") from exc
        if isinstance(decoded, list) and all(isinstance(row, Mapping) for row in decoded):
            return tuple(decoded)
        raise ValueError("JSON samples must be a list of objects")
    raise ValueError("samples must be a .csv or .json file")


def _residuals(
    samples: Iterable[_MeasuredSample],
    *,
    mapper: Callable[[float], float],
) -> tuple[float, ...]:
    return tuple(mapper(sample.raw_depth_m) - sample.actual_distance_m for sample in samples)


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


def _relative_metrics(
    samples: Iterable[_MeasuredSample],
    *,
    mapper: Callable[[float], float],
) -> dict[str, float | int | None]:
    values = tuple(samples)
    if not values:
        return {
            "sample_count": 0,
            "mean_absolute_relative_error": None,
            "maximum_absolute_relative_error": None,
        }
    relative = tuple(
        abs(mapper(sample.raw_depth_m) - sample.actual_distance_m) / sample.actual_distance_m
        for sample in values
    )
    return {
        "sample_count": len(relative),
        "mean_absolute_relative_error": math.fsum(relative) / len(relative),
        "maximum_absolute_relative_error": max(relative),
    }


def _quality_gate(
    *,
    holdout_count: int,
    holdout_metrics: Mapping[str, object],
    holdout_relative_metrics: Mapping[str, object],
    required: bool,
    minimum_independent_holdout: int,
    maximum_holdout_mae_m: float | None,
    maximum_holdout_rmse_m: float | None,
    maximum_holdout_error_m: float | None,
    maximum_holdout_relative_mae: float | None,
) -> dict[str, object]:
    thresholds = {
        "minimum_independent_holdout": minimum_independent_holdout,
        "maximum_holdout_mae_m": maximum_holdout_mae_m,
        "maximum_holdout_rmse_m": maximum_holdout_rmse_m,
        "maximum_holdout_error_m": maximum_holdout_error_m,
        "maximum_holdout_relative_mae": maximum_holdout_relative_mae,
    }
    observed = {
        "independent_holdout_count": holdout_count,
        "holdout_mae_m": holdout_metrics.get("mean_absolute_error_m"),
        "holdout_rmse_m": holdout_metrics.get("root_mean_square_error_m"),
        "holdout_maximum_error_m": holdout_metrics.get("maximum_absolute_error_m"),
        "holdout_relative_mae": holdout_relative_metrics.get("mean_absolute_relative_error"),
    }
    if not required:
        return {
            "status": "advisory",
            "required": False,
            "passed": None,
            "failure_reasons": [],
            "thresholds": thresholds,
            "observed": observed,
        }
    failures: list[str] = []
    if holdout_count < minimum_independent_holdout:
        failures.append(
            "independent_holdout_count_below_minimum "
            f"({holdout_count}<{minimum_independent_holdout})"
        )
    _check_metric_threshold(
        failures,
        holdout_metrics.get("mean_absolute_error_m"),
        maximum_holdout_mae_m,
        "holdout_mae_exceeds_threshold",
    )
    _check_metric_threshold(
        failures,
        holdout_metrics.get("root_mean_square_error_m"),
        maximum_holdout_rmse_m,
        "holdout_rmse_exceeds_threshold",
    )
    _check_metric_threshold(
        failures,
        holdout_metrics.get("maximum_absolute_error_m"),
        maximum_holdout_error_m,
        "holdout_maximum_error_exceeds_threshold",
    )
    _check_metric_threshold(
        failures,
        holdout_relative_metrics.get("mean_absolute_relative_error"),
        maximum_holdout_relative_mae,
        "holdout_relative_mae_exceeds_threshold",
    )
    return {
        "status": "failed" if failures else "passed",
        "required": True,
        "passed": not failures,
        "failure_reasons": failures,
        "thresholds": thresholds,
        "observed": observed,
    }


def _check_metric_threshold(
    failures: list[str],
    observed: object,
    threshold: float | None,
    reason: str,
) -> None:
    if threshold is None:
        return
    if observed is None or float(observed) > threshold:
        failures.append(reason)


def _validate_quality_gate_arguments(
    *,
    minimum_independent_holdout: int,
    maximum_holdout_mae_m: float | None,
    maximum_holdout_rmse_m: float | None,
    maximum_holdout_error_m: float | None,
    maximum_holdout_relative_mae: float | None,
) -> None:
    if isinstance(minimum_independent_holdout, bool) or minimum_independent_holdout < 1:
        raise ValueError("minimum independent holdout must be a positive integer")
    for value, name in (
        (maximum_holdout_mae_m, "maximum holdout MAE"),
        (maximum_holdout_rmse_m, "maximum holdout RMSE"),
        (maximum_holdout_error_m, "maximum holdout error"),
        (maximum_holdout_relative_mae, "maximum holdout relative MAE"),
    ):
        if value is not None and (not math.isfinite(value) or value <= 0.0):
            raise ValueError(f"{name} must be finite and positive")


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
