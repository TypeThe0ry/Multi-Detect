"""Build WGS-84 target accuracy reports from verified target-event replays.

The live target-event replay bundle contains the *estimated* WGS-84 target
coordinate and its reported horizontal 1-sigma value.  An independent survey
table contains the truth coordinate for the same event.  This module joins the
two using an explicit event/frame identifier and then delegates all distance
and uncertainty calculations to :mod:`multidetect.geolocation_accuracy`.

Keeping the join here, rather than asking an operator to copy coordinates by
hand, makes an outdoor acceptance run reproducible.  The replay loader verifies
the bundle and every referenced media/hash before any sample is admitted.  A
stale, unknown, or unqualified GPS event is never converted into a surveyed
sample.  The resulting report remains offline evidence and has no runtime,
mission, or flight-control inputs.  Replay-derived reports also carry a
strict ``selection`` object with source-manifest identity and join counts so
an audit can distinguish a complete survey from a filtered or stale stream.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .autonomy.serialization import AutonomyContractError, strict_json_loads
from .geolocation_accuracy import (
    GeolocationAccuracySample,
    evaluate_geolocation_accuracy,
)
from .target_event_replay import TargetEvent, load_target_event_replay_manifest

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
_TRUTH_ID_KEYS = ("sample_id", "event_id", "frame_id", "id")
_TARGET_ID_KEYS = ("target_id", "source_target_id")
_CAPTURE_TIME_KEYS = ("captured_at_s", "timestamp_s", "observed_at_s")


@dataclass(frozen=True, slots=True)
class GeolocationSurveyResult:
    """Joined samples and deterministic selection diagnostics."""

    report: dict[str, object]
    source_manifest: Path
    truth_row_count: int
    eligible_event_count: int
    matched_event_count: int
    skipped_event_count: int
    unmatched_event_ids: tuple[str, ...]
    unused_truth_ids: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        """Return JSON-compatible selection metadata for CLI/log consumers."""

        return {
            "source_manifest": str(self.source_manifest),
            "truth_row_count": self.truth_row_count,
            "eligible_event_count": self.eligible_event_count,
            "matched_event_count": self.matched_event_count,
            "skipped_event_count": self.skipped_event_count,
            "unmatched_event_ids": list(self.unmatched_event_ids),
            "unused_truth_ids": list(self.unused_truth_ids),
            "report_sample_count": self.report["measurement"]["sample_count"],  # type: ignore[index]
            "gate_passed": self.report["gate_passed"],
        }


@dataclass(frozen=True, slots=True)
class _TruthRow:
    key: str
    latitude_deg: float
    longitude_deg: float
    target_id: str | None
    captured_at_s: float | None


def load_survey_truth(path: str | Path) -> tuple[_TruthRow, ...]:
    """Load independently surveyed coordinates from CSV, JSON, or JSONL.

    A truth row must carry one identifier from ``sample_id``, ``event_id``,
    ``frame_id`` or ``id`` and a latitude/longitude pair.  The identifier is
    deliberately explicit: positional joins are unsafe when a replay is
    filtered or a frame is dropped.
    """

    source = Path(path).expanduser().resolve()
    rows = _load_rows(source)
    parsed: list[_TruthRow] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(f"truth row {index} must be an object")
        key = _row_identifier(row, index)
        if key in seen:
            raise ValueError(f"truth identifier {key!r} is duplicated")
        seen.add(key)
        latitude, longitude = _coordinate_values(row, index)
        target_id = _optional_text(_first(row, _TARGET_ID_KEYS), "target_id", index)
        captured_at_s = _optional_number(_first(row, _CAPTURE_TIME_KEYS), "captured_at_s", index)
        parsed.append(
            _TruthRow(
                key=key,
                latitude_deg=latitude,
                longitude_deg=longitude,
                target_id=target_id,
                captured_at_s=captured_at_s,
            )
        )
    if not parsed:
        raise ValueError("truth contains no rows")
    return tuple(parsed)


def build_accuracy_report_from_target_event_replay(
    replay_manifest: str | Path,
    truth: str | Path | Sequence[Mapping[str, object]],
    *,
    minimum_samples: int = 10,
    maximum_p95_m: float | None = None,
    minimum_one_sigma_coverage: float | None = None,
    minimum_two_sigma_coverage: float | None = None,
    target_id: str | None = None,
    allow_unmatched_events: bool = False,
    allow_extra_truth: bool = False,
    max_truth_time_delta_s: float | None = None,
) -> GeolocationSurveyResult:
    """Join a verified replay bundle with independent truth and evaluate it.

    ``event_id`` is preferred for a truth key, while ``frame_id`` is accepted
    for field survey sheets that were labelled from the camera frame.  If both
    identifiers resolve to different truth rows the operation fails closed.
    By default every fresh/qualified event in the selected target stream and
    every truth row must participate in the join.  The two ``allow_*`` flags
    are explicit diagnostics switches and are reported in the returned
    selection object; they never weaken GPS quality or report recomputation.
    """

    if type(allow_unmatched_events) is not bool:
        raise ValueError("allow_unmatched_events must be a boolean")
    if type(allow_extra_truth) is not bool:
        raise ValueError("allow_extra_truth must be a boolean")
    if target_id is not None:
        target_id = _required_text(target_id, "target_id")
    if max_truth_time_delta_s is not None:
        max_truth_time_delta_s = _finite_non_negative(
            max_truth_time_delta_s, "max_truth_time_delta_s"
        )

    manifest_path = Path(replay_manifest).expanduser().resolve()
    bundle = load_target_event_replay_manifest(manifest_path)
    truth_rows = (
        load_survey_truth(truth)
        if isinstance(truth, (str, Path))
        else _truth_rows_from_mappings(truth)
    )
    truth_by_key = {row.key: row for row in truth_rows}

    selected_events = tuple(bundle.iter_events(target_id=target_id))
    events_by_key: dict[str, TargetEvent] = {}
    for event in selected_events:
        for key in (event.event_id, event.frame_id):
            previous = events_by_key.get(key)
            if previous is not None and previous.event_id != event.event_id:
                raise ValueError(f"replay identifier {key!r} is shared by multiple events")
            events_by_key[key] = event

    samples: list[GeolocationAccuracySample] = []
    matched_truth_keys: set[str] = set()
    unmatched_event_ids: list[str] = []
    skipped_event_count = 0
    eligible_event_count = 0

    for event in selected_events:
        # A qualified status with an omitted timestamp is legacy evidence, not
        # a contemporaneous outdoor measurement.  Requiring ``fresh`` here
        # keeps this report stricter than the backwards-compatible replay
        # reader and prevents old GPS from looking like a live fix.
        if not event.gps.qualified or event.gps_freshness != "fresh":
            skipped_event_count += 1
            continue
        if (
            event.gps.latitude_deg is None
            or event.gps.longitude_deg is None
            or event.gps.horizontal_sigma_m is None
        ):
            skipped_event_count += 1
            continue
        eligible_event_count += 1

        candidates = {
            truth_by_key[key].key: truth_by_key[key]
            for key in (event.event_id, event.frame_id)
            if key in truth_by_key
        }
        if not candidates:
            unmatched_event_ids.append(event.event_id)
            continue
        if len(candidates) > 1:
            raise ValueError(
                f"event {event.event_id!r} matches conflicting truth rows "
                f"{sorted(candidates)!r} via event_id/frame_id"
            )
        truth_row = next(iter(candidates.values()))
        matched_truth_keys.add(truth_row.key)
        if truth_row.target_id is not None and truth_row.target_id != event.target_id:
            raise ValueError(
                f"truth row {truth_row.key!r} target_id does not match event {event.target_id!r}"
            )
        if (
            max_truth_time_delta_s is not None
            and truth_row.captured_at_s is not None
            and abs(truth_row.captured_at_s - event.captured_at_s) > max_truth_time_delta_s
        ):
            raise ValueError(
                f"truth row {truth_row.key!r} capture time is outside "
                f"{max_truth_time_delta_s:g}s tolerance"
            )
        samples.append(
            GeolocationAccuracySample(
                sample_id=event.event_id,
                estimated_latitude_deg=event.gps.latitude_deg,
                estimated_longitude_deg=event.gps.longitude_deg,
                ground_truth_latitude_deg=truth_row.latitude_deg,
                ground_truth_longitude_deg=truth_row.longitude_deg,
                reported_sigma_m=event.gps.horizontal_sigma_m,
                target_id=event.target_id,
                captured_at_s=event.captured_at_s,
            )
        )

    unused_truth_keys = sorted(set(truth_by_key).difference(matched_truth_keys))
    if unmatched_event_ids and not allow_unmatched_events:
        _raise_join_incomplete("unmatched fresh replay events", unmatched_event_ids)
    if unused_truth_keys and not allow_extra_truth:
        _raise_join_incomplete("truth rows not present in replay", unused_truth_keys)
    if not samples:
        raise ValueError("no fresh/qualified replay events matched independent truth")

    report = evaluate_geolocation_accuracy(
        samples,
        minimum_samples=minimum_samples,
        maximum_p95_m=maximum_p95_m,
        minimum_one_sigma_coverage=minimum_one_sigma_coverage,
        minimum_two_sigma_coverage=minimum_two_sigma_coverage,
    )
    source_payload = manifest_path.read_bytes()
    report["selection"] = {
        "source_manifest_sha256": hashlib.sha256(source_payload).hexdigest(),
        "source_manifest_bytes": len(source_payload),
        "truth_row_count": len(truth_rows),
        "selected_event_count": len(selected_events),
        "eligible_event_count": eligible_event_count,
        "matched_event_count": len(samples),
        "skipped_event_count": skipped_event_count,
        "unmatched_event_ids": list(unmatched_event_ids),
        "unused_truth_ids": list(unused_truth_keys),
        "target_id": target_id,
        "allow_unmatched_events": allow_unmatched_events,
        "allow_extra_truth": allow_extra_truth,
        "max_truth_time_delta_s": max_truth_time_delta_s,
    }
    return GeolocationSurveyResult(
        report=report,
        source_manifest=manifest_path,
        truth_row_count=len(truth_rows),
        eligible_event_count=eligible_event_count,
        matched_event_count=len(samples),
        skipped_event_count=skipped_event_count,
        unmatched_event_ids=tuple(unmatched_event_ids),
        unused_truth_ids=tuple(unused_truth_keys),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for replay-to-survey report generation."""

    args = build_parser().parse_args(argv)
    try:
        result = build_accuracy_report_from_target_event_replay(
            args.replay_manifest,
            args.truth,
            minimum_samples=args.minimum_samples,
            maximum_p95_m=args.maximum_p95_m,
            minimum_one_sigma_coverage=args.minimum_one_sigma_coverage,
            minimum_two_sigma_coverage=args.minimum_two_sigma_coverage,
            target_id=args.target_id,
            allow_unmatched_events=args.allow_unmatched_events,
            allow_extra_truth=args.allow_extra_truth,
            max_truth_time_delta_s=args.max_truth_time_delta_s,
        )
        _write_report(args.output, result.report)
    except (AutonomyContractError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"target event geolocation survey failed: {exc}", file=sys.stderr)
        return 2

    distance = result.report["distance_error_m"]
    measurement = result.report["measurement"]
    assert isinstance(distance, Mapping) and isinstance(measurement, Mapping)
    print(f"wrote {args.output}")
    print(f"source_manifest={result.source_manifest}")
    selection = result.report.get("selection")
    if isinstance(selection, Mapping):
        print(f"source_manifest_sha256={selection['source_manifest_sha256']}")
    print(f"truth_rows={result.truth_row_count}")
    print(f"eligible_events={result.eligible_event_count}")
    print(f"matched_events={result.matched_event_count}")
    print(f"skipped_events={result.skipped_event_count}")
    print(f"samples={measurement['sample_count']}")
    print(f"p95_error_m={distance['p95_m']:.6g}")
    print(f"gate_passed={result.report['gate_passed']}")
    return 0 if result.report["gate_passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "replay_manifest",
        type=Path,
        help="verified target-event replay manifest.json",
    )
    parser.add_argument("truth", type=Path, help="independent truth CSV/JSON/JSONL")
    parser.add_argument("--output", type=Path, required=True, help="accuracy report JSON")
    parser.add_argument("--target-id", help="evaluate only events for one target_id")
    parser.add_argument("--minimum-samples", type=int, default=10)
    parser.add_argument("--maximum-p95-m", type=float)
    parser.add_argument("--minimum-one-sigma-coverage", type=float)
    parser.add_argument("--minimum-two-sigma-coverage", type=float)
    parser.add_argument(
        "--allow-unmatched-events",
        action="store_true",
        help="allow fresh replay events without a truth row (explicit diagnostic mode)",
    )
    parser.add_argument(
        "--allow-extra-truth",
        action="store_true",
        help="allow truth rows that are not present in the selected replay stream",
    )
    parser.add_argument(
        "--max-truth-time-delta-s",
        type=float,
        help="optional maximum absolute difference from event captured_at_s",
    )
    return parser


def _truth_rows_from_mappings(
    rows: Sequence[Mapping[str, object]],
) -> tuple[_TruthRow, ...]:
    if isinstance(rows, (str, bytes, bytearray)):
        raise TypeError("truth mappings must be a sequence of objects")
    parsed: list[_TruthRow] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(f"truth row {index} must be an object")
        key = _row_identifier(row, index)
        if key in seen:
            raise ValueError(f"truth identifier {key!r} is duplicated")
        seen.add(key)
        latitude, longitude = _coordinate_values(row, index)
        parsed.append(
            _TruthRow(
                key=key,
                latitude_deg=latitude,
                longitude_deg=longitude,
                target_id=_optional_text(_first(row, _TARGET_ID_KEYS), "target_id", index),
                captured_at_s=_optional_number(
                    _first(row, _CAPTURE_TIME_KEYS), "captured_at_s", index
                ),
            )
        )
    if not parsed:
        raise ValueError("truth contains no rows")
    return tuple(parsed)


def _load_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError("truth CSV has no header")
            return tuple(reader)
    if suffix == ".json":
        try:
            decoded = strict_json_loads(
                path.read_text(encoding="utf-8-sig"),
                name="truth JSON",
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"truth file is not valid UTF-8 JSON: {exc}") from exc
        if isinstance(decoded, Mapping):
            for key in ("truth", "samples", "rows"):
                if key in decoded:
                    decoded = decoded[key]
                    break
        if isinstance(decoded, list) and all(isinstance(row, Mapping) for row in decoded):
            return tuple(decoded)
        raise ValueError("truth JSON must be a list or an object containing truth/samples/rows")
    if suffix == ".jsonl":
        rows: list[Mapping[str, object]] = []
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                value = strict_json_loads(line, name=f"truth JSONL line {line_number}")
            except (UnicodeError, ValueError) as exc:
                raise ValueError(
                    f"truth JSONL line {line_number} is not strict JSON: {exc}"
                ) from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"truth JSONL line {line_number} must be an object")
            rows.append(value)
        return tuple(rows)
    raise ValueError("truth must be a .csv, .json, or .jsonl file")


def _row_identifier(row: Mapping[str, object], index: int) -> str:
    values: list[str] = []
    for key in _TRUTH_ID_KEYS:
        value = _optional_text(row.get(key), key, index)
        if value is not None:
            values.append(value)
    if not values:
        raise ValueError(f"truth row {index} needs one of sample_id/event_id/frame_id/id")
    if len(set(values)) != 1:
        raise ValueError(f"truth row {index} contains conflicting identifiers")
    return values[0]


def _coordinate_values(row: Mapping[str, object], index: int) -> tuple[float, float]:
    latitude = _first(row, _TRUTH_LATITUDE_KEYS)
    longitude = _first(row, _TRUTH_LONGITUDE_KEYS)
    if latitude is None or longitude is None:
        for key in ("ground_truth", "truth", "survey"):
            nested = row.get(key)
            if isinstance(nested, Mapping):
                latitude = (
                    latitude
                    if latitude is not None
                    else _first(nested, ("latitude_deg", "latitude", "lat_deg", "lat"))
                )
                longitude = (
                    longitude
                    if longitude is not None
                    else _first(nested, ("longitude_deg", "longitude", "lon_deg", "lon"))
                )
    if latitude is None or longitude is None:
        raise ValueError(f"truth row {index} is missing latitude/longitude")
    return (
        _number(latitude, "ground_truth_latitude_deg", index),
        _number(longitude, "ground_truth_longitude_deg", index),
    )


def _first(row: Mapping[str, object], names: Iterable[str]) -> object | None:
    for name in names:
        if name in row:
            return row[name]
    return None


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object | None, name: str, index: int) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string when supplied (row {index})")
    return value.strip()


def _number(value: object, name: str, index: int) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric (row {index})")
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric (row {index})") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite (row {index})")
    return converted


def _optional_number(value: object | None, name: str, index: int) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    converted = _number(value, name, index)
    if converted < 0.0:
        raise ValueError(f"{name} must be non-negative (row {index})")
    return converted


def _finite_non_negative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return converted


def _raise_join_incomplete(name: str, values: Sequence[str]) -> None:
    preview = ", ".join(repr(value) for value in values[:8])
    suffix = "..." if len(values) > 8 else ""
    raise ValueError(f"{name}: {preview}{suffix}")


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "GeolocationSurveyResult",
    "build_accuracy_report_from_target_event_replay",
    "load_survey_truth",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
