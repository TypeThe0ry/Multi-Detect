"""Operate the six-leaf HIL/REAL Pixhawk covariance field campaign.

The command processes at most one pending leaf per ``capture-next`` call.  This
is intentional: the operator must connect and identify the requested airframe
before each immutable observation is collected.  Existing reports are always
re-opened and verified; they are never silently overwritten or trusted by
filename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .mission_planning import load_executable_mission_document
from .online_covariance_preflight import (
    DEFAULT_MAXIMUM_AGE_SECONDS,
    OnlineCovariancePreflightError,
    build_online_covariance_preflight_observation,
    load_online_covariance_preflight_observation,
    write_online_covariance_preflight_observation,
)
from .pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from .pixhawk_covariance_binding import (
    REQUIRED_COMBINATIONS,
    PixhawkCovarianceBindingError,
    build_pixhawk_covariance_binding,
    build_pixhawk_covariance_mode_binding,
    verify_pixhawk_covariance_leaf,
)
from .px4_mission import (
    PX4_FIXED_WING_ADAPTER_HASH,
    PX4_FIXED_WING_ADAPTER_ID,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    PX4_VTOL_ADAPTER_HASH,
    PX4_VTOL_ADAPTER_ID,
)
from .px4_mission_feedback_cli import load_px4_mission_feedback_observation

SCHEMA = "multidetect.pixhawk-covariance-field-campaign/v1"
SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
CONFIG_SCHEMA_PATH = ROOT / "configs" / "schemas" / "pixhawk-covariance-field-campaign.schema.json"
CAPTURE_SCRIPT = ROOT / "scripts" / "capture_pixhawk_odometry_covariance.py"
DEFAULT_SAMPLES = 100
DEFAULT_INTERVAL_S = 0.1
DEFAULT_MINIMUM_ODOMETRY = 20
DEFAULT_MINIMUM_COVARIANCES = 10
DEFAULT_PROBE_SAMPLES = 100
DEFAULT_PROBE_INTERVAL_S = 0.1

_ADAPTER_BY_VEHICLE_CLASS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}


class FieldCovarianceCampaignError(ValueError):
    """Raised when a campaign config, source or transition is invalid."""


@dataclass(frozen=True, slots=True)
class CampaignLeaf:
    runtime_mode: str
    vehicle_class: str
    endpoint: str
    baud: int
    mission_path: Path
    mission_feedback_path: Path
    runtime_profile_path: Path
    report_path: Path

    @property
    def key(self) -> tuple[str, str]:
        return self.runtime_mode, self.vehicle_class


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    path: Path
    release_label: str
    leaves: tuple[CampaignLeaf, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FieldCovarianceCampaignError(f"cannot read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise FieldCovarianceCampaignError(f"{name} must be a JSON object")
    return value


def _validate_schema(document: Mapping[str, object]) -> None:
    schema = _load_json_object(CONFIG_SCHEMA_PATH, name="campaign JSON Schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise FieldCovarianceCampaignError(f"campaign schema validation failed: {exc}") from exc


def _resolve_source(base: Path, value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FieldCovarianceCampaignError(f"{name} is missing")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.is_symlink():
        raise FieldCovarianceCampaignError(f"{name} cannot be a symbolic link")
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise FieldCovarianceCampaignError(f"{name} cannot be resolved: {exc}") from exc
    if not path.is_file():
        raise FieldCovarianceCampaignError(f"{name} must be a file")
    return path


def _resolve_output(base: Path, value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FieldCovarianceCampaignError(f"{name} is missing")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.exists() and candidate.is_symlink():
        raise FieldCovarianceCampaignError(f"{name} cannot be a symbolic link")
    return candidate.resolve()


def load_campaign_config(path: str | Path) -> CampaignConfig:
    """Load, schema-check and resolve all six immutable campaign leaves."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise FieldCovarianceCampaignError("campaign config cannot be a symbolic link")
    requested = candidate.resolve(strict=True)
    if not requested.is_file():
        raise FieldCovarianceCampaignError("campaign config must be a regular file")
    document = _load_json_object(requested, name="campaign config")
    _validate_schema(document)
    if document.get("schema") != SCHEMA or document.get("schema_version") != SCHEMA_VERSION:
        raise FieldCovarianceCampaignError("unsupported campaign schema")
    base = requested.parent
    by_key: dict[tuple[str, str], CampaignLeaf] = {}
    source_paths: set[Path] = set()
    report_paths: set[Path] = set()
    entries = document["entries"]
    if not isinstance(entries, list):  # Schema makes this a defensive guard.
        raise FieldCovarianceCampaignError("entries must be an array")
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping):
            raise FieldCovarianceCampaignError(f"entry {index} must be an object")
        mode = str(raw["runtime_mode"])
        vehicle = str(raw["vehicle_class"])
        key = (mode, vehicle)
        if key in by_key:
            raise FieldCovarianceCampaignError(f"duplicate campaign leaf {mode}/{vehicle}")
        mission = _resolve_source(base, raw["mission_path"], name=f"{mode}/{vehicle} Mission")
        feedback = _resolve_source(
            base,
            raw["mission_feedback_path"],
            name=f"{mode}/{vehicle} Mission feedback",
        )
        profile = _resolve_source(
            base,
            raw["runtime_profile_path"],
            name=f"{mode}/{vehicle} runtime profile",
        )
        report = _resolve_output(
            base,
            raw["report_path"],
            name=f"{mode}/{vehicle} report",
        )
        if len({mission, feedback, profile}) != 3:
            raise FieldCovarianceCampaignError(
                f"{mode}/{vehicle} Mission, feedback and profile must be distinct"
            )
        if report in {mission, feedback, profile}:
            raise FieldCovarianceCampaignError(f"{mode}/{vehicle} report aliases a source")
        if report in report_paths:
            raise FieldCovarianceCampaignError("every campaign leaf needs a distinct report path")
        source_paths.update((mission, feedback, profile))
        report_paths.add(report)
        by_key[key] = CampaignLeaf(
            runtime_mode=mode,
            vehicle_class=vehicle,
            endpoint=str(raw["endpoint"]),
            baud=int(raw["baud"]),
            mission_path=mission,
            mission_feedback_path=feedback,
            runtime_profile_path=profile,
            report_path=report,
        )
    if set(by_key) != set(REQUIRED_COMBINATIONS):
        missing = sorted(set(REQUIRED_COMBINATIONS).difference(by_key))
        unknown = sorted(set(by_key).difference(REQUIRED_COMBINATIONS))
        raise FieldCovarianceCampaignError(
            f"campaign coverage must be exact; missing={missing!r}, unknown={unknown!r}"
        )
    if source_paths.intersection(report_paths):
        raise FieldCovarianceCampaignError("a report path aliases another leaf's source")
    return CampaignConfig(
        path=requested,
        release_label=str(document["release_label"]),
        leaves=tuple(by_key[key] for key in REQUIRED_COMBINATIONS),
    )


def capture_command(
    leaf: CampaignLeaf,
    *,
    samples: int = DEFAULT_SAMPLES,
    interval_seconds: float = DEFAULT_INTERVAL_S,
    minimum_odometry_messages: int = DEFAULT_MINIMUM_ODOMETRY,
    minimum_accepted_covariances: int = DEFAULT_MINIMUM_COVARIANCES,
) -> list[str]:
    """Return the exact isolated capture command for one requested airframe."""

    return [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "--runtime-mode",
        leaf.runtime_mode,
        "--endpoint",
        leaf.endpoint,
        "--baud",
        str(leaf.baud),
        "--mission",
        str(leaf.mission_path),
        "--mission-feedback-observation",
        str(leaf.mission_feedback_path),
        "--runtime-profile",
        str(leaf.runtime_profile_path),
        "--samples",
        str(samples),
        "--interval-seconds",
        str(interval_seconds),
        "--minimum-odometry-messages",
        str(minimum_odometry_messages),
        "--minimum-accepted-covariances",
        str(minimum_accepted_covariances),
        "--out",
        str(leaf.report_path),
    ]


def _source_preflight(leaf: CampaignLeaf) -> dict[str, object]:
    """Re-open and cross-bind one leaf's static sources without transport I/O."""

    base: dict[str, object] = {
        "runtime_mode": leaf.runtime_mode,
        "vehicle_class": leaf.vehicle_class,
        "endpoint": leaf.endpoint,
        "baud": leaf.baud,
        "mission_path": str(leaf.mission_path),
        "mission_feedback_path": str(leaf.mission_feedback_path),
        "runtime_profile_path": str(leaf.runtime_profile_path),
    }
    try:
        mission = load_executable_mission_document(leaf.mission_path)
        expected_adapter_id, expected_adapter_hash = _ADAPTER_BY_VEHICLE_CLASS[leaf.vehicle_class]
        if mission.airframe_adapter_id != expected_adapter_id:
            raise FieldCovarianceCampaignError(
                f"Mission adapter {mission.airframe_adapter_id!r} does not match "
                f"{leaf.vehicle_class} ({expected_adapter_id!r})"
            )
        if mission.airframe_adapter_hash.lower() != expected_adapter_hash.lower():
            raise FieldCovarianceCampaignError(
                f"Mission adapter hash is not current for {leaf.vehicle_class}"
            )
        feedback = load_px4_mission_feedback_observation(
            leaf.mission_feedback_path,
            expected_mission=mission,
            runtime_profile_path=leaf.runtime_profile_path,
        )
        if feedback.get("runtime_mode") != leaf.runtime_mode:
            raise FieldCovarianceCampaignError(
                "Mission feedback runtime_mode does not match campaign leaf"
            )
        if feedback.get("feedback_gate_passed") is not True:
            raise FieldCovarianceCampaignError("Mission feedback gate is not qualified")
        transport = feedback.get("transport")
        runtime_binding = feedback.get("runtime_binding")
        if not isinstance(transport, Mapping) or not isinstance(runtime_binding, Mapping):
            raise FieldCovarianceCampaignError(
                "Mission feedback has no verified transport/runtime binding"
            )
        if transport.get("configured_endpoint") != leaf.endpoint:
            raise FieldCovarianceCampaignError(
                "Mission feedback configured endpoint does not match campaign leaf"
            )
        if transport.get("baud") != leaf.baud:
            raise FieldCovarianceCampaignError("Mission feedback baud does not match campaign leaf")
        if transport.get("read_only") is not True or transport.get("messages_transmitted") != 0:
            raise FieldCovarianceCampaignError("Mission feedback is not receive-only")
        if runtime_binding.get("runtime_mode") != leaf.runtime_mode:
            raise FieldCovarianceCampaignError("runtime binding mode does not match campaign leaf")
        if runtime_binding.get("gate_passed") is not True:
            raise FieldCovarianceCampaignError("runtime binding gate is not qualified")
        if runtime_binding.get("signature_valid") is not True:
            raise FieldCovarianceCampaignError("runtime profile signature was not qualified")
        if runtime_binding.get("profile_valid") is not True:
            raise FieldCovarianceCampaignError("runtime profile is not qualified")
        if runtime_binding.get("flight_control_enabled") is not False:
            raise FieldCovarianceCampaignError("Mission feedback enabled flight-control output")
        if runtime_binding.get("output_locked") is not True:
            raise FieldCovarianceCampaignError("Mission feedback output is not locked")
        profile = runtime_binding.get("profile")
        identity = runtime_binding.get("observed_identity")
        if not isinstance(profile, Mapping) or not isinstance(identity, Mapping):
            raise FieldCovarianceCampaignError(
                "runtime binding has no verified profile/observed identity"
            )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {
            **base,
            "status": "blocked",
            "gate_passed": False,
            "reason": str(exc),
        }
    return {
        **base,
        "status": "ready",
        "gate_passed": True,
        "mission_sha256": _sha256(leaf.mission_path),
        "mission_feedback_sha256": _sha256(leaf.mission_feedback_path),
        "runtime_profile_sha256": _sha256(leaf.runtime_profile_path),
        "mission_id": mission.mission_id,
        "mission_hash": mission.canonical_hash,
        "airframe_adapter_id": mission.airframe_adapter_id,
        "airframe_adapter_hash": mission.airframe_adapter_hash,
        "airframe_profile_hash": mission.airframe_profile_hash,
        "runtime_profile_id": profile.get("profile_id"),
        "runtime_profile_key_id": runtime_binding.get("key_id"),
        "runtime_binding_sha256": runtime_binding.get("binding_sha256"),
        "vehicle_identity_hash": identity.get("vehicle_identity_hash"),
        "firmware_identity_hash": identity.get("firmware_identity_hash"),
        "endpoint_binding_hash": identity.get("endpoint_binding_hash"),
        "signing_identity_hash": identity.get("signing_identity_hash"),
        "read_only": True,
        "messages_transmitted": 0,
        "flight_control_enabled": False,
        "output_locked": True,
    }


def campaign_preflight(config: CampaignConfig) -> dict[str, object]:
    """Return a deterministic six-leaf READY/BLOCKED source checklist."""

    records = [_source_preflight(leaf) for leaf in config.leaves]
    blocked = [record for record in records if record["status"] == "blocked"]
    return {
        "schema": "multidetect.pixhawk-covariance-field-source-preflight/v1",
        "schema_version": 1,
        "release_label": config.release_label,
        "config_path": str(config.path),
        "config_sha256": _sha256(config.path),
        "records": records,
        "ready_count": len(records) - len(blocked),
        "blocked_count": len(blocked),
        "gate_passed": bool(records) and not blocked,
    }


def _finite_clock(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise FieldCovarianceCampaignError(f"{name} must be finite and non-negative")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FieldCovarianceCampaignError(f"{name} must be finite and non-negative") from exc
    if not math.isfinite(result) or result < 0.0:
        raise FieldCovarianceCampaignError(f"{name} must be finite and non-negative")
    return result


def _online_probe_leaf(
    leaf: CampaignLeaf,
    *,
    samples: int,
    interval_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, object]]:
    """Probe current Pixhawk identity with bounded telemetry-only requests."""

    if isinstance(samples, bool) or not isinstance(samples, int) or samples <= 0:
        raise FieldCovarianceCampaignError("probe samples must be a positive integer")
    if not math.isfinite(interval_seconds) or interval_seconds <= 0.0:
        raise FieldCovarianceCampaignError("probe interval_seconds must be finite and positive")
    if not callable(clock) or not callable(sleeper):
        raise FieldCovarianceCampaignError("probe clock and sleeper must be callable")
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            leaf.endpoint,
            baud=leaf.baud,
            expected_system_id=1,
            expected_autopilot_id=12,
            require_operational_state=True,
            request_ranging_streams=False,
            request_autopilot_version=True,
        )
    )
    started = _finite_clock(clock(), name="probe start clock")
    previous = started
    receive_errors: list[dict[str, str]] = []
    diagnostics: Mapping[str, object] = {}
    mode_binding: dict[str, object] = {}
    sample_count = 0
    try:
        for sample_index in range(samples):
            now_s = _finite_clock(clock(), name="probe sample clock")
            if now_s < previous:
                raise FieldCovarianceCampaignError("probe clock moved backwards")
            previous = now_s
            try:
                provider.snapshot(now_s=now_s)
            except Exception as exc:
                if len(receive_errors) < 8:
                    receive_errors.append(
                        {
                            "error_type": type(exc).__name__,
                            "message": (str(exc).strip() or type(exc).__name__)[:512],
                        }
                    )
            sample_count = sample_index + 1
            diagnostics = provider.diagnostics(now_s=now_s)
            mode_binding = build_pixhawk_covariance_mode_binding(
                runtime_mode=leaf.runtime_mode,
                endpoint=leaf.endpoint,
                baud=leaf.baud,
                diagnostics=diagnostics,
                mission_path=leaf.mission_path,
                mission_feedback_path=leaf.mission_feedback_path,
                runtime_profile_path=leaf.runtime_profile_path,
            )
            if mode_binding.get("gate_passed") is True and not receive_errors:
                break
            if sample_index + 1 < samples:
                sleeper(interval_seconds)
        completed = _finite_clock(clock(), name="probe completion clock")
        if completed < previous:
            raise FieldCovarianceCampaignError("probe clock moved backwards")
    finally:
        provider.close()
    requests = diagnostics.get("autopilot_version_requests")
    if not isinstance(requests, Mapping):
        requests = {}
    attempts = requests.get("attempts")
    transmitted = diagnostics.get("messages_transmitted")
    bounded_identity_query = (
        type(attempts) is int
        and 0 <= attempts <= 3
        and type(transmitted) is int
        and transmitted == attempts
        and diagnostics.get("telemetry_configuration_enabled") is False
    )
    checks = mode_binding.get("checks")
    failed_checks = (
        sorted(name for name, passed in checks.items() if passed is not True)
        if isinstance(checks, Mapping)
        else ["mode_binding_unavailable"]
    )
    reasons = [f"mode_binding:{name}" for name in failed_checks]
    if receive_errors:
        reasons.append("mavlink_receive_errors")
    if not bounded_identity_query:
        reasons.append("identity_query_budget_invalid")
    gate_passed = (
        mode_binding.get("gate_passed") is True and not receive_errors and bounded_identity_query
    )
    result: dict[str, object] = {
        "schema": "multidetect.pixhawk-covariance-online-preflight/v1",
        "schema_version": 1,
        "runtime_mode": leaf.runtime_mode,
        "vehicle_class": leaf.vehicle_class,
        "endpoint": leaf.endpoint,
        "baud": leaf.baud,
        "probe": {
            "samples_requested": samples,
            "sample_count": sample_count,
            "interval_seconds": interval_seconds,
            "started_at_s": started,
            "completed_at_s": completed,
            "duration_seconds": completed - started,
            "receive_errors": receive_errors,
        },
        "transport": {
            "configured_endpoint": diagnostics.get("configured_endpoint"),
            "resolved_endpoint": diagnostics.get("resolved_endpoint"),
            "read_only": diagnostics.get("read_only"),
            "hardware_control_enabled": diagnostics.get("hardware_control_enabled"),
            "messages_received": diagnostics.get("messages_received"),
            "messages_transmitted": transmitted,
            "transport_link_healthy": diagnostics.get("transport_link_healthy"),
            "qualified_link_healthy": diagnostics.get("qualified_link_healthy"),
            "qualification": diagnostics.get("qualification"),
        },
        "identity_query": {
            "request_ranging_streams": False,
            "request_autopilot_version": True,
            "attempts": attempts,
            "maximum_attempts": 3,
            "response_observed": requests.get("response_observed"),
            "budget_valid": bounded_identity_query,
        },
        "heartbeat_identity": diagnostics.get("heartbeat_identity"),
        "autopilot_version_identity": diagnostics.get("autopilot_version_identity"),
        "mode_binding": mode_binding,
        "reasons": reasons,
        "gate_passed": gate_passed,
        "claims": {
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "flight_acceptance": False,
        },
    }
    return (0 if gate_passed else 1), result


def probe_next(
    config: CampaignConfig,
    *,
    samples: int = DEFAULT_PROBE_SAMPLES,
    interval_seconds: float = DEFAULT_PROBE_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, object]]:
    """Static-preflight then probe the one currently pending campaign leaf."""

    status = campaign_status(config)
    if status["failed_count"]:
        raise FieldCovarianceCampaignError(
            "campaign has a failed immutable report; online probe is unsafe"
        )
    if status["complete"]:
        raise FieldCovarianceCampaignError("campaign is complete; no leaf needs probing")
    preflight = campaign_preflight(config)
    if preflight["gate_passed"] is not True:
        raise FieldCovarianceCampaignError("source preflight blocked online probe")
    next_leaf = status["next_leaf"]
    if not isinstance(next_leaf, Mapping):
        raise FieldCovarianceCampaignError("campaign has no safe next leaf")
    key = (str(next_leaf["runtime_mode"]), str(next_leaf["vehicle_class"]))
    leaf = next(item for item in config.leaves if item.key == key)
    exit_code, result = _online_probe_leaf(
        leaf,
        samples=samples,
        interval_seconds=interval_seconds,
        clock=clock,
        sleeper=sleeper,
    )
    observation = build_online_covariance_preflight_observation(
        {**result, "source_preflight": preflight},
        release_label=config.release_label,
        config_path=config.path,
        mission_path=leaf.mission_path,
        mission_feedback_path=leaf.mission_feedback_path,
        runtime_profile_path=leaf.runtime_profile_path,
    )
    return exit_code, observation


def _leaf_status(leaf: CampaignLeaf, *, manifest_directory: Path) -> dict[str, object]:
    base: dict[str, object] = {
        "runtime_mode": leaf.runtime_mode,
        "vehicle_class": leaf.vehicle_class,
        "report_path": str(leaf.report_path),
    }
    if not leaf.report_path.exists():
        return {**base, "status": "pending", "gate_passed": False, "reason": "report absent"}
    if leaf.report_path.is_symlink() or not leaf.report_path.is_file():
        return {
            **base,
            "status": "failed",
            "gate_passed": False,
            "reason": "report is not a regular file",
        }
    try:
        record = verify_pixhawk_covariance_leaf(
            leaf.runtime_mode,
            leaf.vehicle_class,
            leaf.report_path,
            manifest_directory=manifest_directory,
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**base, "status": "failed", "gate_passed": False, "reason": str(exc)}
    return {
        **base,
        "status": "passed",
        "gate_passed": True,
        "report_sha256": record["report_sha256"],
        "report_bytes": record["report_bytes"],
        "evidence_sha256": record["evidence_sha256"],
        "records_verified": record["records_verified"],
        "firmware_identity_hash": record["firmware_identity_hash"],
    }


def campaign_status(config: CampaignConfig) -> dict[str, object]:
    """Re-open every existing report and return the next safe operator action."""

    records = [_leaf_status(leaf, manifest_directory=config.path.parent) for leaf in config.leaves]
    pending = [record for record in records if record["status"] == "pending"]
    failed = [record for record in records if record["status"] == "failed"]
    next_leaf = pending[0] if pending and not failed else None
    e7_arguments: list[str] = []
    if all(record["status"] == "passed" for record in records):
        for leaf in config.leaves:
            e7_arguments.extend(
                [
                    "--pixhawk-covariance-evidence",
                    leaf.runtime_mode,
                    leaf.vehicle_class,
                    str(leaf.report_path),
                ]
            )
    return {
        "schema": "multidetect.pixhawk-covariance-field-campaign-status/v1",
        "schema_version": 1,
        "release_label": config.release_label,
        "config_path": str(config.path),
        "config_sha256": _sha256(config.path),
        "records": records,
        "passed_count": sum(record["status"] == "passed" for record in records),
        "pending_count": len(pending),
        "failed_count": len(failed),
        "next_leaf": next_leaf,
        "complete": bool(records) and not pending and not failed,
        "e7_arguments": e7_arguments,
    }


def _atomic_write(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_status_output(
    config: CampaignConfig,
    status_out: Path | None,
    *,
    other_outputs: Sequence[Path | None] = (),
) -> None:
    if status_out is None:
        return
    candidate = status_out.expanduser()
    if candidate.is_symlink():
        raise FieldCovarianceCampaignError("status output cannot be a symbolic link")
    output = candidate.resolve(strict=False)
    reserved = {config.path}
    for leaf in config.leaves:
        reserved.update(
            {
                leaf.mission_path,
                leaf.mission_feedback_path,
                leaf.runtime_profile_path,
                leaf.report_path,
            }
        )
    reserved.update(
        item.expanduser().resolve(strict=False) for item in other_outputs if item is not None
    )
    if output in reserved:
        raise FieldCovarianceCampaignError("status output aliases a campaign source or output")


def capture_next(
    config: CampaignConfig,
    *,
    samples: int,
    interval_seconds: float,
    minimum_odometry_messages: int,
    minimum_accepted_covariances: int,
    dry_run: bool,
    probe_samples: int = DEFAULT_PROBE_SAMPLES,
    probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_S,
    probe_report_out: Path | None = None,
) -> tuple[int, dict[str, object]]:
    """Capture exactly one pending leaf; failed immutable reports stop progress."""

    for name, value in (
        ("samples", samples),
        ("minimum_odometry_messages", minimum_odometry_messages),
        ("minimum_accepted_covariances", minimum_accepted_covariances),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FieldCovarianceCampaignError(f"{name} must be a positive integer")
    if not math.isfinite(interval_seconds) or interval_seconds <= 0.0:
        raise FieldCovarianceCampaignError("interval_seconds must be finite and positive")

    before = campaign_status(config)
    if before["failed_count"]:
        raise FieldCovarianceCampaignError(
            "campaign has a failed immutable report; inspect and archive it before retrying"
        )
    if before["complete"]:
        return 0, before
    next_leaf = before["next_leaf"]
    if not isinstance(next_leaf, Mapping):
        raise FieldCovarianceCampaignError("campaign has no safe next leaf")
    key = (str(next_leaf["runtime_mode"]), str(next_leaf["vehicle_class"]))
    leaf = next(item for item in config.leaves if item.key == key)
    preflight = campaign_preflight(config)
    if preflight["gate_passed"] is not True:
        blocked = [
            f"{record['runtime_mode']}/{record['vehicle_class']}: {record['reason']}"
            for record in preflight["records"]
            if record["status"] == "blocked"
        ]
        raise FieldCovarianceCampaignError(
            "source preflight blocked capture: " + "; ".join(blocked)
        )
    command = capture_command(
        leaf,
        samples=samples,
        interval_seconds=interval_seconds,
        minimum_odometry_messages=minimum_odometry_messages,
        minimum_accepted_covariances=minimum_accepted_covariances,
    )
    if dry_run:
        if probe_report_out is not None:
            raise FieldCovarianceCampaignError(
                "--probe-report-out cannot be used with --dry-run"
            )
        return 0, {
            **before,
            "source_preflight": preflight,
            "dry_run": True,
            "capture_command": command,
        }
    probe_exit_code, online_preflight = probe_next(
        config,
        samples=probe_samples,
        interval_seconds=probe_interval_seconds,
    )
    probe_record: dict[str, object] = {}
    if probe_report_out is not None:
        candidate = probe_report_out.expanduser()
        if candidate.is_symlink():
            raise FieldCovarianceCampaignError("online preflight output cannot be a symlink")
        resolved_probe = candidate.resolve(strict=False)
        reserved = {config.path, *(item.report_path for item in config.leaves)}
        if resolved_probe in reserved:
            raise FieldCovarianceCampaignError(
                "online preflight output aliases campaign or covariance report"
            )
        written = write_online_covariance_preflight_observation(
            resolved_probe,
            online_preflight,
            overwrite=False,
        )
        reloaded = load_online_covariance_preflight_observation(
            written,
            maximum_age_seconds=DEFAULT_MAXIMUM_AGE_SECONDS,
        )
        if reloaded != online_preflight:
            raise FieldCovarianceCampaignError(
                "saved online preflight differs from the in-memory observation"
            )
        probe_record = {
            "online_preflight_path": str(written),
            "online_preflight_bytes": written.stat().st_size,
            "online_preflight_file_sha256": _sha256(written),
            "online_preflight_evidence_sha256": reloaded["evidence_sha256"],
            "online_preflight_verified_before_capture": True,
        }
    if probe_exit_code != 0 or online_preflight.get("gate_passed") is not True:
        return 1, {
            **before,
            "source_preflight": preflight,
            "online_preflight": online_preflight,
            "capture_started": False,
            "capture_verified": False,
            **probe_record,
        }
    leaf.report_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    after = campaign_status(config)
    captured = next(
        record
        for record in after["records"]
        if (record["runtime_mode"], record["vehicle_class"]) == key
    )
    exit_code = (
        result.returncode
        if result.returncode != 0
        else (0 if captured["status"] == "passed" else 1)
    )
    return exit_code, {
        **after,
        "source_preflight": preflight,
        "online_preflight": online_preflight,
        "capture_started": True,
        "capture_process_exit_code": result.returncode,
        "capture_verified": captured["status"] == "passed",
        **probe_record,
    }


def verify_complete_campaign(
    config: CampaignConfig,
    *,
    binding_out: Path | None = None,
) -> dict[str, object]:
    """Build the same six-leaf record consumed by E7 and optionally save it."""

    status = campaign_status(config)
    if status["complete"] is not True:
        raise FieldCovarianceCampaignError("campaign is incomplete")
    parity = _load_json_object(ROOT / "configs" / "release-parity.json", name="release parity")
    hashes = parity.get("parity")
    if not isinstance(hashes, Mapping) or not isinstance(hashes.get("px4_mission_path_hash"), str):
        raise FieldCovarianceCampaignError("release parity has no PX4 Mission path hash")
    output_identity = binding_out or (config.path.parent / "pixhawk-covariance-binding.json")
    record = build_pixhawk_covariance_binding(
        [(leaf.runtime_mode, leaf.vehicle_class, leaf.report_path) for leaf in config.leaves],
        manifest_directory=output_identity.parent,
        output_identity=output_identity,
        release_label=config.release_label,
        px4_mission_path_hash=hashes["px4_mission_path_hash"],
    )
    result = {**status, "binding": record, "binding_output": str(output_identity)}
    if binding_out is not None:
        if binding_out.exists():
            raise FieldCovarianceCampaignError(f"binding output already exists: {binding_out}")
        _atomic_write(binding_out, record)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "status",
            "preflight",
            "probe-next",
            "verify-probe",
            "capture-next",
            "verify",
        ),
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--status-out", type=Path)
    parser.add_argument("--binding-out", type=Path)
    parser.add_argument("--probe-report", type=Path)
    parser.add_argument("--probe-report-out", type=Path)
    parser.add_argument("--maximum-age-seconds", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--probe-samples", type=int, default=DEFAULT_PROBE_SAMPLES)
    parser.add_argument(
        "--probe-interval-seconds",
        type=float,
        default=DEFAULT_PROBE_INTERVAL_S,
    )
    parser.add_argument("--minimum-odometry-messages", type=int, default=DEFAULT_MINIMUM_ODOMETRY)
    parser.add_argument(
        "--minimum-accepted-covariances",
        type=int,
        default=DEFAULT_MINIMUM_COVARIANCES,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_campaign_config(args.config)
        _validate_status_output(
            config,
            args.status_out,
            other_outputs=(args.binding_out, args.probe_report_out),
        )
        if args.command != "verify-probe" and args.probe_report is not None:
            raise FieldCovarianceCampaignError("--probe-report is reserved for verify-probe")
        if args.command != "capture-next" and args.probe_report_out is not None:
            raise FieldCovarianceCampaignError(
                "--probe-report-out is reserved for capture-next"
            )
        if args.command != "verify-probe" and args.maximum_age_seconds is not None:
            raise FieldCovarianceCampaignError("--maximum-age-seconds is reserved for verify-probe")
        if args.command == "status":
            result = campaign_status(config)
            exit_code = 0
        elif args.command == "preflight":
            if args.binding_out is not None or args.dry_run:
                raise FieldCovarianceCampaignError(
                    "preflight does not accept --binding-out or --dry-run"
                )
            result = campaign_preflight(config)
            exit_code = 0 if result["gate_passed"] is True else 1
        elif args.command == "probe-next":
            if args.binding_out is not None or args.dry_run:
                raise FieldCovarianceCampaignError(
                    "probe-next does not accept --binding-out or --dry-run"
                )
            exit_code, result = probe_next(
                config,
                samples=args.probe_samples,
                interval_seconds=args.probe_interval_seconds,
            )
        elif args.command == "verify-probe":
            if args.probe_report is None:
                raise FieldCovarianceCampaignError("verify-probe requires --probe-report")
            if args.binding_out is not None or args.dry_run:
                raise FieldCovarianceCampaignError(
                    "verify-probe does not accept --binding-out or --dry-run"
                )
            result = load_online_covariance_preflight_observation(
                args.probe_report,
                maximum_age_seconds=(
                    DEFAULT_MAXIMUM_AGE_SECONDS
                    if args.maximum_age_seconds is None
                    else args.maximum_age_seconds
                ),
            )
            source_binding = result.get("source_binding")
            if (
                not isinstance(source_binding, Mapping)
                or source_binding.get("campaign_config_path") != str(config.path)
                or source_binding.get("campaign_config_sha256") != _sha256(config.path)
            ):
                raise FieldCovarianceCampaignError("probe report does not match --config")
            exit_code = 0 if result["gate_passed"] is True else 1
        elif args.command == "capture-next":
            if args.binding_out is not None:
                raise FieldCovarianceCampaignError("--binding-out is reserved for verify")
            exit_code, result = capture_next(
                config,
                samples=args.samples,
                interval_seconds=args.interval_seconds,
                minimum_odometry_messages=args.minimum_odometry_messages,
                minimum_accepted_covariances=args.minimum_accepted_covariances,
                dry_run=args.dry_run,
                probe_samples=args.probe_samples,
                probe_interval_seconds=args.probe_interval_seconds,
                probe_report_out=args.probe_report_out,
            )
        else:
            if args.dry_run:
                raise FieldCovarianceCampaignError("--dry-run is reserved for capture-next")
            result = verify_complete_campaign(config, binding_out=args.binding_out)
            exit_code = 0
        if args.status_out is not None:
            _atomic_write(args.status_out.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return exit_code
    except (
        FieldCovarianceCampaignError,
        OnlineCovariancePreflightError,
        PixhawkCovarianceBindingError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
