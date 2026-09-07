"""Deterministic session verification for saved target-observation diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .target_observation_diagnostics import (
    TargetObservationDiagnosticsError,
    load_target_observation_diagnostics,
    write_json_atomic,
)
from .target_observation_recording_manifest import (
    MANIFEST_NAME,
    TargetObservationRecordingManifestError,
    verify_target_observation_recording_manifest,
)

SCHEMA = "multidetect.target-observation-diagnostics-session.v1"
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "target-observation-diagnostics-session.schema.json"


class TargetObservationDiagnosticsSessionError(ValueError):
    """Raised when a diagnostics session is ambiguous or internally inconsistent."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expand_inputs(inputs: Sequence[str | Path]) -> list[Path]:
    if not inputs:
        raise TargetObservationDiagnosticsSessionError("at least one input is required")
    candidates: list[Path] = []
    for raw in inputs:
        candidate = Path(raw).expanduser()
        if candidate.is_symlink():
            raise TargetObservationDiagnosticsSessionError(
                f"session input cannot be a symbolic link: {candidate}"
            )
        if candidate.is_dir():
            candidates.extend(sorted(candidate.glob("target-observation-diagnostics-*.json")))
        else:
            candidates.append(candidate)
    if not candidates:
        raise TargetObservationDiagnosticsSessionError(
            "no target-observation diagnostics JSON files were found"
        )
    resolved: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate.is_symlink():
            raise TargetObservationDiagnosticsSessionError(
                f"session input cannot be a symbolic link: {candidate}"
            )
        try:
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise TargetObservationDiagnosticsSessionError(
                f"session input cannot be resolved: {candidate}: {exc}"
            ) from exc
        if path in seen:
            raise TargetObservationDiagnosticsSessionError(f"duplicate session input: {path}")
        seen.add(path)
        resolved.append(path)
    return resolved


def _identity(document: Mapping[str, Any]) -> tuple[int, int, int] | None:
    readiness = document["readiness"]
    if not readiness["atomic_identity_matched"]:
        return None
    status = document["range_status"]
    return int(status["producerEpoch"]), int(status["sequence"]), int(status["sentAtMs"])


def _count_map(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _validate_report(report: Mapping[str, object]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(report),
        key=lambda item: list(item.path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise TargetObservationDiagnosticsSessionError(
            f"session report schema validation failed at {location}: {first.message}"
        )


def verify_target_observation_diagnostics_session(
    inputs: Sequence[str | Path],
    *,
    minimum_samples: int = 2,
    minimum_locatable_rate: float = 0.0,
    require_recording_manifest: bool = False,
) -> dict[str, object]:
    """Verify a chronological diagnostics session and return a source-bound report."""

    if (
        isinstance(minimum_samples, bool)
        or not isinstance(minimum_samples, int)
        or minimum_samples < 1
    ):
        raise TargetObservationDiagnosticsSessionError("minimum_samples must be at least 1")
    if (
        isinstance(minimum_locatable_rate, bool)
        or not isinstance(minimum_locatable_rate, (int, float))
        or not math.isfinite(float(minimum_locatable_rate))
        or not 0.0 <= minimum_locatable_rate <= 1.0
    ):
        raise TargetObservationDiagnosticsSessionError(
            "minimum_locatable_rate must be between 0 and 1"
        )

    if not isinstance(require_recording_manifest, bool):
        raise TargetObservationDiagnosticsSessionError(
            "require_recording_manifest must be a boolean"
        )
    recording_manifest: dict[str, object] | None = None
    session_inputs: Sequence[str | Path] = inputs
    if len(inputs) == 1:
        candidate = Path(inputs[0]).expanduser()
        manifest_path = candidate / MANIFEST_NAME if candidate.is_dir() else None
        if manifest_path is not None and manifest_path.exists():
            try:
                manifest_report = verify_target_observation_recording_manifest(manifest_path)
            except TargetObservationRecordingManifestError as exc:
                raise TargetObservationDiagnosticsSessionError(
                    f"invalid recording manifest: {exc}"
                ) from exc
            session_inputs = [item["path"] for item in manifest_report["snapshots"]]
            recording_manifest = {
                "path": manifest_report["manifest_path"],
                "bytes": manifest_report["manifest_bytes"],
                "sha256": manifest_report["manifest_sha256"],
                "session_id": manifest_report["session_id"],
                "stop_reason": manifest_report["stop_reason"],
                "sample_count": manifest_report["sample_count"],
                "directory_closed": manifest_report["directory_closed"],
            }
    if require_recording_manifest and recording_manifest is None:
        raise TargetObservationDiagnosticsSessionError(
            "a single recording directory with a valid manifest is required"
        )

    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in _expand_inputs(session_inputs):
        try:
            loaded.append(load_target_observation_diagnostics(path))
        except TargetObservationDiagnosticsError as exc:
            raise TargetObservationDiagnosticsSessionError(
                f"invalid snapshot {path}: {exc}"
            ) from exc
    loaded.sort(key=lambda item: (int(item[1]["local_now_ms"]), str(item[0]).casefold()))

    samples: list[dict[str, object]] = []
    reason_counts: Counter[str] = Counter()
    validity_counts: Counter[str] = Counter()
    gnss_reason_counts: Counter[str] = Counter()
    observed_epochs: list[int] = []
    retired_epochs: set[int] = set()
    active_epoch: int | None = None
    last_identity_by_epoch: dict[int, tuple[int, int]] = {}
    previous_local_now_ms: int | None = None
    epoch_transitions = 0
    locatable_count = 0
    current_nonlocatable_run = 0
    longest_nonlocatable_run = 0

    for index, (path, document) in enumerate(loaded):
        local_now_ms = int(document["local_now_ms"])
        if previous_local_now_ms is not None and local_now_ms <= previous_local_now_ms:
            raise TargetObservationDiagnosticsSessionError(
                "snapshot local_now_ms values must be strictly increasing"
            )
        previous_local_now_ms = local_now_ms
        readiness = document["readiness"]
        locatable = bool(readiness["locatable"])
        if locatable:
            locatable_count += 1
            current_nonlocatable_run = 0
        else:
            current_nonlocatable_run += 1
            longest_nonlocatable_run = max(longest_nonlocatable_run, current_nonlocatable_run)
        diagnostic_reasons = list(readiness["reasons"])
        reason_counts.update(diagnostic_reasons)

        identity = _identity(document)
        producer_epoch: int | None = None
        sequence: int | None = None
        sent_at_ms: int | None = None
        if identity is not None:
            producer_epoch, sequence, sent_at_ms = identity
            if producer_epoch in retired_epochs:
                raise TargetObservationDiagnosticsSessionError(
                    f"retired producer epoch reappeared: {producer_epoch}"
                )
            if active_epoch is None:
                active_epoch = producer_epoch
                observed_epochs.append(producer_epoch)
            elif producer_epoch != active_epoch:
                retired_epochs.add(active_epoch)
                active_epoch = producer_epoch
                observed_epochs.append(producer_epoch)
                epoch_transitions += 1
            previous_identity = last_identity_by_epoch.get(producer_epoch)
            if previous_identity is not None:
                if sequence < previous_identity[0]:
                    raise TargetObservationDiagnosticsSessionError(
                        f"sequence rollback in producer epoch {producer_epoch}"
                    )
                if sent_at_ms < previous_identity[1]:
                    raise TargetObservationDiagnosticsSessionError(
                        f"sentAtMs rollback in producer epoch {producer_epoch}"
                    )
            last_identity_by_epoch[producer_epoch] = (sequence, sent_at_ms)

        range_status = document["range_status"]
        gnss_status = document["gnss_status"]
        range_validity = range_status.get("validity")
        if isinstance(range_validity, str) and range_validity:
            validity_counts[range_validity] += 1
        gnss_reason = gnss_status.get("reason")
        if isinstance(gnss_reason, str) and gnss_reason:
            gnss_reason_counts[gnss_reason] += 1
        gnss_qualified = gnss_status.get("qualified")
        samples.append(
            {
                "index": index,
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "generated_at_utc": document["generated_at_utc"],
                "local_now_ms": local_now_ms,
                "locatable": locatable,
                "diagnostic_reasons": diagnostic_reasons,
                "atomic_identity_matched": identity is not None,
                "producer_epoch": producer_epoch,
                "sequence": sequence,
                "sent_at_ms": sent_at_ms,
                "range_validity": range_validity if isinstance(range_validity, str) else None,
                "gnss_qualified": gnss_qualified if isinstance(gnss_qualified, bool) else None,
                "gnss_reason": gnss_reason if isinstance(gnss_reason, str) else None,
            }
        )

    sample_count = len(samples)
    locatable_rate = locatable_count / sample_count
    gate_reasons: list[str] = []
    if sample_count < minimum_samples:
        gate_reasons.append(
            f"sample_count {sample_count} is below minimum_samples {minimum_samples}"
        )
    if locatable_rate < minimum_locatable_rate:
        gate_reasons.append(
            f"locatable_rate {locatable_rate:.6f} is below minimum {minimum_locatable_rate:.6f}"
        )
    report: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "session_started_at_utc": samples[0]["generated_at_utc"],
        "session_ended_at_utc": samples[-1]["generated_at_utc"],
        "sample_count": sample_count,
        "locatable_count": locatable_count,
        "non_locatable_count": sample_count - locatable_count,
        "locatable_rate": locatable_rate,
        "longest_non_locatable_run": longest_nonlocatable_run,
        "epoch_transition_count": epoch_transitions,
        "observed_epochs": observed_epochs,
        "reason_counts": _count_map(reason_counts),
        "range_validity_counts": _count_map(validity_counts),
        "gnss_reason_counts": _count_map(gnss_reason_counts),
        "requirements": {
            "minimum_samples": minimum_samples,
            "minimum_locatable_rate": minimum_locatable_rate,
            "recording_manifest_required": require_recording_manifest,
        },
        "recording_manifest": recording_manifest,
        "samples": samples,
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": not gate_reasons,
        "reasons": gate_reasons,
    }
    _validate_report(report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a chronological session of MultiDetectGCS diagnostics snapshots."
    )
    parser.add_argument("input", nargs="+", type=Path, help="snapshot file(s) or directories")
    parser.add_argument("--minimum-samples", type=int, default=2)
    parser.add_argument("--minimum-locatable-rate", type=float, default=0.0)
    parser.add_argument(
        "--require-recording-manifest",
        action="store_true",
        help="require one native recording directory and verify its closed manifest",
    )
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify_target_observation_diagnostics_session(
            args.input,
            minimum_samples=args.minimum_samples,
            minimum_locatable_rate=args.minimum_locatable_rate,
            require_recording_manifest=args.require_recording_manifest,
        )
    except (TargetObservationDiagnosticsSessionError, OSError, ValueError) as exc:
        report = {
            "schema": "multidetect.target-observation-diagnostics-session-verification/v1",
            "schema_version": 1,
            "advisory_only": True,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
            "gate_passed": False,
            "reasons": [str(exc)],
        }
    if args.out is not None:
        write_json_atomic(args.out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA",
    "SCHEMA_PATH",
    "TargetObservationDiagnosticsSessionError",
    "main",
    "verify_target_observation_diagnostics_session",
]
