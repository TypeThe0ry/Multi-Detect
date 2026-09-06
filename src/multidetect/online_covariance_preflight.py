"""Strict, expiring observation records for the covariance online preflight."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .compat import UTC
from .pixhawk_covariance_binding import build_pixhawk_covariance_mode_binding

SCHEMA = "multidetect.pixhawk-covariance-online-preflight/v2"
SCHEMA_VERSION = 2
DEFAULT_MAXIMUM_AGE_SECONDS = 300.0
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "pixhawk-covariance-online-preflight.schema.json"
COVARIANCE_SCHEMA_PATH = (
    ROOT / "configs" / "schemas" / "pixhawk-odometry-covariance-observation.schema.json"
)
PARITY_PATH = ROOT / "configs" / "release-parity.json"
RUNNER_PATH = ROOT / "src" / "multidetect" / "field_covariance_campaign.py"
BINDING_PATH = ROOT / "src" / "multidetect" / "pixhawk_covariance_binding.py"


class OnlineCovariancePreflightError(ValueError):
    """Raised when an online preflight observation cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OnlineCovariancePreflightError(f"cannot read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise OnlineCovariancePreflightError(f"{name} must be a JSON object")
    return value


def _schema_validator() -> Draft202012Validator:
    schema = _json_object(SCHEMA_PATH, name="online preflight JSON Schema")
    covariance_schema = _json_object(COVARIANCE_SCHEMA_PATH, name="covariance JSON Schema")
    try:
        Draft202012Validator.check_schema(schema)
        registry = Registry().with_resource(
            covariance_schema["$id"],
            Resource.from_contents(covariance_schema),
        )
    except Exception as exc:
        raise OnlineCovariancePreflightError(f"invalid online preflight schema: {exc}") from exc
    return Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )


def _validate_schema(document: Mapping[str, object]) -> None:
    errors = sorted(_schema_validator().iter_errors(document), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise OnlineCovariancePreflightError(
            f"online preflight schema validation failed at {location}: {first.message}"
        )


def _parse_utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OnlineCovariancePreflightError(f"{name} must be a UTC date-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OnlineCovariancePreflightError(f"{name} must be a UTC date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OnlineCovariancePreflightError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _resolved_file(value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise OnlineCovariancePreflightError(f"{name} path is missing")
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise OnlineCovariancePreflightError(f"{name} cannot be a symbolic link")
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise OnlineCovariancePreflightError(f"{name} cannot be resolved: {exc}") from exc
    if not path.is_file():
        raise OnlineCovariancePreflightError(f"{name} must be a regular file")
    return path


def _current_parity() -> tuple[dict[str, object], str, str]:
    parity = _json_object(PARITY_PATH, name="release parity")
    hashes = parity.get("parity")
    if not isinstance(hashes, dict):
        raise OnlineCovariancePreflightError("release parity has no parity hashes")
    release_label = parity.get("release_label")
    if not isinstance(release_label, str) or not release_label:
        raise OnlineCovariancePreflightError("release parity has no release label")
    return hashes, _sha256(PARITY_PATH), release_label


def _source_binding(
    *,
    config_path: Path,
    mission_path: Path,
    mission_feedback_path: Path,
    runtime_profile_path: Path,
) -> dict[str, object]:
    parity, parity_sha256, _ = _current_parity()
    return {
        "campaign_config_path": str(config_path),
        "campaign_config_sha256": _sha256(config_path),
        "mission_path": str(mission_path),
        "mission_sha256": _sha256(mission_path),
        "mission_feedback_path": str(mission_feedback_path),
        "mission_feedback_sha256": _sha256(mission_feedback_path),
        "runtime_profile_path": str(runtime_profile_path),
        "runtime_profile_sha256": _sha256(runtime_profile_path),
        "online_preflight_schema_path": str(SCHEMA_PATH),
        "online_preflight_schema_sha256": _sha256(SCHEMA_PATH),
        "campaign_runner_path": str(RUNNER_PATH),
        "campaign_runner_sha256": _sha256(RUNNER_PATH),
        "covariance_binding_path": str(BINDING_PATH),
        "covariance_binding_sha256": _sha256(BINDING_PATH),
        "release_parity_path": str(PARITY_PATH),
        "release_parity_sha256": parity_sha256,
        "release_parity_hashes": parity,
    }


def _verify_current_campaign(
    value: Mapping[str, object],
    *,
    config_path: Path,
    mission_path: Path,
    mission_feedback_path: Path,
    runtime_profile_path: Path,
    require_current_next_leaf: bool,
) -> dict[str, object]:
    """Re-run the authoritative campaign selection and six-leaf source gate."""

    # Local import avoids a module cycle: the campaign imports this observation
    # verifier, while standalone verification must reopen the campaign itself.
    from .field_covariance_campaign import (
        campaign_preflight,
        campaign_status,
        load_campaign_config,
    )

    config = load_campaign_config(config_path)
    key = (str(value["runtime_mode"]), str(value["vehicle_class"]))
    try:
        leaf = next(item for item in config.leaves if item.key == key)
    except StopIteration as exc:
        raise OnlineCovariancePreflightError(
            "online preflight leaf is absent from current campaign"
        ) from exc
    if (
        config.release_label != value["release_label"]
        or leaf.endpoint != value["endpoint"]
        or leaf.baud != value["baud"]
        or leaf.mission_path != mission_path
        or leaf.mission_feedback_path != mission_feedback_path
        or leaf.runtime_profile_path != runtime_profile_path
    ):
        raise OnlineCovariancePreflightError(
            "online preflight leaf does not match current campaign"
        )
    status = campaign_status(config)
    record = next(
        item for item in status["records"] if (item["runtime_mode"], item["vehicle_class"]) == key
    )
    if require_current_next_leaf:
        next_leaf = status.get("next_leaf")
        if (
            not isinstance(next_leaf, Mapping)
            or (next_leaf.get("runtime_mode"), next_leaf.get("vehicle_class")) != key
        ):
            raise OnlineCovariancePreflightError(
                "online preflight leaf is no longer the current next leaf"
            )
    elif record["status"] not in {"pending", "passed"}:
        raise OnlineCovariancePreflightError(
            "archived online preflight leaf has a failed current report"
        )
    preflight = campaign_preflight(config)
    if preflight.get("gate_passed") is not True:
        raise OnlineCovariancePreflightError("current six-leaf source preflight is not qualified")
    return {
        "schema": preflight.get("schema"),
        "schema_version": preflight.get("schema_version"),
        "release_label": preflight.get("release_label"),
        "config_path": preflight.get("config_path"),
        "config_sha256": preflight.get("config_sha256"),
        "ready_count": preflight.get("ready_count"),
        "blocked_count": preflight.get("blocked_count"),
        "gate_passed": preflight.get("gate_passed"),
    }


def build_online_covariance_preflight_observation(
    raw_probe: Mapping[str, object],
    *,
    release_label: str,
    config_path: Path,
    mission_path: Path,
    mission_feedback_path: Path,
    runtime_profile_path: Path,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Bind one live probe result to current sources and parity."""

    if not isinstance(raw_probe, Mapping):
        raise OnlineCovariancePreflightError("raw_probe must be an object")
    timestamp = datetime.now(UTC) if observed_at is None else observed_at
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise OnlineCovariancePreflightError("observed_at must be timezone-aware")
    clean = dict(raw_probe)
    source_preflight = clean.get("source_preflight")
    if not isinstance(source_preflight, Mapping):
        raise OnlineCovariancePreflightError("raw probe has no source preflight")
    clean["source_preflight"] = {
        "schema": source_preflight.get("schema"),
        "schema_version": source_preflight.get("schema_version"),
        "release_label": source_preflight.get("release_label"),
        "config_path": source_preflight.get("config_path"),
        "config_sha256": source_preflight.get("config_sha256"),
        "ready_count": source_preflight.get("ready_count"),
        "blocked_count": source_preflight.get("blocked_count"),
        "gate_passed": source_preflight.get("gate_passed"),
    }
    clean.update(
        {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "release_label": release_label,
            "observed_at_utc": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "source_binding": _source_binding(
                config_path=config_path,
                mission_path=mission_path,
                mission_feedback_path=mission_feedback_path,
                runtime_profile_path=runtime_profile_path,
            ),
        }
    )
    clean.pop("evidence_sha256", None)
    clean["evidence_sha256"] = _canonical_sha256(clean)
    return verify_online_covariance_preflight_observation(clean)


def verify_online_covariance_preflight_observation(
    raw: object,
    *,
    maximum_age_seconds: float | None = None,
    now: datetime | None = None,
    require_current_next_leaf: bool = True,
) -> dict[str, object]:
    """Rebuild source/current-link claims and optionally enforce UTC freshness."""

    if not isinstance(raw, Mapping):
        raise OnlineCovariancePreflightError("online preflight observation must be an object")
    if not isinstance(require_current_next_leaf, bool):
        raise OnlineCovariancePreflightError("require_current_next_leaf must be boolean")
    value = dict(raw)
    _validate_schema(value)
    claimed_hash = value["evidence_sha256"]
    without_hash = dict(value)
    del without_hash["evidence_sha256"]
    if claimed_hash != _canonical_sha256(without_hash):
        raise OnlineCovariancePreflightError("online preflight evidence hash mismatch")
    parity, parity_sha256, release_label = _current_parity()
    if value["release_label"] != release_label:
        raise OnlineCovariancePreflightError("online preflight release label is not current")
    source = value["source_binding"]
    if not isinstance(source, Mapping):  # Schema makes this defensive.
        raise OnlineCovariancePreflightError("source_binding must be an object")
    paths = {
        "campaign_config": _resolved_file(source["campaign_config_path"], name="campaign config"),
        "mission": _resolved_file(source["mission_path"], name="Mission"),
        "mission_feedback": _resolved_file(
            source["mission_feedback_path"], name="Mission feedback"
        ),
        "runtime_profile": _resolved_file(source["runtime_profile_path"], name="runtime profile"),
    }
    expected_sources = _source_binding(
        config_path=paths["campaign_config"],
        mission_path=paths["mission"],
        mission_feedback_path=paths["mission_feedback"],
        runtime_profile_path=paths["runtime_profile"],
    )
    if dict(source) != expected_sources:
        raise OnlineCovariancePreflightError("online preflight source binding is not current")
    if (
        source["release_parity_sha256"] != parity_sha256
        or source["release_parity_hashes"] != parity
    ):
        raise OnlineCovariancePreflightError("online preflight release parity is not current")
    source_preflight = value["source_preflight"]
    if not isinstance(source_preflight, Mapping):
        raise OnlineCovariancePreflightError("source_preflight must be an object")
    if (
        source_preflight["release_label"] != value["release_label"]
        or source_preflight["config_path"] != source["campaign_config_path"]
        or source_preflight["config_sha256"] != source["campaign_config_sha256"]
    ):
        raise OnlineCovariancePreflightError(
            "online observation does not match its source preflight"
        )
    current_preflight = _verify_current_campaign(
        value,
        config_path=paths["campaign_config"],
        mission_path=paths["mission"],
        mission_feedback_path=paths["mission_feedback"],
        runtime_profile_path=paths["runtime_profile"],
        require_current_next_leaf=require_current_next_leaf,
    )
    if dict(source_preflight) != current_preflight:
        raise OnlineCovariancePreflightError("online observation source preflight is not current")

    transport = value["transport"]
    if not isinstance(transport, Mapping):
        raise OnlineCovariancePreflightError("transport must be an object")
    diagnostics = {
        "configured_endpoint": transport["configured_endpoint"],
        "resolved_endpoint": transport["resolved_endpoint"],
        "read_only": transport["read_only"],
        "hardware_control_enabled": transport["hardware_control_enabled"],
        "transport_link_healthy": transport["transport_link_healthy"],
        "qualified_link_healthy": transport["qualified_link_healthy"],
        "qualification": transport["qualification"],
        "heartbeat_identity": value["heartbeat_identity"],
        "autopilot_version_identity": value["autopilot_version_identity"],
    }
    expected_mode_binding = build_pixhawk_covariance_mode_binding(
        runtime_mode=str(value["runtime_mode"]),
        endpoint=str(value["endpoint"]),
        baud=int(value["baud"]),
        diagnostics=diagnostics,
        mission_path=paths["mission"],
        mission_feedback_path=paths["mission_feedback"],
        runtime_profile_path=paths["runtime_profile"],
    )
    if value["mode_binding"] != expected_mode_binding:
        raise OnlineCovariancePreflightError("online preflight mode binding cannot be reproduced")

    identity_query = value["identity_query"]
    probe = value["probe"]
    if not isinstance(identity_query, Mapping) or not isinstance(probe, Mapping):
        raise OnlineCovariancePreflightError("probe/identity_query must be objects")
    if (
        probe["sample_count"] > probe["samples_requested"]
        or probe["completed_at_s"] < probe["started_at_s"]
        or not math.isclose(
            probe["duration_seconds"],
            probe["completed_at_s"] - probe["started_at_s"],
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        raise OnlineCovariancePreflightError("online preflight probe timing is inconsistent")
    attempts = identity_query["attempts"]
    transmitted = transport["messages_transmitted"]
    budget_valid = (
        type(attempts) is int
        and 0 <= attempts <= 3
        and type(transmitted) is int
        and transmitted == attempts
        and identity_query["request_ranging_streams"] is False
        and identity_query["request_autopilot_version"] is True
        and identity_query["maximum_attempts"] == 3
    )
    if identity_query["budget_valid"] is not budget_valid:
        raise OnlineCovariancePreflightError("identity query budget claim cannot be reproduced")
    response_observed = value["autopilot_version_identity"] is not None
    if identity_query["response_observed"] is not response_observed:
        raise OnlineCovariancePreflightError("identity query response claim cannot be reproduced")
    checks = expected_mode_binding["checks"]
    failed_checks = sorted(name for name, passed in checks.items() if passed is not True)
    reasons = [f"mode_binding:{name}" for name in failed_checks]
    if probe["receive_errors"]:
        reasons.append("mavlink_receive_errors")
    if not budget_valid:
        reasons.append("identity_query_budget_invalid")
    if value["reasons"] != reasons:
        raise OnlineCovariancePreflightError("online preflight reasons cannot be reproduced")
    expected_gate = (
        expected_mode_binding["gate_passed"] is True
        and not probe["receive_errors"]
        and budget_valid
    )
    if value["gate_passed"] is not expected_gate:
        raise OnlineCovariancePreflightError("online preflight gate cannot be reproduced")

    observed = _parse_utc(value["observed_at_utc"], name="observed_at_utc")
    if maximum_age_seconds is not None:
        if (
            isinstance(maximum_age_seconds, bool)
            or not math.isfinite(maximum_age_seconds)
            or maximum_age_seconds <= 0.0
        ):
            raise OnlineCovariancePreflightError("maximum_age_seconds must be finite and positive")
        current = datetime.now(UTC) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise OnlineCovariancePreflightError("now must be timezone-aware")
        age = (current.astimezone(UTC) - observed).total_seconds()
        if age < 0.0:
            raise OnlineCovariancePreflightError("online preflight is future-dated")
        if age > maximum_age_seconds:
            raise OnlineCovariancePreflightError("online preflight is stale")
    return value


def load_online_covariance_preflight_observation(
    path: str | Path,
    *,
    maximum_age_seconds: float | None = DEFAULT_MAXIMUM_AGE_SECONDS,
    now: datetime | None = None,
    require_current_next_leaf: bool = True,
) -> dict[str, object]:
    """Load and independently verify one saved online preflight observation."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise OnlineCovariancePreflightError("observation cannot be a symbolic link")
    source = candidate.resolve(strict=True)
    if not source.is_file():
        raise OnlineCovariancePreflightError("observation must be a regular file")
    return verify_online_covariance_preflight_observation(
        _json_object(source, name="online preflight observation"),
        maximum_age_seconds=maximum_age_seconds,
        now=now,
        require_current_next_leaf=require_current_next_leaf,
    )


def write_online_covariance_preflight_observation(
    path: str | Path,
    document: Mapping[str, object],
    *,
    overwrite: bool = True,
) -> Path:
    """Verify and atomically persist an operational preflight observation."""

    verified = verify_online_covariance_preflight_observation(document)
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise OnlineCovariancePreflightError("output cannot be a symbolic link")
    output = candidate.resolve()
    if output.exists() and not overwrite:
        raise OnlineCovariancePreflightError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(verified, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


__all__ = [
    "DEFAULT_MAXIMUM_AGE_SECONDS",
    "OnlineCovariancePreflightError",
    "SCHEMA",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "build_online_covariance_preflight_observation",
    "load_online_covariance_preflight_observation",
    "verify_online_covariance_preflight_observation",
    "write_online_covariance_preflight_observation",
]
