"""Capture a receive-only PX4 Mission/profile/identity sidecar set.

This is the field-facing bridge between the bounded live camera observation
and the existing PX4 Mission feedback/join contracts.  One provider session
collects a Mission feedback report, keeps telemetry requests separate from
application/flight-control output, derives the observed endpoint/vehicle
identity, and immediately reruns the five-source software join.  It never
constructs a flight-control provider or emits a command other than the
optional bounded telemetry discovery requests owned by
``PixhawkReadOnlyTelemetryProvider``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from .autonomy.policy_artifact import MAX_POLICY_ARTIFACT_BYTES, PolicyArtifact
from .autonomy.serialization import (
    AutonomyContractError,
    require_sha256,
    require_text,
    strict_json_loads,
)
from .compat import UTC
from .live_runtime_observation_mission_binding import (
    build_runtime_endpoint_vehicle_identity_sidecar,
    verify_live_runtime_observation_mission_binding,
    verify_runtime_endpoint_vehicle_identity_sidecar,
)
from .mission_planning import load_executable_mission_document
from .pixhawk import (
    PIXHAWK_AUTOPILOT_IDS,
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
)
from .px4_mission_feedback import Px4MissionFeedbackConfig, Px4MissionFeedbackTracker
from .px4_mission_feedback_cli import (
    load_px4_mission_feedback_observation,
    observe_px4_mission_feedback,
    write_px4_mission_feedback_observation,
)
from .runtime import RuntimeMode
from .runtime_launcher import build_pixhawk_runtime_identity, signing_identity_hash_from_hex
from .runtime_observation import load_runtime_observation_result

SCHEMA = "multidetect.px4-runtime-capture"
SCHEMA_VERSION = 1
EVENT = "px4_runtime_capture_completed"
ERROR_EVENT = "px4_runtime_capture_error"
_MODES = frozenset({RuntimeMode.HIL.value, RuntimeMode.REAL.value})
_MAX_HMAC_KEY_BYTES = 4096


class Px4RuntimeCaptureError(AutonomyContractError):
    """Raised when a field capture cannot produce a consistent sidecar set."""


def _base_path(value: str | Path) -> Path:
    base = Path(value).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    if not base.is_dir():
        raise Px4RuntimeCaptureError(f"base directory is not a directory: {base}")
    return base


def _input_path(value: str | Path, *, base: Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.is_symlink():
        raise Px4RuntimeCaptureError(f"{label} must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Px4RuntimeCaptureError(f"{label} must resolve inside base directory {base}") from exc
    if not resolved.is_file():
        raise Px4RuntimeCaptureError(f"{label} must be a regular file")
    return resolved


def _output_path(value: str | Path, *, base: Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.exists() and candidate.is_symlink():
        raise Px4RuntimeCaptureError(f"{label} must not be a symbolic link")
    try:
        parent = candidate.parent.resolve(strict=False)
        parent.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Px4RuntimeCaptureError(f"{label} must resolve inside base directory {base}") from exc
    parent.mkdir(parents=True, exist_ok=True)
    resolved = parent / candidate.name
    if resolved == base:
        raise Px4RuntimeCaptureError(f"{label} must be a file path")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_policy_artifact(
    path: Path,
    *,
    mission_airframe_profile_hash: str,
    expected_file_sha256: str | None,
) -> tuple[PolicyArtifact, str]:
    """Load and bind the exact policy artifact used by a field capture.

    The artifact remains metadata-only and never grants actuator authority.  The
    airframe identity check prevents a policy trained/bound for a different
    vehicle profile from entering a HIL/REAL capture, while the optional file
    digest lets the operator bind the copied artifact before opening telemetry.
    """

    payload = path.read_bytes()
    if len(payload) > MAX_POLICY_ARTIFACT_BYTES:
        raise Px4RuntimeCaptureError("policy artifact exceeds size limit")
    file_sha256 = hashlib.sha256(payload).hexdigest()
    if expected_file_sha256 is not None:
        if file_sha256 != require_sha256(expected_file_sha256, "policy artifact file SHA-256"):
            raise Px4RuntimeCaptureError("policy artifact file SHA-256 mismatch")
    try:
        artifact = PolicyArtifact.from_mapping(strict_json_loads(payload, name="policy artifact"))
    except (AutonomyContractError, TypeError, ValueError, KeyError) as exc:
        raise Px4RuntimeCaptureError("policy artifact is invalid") from exc
    if artifact.airframe_profile_hash != mission_airframe_profile_hash:
        raise Px4RuntimeCaptureError(
            "policy artifact airframe profile does not match Mission airframe profile"
        )
    return artifact, file_sha256


def _atomic_write_json(
    path: Path,
    document: Mapping[str, object],
    *,
    overwrite: bool,
) -> Path:
    if path.exists() and not overwrite:
        raise Px4RuntimeCaptureError(f"refusing to overwrite existing output: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _hmac_key_from_environment(name: str) -> bytes:
    variable = require_text(name, "runtime profile HMAC key environment variable")
    value = os.environ.get(variable)
    if value is None or not value:
        raise Px4RuntimeCaptureError(
            f"runtime profile HMAC key environment variable is missing: {variable}"
        )
    encoded = value.encode("utf-8")
    if len(encoded) < 32 or len(encoded) > _MAX_HMAC_KEY_BYTES:
        raise Px4RuntimeCaptureError(
            f"runtime profile HMAC key must contain between 32 and {_MAX_HMAC_KEY_BYTES} bytes"
        )
    return encoded


def _signing_hash(
    *,
    runtime_mode: str,
    identity_hash: str | None,
    key_environment: str | None,
) -> str | None:
    if identity_hash is not None and key_environment is not None:
        raise Px4RuntimeCaptureError(
            "--runtime-signing-identity-hash and --runtime-signing-key-hex-env "
            "are mutually exclusive"
        )
    resolved = identity_hash
    if key_environment is not None:
        variable = require_text(key_environment, "runtime signing key environment variable")
        value = os.environ.get(variable)
        if value is None or not value.strip():
            raise Px4RuntimeCaptureError(
                f"runtime signing key environment variable is missing: {variable}"
            )
        resolved = signing_identity_hash_from_hex(value)
    if runtime_mode == RuntimeMode.REAL.value and resolved is None:
        raise Px4RuntimeCaptureError(
            "REAL capture requires --runtime-signing-identity-hash or --runtime-signing-key-hex-env"
        )
    if runtime_mode == RuntimeMode.HIL.value and resolved is not None:
        raise Px4RuntimeCaptureError("HIL capture cannot carry a REAL signing identity")
    return resolved


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Px4RuntimeCaptureError("observed_at must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def capture_px4_runtime_sidecars(
    *,
    base_directory: str | Path,
    mission: str | Path,
    runtime_observation: str | Path,
    runtime_profile: str | Path,
    policy_artifact: str | Path | None = None,
    policy_artifact_file_sha256: str | None = None,
    runtime_mode: str,
    endpoint: str,
    mission_feedback_out: str | Path = Path("feedback/px4-mission-feedback.json"),
    endpoint_vehicle_identity_out: str | Path = Path("identity/px4-endpoint-vehicle.json"),
    join_out: str | Path = Path("join/live-runtime-observation-mission-binding.json"),
    capture_summary_out: str | Path | None = None,
    baud: int = 57_600,
    expected_system_id: int = 1,
    expected_component_id: int = 1,
    provider_stale_after_seconds: float = 1.5,
    duration_seconds: float = 30.0,
    poll_interval_seconds: float = 0.05,
    request_autopilot_version: bool = True,
    request_ranging_streams: bool = False,
    runtime_profile_hmac_key: bytes,
    runtime_profile_key_id: str,
    runtime_signing_identity_hash: str | None = None,
    overwrite: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
    provider_factory: Callable[..., PixhawkReadOnlyTelemetryProvider] | None = None,
) -> dict[str, object]:
    """Capture feedback and identity sidecars, then rerun the five-source join."""

    mode = require_text(runtime_mode, "runtime_mode").lower()
    if mode not in _MODES:
        raise Px4RuntimeCaptureError("runtime_mode must be hil or real")
    if (
        not isinstance(runtime_profile_hmac_key, bytes)
        or not 32 <= len(runtime_profile_hmac_key) <= _MAX_HMAC_KEY_BYTES
    ):
        raise Px4RuntimeCaptureError("runtime_profile_hmac_key must contain 32..4096 bytes")
    key_id = require_text(runtime_profile_key_id, "runtime_profile_key_id")
    if not isinstance(request_autopilot_version, bool) or not isinstance(
        request_ranging_streams, bool
    ):
        raise Px4RuntimeCaptureError("telemetry request flags must be boolean")

    base = _base_path(base_directory)
    mission_path = _input_path(mission, base=base, label="mission")
    live_path = _input_path(runtime_observation, base=base, label="runtime_observation")
    profile_path = _input_path(runtime_profile, base=base, label="runtime_profile")
    policy_artifact_path = (
        None
        if policy_artifact is None
        else _input_path(policy_artifact, base=base, label="policy_artifact")
    )
    feedback_path = _output_path(
        mission_feedback_out,
        base=base,
        label="mission_feedback_out",
    )
    identity_path = _output_path(
        endpoint_vehicle_identity_out,
        base=base,
        label="endpoint_vehicle_identity_out",
    )
    join_path = _output_path(join_out, base=base, label="join_out")
    capture_summary_path = (
        None
        if capture_summary_out is None
        else _output_path(capture_summary_out, base=base, label="capture_summary_out")
    )
    source_paths = (mission_path, live_path, profile_path) + (
        (policy_artifact_path,) if policy_artifact_path is not None else ()
    )
    output_paths = (feedback_path, identity_path, join_path)
    if capture_summary_path is not None:
        output_paths = output_paths + (capture_summary_path,)
    if len({path for path in source_paths + output_paths}) != len(source_paths + output_paths):
        raise Px4RuntimeCaptureError("input and output paths must be distinct")

    mission_document = load_executable_mission_document(mission_path)
    policy_document = None
    policy_file_sha256 = None
    if policy_artifact_path is not None:
        policy_document, policy_file_sha256 = _load_policy_artifact(
            policy_artifact_path,
            mission_airframe_profile_hash=mission_document.airframe_profile_hash,
            expected_file_sha256=policy_artifact_file_sha256,
        )
    live_result = load_runtime_observation_result(live_path)
    if live_result.mode.value != mode:
        raise Px4RuntimeCaptureError("runtime observation mode does not match capture mode")
    if live_result.gate_passed is not True:
        raise Px4RuntimeCaptureError("runtime observation result gate is not passed")
    timestamp = _timestamp(observed_at())
    signing_hash = _signing_hash(
        runtime_mode=mode,
        identity_hash=runtime_signing_identity_hash,
        key_environment=None,
    )

    tracker = Px4MissionFeedbackTracker(
        mission_document,
        config=Px4MissionFeedbackConfig(
            expected_system_id=expected_system_id,
            expected_component_id=expected_component_id,
        ),
    )
    factory = PixhawkReadOnlyTelemetryProvider if provider_factory is None else provider_factory
    provider = factory(
        PixhawkReadOnlyConfig(
            endpoint=endpoint,
            baud=baud,
            stale_after_seconds=provider_stale_after_seconds,
            expected_system_id=expected_system_id,
            expected_autopilot_id=PIXHAWK_AUTOPILOT_IDS["px4"],
            require_operational_state=True,
            request_ranging_streams=request_ranging_streams,
            request_autopilot_version=request_autopilot_version,
        ),
        mission_feedback_tracker=tracker,
    )
    try:
        feedback_document = observe_px4_mission_feedback(
            mission_document,
            provider,
            runtime_mode=mode,
            duration_s=duration_seconds,
            poll_interval_s=poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
            observed_at=lambda: datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
            runtime_profile_path=profile_path,
            runtime_profile_hmac_key=runtime_profile_hmac_key,
            runtime_profile_key_id=key_id,
            runtime_signing_identity_hash=signing_hash,
        )
        heartbeat = provider.heartbeat_identity
        autopilot = provider.autopilot_version_identity
        resolved_endpoint = provider.resolved_endpoint
        if not isinstance(heartbeat, PixhawkHeartbeatIdentity):
            raise Px4RuntimeCaptureError("provider did not expose a complete heartbeat identity")
        if not isinstance(autopilot, PixhawkAutopilotVersionIdentity):
            raise Px4RuntimeCaptureError(
                "provider did not expose a complete AUTOPILOT_VERSION identity"
            )
        if not isinstance(resolved_endpoint, str) or not resolved_endpoint.strip():
            raise Px4RuntimeCaptureError("provider endpoint was not resolved")
        identity = build_pixhawk_runtime_identity(
            provider,
            airframe_profile_hash=mission_document.airframe_profile_hash,
            signing_identity_hash=signing_hash,
        )
        if identity.vehicle_identity_hash is None or identity.firmware_identity_hash is None:
            raise Px4RuntimeCaptureError("provider identity is incomplete")
        identity_document = build_runtime_endpoint_vehicle_identity_sidecar(
            runtime_mode=mode,
            runtime_descriptor_hash=live_result.runtime_run_report.descriptor_hash,
            runtime_input_hash=live_result.runtime_run_report.input_hash,
            runtime_profile_document_sha256=_file_sha256(profile_path),
            endpoint={
                "configured_endpoint": provider.config.endpoint,
                "resolved_endpoint": resolved_endpoint,
                "baud": provider.config.baud,
            },
            heartbeat_identity=heartbeat,
            observed_identity=identity,
            observed_at_utc=timestamp,
        )
        verify_runtime_endpoint_vehicle_identity_sidecar(identity_document)
        written_feedback = write_px4_mission_feedback_observation(
            feedback_path,
            feedback_document,
            overwrite=overwrite,
        )
        reopened_feedback = load_px4_mission_feedback_observation(
            written_feedback,
            expected_mission=mission_document,
            runtime_profile_path=profile_path,
        )
        written_identity = _atomic_write_json(
            identity_path,
            identity_document,
            overwrite=overwrite,
        )
        identity_raw = strict_json_loads(
            written_identity.read_bytes(),
            name="endpoint/vehicle identity sidecar",
        )
        verify_runtime_endpoint_vehicle_identity_sidecar(identity_raw)
        join_document = verify_live_runtime_observation_mission_binding(
            live_path,
            written_feedback,
            profile_path,
            written_identity,
            mission=mission_path,
            base_directory=base,
            hmac_key=runtime_profile_hmac_key,
            expected_key_id=key_id,
        )
        _atomic_write_json(join_path, join_document, overwrite=overwrite)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    transport = reopened_feedback.get("transport")
    summary = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "runtime_mode": mode,
        "mission": {
            "path": str(mission_path),
            "mission_id": mission_document.mission_id,
            "mission_hash": mission_document.canonical_hash,
        },
        "policy_artifact": (
            {
                "path": str(policy_artifact_path),
                "file_sha256": policy_file_sha256,
                "canonical_sha256": policy_document.canonical_hash,
                "policy_id": policy_document.policy_id,
                "policy_version": policy_document.policy_version,
                "ranker_sha256": policy_document.ranker_sha256,
                "airframe_profile_hash": policy_document.airframe_profile_hash,
                "mission_contract_hash": policy_document.mission_contract_hash,
                "safety_policy_hash": policy_document.safety_policy_hash,
            }
            if policy_artifact_path is not None and policy_document is not None
            else None
        ),
        "runtime_observation": {
            "path": str(live_path),
            "result_sha256": live_result.canonical_hash,
            "runtime_descriptor_hash": live_result.runtime_run_report.descriptor_hash,
            "runtime_input_hash": live_result.runtime_run_report.input_hash,
        },
        "mission_feedback": {
            "path": str(written_feedback),
            "evidence_sha256": reopened_feedback["evidence_sha256"],
            "gate_passed": reopened_feedback["feedback_gate_passed"],
        },
        "runtime_profile": {
            "path": str(profile_path),
            "document_sha256": _file_sha256(profile_path),
        },
        "endpoint_vehicle_identity": {
            "path": str(written_identity),
            "identity_sha256": identity_document["identity_sha256"],
            "gate_passed": True,
        },
        "transport": dict(transport) if isinstance(transport, Mapping) else None,
        "join": {
            "path": str(join_path),
            "evidence_sha256": join_document["evidence_sha256"],
            "gate_passed": join_document["gate_passed"],
            "reasons": join_document["reasons"],
        },
        "claims": {
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "operator_visual_acceptance": False,
        },
    }
    if capture_summary_path is not None:
        _atomic_write_json(capture_summary_path, summary, overwrite=overwrite)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-directory", type=Path, required=True)
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--runtime-observation", type=Path, required=True)
    parser.add_argument("--runtime-profile", type=Path, required=True)
    parser.add_argument("--policy-artifact", type=Path)
    parser.add_argument("--policy-artifact-file-sha256")
    parser.add_argument("--runtime-mode", choices=sorted(_MODES), required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--baud", type=int, default=57_600)
    parser.add_argument("--expected-system-id", type=int, default=1)
    parser.add_argument("--expected-component-id", type=int, default=1)
    parser.add_argument("--provider-stale-after-seconds", type=float, default=1.5)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.05)
    parser.add_argument(
        "--request-autopilot-version",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="request a bounded AUTOPILOT_VERSION telemetry response (default: true)",
    )
    parser.add_argument("--request-ranging-streams", action="store_true")
    parser.add_argument("--runtime-profile-hmac-key-env", required=True)
    parser.add_argument("--runtime-profile-key-id", required=True)
    parser.add_argument("--runtime-signing-identity-hash")
    parser.add_argument("--runtime-signing-key-hex-env")
    parser.add_argument(
        "--mission-feedback-out", type=Path, default=Path("feedback/px4-mission-feedback.json")
    )
    parser.add_argument(
        "--endpoint-vehicle-identity-out",
        type=Path,
        default=Path("identity/px4-endpoint-vehicle.json"),
    )
    parser.add_argument(
        "--join-out",
        type=Path,
        default=Path("join/live-runtime-observation-mission-binding.json"),
    )
    parser.add_argument(
        "--capture-summary-out",
        type=Path,
        help="write the completed capture summary to a file beneath --base-directory",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        hmac_variable = require_text(
            args.runtime_profile_hmac_key_env,
            "runtime profile HMAC key environment variable",
        )
        key = _hmac_key_from_environment(hmac_variable)
        signing_hash = args.runtime_signing_identity_hash
        signing_key_environment = args.runtime_signing_key_hex_env
        if signing_hash is not None and signing_key_environment is not None:
            raise Px4RuntimeCaptureError(
                "--runtime-signing-identity-hash and --runtime-signing-key-hex-env "
                "are mutually exclusive"
            )
        if signing_key_environment is not None:
            variable = require_text(
                signing_key_environment,
                "runtime signing key environment variable",
            )
            value = os.environ.get(variable)
            if value is None or not value.strip():
                raise Px4RuntimeCaptureError(
                    f"runtime signing key environment variable is missing: {variable}"
                )
            signing_hash = signing_identity_hash_from_hex(value)
        summary = capture_px4_runtime_sidecars(
            base_directory=args.base_directory,
            mission=args.mission,
            runtime_observation=args.runtime_observation,
            runtime_profile=args.runtime_profile,
            policy_artifact=args.policy_artifact,
            policy_artifact_file_sha256=args.policy_artifact_file_sha256,
            runtime_mode=args.runtime_mode,
            endpoint=args.endpoint,
            mission_feedback_out=args.mission_feedback_out,
            endpoint_vehicle_identity_out=args.endpoint_vehicle_identity_out,
            join_out=args.join_out,
            capture_summary_out=args.capture_summary_out,
            baud=args.baud,
            expected_system_id=args.expected_system_id,
            expected_component_id=args.expected_component_id,
            provider_stale_after_seconds=args.provider_stale_after_seconds,
            duration_seconds=args.duration_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            request_autopilot_version=args.request_autopilot_version,
            request_ranging_streams=args.request_ranging_streams,
            runtime_profile_hmac_key=key,
            runtime_profile_key_id=args.runtime_profile_key_id,
            runtime_signing_identity_hash=signing_hash,
            overwrite=args.overwrite,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["join"]["gate_passed"] is True else 1
    except (OSError, TypeError, ValueError, AutonomyContractError) as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "event": ERROR_EVENT,
                    "error_type": type(exc).__name__,
                    "message": str(exc) or type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ERROR_EVENT",
    "EVENT",
    "Px4RuntimeCaptureError",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_parser",
    "capture_px4_runtime_sidecars",
    "main",
]
