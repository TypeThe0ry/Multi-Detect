"""Create and verify a portable GR01 release-plan deployment handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import gr01_release_plan as plan_module
from .autonomy.serialization import strict_json_loads

HANDOFF_SCHEMA = "multidetect.gr01-release-session-handoff/v1"
HANDOFF_SCHEMA_VERSION = 1
HANDOFF_EVENT = "multidetect_gr01_release_session_handoff_verified"


class Gr01ReleaseHandoffError(ValueError):
    """Raised when a GR01 plan handoff or one of its sources is invalid."""


def _load_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_bytes(), name=name)
    except OSError as exc:
        raise Gr01ReleaseHandoffError(f"unable to read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise Gr01ReleaseHandoffError(f"{name} must contain a JSON object")
    return value


def _source(path: str | Path, *, name: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise Gr01ReleaseHandoffError(f"{name} must not be a symbolic link")
    resolved = requested.resolve()
    if not resolved.is_file():
        raise Gr01ReleaseHandoffError(f"{name} does not exist: {resolved}")
    return resolved


def _directory(path: str | Path, *, name: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise Gr01ReleaseHandoffError(f"{name} must not be a symbolic link")
    resolved = requested.resolve()
    if not resolved.is_dir():
        raise Gr01ReleaseHandoffError(f"{name} does not exist: {resolved}")
    return resolved


def _inside(path: Path, *, root: Path, name: str) -> Path:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Gr01ReleaseHandoffError(f"{name} escapes target repository root: {path}") from exc
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Gr01ReleaseHandoffError(f"unable to hash {path}: {exc}") from exc
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _relative(path: Path, *, directory: Path) -> str:
    try:
        value = path.relative_to(directory)
    except ValueError:
        value = Path(os.path.relpath(path, directory))
    return value.as_posix()


def _resolve_record_path(value: object, *, directory: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Gr01ReleaseHandoffError("source_plan.path must be a non-empty string")
    requested = Path(value)
    if not requested.is_absolute():
        requested = directory / requested
    return _source(requested, name="source plan")


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Gr01ReleaseHandoffError(f"{name} must be a non-empty timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReleaseHandoffError(f"{name} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Gr01ReleaseHandoffError(f"{name} must include a UTC offset")
    return value


def _plan_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "schema_version",
        "generated_at_utc",
        "host_address",
        "port",
        "hardware_id",
        "expected_source_cidr",
        "secret_environment",
        "thresholds",
        "paths",
        "source_identity",
        "operations",
        "ready_to_execute",
        "reasons",
        "claims",
    }
    if set(document) != expected_keys:
        raise Gr01ReleaseHandoffError("source plan fields do not match the v2 contract")
    if (
        document.get("schema") != plan_module.PLAN_SCHEMA
        or document.get("schema_version") != plan_module.PLAN_SCHEMA_VERSION
    ):
        raise Gr01ReleaseHandoffError("source plan schema identity does not match")
    _timestamp(document.get("generated_at_utc"), name="source plan generated_at_utc")
    thresholds = document.get("thresholds")
    secrets = document.get("secret_environment")
    identity = document.get("source_identity")
    if not all(isinstance(value, Mapping) for value in (thresholds, secrets, identity)):
        raise Gr01ReleaseHandoffError("source plan configuration or identity is incomplete")
    operator = secrets.get("operator_hmac")
    signing = secrets.get("mavlink_signing")
    repository = identity.get("repository")
    parity = identity.get("release_parity")
    tools = identity.get("tools")
    if not all(
        isinstance(value, Mapping) for value in (operator, signing, repository, parity, tools)
    ):
        raise Gr01ReleaseHandoffError("source plan portable identity is incomplete")
    configuration = plan_module._normalise_configuration(
        host_address=document.get("host_address"),
        port=document.get("port"),
        hardware_id=document.get("hardware_id"),
        expected_source_cidr=document.get("expected_source_cidr"),
        minimum_round_trips=thresholds.get("minimum_round_trips"),
        maximum_packet_loss_rate=thresholds.get("maximum_packet_loss_rate"),
        maximum_ack_latency_p95_ms=thresholds.get("maximum_ack_latency_p95_ms"),
        maximum_attempts=thresholds.get("maximum_attempts"),
        maximum_evidence_age_s=thresholds.get("maximum_evidence_age_s"),
    )
    secret_names = {
        "operator_hmac": plan_module._secret(operator.get("name"), present=False)["name"],
        "mavlink_signing": plan_module._secret(signing.get("name"), present=False)["name"],
    }
    if document.get("operations") != list(plan_module.OPERATIONS):
        raise Gr01ReleaseHandoffError("source plan operations do not match")
    if document.get("claims") != {
        "network_contacted": False,
        "hardware_observed": False,
        "hardware_flight_acceptance": "not_claimed",
        "physical_release_enabled": False,
        "flight_control_enabled": False,
        "payload_release_requested": False,
    }:
        raise Gr01ReleaseHandoffError("source plan claim boundary does not match")
    return {
        "configuration": configuration,
        "secret_names": secret_names,
        "operations": list(plan_module.OPERATIONS),
        "source_identity": {
            "git_head": repository.get("git_head"),
            "origin_tracked_dirty": repository.get("tracked_dirty"),
            "release_parity": dict(parity),
            "tools": dict(tools),
        },
    }


def _secret_projection(name: str, *, present: bool | None) -> dict[str, Any]:
    return plan_module._secret(name, present=present)


def _build_document(
    *,
    generated_at_utc: str,
    source_plan: str | Path,
    target_repository_root: str | Path,
    target_python: str | Path,
    target_output_root: str | Path,
    host_id: str,
    receipt_directory: Path,
    operator_hmac_present: bool | None = None,
    mavlink_signing_present: bool | None = None,
) -> dict[str, Any]:
    generated_at = _timestamp(generated_at_utc, name="generated_at_utc")
    if (
        not isinstance(host_id, str)
        or not host_id.strip()
        or any(ord(character) < 32 for character in host_id)
    ):
        raise Gr01ReleaseHandoffError("host_id must be a non-empty printable string")
    plan_path = _source(source_plan, name="source plan")
    plan = _load_object(plan_path, name="source plan")
    portable = _plan_projection(plan)
    root = _directory(target_repository_root, name="target repository root")
    python = _source(target_python, name="target Python interpreter")
    output = _inside(
        _directory(target_output_root, name="target output root"),
        root=root,
        name="target output root",
    )
    target = plan_module._source_identity(root)
    target_portable = {
        "git_head": target["repository"]["git_head"],
        "tracked_dirty": target["repository"]["tracked_dirty"],
        "release_parity": target["release_parity"],
        "tools": target["tools"],
    }
    origin = portable["source_identity"]
    checks = {
        "origin_tracked_clean": origin["origin_tracked_dirty"] is False,
        "target_tracked_clean": target_portable["tracked_dirty"] is False,
        "git_head_matches": origin["git_head"] == target_portable["git_head"],
        "release_parity_matches": origin["release_parity"] == target_portable["release_parity"],
        "tool_identity_matches": origin["tools"] == target_portable["tools"],
        "target_parity_source_verified": target_portable["release_parity"].get("source_verified")
        is True,
    }
    secrets = {
        "operator_hmac": _secret_projection(
            portable["secret_names"]["operator_hmac"],
            present=operator_hmac_present,
        ),
        "mavlink_signing": _secret_projection(
            portable["secret_names"]["mavlink_signing"],
            present=mavlink_signing_present,
        ),
    }
    reasons = [name for name, passed in checks.items() if not passed]
    reasons.extend(
        f"required secret environment variable is not set: {item['name']}"
        for item in secrets.values()
        if item["present"] is not True
    )
    return {
        "schema": HANDOFF_SCHEMA,
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "event": HANDOFF_EVENT,
        "generated_at_utc": generated_at,
        "host_id": host_id.strip(),
        "source_plan": {
            "path": _relative(plan_path, directory=receipt_directory),
            "sha256": _sha256(plan_path),
            "bytes": plan_path.stat().st_size,
            "schema": plan_module.PLAN_SCHEMA,
            "schema_version": plan_module.PLAN_SCHEMA_VERSION,
            "configuration_sha256": _canonical_sha256(
                {
                    "configuration": portable["configuration"],
                    "secret_names": portable["secret_names"],
                    "operations": portable["operations"],
                }
            ),
            "git_head": origin["git_head"],
        },
        "target_paths": {
            "repository_root": str(root),
            "python": str(python),
            "output_root": str(output),
        },
        "target_identity": target_portable,
        "secret_environment": secrets,
        "checks": checks,
        "source_equivalent": all(checks.values()),
        "ready_to_execute": not reasons,
        "reasons": reasons,
        "claims": {
            "network_contacted": False,
            "hardware_observed": False,
            "hardware_flight_acceptance": "not_claimed",
            "physical_release_enabled": False,
            "flight_control_enabled": False,
            "payload_release_requested": False,
            "secret_values_serialized": False,
        },
    }


def create_gr01_release_session_handoff(
    output: str | Path,
    *,
    source_plan: str | Path,
    target_repository_root: str | Path,
    target_python: str | Path,
    target_output_root: str | Path,
    host_id: str,
) -> dict[str, Any]:
    destination = Path(output).expanduser().resolve()
    if destination.exists():
        raise Gr01ReleaseHandoffError(f"output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = _build_document(
        generated_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source_plan=source_plan,
        target_repository_root=target_repository_root,
        target_python=target_python,
        target_output_root=target_output_root,
        host_id=host_id,
        receipt_directory=destination.parent,
    )
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


def verify_gr01_release_session_handoff(
    receipt: str | Path,
    *,
    check_environment: bool = False,
    require_ready: bool = False,
) -> dict[str, Any]:
    path = _source(receipt, name="GR01 release-session handoff")
    reasons: list[str] = []
    try:
        document = _load_object(path, name="GR01 release-session handoff")
        expected_keys = {
            "schema",
            "schema_version",
            "event",
            "generated_at_utc",
            "host_id",
            "source_plan",
            "target_paths",
            "target_identity",
            "secret_environment",
            "checks",
            "source_equivalent",
            "ready_to_execute",
            "reasons",
            "claims",
        }
        if set(document) != expected_keys:
            raise Gr01ReleaseHandoffError("handoff fields do not match the v1 contract")
        source_plan = document.get("source_plan")
        target_paths = document.get("target_paths")
        secrets = document.get("secret_environment")
        if not all(isinstance(value, Mapping) for value in (source_plan, target_paths, secrets)):
            raise Gr01ReleaseHandoffError("handoff source, paths or secrets are incomplete")
        operator = secrets.get("operator_hmac")
        signing = secrets.get("mavlink_signing")
        if not isinstance(operator, Mapping) or not isinstance(signing, Mapping):
            raise Gr01ReleaseHandoffError("handoff secret_environment is incomplete")
        plan_path = _resolve_record_path(source_plan.get("path"), directory=path.parent)
        expected = _build_document(
            generated_at_utc=document.get("generated_at_utc"),
            source_plan=plan_path,
            target_repository_root=target_paths.get("repository_root"),
            target_python=target_paths.get("python"),
            target_output_root=target_paths.get("output_root"),
            host_id=document.get("host_id"),
            receipt_directory=path.parent,
            operator_hmac_present=None if check_environment else operator.get("present"),
            mavlink_signing_present=None if check_environment else signing.get("present"),
        )
        if document != expected:
            reasons.append("handoff does not match target source and path recomputation")
        if require_ready and document.get("ready_to_execute") is not True:
            reasons.append("handoff is not ready to execute")
    except (Gr01ReleaseHandoffError, OSError, TypeError, ValueError) as exc:
        reasons.append(str(exc))
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "event": "multidetect_gr01_release_session_handoff_reverified",
        "receipt_path": str(path),
        "receipt_sha256": _sha256(path),
        "gate_passed": not reasons,
        "reasons": reasons,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="write a target-specific handoff receipt")
    create.add_argument("--source-plan", required=True, type=Path)
    create.add_argument("--repository-root", required=True, type=Path)
    create.add_argument("--python", required=True, type=Path)
    create.add_argument("--output-root", required=True, type=Path)
    create.add_argument("--host-id", required=True)
    create.add_argument("--out", required=True, type=Path)
    verify = subparsers.add_parser("verify", help="reverify a saved handoff receipt")
    verify.add_argument("receipt", type=Path)
    verify.add_argument("--check-environment", action="store_true")
    verify.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            document = create_gr01_release_session_handoff(
                args.out,
                source_plan=args.source_plan,
                target_repository_root=args.repository_root,
                target_python=args.python,
                target_output_root=args.output_root,
                host_id=args.host_id,
            )
        else:
            document = verify_gr01_release_session_handoff(
                args.receipt,
                check_environment=args.check_environment,
                require_ready=args.require_ready,
            )
    except (Gr01ReleaseHandoffError, OSError, ValueError) as exc:
        print(f"GR01 release-session handoff: {exc}")
        return 2
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if args.command == "create" or document.get("gate_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
