"""Create and independently verify a no-network GR01 release-session plan."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .autonomy.serialization import strict_json_loads
from .release_parity import build_release_parity_document

PLAN_SCHEMA_VERSION = 2
PLAN_SCHEMA = "multidetect.gr01-release-session-plan/v2"
OPERATIONS = (
    "gr01_hardware_bench",
    "independent_bench_verification",
    "freshness_bound_e7_evaluation",
    "independent_release_session_verification",
)
TOOL_PATHS = (
    "scripts/run_gr01_release_session.ps1",
    "scripts/run_multidetect_cli.py",
    "scripts/verify_gr01_release_session_plan.py",
    "src/multidetect/gr01_release_plan.py",
)


class Gr01ReleasePlanError(ValueError):
    """Raised when a GR01 release-session plan is malformed or stale."""


def _load_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_bytes(), name=name)
    except OSError as exc:
        raise Gr01ReleasePlanError(f"unable to read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise Gr01ReleasePlanError(f"{name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Gr01ReleasePlanError(f"unable to hash {path}: {exc}") from exc
    return digest.hexdigest()


def _source(path: Path, *, name: str) -> Path:
    if path.is_symlink():
        raise Gr01ReleasePlanError(f"{name} must not be a symbolic link")
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise Gr01ReleasePlanError(f"{name} does not exist: {resolved}")
    return resolved


def _directory(path: str | Path, *, name: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise Gr01ReleasePlanError(f"{name} must not be a symbolic link")
    resolved = requested.resolve()
    if not resolved.is_dir():
        raise Gr01ReleasePlanError(f"{name} does not exist: {resolved}")
    return resolved


def _inside(path: Path, *, root: Path, name: str) -> Path:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Gr01ReleasePlanError(f"{name} escapes repository root: {path}") from exc
    return path


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Gr01ReleasePlanError(f"unable to inspect Git repository: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise Gr01ReleasePlanError(f"Git inspection failed: {detail}")
    return result.stdout.strip()


def _file_projection(path: Path, *, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _source_identity(root: Path) -> dict[str, Any]:
    parity_path = _source(root / "configs" / "release-parity.json", name="release parity")
    parity = _load_object(parity_path, name="release parity")
    source_files = parity.get("source_files")
    if not isinstance(source_files, Mapping):
        raise Gr01ReleasePlanError("release parity lacks source_files")
    regenerated = build_release_parity_document(root, file_list=source_files)
    parity_source_verified = parity == regenerated
    parity_hashes = parity.get("parity")
    if not isinstance(parity_hashes, Mapping):
        raise Gr01ReleasePlanError("release parity lacks parity hashes")
    release_label = parity.get("release_label")
    safety_hash = parity_hashes.get("safety_policy_hash")
    if not isinstance(release_label, str) or not isinstance(safety_hash, str):
        raise Gr01ReleasePlanError("release parity identity is incomplete")
    tracked_status = _git(root, "status", "--porcelain", "--untracked-files=no")
    return {
        "repository": {
            "root": str(root),
            "git_head": _git(root, "rev-parse", "HEAD").lower(),
            "tracked_dirty": bool(tracked_status),
        },
        "release_parity": {
            **_file_projection(parity_path, root=root),
            "release_label": release_label,
            "safety_policy_hash": safety_hash,
            "source_verified": parity_source_verified,
        },
        "tools": {
            path.replace("/", "_").replace(".", "_"): _file_projection(
                _source(root / path, name=path), root=root
            )
            for path in TOOL_PATHS
        },
    }


def _normalise_configuration(
    *,
    host_address: str,
    port: int,
    hardware_id: str,
    expected_source_cidr: str,
    minimum_round_trips: int,
    maximum_packet_loss_rate: float,
    maximum_ack_latency_p95_ms: float,
    maximum_attempts: int,
    maximum_evidence_age_s: float,
) -> dict[str, Any]:
    try:
        host = ipaddress.ip_address(host_address)
    except ValueError as exc:
        raise Gr01ReleasePlanError("host_address must be a literal IPv4 address") from exc
    if host.version != 4 or host.is_loopback or host.is_unspecified or host.is_multicast:
        raise Gr01ReleasePlanError("host_address must be a routable non-loopback IPv4 address")
    try:
        cidr = ipaddress.ip_network(expected_source_cidr, strict=False)
    except ValueError as exc:
        raise Gr01ReleasePlanError("expected_source_cidr must be an IPv4 CIDR") from exc
    if cidr.version != 4:
        raise Gr01ReleasePlanError("expected_source_cidr must be an IPv4 CIDR")
    if not hardware_id.strip():
        raise Gr01ReleasePlanError("hardware_id must not be empty")
    if not 1 <= port <= 65535:
        raise Gr01ReleasePlanError("port must be in 1..65535")
    if not 1 <= minimum_round_trips <= 10_000:
        raise Gr01ReleasePlanError("minimum_round_trips must be in 1..10000")
    if not 0.0 <= maximum_packet_loss_rate <= 1.0:
        raise Gr01ReleasePlanError("maximum_packet_loss_rate must be in 0..1")
    if not 0.001 <= maximum_ack_latency_p95_ms <= 600_000.0:
        raise Gr01ReleasePlanError("maximum_ack_latency_p95_ms is outside policy")
    if not 1 <= maximum_attempts <= 100:
        raise Gr01ReleasePlanError("maximum_attempts must be in 1..100")
    if not 0.0 <= maximum_evidence_age_s <= 86_400.0:
        raise Gr01ReleasePlanError("maximum_evidence_age_s must be in 0..86400")
    return {
        "host_address": str(host),
        "port": port,
        "hardware_id": hardware_id.strip(),
        "expected_source_cidr": str(cidr),
        "thresholds": {
            "minimum_round_trips": minimum_round_trips,
            "maximum_packet_loss_rate": maximum_packet_loss_rate,
            "maximum_ack_latency_p95_ms": maximum_ack_latency_p95_ms,
            "maximum_attempts": maximum_attempts,
            "maximum_evidence_age_s": maximum_evidence_age_s,
        },
    }


def _secret(name: str, *, present: bool | None = None) -> dict[str, Any]:
    if (
        not name
        or not (name[0].isalpha() or name[0] == "_")
        or not all(character.isalnum() or character == "_" for character in name)
    ):
        raise Gr01ReleasePlanError(f"invalid environment variable name: {name}")
    actual = bool(os.environ.get(name, "").strip()) if present is None else present
    return {"name": name, "present": actual}


def _build_document(
    *,
    generated_at_utc: str,
    repository_root: str | Path,
    python_path: str | Path,
    output_root: str | Path,
    operator_hmac_environment: str,
    mavlink_signing_environment: str,
    operator_hmac_present: bool | None = None,
    mavlink_signing_present: bool | None = None,
    **configuration: Any,
) -> dict[str, Any]:
    if not isinstance(generated_at_utc, str) or not generated_at_utc.strip():
        raise Gr01ReleasePlanError("generated_at_utc must be a timestamp string")
    try:
        generated_at = datetime.fromisoformat(generated_at_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReleasePlanError("generated_at_utc is not a valid ISO 8601 timestamp") from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise Gr01ReleasePlanError("generated_at_utc must include a UTC offset")
    root = _directory(repository_root, name="repository root")
    python = _source(Path(python_path), name="Python interpreter")
    output = _inside(_directory(output_root, name="output root"), root=root, name="output root")
    normalised = _normalise_configuration(**configuration)
    secrets = {
        "operator_hmac": _secret(
            operator_hmac_environment,
            present=operator_hmac_present,
        ),
        "mavlink_signing": _secret(
            mavlink_signing_environment,
            present=mavlink_signing_present,
        ),
    }
    identity = _source_identity(root)
    reasons = [
        f"required secret environment variable is not set: {item['name']}"
        for item in secrets.values()
        if item["present"] is not True
    ]
    if identity["repository"]["tracked_dirty"] is True:
        reasons.append("repository has tracked working-tree changes")
    if identity["release_parity"]["source_verified"] is not True:
        reasons.append("release parity does not match current source files")
    return {
        "schema": PLAN_SCHEMA,
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc,
        **normalised,
        "secret_environment": secrets,
        "paths": {
            "repository_root": str(root),
            "python": str(python),
            "output_root": str(output),
        },
        "source_identity": identity,
        "operations": list(OPERATIONS),
        "ready_to_execute": not reasons,
        "reasons": reasons,
        "claims": {
            "network_contacted": False,
            "hardware_observed": False,
            "hardware_flight_acceptance": "not_claimed",
            "physical_release_enabled": False,
            "flight_control_enabled": False,
            "payload_release_requested": False,
        },
    }


def create_gr01_release_session_plan(
    output: str | Path,
    **arguments: Any,
) -> dict[str, Any]:
    destination = Path(output).expanduser().resolve()
    if destination.exists():
        raise Gr01ReleasePlanError(f"output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = _build_document(
        generated_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **arguments,
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


def verify_gr01_release_session_plan(
    plan: str | Path,
    *,
    check_environment: bool = False,
    require_ready: bool = False,
) -> dict[str, Any]:
    path = _source(Path(plan), name="GR01 release-session plan")
    reasons: list[str] = []
    try:
        document = _load_object(path, name="GR01 release-session plan")
        paths = document.get("paths")
        secrets = document.get("secret_environment")
        thresholds = document.get("thresholds")
        if not all(isinstance(item, Mapping) for item in (paths, secrets, thresholds)):
            raise Gr01ReleasePlanError("plan paths, secrets and thresholds must be objects")
        operator = secrets.get("operator_hmac")
        signing = secrets.get("mavlink_signing")
        if not isinstance(operator, Mapping) or not isinstance(signing, Mapping):
            raise Gr01ReleasePlanError("plan secret_environment is incomplete")
        expected = _build_document(
            generated_at_utc=document.get("generated_at_utc"),
            repository_root=paths.get("repository_root"),
            python_path=paths.get("python"),
            output_root=paths.get("output_root"),
            host_address=document.get("host_address"),
            port=document.get("port"),
            hardware_id=document.get("hardware_id"),
            expected_source_cidr=document.get("expected_source_cidr"),
            operator_hmac_environment=operator.get("name"),
            mavlink_signing_environment=signing.get("name"),
            operator_hmac_present=None if check_environment else operator.get("present"),
            mavlink_signing_present=None if check_environment else signing.get("present"),
            minimum_round_trips=thresholds.get("minimum_round_trips"),
            maximum_packet_loss_rate=thresholds.get("maximum_packet_loss_rate"),
            maximum_ack_latency_p95_ms=thresholds.get("maximum_ack_latency_p95_ms"),
            maximum_attempts=thresholds.get("maximum_attempts"),
            maximum_evidence_age_s=thresholds.get("maximum_evidence_age_s"),
        )
        if document != expected:
            reasons.append("plan does not match current source and configuration recomputation")
        if require_ready and document.get("ready_to_execute") is not True:
            reasons.append("plan is not ready to execute")
    except (Gr01ReleasePlanError, OSError, TypeError, ValueError) as exc:
        reasons.append(str(exc))
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "event": "multidetect_gr01_release_session_plan_reverified",
        "plan_path": str(path),
        "plan_sha256": _sha256(path),
        "gate_passed": not reasons,
        "reasons": reasons,
    }


def _configuration_arguments(parser: argparse.ArgumentParser) -> None:
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
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="write a source-bound no-network plan")
    _configuration_arguments(create)
    create.add_argument("--out", required=True, type=Path)
    verify = subparsers.add_parser("verify", help="recompute and verify a saved plan")
    verify.add_argument("plan", type=Path)
    verify.add_argument("--check-environment", action="store_true")
    verify.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            document = create_gr01_release_session_plan(
                args.out,
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
                repository_root=args.repository_root,
                python_path=args.python,
                output_root=args.output_root,
            )
        else:
            document = verify_gr01_release_session_plan(
                args.plan,
                check_environment=args.check_environment,
                require_ready=args.require_ready,
            )
    except (Gr01ReleasePlanError, OSError, TypeError, ValueError) as exc:
        print(f"GR01 release-session plan: {exc}")
        return 2
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if args.command == "create" or document.get("gate_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
