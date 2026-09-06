"""Verify one freshness-bound GR01 bench-to-E7 release session.

The producer runs the existing hardware bench and current release-gate wrapper.
This module is the independent consumer: it reopens the source-bound execution
plan, bench, manifest and evaluation report, reruns the canonical verifiers,
and writes a success record only when the planned configuration is the one that
reached the GR01 and E7 evidence.  The record does not upgrade the overall
release gate or any physical-flight claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .autonomy.serialization import strict_json_loads
from .gr01_bench import verify_gr01_link_bench_report
from .gr01_release_plan import (
    PLAN_SCHEMA,
    PLAN_SCHEMA_VERSION,
    verify_gr01_release_session_plan,
)
from .release_gate import evaluate_release_gate

SESSION_SCHEMA_VERSION = 2
SESSION_EVENT = "multidetect_gr01_release_session_verified"


class Gr01ReleaseSessionError(ValueError):
    """Raised when a GR01 release-session source or projection is invalid."""


def _load_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_bytes(), name=name)
    except OSError as exc:
        raise Gr01ReleaseSessionError(f"unable to read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise Gr01ReleaseSessionError(f"{name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Gr01ReleaseSessionError(f"unable to hash {path}: {exc}") from exc
    return digest.hexdigest()


def _source(path: str | Path, *, name: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise Gr01ReleaseSessionError(f"{name} must not be a symbolic link")
    resolved = requested.resolve()
    if not resolved.is_file():
        raise Gr01ReleaseSessionError(f"{name} does not exist: {resolved}")
    return resolved


def _relative(path: Path, *, directory: Path) -> str:
    try:
        value = path.relative_to(directory)
    except ValueError:
        value = Path(os.path.relpath(path, directory))
    return value.as_posix()


def _resolve_record_path(value: object, *, directory: Path, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Gr01ReleaseSessionError(f"{name}.path must be a non-empty string")
    requested = Path(value)
    if not requested.is_absolute():
        requested = directory / requested
    return _source(requested, name=name)


def _require_timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Gr01ReleaseSessionError(f"{name} must be a non-empty timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReleaseSessionError(f"{name} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Gr01ReleaseSessionError(f"{name} must include a UTC offset")
    return value


def _validate_sources(
    release_plan: str | Path,
    bench_report: str | Path,
    release_manifest: str | Path,
    release_report: str | Path,
    *,
    record_directory: Path,
    version_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    plan_path = _source(release_plan, name="GR01 release-session plan")
    bench_path = _source(bench_report, name="GR01 bench report")
    manifest_path = _source(release_manifest, name="release manifest")
    report_path = _source(release_report, name="release evaluation report")
    if len({plan_path, bench_path, manifest_path, report_path}) != 4:
        raise Gr01ReleaseSessionError(
            "plan, bench, manifest and evaluation report must be distinct files"
        )

    plan = _load_object(plan_path, name="GR01 release-session plan")
    plan_verification = verify_gr01_release_session_plan(plan_path, require_ready=True)
    if plan_verification.get("gate_passed") is not True:
        detail = "; ".join(str(item) for item in plan_verification.get("reasons", []))
        raise Gr01ReleaseSessionError(f"GR01 release-session plan verification failed: {detail}")

    bench = _load_object(bench_path, name="GR01 bench report")
    bench_verification = verify_gr01_link_bench_report(bench, require_passed=True)
    if bench_verification.get("gate_passed") is not True:
        detail = "; ".join(str(item) for item in bench_verification.get("reasons", []))
        raise Gr01ReleaseSessionError(f"GR01 bench verification failed: {detail}")

    saved_report = _load_object(report_path, name="release evaluation report")
    evaluated_at = _require_timestamp(
        saved_report.get("evaluated_at_utc"),
        name="release evaluation report evaluated_at_utc",
    )
    recomputed = evaluate_release_gate(
        manifest_path,
        version_manifest_path=version_manifest_path,
        evaluated_at_utc=evaluated_at,
    )
    if saved_report != recomputed:
        raise Gr01ReleaseSessionError(
            "release evaluation report does not match canonical source recomputation"
        )

    gr01 = recomputed.get("real_gr01_operator_link")
    if not isinstance(gr01, Mapping) or gr01.get("present") is not True:
        raise Gr01ReleaseSessionError("release evaluation lacks a GR01 operator-link binding")
    if gr01.get("passed") is not True:
        detail = "; ".join(str(item) for item in gr01.get("reasons", []))
        raise Gr01ReleaseSessionError(f"release GR01 operator-link binding failed: {detail}")
    age_s = gr01.get("age_s")
    max_age_s = gr01.get("max_age_s")
    if (
        isinstance(age_s, bool)
        or not isinstance(age_s, (int, float))
        or not math.isfinite(float(age_s))
        or isinstance(max_age_s, bool)
        or not isinstance(max_age_s, (int, float))
        or not math.isfinite(float(max_age_s))
        or float(age_s) < -5.0
        or float(age_s) > float(max_age_s)
    ):
        raise Gr01ReleaseSessionError("release GR01 freshness result is invalid or expired")

    manifest = _load_object(manifest_path, name="release manifest")
    binding = manifest.get("real_gr01_operator_link")
    if not isinstance(binding, Mapping):
        raise Gr01ReleaseSessionError("release manifest lacks real_gr01_operator_link")
    bench_sha = _sha256(bench_path)
    if binding.get("report_sha256") != bench_sha:
        raise Gr01ReleaseSessionError("release manifest is bound to a different GR01 bench")
    if binding.get("report_bytes") != bench_path.stat().st_size:
        raise Gr01ReleaseSessionError("release manifest GR01 bench byte count does not match")

    thresholds = plan.get("thresholds")
    source_identity = plan.get("source_identity")
    repository_identity = (
        source_identity.get("repository") if isinstance(source_identity, Mapping) else None
    )
    parity_identity = (
        source_identity.get("release_parity") if isinstance(source_identity, Mapping) else None
    )
    if not all(
        isinstance(item, Mapping) for item in (thresholds, repository_identity, parity_identity)
    ):
        raise Gr01ReleaseSessionError("GR01 release-session plan identity is incomplete")
    expected_configuration = {
        "host_address": bench.get("remote_host"),
        "port": bench.get("remote_port"),
        "hardware_id": bench.get("hardware_id"),
        "expected_source_cidr": bench.get("expected_source_cidr"),
        "minimum_round_trips": bench.get("requested_round_trips"),
        "maximum_packet_loss_rate": bench.get("thresholds", {}).get("maximum_packet_loss_rate"),
        "maximum_ack_latency_p95_ms": bench.get("thresholds", {}).get("maximum_ack_latency_p95_ms"),
        "maximum_attempts": bench.get("thresholds", {}).get("maximum_attempts"),
        "maximum_evidence_age_s": float(max_age_s),
    }
    planned_configuration = {
        "host_address": plan.get("host_address"),
        "port": plan.get("port"),
        "hardware_id": plan.get("hardware_id"),
        "expected_source_cidr": plan.get("expected_source_cidr"),
        "minimum_round_trips": thresholds.get("minimum_round_trips"),
        "maximum_packet_loss_rate": thresholds.get("maximum_packet_loss_rate"),
        "maximum_ack_latency_p95_ms": thresholds.get("maximum_ack_latency_p95_ms"),
        "maximum_attempts": thresholds.get("maximum_attempts"),
        "maximum_evidence_age_s": thresholds.get("maximum_evidence_age_s"),
    }
    if planned_configuration != expected_configuration:
        raise Gr01ReleaseSessionError(
            "GR01 release-session plan does not match bench and E7 configuration"
        )

    return {
        "release_plan": {
            "path": _relative(plan_path, directory=record_directory),
            "sha256": _sha256(plan_path),
            "bytes": plan_path.stat().st_size,
            "schema": PLAN_SCHEMA,
            "schema_version": PLAN_SCHEMA_VERSION,
            "git_head": repository_identity.get("git_head"),
            "release_label": parity_identity.get("release_label"),
            "safety_policy_hash": parity_identity.get("safety_policy_hash"),
            "source_verified": parity_identity.get("source_verified"),
            "ready_to_execute": plan.get("ready_to_execute"),
            "configuration_matches_bench_and_e7": True,
        },
        "bench_report": {
            "path": _relative(bench_path, directory=record_directory),
            "sha256": bench_sha,
            "bytes": bench_path.stat().st_size,
            "observed_at_utc": bench.get("observed_at_utc"),
            "hardware_id": bench.get("hardware_id"),
            "remote_host": bench.get("remote_host"),
            "remote_port": bench.get("remote_port"),
            "expected_source_cidr": bench.get("expected_source_cidr"),
            "local_source_address": bench.get("local_source_address"),
        },
        "release_manifest": {
            "path": _relative(manifest_path, directory=record_directory),
            "sha256": _sha256(manifest_path),
            "bytes": manifest_path.stat().st_size,
        },
        "release_report": {
            "path": _relative(report_path, directory=record_directory),
            "sha256": _sha256(report_path),
            "bytes": report_path.stat().st_size,
            "evaluated_at_utc": evaluated_at,
            "gate_passed": recomputed.get("gate_passed"),
            "release_allowed": recomputed.get("release_allowed"),
            "blocker_count": len(recomputed.get("release_blocked_reasons", [])),
            "real_gr01_operator_link_passed": True,
            "age_s": float(age_s),
            "max_age_s": float(max_age_s),
        },
        "claims": {
            "gr01_binding_verified": True,
            "overall_release_allowed": recomputed.get("release_allowed") is True,
            "hardware_flight_acceptance": "not_claimed",
            "physical_release_enabled": False,
            "flight_control_enabled": False,
            "payload_release_requested": False,
        },
    }


def create_gr01_release_session_record(
    output: str | Path,
    *,
    release_plan: str | Path,
    bench_report: str | Path,
    release_manifest: str | Path,
    release_report: str | Path,
    version_manifest_path: str | Path | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    destination = Path(output).expanduser().resolve()
    if destination.exists():
        raise Gr01ReleaseSessionError(f"output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sources = _validate_sources(
        release_plan,
        bench_report,
        release_manifest,
        release_report,
        record_directory=destination.parent,
        version_manifest_path=version_manifest_path,
    )
    document = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "event": SESSION_EVENT,
        "session_id": session_id or str(uuid4()),
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **sources,
        "gate_passed": True,
        "reasons": [],
    }
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return document


def verify_gr01_release_session_record(
    record: str | Path,
    *,
    version_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    path = _source(record, name="GR01 release-session record")
    reasons: list[str] = []
    try:
        document = _load_object(path, name="GR01 release-session record")
        expected_keys = {
            "schema_version",
            "event",
            "session_id",
            "created_at_utc",
            "release_plan",
            "bench_report",
            "release_manifest",
            "release_report",
            "claims",
            "gate_passed",
            "reasons",
        }
        unknown = sorted(set(document).difference(expected_keys))
        missing = sorted(expected_keys.difference(document))
        if unknown:
            reasons.append(f"unknown record field(s): {unknown!r}")
        if missing:
            reasons.append(f"missing record field(s): {missing!r}")
        if document.get("schema_version") != SESSION_SCHEMA_VERSION:
            reasons.append("schema_version does not match")
        if document.get("event") != SESSION_EVENT:
            reasons.append("event does not match")
        if not isinstance(document.get("session_id"), str) or not document["session_id"].strip():
            reasons.append("session_id must be a non-empty string")
        _require_timestamp(document.get("created_at_utc"), name="created_at_utc")
        if document.get("gate_passed") is not True or document.get("reasons") != []:
            reasons.append("stored gate result is not a clean pass")

        directory = path.parent
        plan = _resolve_record_path(
            document.get("release_plan", {}).get("path"),
            directory=directory,
            name="release_plan",
        )
        bench = _resolve_record_path(
            document.get("bench_report", {}).get("path"),
            directory=directory,
            name="bench_report",
        )
        manifest = _resolve_record_path(
            document.get("release_manifest", {}).get("path"),
            directory=directory,
            name="release_manifest",
        )
        report = _resolve_record_path(
            document.get("release_report", {}).get("path"),
            directory=directory,
            name="release_report",
        )
        recomputed = _validate_sources(
            plan,
            bench,
            manifest,
            report,
            record_directory=directory,
            version_manifest_path=version_manifest_path,
        )
        for field in (
            "release_plan",
            "bench_report",
            "release_manifest",
            "release_report",
            "claims",
        ):
            if document.get(field) != recomputed[field]:
                reasons.append(f"{field} does not match source recomputation")
    except (AttributeError, Gr01ReleaseSessionError, OSError, TypeError, ValueError) as exc:
        reasons.append(str(exc))
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "event": "multidetect_gr01_release_session_reverified",
        "record_path": str(path),
        "record_sha256": _sha256(path),
        "gate_passed": not reasons,
        "reasons": reasons,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="write a verified success record")
    create.add_argument("--plan", required=True, type=Path)
    create.add_argument("--bench", required=True, type=Path)
    create.add_argument("--manifest", required=True, type=Path)
    create.add_argument("--report", required=True, type=Path)
    create.add_argument("--out", required=True, type=Path)
    create.add_argument("--version-manifest", type=Path)
    create.add_argument("--session-id")
    verify = subparsers.add_parser("verify", help="reverify a saved session record")
    verify.add_argument("record", type=Path)
    verify.add_argument("--version-manifest", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            document = create_gr01_release_session_record(
                args.out,
                release_plan=args.plan,
                bench_report=args.bench,
                release_manifest=args.manifest,
                release_report=args.report,
                version_manifest_path=args.version_manifest,
                session_id=args.session_id,
            )
        else:
            document = verify_gr01_release_session_record(
                args.record,
                version_manifest_path=args.version_manifest,
            )
    except (Gr01ReleaseSessionError, OSError, ValueError) as exc:
        print(f"GR01 release session: {exc}")
        return 2
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if document.get("gate_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
