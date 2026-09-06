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


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _file_identity(value: object, *, name: str, expected_path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "bytes"}:
        raise Gr01ReleaseHandoffError(f"{name} fields do not match the file contract")
    size = value.get("bytes")
    if (
        value.get("path") != expected_path
        or not _is_sha256(value.get("sha256"))
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
    ):
        raise Gr01ReleaseHandoffError(f"{name} identity is invalid")
    return dict(value)


def _portable_source_identity(
    repository: Mapping[str, Any],
    parity: Mapping[str, Any],
    tools: Mapping[str, Any],
) -> dict[str, Any]:
    if set(repository) != {"root", "git_head", "tracked_dirty"}:
        raise Gr01ReleaseHandoffError("source plan repository fields do not match")
    root = repository.get("root")
    git_head = repository.get("git_head")
    tracked_dirty = repository.get("tracked_dirty")
    if (
        not isinstance(root, str)
        or not root.strip()
        or not isinstance(git_head, str)
        or len(git_head) != 40
        or git_head != git_head.lower()
        or any(character not in "0123456789abcdef" for character in git_head)
        or not isinstance(tracked_dirty, bool)
    ):
        raise Gr01ReleaseHandoffError("source plan repository identity is invalid")
    parity_keys = {
        "path",
        "sha256",
        "bytes",
        "release_label",
        "safety_policy_hash",
        "source_verified",
    }
    if set(parity) != parity_keys:
        raise Gr01ReleaseHandoffError("source plan release parity fields do not match")
    parity_file = _file_identity(
        {key: parity.get(key) for key in ("path", "sha256", "bytes")},
        name="source plan release parity",
        expected_path="configs/release-parity.json",
    )
    release_label = parity.get("release_label")
    if (
        not isinstance(release_label, str)
        or not release_label.startswith("0.3.0-pre.")
        or not release_label.removeprefix("0.3.0-pre.").isdigit()
        or not _is_sha256(parity.get("safety_policy_hash"))
        or not isinstance(parity.get("source_verified"), bool)
    ):
        raise Gr01ReleaseHandoffError("source plan release parity identity is invalid")
    expected_tools = {
        path.replace("/", "_").replace(".", "_"): path for path in plan_module.TOOL_PATHS
    }
    if set(tools) != set(expected_tools):
        raise Gr01ReleaseHandoffError("source plan tool fields do not match")
    tool_identity = {
        key: _file_identity(tools[key], name=f"source plan tool {key}", expected_path=path)
        for key, path in expected_tools.items()
    }
    return {
        "git_head": git_head,
        "origin_tracked_dirty": tracked_dirty,
        "release_parity": {
            **parity_file,
            "release_label": release_label,
            "safety_policy_hash": parity["safety_policy_hash"],
            "source_verified": parity["source_verified"],
        },
        "tools": tool_identity,
    }


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
        "source_identity": _portable_source_identity(repository, parity, tools),
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


def verify_gr01_release_session_handoff_portable(
    receipt: str | Path,
) -> dict[str, Any]:
    """Verify a copied handoff without reopening target-machine absolute paths."""

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
        if (
            document.get("schema") != HANDOFF_SCHEMA
            or document.get("schema_version") != HANDOFF_SCHEMA_VERSION
            or document.get("event") != HANDOFF_EVENT
        ):
            raise Gr01ReleaseHandoffError("handoff schema or event identity does not match")
        _timestamp(document.get("generated_at_utc"), name="generated_at_utc")
        host_id = document.get("host_id")
        if (
            not isinstance(host_id, str)
            or not host_id.strip()
            or any(ord(character) < 32 for character in host_id)
        ):
            raise Gr01ReleaseHandoffError("host_id must be a non-empty printable string")

        source_plan = document.get("source_plan")
        target_paths = document.get("target_paths")
        target_identity = document.get("target_identity")
        secrets = document.get("secret_environment")
        checks = document.get("checks")
        if not all(
            isinstance(value, Mapping)
            for value in (source_plan, target_paths, target_identity, secrets, checks)
        ):
            raise Gr01ReleaseHandoffError("handoff portable identity is incomplete")
        expected_source_fields = {
            "path",
            "sha256",
            "bytes",
            "schema",
            "schema_version",
            "configuration_sha256",
            "git_head",
        }
        if set(source_plan) != expected_source_fields:
            raise Gr01ReleaseHandoffError("handoff source_plan fields do not match")
        plan_path = _resolve_record_path(source_plan.get("path"), directory=path.parent)
        portable = _plan_projection(_load_object(plan_path, name="source plan"))
        configuration_digest = _canonical_sha256(
            {
                "configuration": portable["configuration"],
                "secret_names": portable["secret_names"],
                "operations": portable["operations"],
            }
        )
        if (
            source_plan.get("sha256") != _sha256(plan_path)
            or source_plan.get("bytes") != plan_path.stat().st_size
            or source_plan.get("schema") != plan_module.PLAN_SCHEMA
            or source_plan.get("schema_version") != plan_module.PLAN_SCHEMA_VERSION
            or source_plan.get("configuration_sha256") != configuration_digest
            or source_plan.get("git_head") != portable["source_identity"]["git_head"]
        ):
            raise Gr01ReleaseHandoffError("handoff source_plan identity does not match")

        if set(target_paths) != {"repository_root", "python", "output_root"} or any(
            not isinstance(target_paths.get(name), str) or not target_paths[name].strip()
            for name in ("repository_root", "python", "output_root")
        ):
            raise Gr01ReleaseHandoffError("handoff target_paths do not match")
        if set(target_identity) != {"git_head", "tracked_dirty", "release_parity", "tools"}:
            raise Gr01ReleaseHandoffError("handoff target_identity fields do not match")
        target_portable = _portable_source_identity(
            {
                "root": target_paths["repository_root"],
                "git_head": target_identity.get("git_head"),
                "tracked_dirty": target_identity.get("tracked_dirty"),
            },
            target_identity.get("release_parity"),
            target_identity.get("tools"),
        )
        if target_portable["git_head"] != portable["source_identity"]["git_head"]:
            raise Gr01ReleaseHandoffError("handoff target Git does not match source plan")

        if set(secrets) != {"operator_hmac", "mavlink_signing"}:
            raise Gr01ReleaseHandoffError("handoff secret_environment fields do not match")
        projected_secrets: dict[str, dict[str, Any]] = {}
        for name in ("operator_hmac", "mavlink_signing"):
            item = secrets.get(name)
            if not isinstance(item, Mapping) or set(item) != {"name", "present"}:
                raise Gr01ReleaseHandoffError(f"handoff secret {name} fields do not match")
            if not isinstance(item.get("present"), bool):
                raise Gr01ReleaseHandoffError(f"handoff secret {name} presence is invalid")
            projected_secrets[name] = plan_module._secret(
                item.get("name"),
                present=item["present"],
            )
        if {name: item["name"] for name, item in projected_secrets.items()} != portable[
            "secret_names"
        ]:
            raise Gr01ReleaseHandoffError("handoff secret names do not match source plan")

        expected_check_names = {
            "origin_tracked_clean",
            "target_tracked_clean",
            "git_head_matches",
            "release_parity_matches",
            "tool_identity_matches",
            "target_parity_source_verified",
        }
        if set(checks) != expected_check_names or any(
            not isinstance(checks.get(name), bool) for name in expected_check_names
        ):
            raise Gr01ReleaseHandoffError("handoff checks do not match")
        origin = portable["source_identity"]
        expected_checks = {
            "origin_tracked_clean": origin["origin_tracked_dirty"] is False,
            "target_tracked_clean": target_portable["origin_tracked_dirty"] is False,
            "git_head_matches": origin["git_head"] == target_portable["git_head"],
            "release_parity_matches": origin["release_parity"] == target_portable["release_parity"],
            "tool_identity_matches": origin["tools"] == target_portable["tools"],
            "target_parity_source_verified": target_portable["release_parity"].get(
                "source_verified"
            )
            is True,
        }
        if dict(checks) != expected_checks:
            raise Gr01ReleaseHandoffError("handoff checks do not recompute")
        expected_reasons = [name for name, passed in checks.items() if not passed]
        expected_reasons.extend(
            f"required secret environment variable is not set: {item['name']}"
            for item in projected_secrets.values()
            if item["present"] is not True
        )
        if (
            document.get("source_equivalent") != all(checks.values())
            or document.get("ready_to_execute") != (not expected_reasons)
            or document.get("reasons") != expected_reasons
        ):
            raise Gr01ReleaseHandoffError("handoff stored gate result does not recompute")
        if document.get("claims") != {
            "network_contacted": False,
            "hardware_observed": False,
            "hardware_flight_acceptance": "not_claimed",
            "physical_release_enabled": False,
            "flight_control_enabled": False,
            "payload_release_requested": False,
            "secret_values_serialized": False,
        }:
            raise Gr01ReleaseHandoffError("handoff claim boundary does not match")
    except (Gr01ReleaseHandoffError, OSError, TypeError, ValueError) as exc:
        reasons.append(str(exc))
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "event": "multidetect_gr01_release_session_handoff_portable_reverified",
        "receipt_path": str(path),
        "receipt_sha256": _sha256(path),
        "gate_passed": not reasons,
        "target_environment_reverified": False,
        "reasons": reasons,
    }


def verify_gr01_release_session_handoff_configuration(
    receipt: str | Path,
    *,
    host_address: str,
    port: int,
    hardware_id: str,
    expected_source_cidr: str,
    operator_hmac_environment: str,
    mavlink_signing_environment: str,
    minimum_round_trips: int,
    maximum_packet_loss_rate: float,
    maximum_ack_latency_p95_ms: float,
    maximum_attempts: int,
    maximum_evidence_age_s: float,
) -> dict[str, Any]:
    """Fail closed unless a ready target handoff matches the requested run."""

    path = _source(receipt, name="GR01 release-session handoff")
    reasons: list[str] = []
    strict = verify_gr01_release_session_handoff(
        path,
        check_environment=True,
        require_ready=True,
    )
    if strict["gate_passed"] is not True:
        reasons.extend(f"handoff: {reason}" for reason in strict["reasons"])
    try:
        document = _load_object(path, name="GR01 release-session handoff")
        source_plan = document.get("source_plan")
        if not isinstance(source_plan, Mapping):
            raise Gr01ReleaseHandoffError("handoff source_plan is incomplete")
        plan_path = _resolve_record_path(source_plan.get("path"), directory=path.parent)
        portable = _plan_projection(_load_object(plan_path, name="source plan"))
        expected_configuration = plan_module._normalise_configuration(
            host_address=host_address,
            port=port,
            hardware_id=hardware_id,
            expected_source_cidr=expected_source_cidr,
            minimum_round_trips=minimum_round_trips,
            maximum_packet_loss_rate=maximum_packet_loss_rate,
            maximum_ack_latency_p95_ms=maximum_ack_latency_p95_ms,
            maximum_attempts=maximum_attempts,
            maximum_evidence_age_s=maximum_evidence_age_s,
        )
        expected_secret_names = {
            "operator_hmac": plan_module._secret(
                operator_hmac_environment,
                present=False,
            )["name"],
            "mavlink_signing": plan_module._secret(
                mavlink_signing_environment,
                present=False,
            )["name"],
        }
        if portable["configuration"] != expected_configuration:
            reasons.append("source plan configuration does not match requested execution")
        if portable["secret_names"] != expected_secret_names:
            reasons.append("source plan secret names do not match requested execution")
        expected_digest = _canonical_sha256(
            {
                "configuration": expected_configuration,
                "secret_names": expected_secret_names,
                "operations": list(plan_module.OPERATIONS),
            }
        )
        if source_plan.get("configuration_sha256") != expected_digest:
            reasons.append("handoff configuration digest does not match requested execution")
    except (Gr01ReleaseHandoffError, OSError, TypeError, ValueError) as exc:
        reasons.append(str(exc))
        expected_digest = None
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "event": "multidetect_gr01_release_session_handoff_configuration_matched",
        "receipt_path": str(path),
        "receipt_sha256": _sha256(path),
        "requested_configuration_sha256": expected_digest,
        "gate_passed": not reasons,
        "reasons": reasons,
    }


def _requested_configuration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host-address", required=True)
    parser.add_argument("--port", type=int, default=14580)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--expected-source-cidr", required=True)
    parser.add_argument("--operator-hmac-environment", required=True)
    parser.add_argument("--mavlink-signing-environment", required=True)
    parser.add_argument("--minimum-round-trips", type=int, default=100)
    parser.add_argument("--maximum-packet-loss-rate", type=float, default=0.01)
    parser.add_argument("--maximum-ack-latency-p95-ms", type=float, default=500.0)
    parser.add_argument("--maximum-attempts", type=int, default=3)
    parser.add_argument("--maximum-evidence-age-s", type=float, default=900.0)


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
    match = subparsers.add_parser(
        "match",
        help="require a ready handoff that matches the requested execution",
    )
    match.add_argument("receipt", type=Path)
    _requested_configuration_arguments(match)
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
        elif args.command == "verify":
            document = verify_gr01_release_session_handoff(
                args.receipt,
                check_environment=args.check_environment,
                require_ready=args.require_ready,
            )
        else:
            document = verify_gr01_release_session_handoff_configuration(
                args.receipt,
                host_address=args.host_address,
                port=args.port,
                hardware_id=args.hardware_id,
                expected_source_cidr=args.expected_source_cidr,
                operator_hmac_environment=args.operator_hmac_environment,
                mavlink_signing_environment=args.mavlink_signing_environment,
                minimum_round_trips=args.minimum_round_trips,
                maximum_packet_loss_rate=args.maximum_packet_loss_rate,
                maximum_ack_latency_p95_ms=args.maximum_ack_latency_p95_ms,
                maximum_attempts=args.maximum_attempts,
                maximum_evidence_age_s=args.maximum_evidence_age_s,
            )
    except (Gr01ReleaseHandoffError, OSError, ValueError) as exc:
        print(f"GR01 release-session handoff: {exc}")
        return 2
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if args.command == "create" or document.get("gate_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
