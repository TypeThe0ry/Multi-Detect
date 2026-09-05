"""Convert a receive-only GNSS capture report into replay telemetry JSONL.

``capture_gnss_samples`` deliberately stores wire-level GPS records rather than
pretending to know aircraft attitude, AGL, geofence or mission state.  The raw
report is useful evidence, but it is not yet the ``RawReplayTelemetrySample``
contract consumed by a video replay.  This module performs that explicit
conversion: it verifies the capture report, keeps every ``GPS_RAW_INT`` /
``GPS2_RAW`` sample (including unqualified samples), normalizes its monotonic
clock to a caller-selected origin, and fills unknown non-GPS fields with
unknown values.  No global-position-only sample is promoted to raw GPS quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .autonomy.serialization import AutonomyContractError
from .backends import RawReplayTelemetrySample
from .domain import VehicleTelemetry
from .gnss_capture import verify_gnss_capture_report


def create_gnss_replay_sidecar(
    capture_report: str | Path | Mapping[str, object],
    output: str | Path,
    *,
    time_origin_monotonic_s: float | None = None,
    time_offset_s: float = 0.0,
) -> dict[str, Any]:
    """Write a strict replay telemetry sidecar from a GNSS capture report.

    ``time_origin_monotonic_s`` defaults to the first raw GPS sample.  Use the
    explicit origin/offset options when synchronizing a separately started
    camera.  Resulting timestamps must be non-negative and strictly
    increasing; duplicate or out-of-order capture records are rejected.
    """

    document, source_path, source_sha256, source_bytes = _load_capture_report(capture_report)
    verification = verify_gnss_capture_report(document)
    if verification.get("passed") is not True:
        reasons = verification.get("reasons")
        rendered = (
            "; ".join(str(item) for item in reasons)
            if isinstance(reasons, list)
            else "invalid report"
        )
        raise AutonomyContractError(f"GNSS capture report failed verification: {rendered}")

    records = document.get("records")
    if not isinstance(records, list):  # verifier already checks this; retain a local guard
        raise AutonomyContractError("GNSS capture records must be an array")
    raw_records = tuple(
        record
        for record in records
        if isinstance(record, Mapping) and record.get("kind") == "gps_raw"
    )
    if not raw_records:
        raise AutonomyContractError(
            "GNSS capture contains no GPS_RAW_INT/GPS2_RAW records; "
            "GLOBAL_POSITION_INT alone cannot qualify raw GPS"
        )

    first_observed = _required_nonnegative_real(
        raw_records[0].get("observed_at_monotonic_s"),
        "records[0].observed_at_monotonic_s",
    )
    origin = first_observed if time_origin_monotonic_s is None else _finite_real(
        time_origin_monotonic_s, "time_origin_monotonic_s"
    )
    offset = _finite_real(time_offset_s, "time_offset_s")
    assert origin is not None
    assert offset is not None

    samples: list[RawReplayTelemetrySample] = []
    previous_capture_time: float | None = None
    previous_replay_time: float | None = None
    for index, record in enumerate(raw_records):
        capture_time = _required_nonnegative_real(
            record.get("observed_at_monotonic_s"),
            f"records[{index}].observed_at_monotonic_s",
        )
        if previous_capture_time is not None and capture_time <= previous_capture_time:
            raise AutonomyContractError(
                "GNSS raw records must be strictly increasing by observed_at_monotonic_s"
            )
        replay_time = capture_time - origin + offset
        if replay_time < 0.0 or not math.isfinite(replay_time):
            raise AutonomyContractError(
                f"records[{index}] maps to a negative/non-finite replay timestamp"
            )
        if previous_replay_time is not None and replay_time <= previous_replay_time:
            raise AutonomyContractError(
                "time_origin_monotonic_s/time_offset_s collapse raw timestamps"
            )
        previous_capture_time = capture_time
        previous_replay_time = replay_time
        samples.append(
            RawReplayTelemetrySample(
                observed_at_s=replay_time,
                telemetry=_telemetry_from_raw_record(record, replay_time),
            )
        )

    output_path = Path(output).expanduser().resolve()
    if output_path.suffix.lower() != ".jsonl":
        raise ValueError("GNSS replay sidecar output must use a .jsonl suffix")
    if output_path.exists():
        raise FileExistsError(f"GNSS replay sidecar already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(sample.to_mapping(), ensure_ascii=False, allow_nan=False, sort_keys=True)
        + "\n"
        for sample in samples
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "event": "gnss_capture_replay_sidecar_created",
        "schema_version": 1,
        "source_capture_report": str(source_path) if source_path is not None else None,
        "source_capture_report_sha256": source_sha256,
        "source_capture_report_bytes": source_bytes,
        "output": str(output_path),
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "output_bytes": len(payload),
        "raw_gps_sample_count": len(samples),
        "qualified_raw_gps_sample_count": sum(
            sample.telemetry.position_healthy is True for sample in samples
        ),
        "time_origin_monotonic_s": origin,
        "time_offset_s": offset,
        "time_match": "capture_monotonic_normalized_no_interpolation",
        "telemetry_mode": "gnss_capture_navigation_only",
        "global_position_promoted": False,
        "deployment_domain_evidence_complete": False,
        "accuracy_validated": False,
        "software_only": True,
        "gate_passed": False,
        "notes": [
            "Only GPS_RAW_INT/GPS2_RAW records are converted; "
            "GLOBAL_POSITION_INT is not promoted to raw GPS quality.",
            "Unknown attitude, AGL, geofence, mission and actuator predicates "
            "remain unknown or false.",
            "Use the sidecar with join_raw_replay_telemetry.py after synchronizing "
            "its monotonic time origin.",
        ],
    }


def _load_capture_report(
    report: str | Path | Mapping[str, object],
) -> tuple[Mapping[str, object], Path | None, str | None, int | None]:
    source_path: Path | None = None
    if isinstance(report, (str, Path)):
        source_path = Path(report).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise ValueError(f"capture_report must be a regular file: {source_path}")
        payload = source_path.read_bytes()
        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AutonomyContractError("GNSS capture report must be strict UTF-8 JSON") from exc
        source_sha256 = hashlib.sha256(payload).hexdigest()
        source_bytes = len(payload)
    else:
        document = report
        source_sha256 = None
        source_bytes = None
    if not isinstance(document, Mapping):
        raise AutonomyContractError("GNSS capture report must be a JSON object")
    return document, source_path, source_sha256, source_bytes


def _telemetry_from_raw_record(
    record: Mapping[str, object], replay_time: float
) -> VehicleTelemetry:
    qualified = record.get("qualified") is True
    gps_time = _optional_nonnegative_real(record.get("gps_observed_at_s"))
    if gps_time is not None:
        capture_time = _required_nonnegative_real(
            record.get("observed_at_monotonic_s"), "observed_at_monotonic_s"
        )
        gps_time = gps_time - capture_time + replay_time
        if gps_time < 0.0:
            raise AutonomyContractError(
                "gps_observed_at_s maps to a negative replay timestamp"
            )
    return VehicleTelemetry(
        altitude_agl_m=float("nan"),
        roll_deg=float("nan"),
        pitch_deg=float("nan"),
        ground_speed_mps=_number_or_nan(record.get("velocity_mps")),
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=qualified,
        link_healthy=True,
        flight_mode_allows_deploy=False,
        release_zone_clear=None,
        latitude_deg=_number_or_nan(record.get("latitude_deg")),
        longitude_deg=_number_or_nan(record.get("longitude_deg")),
        heading_deg=_number_or_nan(record.get("course_deg")),
        satellites_visible=_optional_integer(record.get("satellites_visible")),
        gps_fix_type=_optional_integer(record.get("fix_type")),
        gps_horizontal_accuracy_m=_number_or_nan(record.get("horizontal_accuracy_m")),
        gps_vertical_accuracy_m=_number_or_nan(record.get("vertical_accuracy_m")),
        gps_observed_at_s=gps_time if gps_time is not None else float("nan"),
        gps_absolute_altitude_m=_number_or_nan(record.get("altitude_m")),
        flight_mode=None,
        armed=None,
        position_observed_at_s=replay_time,
    )


def _number_or_nan(value: object) -> float:
    return float(value) if _is_finite_real(value) else float("nan")


def _optional_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _required_nonnegative_real(value: object, name: str) -> float:
    number = _finite_real(value, name)
    assert number is not None
    if number < 0.0:
        raise AutonomyContractError(f"{name} must be non-negative")
    return number


def _optional_nonnegative_real(value: object) -> float | None:
    if not _is_finite_real(value) or float(value) < 0.0:
        return None
    return float(value)


def _finite_real(value: object, name: str) -> float:
    if not _is_finite_real(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _is_finite_real(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate object keys instead of silently accepting last-wins JSON."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key!r}")
        result[key] = value
    return result


def build_parser():
    """Build the command-line parser for the installed console entry point."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_report", type=Path, help="verified GNSS capture JSON report")
    parser.add_argument("output", type=Path, help="new RawReplayTelemetrySample JSONL sidecar")
    parser.add_argument(
        "--time-origin-monotonic-s",
        type=float,
        help="capture monotonic timestamp mapped to replay t=0; default: first raw GPS sample",
    )
    parser.add_argument(
        "--time-offset-s",
        type=float,
        default=0.0,
        help="finite replay-time shift after origin normalization; default: 0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = create_gnss_replay_sidecar(
            args.capture_report,
            args.output,
            time_origin_monotonic_s=args.time_origin_monotonic_s,
            time_offset_s=args.time_offset_s,
        )
    except (AutonomyContractError, FileExistsError, OSError, ValueError) as exc:
        print(f"GNSS replay sidecar conversion failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


__all__ = ["build_parser", "create_gnss_replay_sidecar", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
