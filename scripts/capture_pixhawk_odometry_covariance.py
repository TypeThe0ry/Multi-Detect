#!/usr/bin/env python3
"""Capture source-bound MAVLink ODOMETRY attitude covariance evidence.

The tool may transmit only MAV_CMD_SET_MESSAGE_INTERVAL requests on the selected
companion link.  It never sends a flight-mode, mission, parameter-write, or
actuator command.  SIM/HIL/REAL use the same Pixhawk telemetry provider and the
same report contract; the selected runtime mode changes claims, not parsing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.pixhawk import (  # noqa: E402
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
)

SCHEMA = "multidetect.pixhawk-odometry-covariance-observation/v1"
UNCERTAINTY_SOURCE = "mavlink_odometry_pose_covariance_local_ned_with_configured_floor"
_LOCAL_UDP = re.compile(r"udpin:(?:0\.0\.0\.0|127\.0\.0\.1):(\d{1,5})")


class ObservationEvidenceError(ValueError):
    """Raised when stored covariance evidence cannot be reproduced exactly."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_new(path: Path, document: Mapping[str, object]) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite evidence: {target}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _record_from_diagnostics(
    diagnostics: Mapping[str, object],
    *,
    sample_index: int,
) -> dict[str, object] | None:
    raw = diagnostics.get("attitude_uncertainty")
    if not isinstance(raw, Mapping) or raw.get("source") != UNCERTAINTY_SOURCE:
        return None
    covariance = raw.get("raw_angular_covariance_rad2")
    if not isinstance(covariance, Sequence) or isinstance(covariance, (str, bytes, bytearray)):
        return None
    return {
        "sample_index": sample_index,
        "accepted_count": raw.get("accepted_count"),
        "observed_at_s": raw.get("observed_at_s"),
        "age_s": raw.get("age_s"),
        "associated_with_attitude": raw.get("associated_with_attitude"),
        "source": raw.get("source"),
        "child_frame_id": raw.get("child_frame_id"),
        "reset_counter": raw.get("reset_counter"),
        "roll_sigma_deg": raw.get("roll_sigma_deg"),
        "pitch_sigma_deg": raw.get("pitch_sigma_deg"),
        "heading_sigma_deg": raw.get("heading_sigma_deg"),
        "raw_angular_covariance_rad2": list(covariance),
    }


def build_observation_report(
    *,
    runtime_mode: str,
    endpoint: str,
    started_at_utc: str,
    finished_at_utc: str,
    duration_s: float,
    samples_requested: int,
    records: Sequence[Mapping[str, object]],
    diagnostics: Mapping[str, object],
    minimum_odometry_messages: int,
    minimum_accepted_covariances: int,
) -> dict[str, object]:
    message_counts = diagnostics.get("message_type_counts")
    uncertainty = diagnostics.get("attitude_uncertainty")
    requests = diagnostics.get("ranging_stream_requests")
    if not isinstance(message_counts, Mapping):
        message_counts = {}
    if not isinstance(uncertainty, Mapping):
        uncertainty = {}
    if not isinstance(requests, Mapping):
        requests = {}
    requested_intervals = requests.get("requested_message_intervals_us")
    if not isinstance(requested_intervals, Mapping):
        requested_intervals = {}
    attempts = requests.get("attempts")
    attempts = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    transmitted = diagnostics.get("messages_transmitted")
    transmitted = (
        transmitted if isinstance(transmitted, int) and not isinstance(transmitted, bool) else -1
    )
    odometry_count = message_counts.get("ODOMETRY", 0)
    odometry_count = (
        odometry_count
        if isinstance(odometry_count, int) and not isinstance(odometry_count, bool)
        else 0
    )
    accepted_count = uncertainty.get("accepted_count", 0)
    accepted_count = (
        accepted_count
        if isinstance(accepted_count, int) and not isinstance(accepted_count, bool)
        else 0
    )

    records_valid = bool(records) and all(
        record.get("source") == UNCERTAINTY_SOURCE
        and record.get("associated_with_attitude") is True
        and record.get("child_frame_id") in {1, 12}
        and all(
            _finite_positive(record.get(field))
            for field in ("roll_sigma_deg", "pitch_sigma_deg", "heading_sigma_deg")
        )
        and isinstance(record.get("raw_angular_covariance_rad2"), Sequence)
        and len(record["raw_angular_covariance_rad2"]) == 9  # type: ignore[arg-type]
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in record["raw_angular_covariance_rad2"]  # type: ignore[union-attr]
        )
        and all(
            float(record["raw_angular_covariance_rad2"][index]) > 0.0  # type: ignore[index]
            for index in (0, 4, 8)
        )
        for record in records
    )
    checks = {
        "runtime_mode_valid": runtime_mode in {"sim", "hil", "real"},
        "odometry_message_count": odometry_count >= minimum_odometry_messages,
        "accepted_covariance_count": accepted_count >= minimum_accepted_covariances,
        "captured_records_valid": records_valid,
        "latest_covariance_attitude_associated": (
            uncertainty.get("associated_with_attitude") is True
        ),
        "latest_covariance_source_exact": uncertainty.get("source") == UNCERTAINTY_SOURCE,
        "odometry_interval_requested": requested_intervals.get("ODOMETRY") == 50_000,
        "telemetry_requests_only": (
            attempts > 0
            and transmitted == attempts * len(requested_intervals)
            and diagnostics.get("hardware_control_enabled") is False
        ),
    }
    parity_path = ROOT / "configs" / "release-parity.json"
    source_path = Path(__file__).resolve()
    provider_path = ROOT / "src" / "multidetect" / "pixhawk.py"
    without_hash: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "release_status": "0.3.0-pre.8",
        "runtime_mode": runtime_mode,
        "endpoint": endpoint,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "duration_s": duration_s,
        "samples_requested": samples_requested,
        "minimum_odometry_messages": minimum_odometry_messages,
        "minimum_accepted_covariances": minimum_accepted_covariances,
        "odometry_messages_observed": odometry_count,
        "accepted_covariances_observed": accepted_count,
        "records": list(records),
        "final_diagnostics": dict(diagnostics),
        "checks": checks,
        "source_binding": {
            "capture_script": str(source_path.relative_to(ROOT)).replace("\\", "/"),
            "capture_script_sha256": _sha256(source_path),
            "provider": str(provider_path.relative_to(ROOT)).replace("\\", "/"),
            "provider_sha256": _sha256(provider_path),
            "release_parity_path": str(parity_path.relative_to(ROOT)).replace("\\", "/"),
            "release_parity_sha256": _sha256(parity_path),
            "release_parity_hashes": json.loads(parity_path.read_text(encoding="utf-8"))["parity"],
        },
        "claims": {
            "estimator_attitude_covariance_observed": bool(checks["captured_records_valid"]),
            "sim_acceptance": (
                "passed" if runtime_mode == "sim" and all(checks.values()) else "not_claimed"
            ),
            "hil_acceptance": "not_claimed",
            "real_acceptance": "not_claimed",
            "hardware_acceptance": "not_claimed",
            "flight_acceptance": "not_claimed",
        },
        "gate_passed": all(checks.values()),
    }
    without_hash["evidence_sha256"] = canonical_sha256(without_hash)
    return without_hash


def _required_int(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ObservationEvidenceError(f"{key} must be an integer")
    return value


def _required_float(document: Mapping[str, object], key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationEvidenceError(f"{key} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ObservationEvidenceError(f"{key} must be finite")
    return converted


def verify_observation_report(document: Mapping[str, object]) -> dict[str, object]:
    """Rebuild and verify a stored observation against the current source tree.

    The verifier rejects a report whose canonical digest was merely recomputed
    after editing: every derived check, claim, count, and source binding is
    rebuilt from the stored raw records and diagnostics before exact comparison.
    """

    if document.get("schema") != SCHEMA or document.get("schema_version") != 1:
        raise ObservationEvidenceError("unsupported evidence schema")
    stored_hash = document.get("evidence_sha256")
    if not isinstance(stored_hash, str):
        raise ObservationEvidenceError("evidence_sha256 must be a string")
    unhashed = {key: value for key, value in document.items() if key != "evidence_sha256"}
    if stored_hash != canonical_sha256(unhashed):
        raise ObservationEvidenceError("evidence_sha256 mismatch")

    runtime_mode = document.get("runtime_mode")
    endpoint = document.get("endpoint")
    started_at_utc = document.get("started_at_utc")
    finished_at_utc = document.get("finished_at_utc")
    records = document.get("records")
    diagnostics = document.get("final_diagnostics")
    if runtime_mode not in {"sim", "hil", "real"}:
        raise ObservationEvidenceError("runtime_mode is invalid")
    if not isinstance(endpoint, str):
        raise ObservationEvidenceError("endpoint must be a string")
    endpoint_match = _LOCAL_UDP.fullmatch(endpoint)
    if endpoint_match is None or int(endpoint_match.group(1)) == 14_550:
        raise ObservationEvidenceError("endpoint is not an isolated local UDP listener")
    if not isinstance(started_at_utc, str) or not isinstance(finished_at_utc, str):
        raise ObservationEvidenceError("observation timestamps must be strings")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise ObservationEvidenceError("records must be an array")
    if not all(isinstance(record, Mapping) for record in records):
        raise ObservationEvidenceError("every record must be an object")
    if not isinstance(diagnostics, Mapping):
        raise ObservationEvidenceError("final_diagnostics must be an object")

    rebuilt = build_observation_report(
        runtime_mode=runtime_mode,
        endpoint=endpoint,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        duration_s=_required_float(document, "duration_s"),
        samples_requested=_required_int(document, "samples_requested"),
        records=records,
        diagnostics=diagnostics,
        minimum_odometry_messages=_required_int(document, "minimum_odometry_messages"),
        minimum_accepted_covariances=_required_int(document, "minimum_accepted_covariances"),
    )
    if dict(document) != rebuilt:
        differing = sorted(
            key for key in set(document) | set(rebuilt) if document.get(key) != rebuilt.get(key)
        )
        raise ObservationEvidenceError(
            "evidence does not reproduce from raw observations: " + ", ".join(differing)
        )
    return {
        "schema": SCHEMA,
        "verified": True,
        "gate_passed": rebuilt["gate_passed"],
        "runtime_mode": runtime_mode,
        "records_verified": len(records),
        "evidence_sha256": stored_hash,
    }


def verify_observation_file(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationEvidenceError(f"cannot read evidence: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ObservationEvidenceError("evidence root must be an object")
    return verify_observation_report(document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--runtime-mode", choices=("sim", "hil", "real"))
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--interval-seconds", type=float, default=0.1)
    parser.add_argument("--minimum-odometry-messages", type=int, default=20)
    parser.add_argument("--minimum-accepted-covariances", type=int, default=10)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verify is not None:
        if args.endpoint is not None or args.runtime_mode is not None or args.out is not None:
            raise ValueError("--verify cannot be combined with capture arguments")
        verification = verify_observation_file(args.verify)
        print(json.dumps(verification, ensure_ascii=False, allow_nan=False, indent=2))
        return 0 if verification["gate_passed"] else 1
    if args.endpoint is None or args.runtime_mode is None or args.out is None:
        raise ValueError("capture requires --endpoint, --runtime-mode, and --out")
    endpoint_match = _LOCAL_UDP.fullmatch(args.endpoint)
    if endpoint_match is None or int(endpoint_match.group(1)) == 14_550:
        raise ValueError("endpoint must be an isolated local udpin port other than 14550")
    if args.samples <= 0:
        raise ValueError("samples must be positive")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0.0:
        raise ValueError("interval-seconds must be finite and positive")
    if args.minimum_odometry_messages <= 0 or args.minimum_accepted_covariances <= 0:
        raise ValueError("minimum counts must be positive")

    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            args.endpoint,
            expected_system_id=1,
            expected_autopilot_id=12,
            request_ranging_streams=True,
        )
    )
    records: list[dict[str, object]] = []
    last_accepted_count = 0
    started_at_utc = _utc_now()
    started = time.monotonic()
    try:
        for sample_index in range(args.samples):
            now_s = time.monotonic()
            provider.snapshot(now_s=now_s)
            diagnostics = provider.diagnostics(now_s=now_s)
            record = _record_from_diagnostics(diagnostics, sample_index=sample_index)
            if record is not None:
                accepted_count = record.get("accepted_count")
                if isinstance(accepted_count, int) and accepted_count > last_accepted_count:
                    records.append(record)
                    last_accepted_count = accepted_count
            if sample_index + 1 < args.samples:
                time.sleep(args.interval_seconds)
        finished = time.monotonic()
        final_diagnostics = provider.diagnostics(now_s=finished)
    finally:
        provider.close()
    report = build_observation_report(
        runtime_mode=args.runtime_mode,
        endpoint=args.endpoint,
        started_at_utc=started_at_utc,
        finished_at_utc=_utc_now(),
        duration_s=finished - started,
        samples_requested=args.samples,
        records=records,
        diagnostics=final_diagnostics,
        minimum_odometry_messages=args.minimum_odometry_messages,
        minimum_accepted_covariances=args.minimum_accepted_covariances,
    )
    _atomic_write_new(args.out, report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
