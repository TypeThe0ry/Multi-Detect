#!/usr/bin/env python3
"""Export only geometry-accepted target depth events for field calibration.

The output is a CSV that can be opened in a spreadsheet. Fill
``sample_id`` and ``actual_distance_m`` only for rows with a surveyed/tape/laser
reference, then pass the same CSV to ``fit_outdoor_depth_calibration.py``.
Rows without a true distance are ignored by the fitter, so raw scene-grid and
unassociated target estimates can never silently enter the field fit.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class _AssociationEvidence:
    source_iou: float | None
    center_distance: float | None
    source_age_s: float | None
    compatible_sample_count: int | None


@dataclass(frozen=True, slots=True)
class _Candidate:
    target_id: str
    source_frame_id: str
    audit_sequence: int
    audit_timestamp_s: float
    raw_depth_m: float
    calibrated_depth_m: float
    sigma_m: float
    calibration_profile: str
    calibration_scale: float
    association: _AssociationEvidence


CSV_FIELDS = (
    "sample_id",
    "actual_distance_m",
    "raw_depth_m",
    "target_id",
    "source_frame_id",
    "audit_sequence",
    "audit_timestamp_s",
    "calibrated_depth_m",
    "sigma_m",
    "calibration_profile",
    "calibration_scale",
    "association_iou",
    "association_center_distance",
    "association_source_age_s",
    "association_compatible_sample_count",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path, help="Jetson .audit.jsonl evidence file")
    parser.add_argument("--output", type=Path, required=True, help="candidate CSV to review")
    parser.add_argument(
        "--minimum-separation-seconds",
        type=float,
        default=2.0,
        help="retain at most one accepted source frame per target within this interval",
    )
    parser.add_argument(
        "--minimum-raw-depth-change-m",
        type=float,
        default=1.0,
        help="retain a later frame only after this raw-depth change for the same target",
    )
    return parser


def load_candidates(
    audit_path: Path,
    *,
    minimum_separation_seconds: float = 2.0,
    minimum_raw_depth_change_m: float = 1.0,
) -> tuple[_Candidate, ...]:
    """Join raw depth events with accepted source-box association evidence."""

    if not math.isfinite(minimum_separation_seconds) or minimum_separation_seconds < 0.0:
        raise ValueError("minimum separation must be finite and non-negative")
    if not math.isfinite(minimum_raw_depth_change_m) or minimum_raw_depth_change_m < 0.0:
        raise ValueError("minimum raw-depth change must be finite and non-negative")
    events = tuple(_iter_events(audit_path))
    associations: dict[tuple[str, str], _AssociationEvidence] = {}
    for event in events:
        if event["event_type"] != "ranging.metric_depth_association":
            continue
        details = event["details"]
        if details.get("status") != "accepted":
            continue
        target_id = _nonempty_string(details.get("target_id"), "association target_id")
        source_frame_id = _nonempty_string(
            details.get("source_frame_id"), "association source_frame_id"
        )
        associations[(target_id, source_frame_id)] = _AssociationEvidence(
            source_iou=_optional_float(details.get("source_iou")),
            center_distance=_optional_float(details.get("center_distance")),
            source_age_s=_optional_float(details.get("source_age_s")),
            compatible_sample_count=_optional_int(details.get("compatible_sample_count")),
        )

    candidates: list[_Candidate] = []
    seen: set[tuple[str, str]] = set()
    last_exported_at_s: dict[str, float] = {}
    last_exported_raw_depth_m: dict[str, float] = {}
    for event in events:
        if event["event_type"] != "ranging.metric_depth_updated":
            continue
        details = event["details"]
        target_id = _nonempty_string(details.get("target_id"), "metric-depth target_id")
        if target_id == "depth-grid":
            continue
        source_frame_id = _nonempty_string(details.get("frame_id"), "metric-depth frame_id")
        key = (target_id, source_frame_id)
        association = associations.get(key)
        if association is None or key in seen:
            continue
        raw_depth_m = _positive_float(details.get("raw_slant_range_m"), "raw_slant_range_m")
        audit_timestamp_s = _positive_float(event["timestamp_s"], "audit timestamp")
        previous_timestamp_s = last_exported_at_s.get(target_id)
        previous_raw_depth_m = last_exported_raw_depth_m.get(target_id)
        if (
            previous_timestamp_s is not None
            and audit_timestamp_s - previous_timestamp_s < minimum_separation_seconds
        ):
            continue
        if (
            previous_raw_depth_m is not None
            and abs(raw_depth_m - previous_raw_depth_m) < minimum_raw_depth_change_m
        ):
            continue
        candidates.append(
            _Candidate(
                target_id=target_id,
                source_frame_id=source_frame_id,
                audit_sequence=_positive_int(event["sequence"], "audit sequence"),
                audit_timestamp_s=audit_timestamp_s,
                raw_depth_m=raw_depth_m,
                calibrated_depth_m=_positive_float(
                    details.get("slant_range_m"), "slant_range_m"
                ),
                sigma_m=_positive_float(details.get("sigma_m"), "sigma_m"),
                calibration_profile=_nonempty_string(
                    details.get("calibration_profile"), "calibration_profile"
                ),
                calibration_scale=_positive_float(
                    details.get("calibration_scale"), "calibration_scale"
                ),
                association=association,
            )
        )
        seen.add(key)
        last_exported_at_s[target_id] = audit_timestamp_s
        last_exported_raw_depth_m[target_id] = raw_depth_m
    return tuple(candidates)


def write_candidates(path: Path, candidates: Iterable[_Candidate]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    "sample_id": "",
                    "actual_distance_m": "",
                    "raw_depth_m": candidate.raw_depth_m,
                    "target_id": candidate.target_id,
                    "source_frame_id": candidate.source_frame_id,
                    "audit_sequence": candidate.audit_sequence,
                    "audit_timestamp_s": candidate.audit_timestamp_s,
                    "calibrated_depth_m": candidate.calibrated_depth_m,
                    "sigma_m": candidate.sigma_m,
                    "calibration_profile": candidate.calibration_profile,
                    "calibration_scale": candidate.calibration_scale,
                    "association_iou": candidate.association.source_iou,
                    "association_center_distance": candidate.association.center_distance,
                    "association_source_age_s": candidate.association.source_age_s,
                    "association_compatible_sample_count": (
                        candidate.association.compatible_sample_count
                    ),
                }
            )
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidates = load_candidates(
        args.audit,
        minimum_separation_seconds=args.minimum_separation_seconds,
        minimum_raw_depth_change_m=args.minimum_raw_depth_change_m,
    )
    if not candidates:
        raise SystemExit("no accepted target metric-depth events found in audit")
    count = write_candidates(args.output, candidates)
    print(f"wrote {count} accepted calibration candidates to {args.output}")
    print("Fill sample_id and actual_distance_m for surveyed rows, then run the fitter.")
    return 0


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"audit line {line_number} is not valid JSON") from exc
            if not isinstance(event, Mapping) or not isinstance(event.get("details"), Mapping):
                raise ValueError(f"audit line {line_number} has no object details")
            yield {
                "event_type": _nonempty_string(event.get("event_type"), "event_type"),
                "sequence": event.get("sequence"),
                "timestamp_s": event.get("timestamp_s"),
                "details": event["details"],
            }


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


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


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("association value must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("association value must be numeric") from exc
    if not math.isfinite(number) or number < 0.0:
        raise ValueError("association value must be finite and non-negative")
    return number


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number <= 0 or number != value:
        raise ValueError(f"{name} must be a positive integer")
    return number


def _optional_int(value: object) -> int | None:
    return None if value is None else _positive_int(value, "association count")


if __name__ == "__main__":
    raise SystemExit(main())
