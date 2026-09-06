"""Generate an independent-survey worksheet from a verified target replay.

The target-event replay already contains the estimated WGS-84 coordinate, the
reported horizontal uncertainty and the event identifiers.  A field operator
should not retype those values into a truth sheet: doing so is an easy way to
swap frames or accidentally turn an old GPS sample into a new one.  This
module verifies the replay first, selects only ``qualified`` and ``fresh``
GPS events, and writes a worksheet whose ground-truth columns are deliberately
blank.

The output is a worksheet, not accuracy evidence.  It contains no truth and
always reports ``gate_passed=false`` in the optional JSON envelope/summary.
After an operator fills the two ground-truth coordinate columns, the result
can be passed to :mod:`multidetect.geolocation_survey` or the existing
``validate_target_geolocation_accuracy.py`` command.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .autonomy.serialization import AutonomyContractError, require_text
from .target_event_replay import TargetEvent, load_target_event_replay_manifest

TEMPLATE_SCHEMA_VERSION = 1
TEMPLATE_EVIDENCE_TYPE = "multidetect.target-geolocation-survey-template.v1"

# Keep this order stable.  It makes the CSV usable in spreadsheet programs and
# keeps generated templates byte-reproducible for the same replay bundle.
TEMPLATE_COLUMNS = (
    "sample_id",
    "event_id",
    "source_frame_id",
    "target_id",
    "target_label",
    "captured_at_s",
    "gps_observed_at_s",
    "gps_age_s",
    "gps_freshness",
    "estimated_latitude_deg",
    "estimated_longitude_deg",
    "reported_sigma_m",
    "ground_truth_latitude_deg",
    "ground_truth_longitude_deg",
    "surveyed_at_utc",
    "survey_method",
    "operator_notes",
)


@dataclass(frozen=True, slots=True)
class GeolocationSurveyTemplateResult:
    """A deterministic worksheet and the selection diagnostics behind it."""

    rows: tuple[dict[str, object], ...]
    source_manifest: Path
    source_manifest_sha256: str
    source_manifest_bytes: int
    selected_event_count: int
    eligible_event_count: int
    skipped_event_count: int
    skipped_event_ids: tuple[str, ...]
    skipped_reasons: Mapping[str, int]
    target_id: str | None
    start_s: float | None
    end_s: float | None

    def summary(self) -> dict[str, object]:
        """Return a JSON-compatible summary suitable for logs and CI."""

        return {
            "event": "multidetect.target-geolocation-survey-template-created",
            "schema_version": TEMPLATE_SCHEMA_VERSION,
            "evidence_type": TEMPLATE_EVIDENCE_TYPE,
            "ground_truth_isolated": True,
            "advisory_only": True,
            "contains_flight_control": False,
            "contains_truth": False,
            "gate_passed": False,
            "source_manifest": str(self.source_manifest),
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_manifest_bytes": self.source_manifest_bytes,
            "target_id": self.target_id,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "selected_event_count": self.selected_event_count,
            "eligible_event_count": self.eligible_event_count,
            "skipped_event_count": self.skipped_event_count,
            "skipped_event_ids": list(self.skipped_event_ids),
            "skipped_reasons": dict(self.skipped_reasons),
            "row_count": len(self.rows),
            "required_truth_columns": [
                "ground_truth_latitude_deg",
                "ground_truth_longitude_deg",
            ],
        }

    def json_envelope(self) -> dict[str, object]:
        """Return the JSON worksheet form, including non-evidence metadata."""

        summary = self.summary()
        summary.pop("source_manifest", None)
        return {
            **summary,
            "rows": [dict(row) for row in self.rows],
        }


def build_geolocation_survey_template(
    replay_manifest: str | Path,
    *,
    target_id: str | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
) -> GeolocationSurveyTemplateResult:
    """Build a truth worksheet from a verified replay manifest.

    ``load_target_event_replay_manifest`` performs the bundle/media/hash
    verification before any row is materialized.  A qualified event with a
    missing observation timestamp is classified as ``gps_timestamp_unknown``
    and is intentionally excluded: it is readable legacy evidence but not a
    contemporaneous outdoor measurement.
    """

    normalized_target = _optional_text(target_id, "target_id")
    start = _optional_non_negative(start_s, "start_s")
    end = _optional_non_negative(end_s, "end_s")
    if start is not None and end is not None and end < start:
        raise AutonomyContractError("survey template end_s must not be before start_s")

    manifest_path = Path(replay_manifest).expanduser().resolve()
    bundle = load_target_event_replay_manifest(manifest_path)
    source_payload = manifest_path.read_bytes()
    source_sha256 = hashlib.sha256(source_payload).hexdigest()
    events = tuple(bundle.iter_events(target_id=normalized_target, start_s=start, end_s=end))

    rows: list[dict[str, object]] = []
    skipped_ids: list[str] = []
    skipped_reasons: Counter[str] = Counter()
    for event in events:
        reason = _ineligible_reason(event)
        if reason is not None:
            skipped_ids.append(event.event_id)
            skipped_reasons[reason] += 1
            continue
        # ``_ineligible_reason`` verifies all values required below.  Keep
        # explicit assertions so a future TargetGpsEvidence change fails at
        # development time instead of silently producing a partial worksheet.
        gps = event.gps
        assert gps.latitude_deg is not None
        assert gps.longitude_deg is not None
        assert gps.horizontal_sigma_m is not None
        rows.append(
            {
                "sample_id": event.event_id,
                "event_id": event.event_id,
                "source_frame_id": event.source_frame_id or event.frame_id,
                "target_id": event.target_id,
                "target_label": event.target_label,
                "captured_at_s": event.captured_at_s,
                "gps_observed_at_s": gps.observed_at_s,
                "gps_age_s": event.gps_age_s,
                "gps_freshness": event.gps_freshness,
                "estimated_latitude_deg": gps.latitude_deg,
                "estimated_longitude_deg": gps.longitude_deg,
                "reported_sigma_m": gps.horizontal_sigma_m,
                # These fields are the only values a survey operator should
                # fill before invoking the accuracy evaluator.
                "ground_truth_latitude_deg": None,
                "ground_truth_longitude_deg": None,
                "surveyed_at_utc": None,
                "survey_method": None,
                "operator_notes": None,
            }
        )

    return GeolocationSurveyTemplateResult(
        rows=tuple(rows),
        source_manifest=manifest_path,
        source_manifest_sha256=source_sha256,
        source_manifest_bytes=len(source_payload),
        selected_event_count=len(events),
        eligible_event_count=len(rows),
        skipped_event_count=len(skipped_ids),
        skipped_event_ids=tuple(skipped_ids),
        skipped_reasons=dict(sorted(skipped_reasons.items())),
        target_id=normalized_target,
        start_s=start,
        end_s=end,
    )


def write_geolocation_survey_template(
    path: str | Path,
    result: GeolocationSurveyTemplateResult,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write a CSV, JSON or JSONL worksheet.

    Existing files are protected by default because an already-filled truth
    sheet is valuable evidence.  ``overwrite=True`` is an explicit operator
    choice for regenerating a template from a new replay.
    """

    if type(overwrite) is not bool:
        raise ValueError("overwrite must be a boolean")
    destination = Path(path).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"survey template already exists: {destination} (use --overwrite to replace it)"
        )
    suffix = destination.suffix.lower()
    if suffix not in {".csv", ".json", ".jsonl"}:
        raise ValueError("survey template output must use .csv, .json, or .jsonl")

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            if suffix == ".csv":
                writer = csv.DictWriter(
                    handle,
                    fieldnames=TEMPLATE_COLUMNS,
                    extrasaction="ignore",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(result.rows)
            elif suffix == ".json":
                json.dump(
                    result.json_envelope(),
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
            else:
                for row in result.rows:
                    handle.write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
                    handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_template_summary(
    path: str | Path,
    result: GeolocationSurveyTemplateResult,
    *,
    output_path: Path,
    overwrite: bool = False,
) -> Path:
    """Write the optional JSON summary sidecar atomically."""

    summary = result.summary()
    summary["output"] = str(output_path)
    summary["output_format"] = output_path.suffix.lower().lstrip(".")
    destination = Path(path).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"survey template summary already exists: {destination} (use --overwrite to replace it)"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(
                summary,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _ineligible_reason(event: TargetEvent) -> str | None:
    if not event.gps.qualified:
        return "gps_not_qualified"
    if event.gps.observed_at_s is None:
        return "gps_timestamp_unknown"
    if event.gps_freshness != "fresh":
        return "gps_not_fresh"
    if (
        event.gps.latitude_deg is None
        or event.gps.longitude_deg is None
        or event.gps.horizontal_sigma_m is None
    ):
        return "gps_measurement_missing"
    return None


def _optional_text(value: object | None, name: str) -> str | None:
    if value is None:
        return None
    return require_text(value, name)


def _optional_non_negative(value: object | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and non-negative")
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return converted


def build_parser() -> argparse.ArgumentParser:
    """Build the installed ``multidetect-target-survey-template`` CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "replay_manifest",
        type=Path,
        help="verified target-event replay manifest.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="worksheet output (.csv, .json or .jsonl)",
    )
    parser.add_argument("--target-id", help="only include events for one target_id")
    parser.add_argument("--start-s", type=float, help="inclusive capture-time lower bound")
    parser.add_argument("--end-s", type=float, help="inclusive capture-time upper bound")
    parser.add_argument("--summary-output", type=Path, help="optional JSON summary sidecar")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing worksheet/summary files explicitly",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Create a worksheet and return non-zero when no fresh event is available."""

    args = build_parser().parse_args(argv)
    try:
        result = build_geolocation_survey_template(
            args.replay_manifest,
            target_id=args.target_id,
            start_s=args.start_s,
            end_s=args.end_s,
        )
        output_path = write_geolocation_survey_template(
            args.output,
            result,
            overwrite=args.overwrite,
        )
        summary_path = None
        if args.summary_output is not None:
            summary_path = write_template_summary(
                args.summary_output,
                result,
                output_path=output_path,
                overwrite=args.overwrite,
            )
    except (AutonomyContractError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"target geolocation survey template failed: {exc}", file=sys.stderr)
        return 2

    summary = result.summary()
    summary["output"] = str(output_path)
    summary["output_format"] = output_path.suffix.lower().lstrip(".")
    if summary_path is not None:
        summary["summary_output"] = str(summary_path)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
    return 0 if result.eligible_event_count > 0 else 1


__all__ = [
    "GeolocationSurveyTemplateResult",
    "TEMPLATE_COLUMNS",
    "TEMPLATE_EVIDENCE_TYPE",
    "TEMPLATE_SCHEMA_VERSION",
    "build_geolocation_survey_template",
    "build_parser",
    "main",
    "write_geolocation_survey_template",
    "write_template_summary",
]
