#!/usr/bin/env python3
"""Compare estimated target coordinates with independently surveyed WGS-84 truth.

The input is a CSV, JSON list, JSON object containing ``samples``, or JSONL
file.  Canonical columns are ``sample_id``, ``estimated_latitude_deg``,
``estimated_longitude_deg``, ``ground_truth_latitude_deg`` and
``ground_truth_longitude_deg``.  ``reported_sigma_m`` is optional.  The tool
only writes an offline accuracy report; it does not alter runtime state,
calibration documents, or flight-control settings.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.geolocation_accuracy import (  # noqa: E402
    GeolocationAccuracySample,
    evaluate_geolocation_accuracy,
)

_ESTIMATED_LATITUDE_KEYS = (
    "estimated_latitude_deg",
    "estimated_lat_deg",
    "latitude_deg",
    "latitude",
    "estimated_latitude",
)
_ESTIMATED_LONGITUDE_KEYS = (
    "estimated_longitude_deg",
    "estimated_lon_deg",
    "longitude_deg",
    "longitude",
    "estimated_longitude",
)
_TRUTH_LATITUDE_KEYS = (
    "ground_truth_latitude_deg",
    "truth_latitude_deg",
    "surveyed_latitude_deg",
    "ground_truth_lat_deg",
    "truth_lat_deg",
)
_TRUTH_LONGITUDE_KEYS = (
    "ground_truth_longitude_deg",
    "truth_longitude_deg",
    "surveyed_longitude_deg",
    "ground_truth_lon_deg",
    "truth_lon_deg",
)
_SIGMA_KEYS = (
    "reported_sigma_m",
    "horizontal_sigma_m",
    "horizontal_1sigma_m",
    "sigma_m",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="surveyed coordinate table (.csv/.json/.jsonl)")
    parser.add_argument("--output", type=Path, required=True, help="accuracy report JSON")
    parser.add_argument(
        "--minimum-samples",
        type=int,
        default=10,
        help="minimum surveyed samples required for a passing completeness gate (default: 10)",
    )
    parser.add_argument(
        "--maximum-p95-m",
        type=float,
        help="optional maximum allowed 95th-percentile horizontal error in metres",
    )
    parser.add_argument(
        "--minimum-one-sigma-coverage",
        type=float,
        help="optional minimum fraction of sigma-bearing samples within reported 1-sigma",
    )
    parser.add_argument(
        "--minimum-two-sigma-coverage",
        type=float,
        help="optional minimum fraction of sigma-bearing samples within 1.96-sigma",
    )
    return parser


def load_samples(path: Path) -> tuple[GeolocationAccuracySample, ...]:
    rows = _load_rows(path)
    samples: list[GeolocationAccuracySample] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(f"sample row {index} must be an object")
        sample_id = _text(_first(row, ("sample_id", "id"))) or f"sample-{index:03d}"
        estimated = _coordinate_values(
            row,
            latitude_keys=_ESTIMATED_LATITUDE_KEYS,
            longitude_keys=_ESTIMATED_LONGITUDE_KEYS,
            nested_keys=("estimated", "estimate", "target_geolocation"),
            label="estimated",
        )
        truth = _coordinate_values(
            row,
            latitude_keys=_TRUTH_LATITUDE_KEYS,
            longitude_keys=_TRUTH_LONGITUDE_KEYS,
            nested_keys=("ground_truth", "truth", "survey"),
            label="ground_truth",
        )
        sigma_value = _first(row, _SIGMA_KEYS)
        if sigma_value is None:
            sigma = None
        elif isinstance(sigma_value, str) and not sigma_value.strip():
            sigma = None
        else:
            sigma = _float(sigma_value, "reported_sigma_m", index)
        target_id = _text(_first(row, ("target_id", "source_target_id")))
        captured_value = _first(row, ("captured_at_s", "timestamp_s", "observed_at_s"))
        captured_at_s = None
        if captured_value is not None and not (
            isinstance(captured_value, str) and not captured_value.strip()
        ):
            captured_at_s = _float(captured_value, "captured_at_s", index)
        samples.append(
            GeolocationAccuracySample(
                sample_id=sample_id,
                estimated_latitude_deg=estimated[0],
                estimated_longitude_deg=estimated[1],
                ground_truth_latitude_deg=truth[0],
                ground_truth_longitude_deg=truth[1],
                reported_sigma_m=sigma,
                target_id=target_id,
                captured_at_s=captured_at_s,
            )
        )
    if not samples:
        raise ValueError("samples contain no rows")
    return tuple(samples)


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
    try:
        samples = load_samples(args.samples)
        report = evaluate_geolocation_accuracy(
            samples,
            minimum_samples=args.minimum_samples,
            maximum_p95_m=args.maximum_p95_m,
            minimum_one_sigma_coverage=args.minimum_one_sigma_coverage,
            minimum_two_sigma_coverage=args.minimum_two_sigma_coverage,
        )
        write_report(args.output, report)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"target geolocation accuracy validation failed: {exc}", file=sys.stderr)
        return 2
    distance = report["distance_error_m"]
    measurement = report["measurement"]
    assert isinstance(distance, Mapping) and isinstance(measurement, Mapping)
    print(f"wrote {args.output}")
    print(f"samples={measurement['sample_count']}")
    print(f"median_error_m={distance['median_m']:.6g}")
    print(f"p95_error_m={distance['p95_m']:.6g}")
    print(f"gate_passed={report['gate_passed']}")
    return 0 if report["gate_passed"] else 1


def _load_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return tuple(csv.DictReader(handle))
    if suffix == ".json":
        decoded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(decoded, Mapping):
            decoded = decoded.get("samples")
        if isinstance(decoded, list) and all(isinstance(row, Mapping) for row in decoded):
            return tuple(decoded)
        raise ValueError("JSON samples must be a list or an object with a samples list")
    if suffix == ".jsonl":
        rows: list[Mapping[str, object]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL line {line_number} is invalid JSON") from exc
            if not isinstance(decoded, Mapping):
                raise ValueError(f"JSONL line {line_number} must be an object")
            rows.append(decoded)
        return tuple(rows)
    raise ValueError("samples must be a .csv, .json, or .jsonl file")


def _coordinate_values(
    row: Mapping[str, object],
    *,
    latitude_keys: Iterable[str],
    longitude_keys: Iterable[str],
    nested_keys: Iterable[str],
    label: str,
) -> tuple[float, float]:
    latitude = _first(row, latitude_keys)
    longitude = _first(row, longitude_keys)
    if latitude is None or longitude is None:
        for nested_key in nested_keys:
            nested = row.get(nested_key)
            if isinstance(nested, Mapping):
                latitude = latitude if latitude is not None else _first(nested, latitude_keys)
                longitude = longitude if longitude is not None else _first(nested, longitude_keys)
    if latitude is None or longitude is None:
        raise ValueError(f"row is missing {label} latitude/longitude")
    return (
        _float(latitude, f"{label}_latitude_deg", 0),
        _float(longitude, f"{label}_longitude_deg", 0),
    )


def _first(row: Mapping[str, object], names: Iterable[str]) -> object | None:
    for name in names:
        if name in row:
            return row[name]
    return None


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


def _float(value: object, name: str, row_number: int) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric (row {row_number})")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        suffix = f" (row {row_number})" if row_number else ""
        raise ValueError(f"{name} must be numeric{suffix}") from exc
    if not math.isfinite(number):
        suffix = f" (row {row_number})" if row_number else ""
        raise ValueError(f"{name} must be finite{suffix}")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
