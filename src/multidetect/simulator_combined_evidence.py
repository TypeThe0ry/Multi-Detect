"""Strict verifier for the Windows PX4/Gazebo/QGC combined-SIM artifact.

The combined acceptance runner produces a large, runner-specific JSON document.
That document is intentionally kept separate from the small four-mode E7
manifest, but it still needs an independent verifier.  In particular, a
successful runner flag must not be enough to hide a reused production UDP
port, a stale cleanup claim, a forged release-mode envelope, or a hardware/
operator-visual claim in a software-only run.

The verifier is deliberately narrow and fail-closed.  It validates the
contract that is stable across the runner's detailed diagnostics:

* schema/runtime identity and the prerelease label;
* nine unique loopback host ports in the dynamic range, with 14550 excluded;
* before/after protection and release of every test port;
* router/container software-only and cleanup assertions;
* nested SIM/QGC/Mission gates which support the root gate;
* an optional hash-bound Mission transaction journal, including JSONL
  hash-chain integrity and summary consistency;
* absence of REAL hardware or human visual acceptance claims; and
* an optional E7 ``sim_release_mode_evidence_prepared`` envelope whose source
  path and SHA-256 are bound to this exact combined artifact.

No field is promoted by this module: every positive result is derived from
the source JSON and, when requested, the independently hashed E7 envelope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .autonomy.serialization import strict_json_loads
from .mission_journal import DurableMissionJournal, MissionJournalError
from .target_evidence_card import TargetEvidenceCard, TargetEvidenceCardError

COMBINED_SIM_SCHEMA = "multidetect.px4-gazebo-qgc-combined-sim-acceptance"
COMBINED_SIM_SCHEMA_VERSION = 2
VERIFIER_SCHEMA_VERSION = 1
PORT_RANGE_MIN = 30_000
PORT_RANGE_MAX = 60_000
EXPECTED_PORT_COUNT = 9
PROTECTED_GROUND_STATION_PORT = 14_550
EXPECTED_RELEASE_MODE_EVENT = "sim_release_mode_evidence_prepared"
EXPECTED_CAMERA_INFO_SEMANTIC_STATUS = "validated_k_p_rectification_plumb_bob_distortion"

PORT_FIELDS = (
    "operator_server_udp",
    "operator_qgc_udp",
    "px4_telemetry_udp",
    "px4_input_host_udp",
    "qgc_px4_udp",
    "qgc_router_udp",
    "evaluator_udp",
    "evaluator_router_udp",
    "rtsp_tcp",
)

_RELEASE_LABEL_RE = re.compile(r"v?0\.3\.0-pre\.[1-9][0-9]*")
_HARDWARE_BOOLEAN_KEYS = frozenset(
    {
        "hardware_outputs_present",
        "actuator_outputs_present",
        "physical_actuator_outputs_present",
        "real_pixhawk_contacted",
        "real_jetson_contacted",
        "real_camera_contacted",
        "hardware_control_enabled",
        "product_runtime_actuator_output_enabled",
        "product_runtime_flight_control_enabled",
        "flight_control_enabled",
    }
)
_VISUAL_CLAIM_KEYS = frozenset(
    {
        "complete_qml_rendering_semantics_proved",
        "exact_source_frame_identity_proved",
        "machine_visual_smoke",
        "operator_visual_acceptance",
        "qgc_fly_view_visual_acceptance",
        "qml_rendering_proved",
    }
)


class CombinedSimulatorEvidenceError(ValueError):
    """Raised when a combined-SIM binding cannot be safely prepared."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash one regular evidence file."""

    resolved = _resolve_file(path, name="combined SIM evidence")
    try:
        return _sha256(resolved)
    except OSError as exc:
        raise CombinedSimulatorEvidenceError(
            f"unable to read combined SIM evidence {resolved}: {exc}"
        ) from exc


def _resolve_file(value: str | Path, *, name: str) -> Path:
    source = Path(value).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CombinedSimulatorEvidenceError(f"{name} does not exist: {source}") from exc
    if not resolved.is_file():
        raise CombinedSimulatorEvidenceError(f"{name} must be a regular file: {resolved}")
    return resolved


def _load_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8-sig"), name=name)
    except (OSError, UnicodeError, ValueError) as exc:
        raise CombinedSimulatorEvidenceError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CombinedSimulatorEvidenceError(f"{name} must contain a JSON object")
    return raw


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _verify_target_evidence_cards(
    integrated: Mapping[str, object],
    *,
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    """Verify the optional target-card contract emitted by the live SIM gate.

    Older combined artifacts predate target-card materialization and remain
    readable.  Once an artifact carries the acceptance object, however, the
    card list is part of that gate and is checked independently of its summary
    counters.  This prevents a runner from reporting geolocation success while
    dropping the read-only map evidence that should accompany it.
    """

    acceptance = integrated.get("target_geolocation_acceptance")
    if not isinstance(acceptance, Mapping):
        checks["target_evidence_cards_contract"] = True
        return
    configured = acceptance.get("configured") is True
    if not configured:
        # A deliberately unconfigured gate has no observation stream to bind.
        checks["target_evidence_cards_contract"] = True
        return

    contract_ok = True
    if acceptance.get("target_evidence_cards_read_only") is not True:
        contract_ok = False
        _append_reason(
            reasons,
            "integrated_sim_qgc.target_geolocation_acceptance must mark target "
            "evidence cards read-only",
        )

    cards = acceptance.get("target_evidence_cards")
    if not isinstance(cards, list):
        contract_ok = False
        _append_reason(
            reasons,
            "integrated_sim_qgc.target_geolocation_acceptance.target_evidence_cards "
            "must be an array",
        )
        cards = []

    count = acceptance.get("target_evidence_card_count")
    if type(count) is not int or count < 0 or count != len(cards):
        contract_ok = False
        _append_reason(
            reasons,
            "target evidence card count must be a non-negative integer matching the card array",
        )

    error_count = acceptance.get("target_evidence_card_error_count")
    errors = acceptance.get("target_evidence_card_errors")
    if (
        type(error_count) is not int
        or error_count < 0
        or not isinstance(errors, list)
        or error_count != len(errors)
    ):
        contract_ok = False
        _append_reason(
            reasons,
            "target evidence card error count must match the error array",
        )

    available_count = acceptance.get("available_geolocation_count")
    if type(available_count) is int and count != available_count:
        contract_ok = False
        _append_reason(
            reasons,
            "target evidence card count must match available geolocation count",
        )

    expected_run_identity = integrated.get("run_identity_hash")
    if expected_run_identity is not None and not isinstance(expected_run_identity, str):
        contract_ok = False
        _append_reason(reasons, "integrated_sim_qgc.run_identity_hash must be text")

    for index, raw_card in enumerate(cards):
        try:
            card = TargetEvidenceCard.from_mapping(raw_card)
        except (TargetEvidenceCardError, TypeError, ValueError) as exc:
            contract_ok = False
            _append_reason(reasons, f"target evidence card {index} is invalid: {exc}")
            continue
        if expected_run_identity is not None and card.run_identity_sha256 != expected_run_identity:
            contract_ok = False
            _append_reason(
                reasons,
                f"target evidence card {index} run identity does not match "
                "integrated SIM run identity",
            )
        if card.available and (
            card.latitude_deg is None
            or card.longitude_deg is None
            or card.horizontal_sigma_m is None
        ):
            contract_ok = False
            _append_reason(reasons, f"target evidence card {index} leaks an incomplete coordinate")

    checks["target_evidence_cards_contract"] = contract_ok


def _bool_check(
    checks: dict[str, bool],
    reasons: list[str],
    name: str,
    value: object,
    *,
    expected: bool = True,
) -> None:
    passed = type(value) is bool and value is expected
    checks[name] = passed
    if not passed:
        _append_reason(reasons, f"{name} must be {expected!r}")


def _walk_for_claims(value: object, *, path: str = "$") -> tuple[list[str], list[str]]:
    """Find positive hardware or unproved visual/identity claims recursively.

    A bare ``"passed"`` is intentionally treated as a positive status while
    more specific historical machine-smoke descriptions remain compatible.
    Those descriptions prove a bounded software diagnostic, not complete QML
    rendering, exact source-frame identity, or human visual acceptance.
    """

    hardware_claims: list[str] = []
    visual_claims: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text in _HARDWARE_BOOLEAN_KEYS and child is True:
                hardware_claims.append(child_path)
            if key_text in _VISUAL_CLAIM_KEYS:
                if child is True or (isinstance(child, str) and child.strip().lower() == "passed"):
                    visual_claims.append(child_path)
            nested_hardware, nested_visual = _walk_for_claims(child, path=child_path)
            hardware_claims.extend(nested_hardware)
            visual_claims.extend(nested_visual)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            nested_hardware, nested_visual = _walk_for_claims(child, path=f"{path}[{index}]")
            hardware_claims.extend(nested_hardware)
            visual_claims.extend(nested_visual)
    return hardware_claims, visual_claims


def _extract_release_label(document: Mapping[str, Any]) -> str | None:
    direct = document.get("release_label")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    status = document.get("release_status")
    if isinstance(status, str):
        match = _RELEASE_LABEL_RE.search(status)
        if match is not None:
            return match.group(0).removeprefix("v")
    return None


def _check_release_mode_envelope(
    combined_path: Path,
    combined_sha256: str,
    envelope_path: Path | None,
    *,
    require: bool,
    reasons: list[str],
    checks: dict[str, bool],
) -> dict[str, Any] | None:
    """Validate the optional E7 envelope and bind it to ``combined_path``."""

    if envelope_path is None:
        checks["release_mode_envelope_present"] = not require
        checks["release_mode_source_path_matches"] = not require
        checks["release_mode_source_hash_matches"] = not require
        checks["release_mode_gate_passed"] = not require
        if require:
            _append_reason(reasons, "release-mode evidence envelope is required")
        return None

    try:
        envelope = _load_object(envelope_path, name="release-mode evidence envelope")
    except CombinedSimulatorEvidenceError as exc:
        for name in (
            "release_mode_envelope_present",
            "release_mode_source_path_matches",
            "release_mode_source_hash_matches",
            "release_mode_gate_passed",
        ):
            checks[name] = False
        _append_reason(reasons, str(exc))
        return {"path": str(envelope_path), "error": str(exc)}

    source = envelope.get("source")
    source_path: Path | None = None
    source_sha = source.get("sha256") if isinstance(source, Mapping) else None
    source_gate = source.get("gate_passed") if isinstance(source, Mapping) else None
    source_mode = source.get("runtime_mode") if isinstance(source, Mapping) else None
    if isinstance(source, Mapping) and isinstance(source.get("path"), str):
        candidate = Path(source["path"])
        if not candidate.is_absolute():
            candidate = envelope_path.parent / candidate
        try:
            source_path = candidate.resolve()
        except (OSError, RuntimeError):
            source_path = None

    envelope_ok = envelope.get("event") == EXPECTED_RELEASE_MODE_EVENT
    source_path_ok = source_path == combined_path
    source_hash_ok = (
        isinstance(source_sha, str) and source_sha.strip().lower() == combined_sha256.lower()
    )
    gate_ok = type(envelope.get("gate_passed")) is bool and envelope.get("gate_passed") is True
    source_gate_ok = type(source_gate) is bool and source_gate is True
    mode_ok = source_mode == "sim" and envelope.get("runtime_mode") == "sim"

    checks["release_mode_envelope_present"] = envelope_ok
    checks["release_mode_source_path_matches"] = source_path_ok
    checks["release_mode_source_hash_matches"] = source_hash_ok
    checks["release_mode_gate_passed"] = gate_ok and source_gate_ok and mode_ok
    if not envelope_ok:
        _append_reason(
            reasons, f"release-mode envelope event must be {EXPECTED_RELEASE_MODE_EVENT!r}"
        )
    if not source_path_ok:
        _append_reason(
            reasons, "release-mode envelope source path does not match combined SIM evidence"
        )
    if not source_hash_ok:
        _append_reason(
            reasons, "release-mode envelope source SHA-256 does not match combined SIM evidence"
        )
    if not checks["release_mode_gate_passed"]:
        _append_reason(
            reasons, "release-mode envelope and source gate_passed/runtime_mode must be true/sim"
        )

    return {
        "path": str(envelope_path),
        "sha256": _sha256(envelope_path),
        "event": envelope.get("event"),
        "runtime_mode": envelope.get("runtime_mode"),
        "gate_passed": envelope.get("gate_passed"),
        "source": {
            "path": str(source_path) if source_path is not None else None,
            "sha256": source_sha,
            "runtime_mode": source_mode,
            "gate_passed": source_gate,
        },
    }


def _resolve_declared_journal_path(
    value: object,
    *,
    combined_path: Path | None,
) -> Path | None:
    """Resolve a journal path without treating a missing file as a pass."""

    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if combined_path is None:
            return None
        candidate = combined_path.parent / candidate
    try:
        return candidate.resolve()
    except (OSError, RuntimeError):
        return None


def _strict_int_tuple(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        return None
    return tuple(value)


def _verify_optional_mission_journal(
    document: Mapping[str, Any],
    *,
    combined_path: Path | None,
    checks: dict[str, bool],
    reasons: list[str],
) -> None:
    """Verify the journal emitted by the combined runner when it is present.

    Historical combined-SIM artifacts predate the durable journal and remain
    verifiable.  Once a source artifact advertises a journal hash/summary,
    every binding and the complete JSONL integrity chain become mandatory.
    """

    mission = document.get("vtol_mission")
    mission_mapping = mission if isinstance(mission, Mapping) else {}
    nested_summary = mission_mapping.get("mission_journal")
    summary = document.get("vtol_mission_journal")
    if not isinstance(summary, Mapping):
        summary = nested_summary
    hashes = document.get("hashes")
    hashes_mapping = hashes if isinstance(hashes, Mapping) else {}
    hash_entry = hashes_mapping.get("mission_journal")
    artifacts = document.get("artifacts")
    artifacts_mapping = artifacts if isinstance(artifacts, Mapping) else {}
    artifact_path_value = artifacts_mapping.get("mission_journal")

    # Do not invalidate pre-journal historical evidence.  The combined runner
    # sets both fields together, which turns this into a strict opt-in contract.
    if summary is None and hash_entry is None:
        return

    present = isinstance(summary, Mapping) and isinstance(hash_entry, Mapping)
    checks["mission_journal_present"] = present
    if not present:
        _append_reason(
            reasons,
            "vtol_mission_journal and hashes.mission_journal must be present together",
        )
        return

    summary_path = _resolve_declared_journal_path(summary.get("path"), combined_path=combined_path)
    hash_path = _resolve_declared_journal_path(hash_entry.get("path"), combined_path=combined_path)
    artifact_path = _resolve_declared_journal_path(artifact_path_value, combined_path=combined_path)
    path_ok = (
        summary_path is not None
        and hash_path is not None
        and artifact_path is not None
        and summary_path == hash_path == artifact_path
    )
    checks["mission_journal_path_bound"] = path_ok
    if not path_ok:
        _append_reason(
            reasons,
            "Mission journal summary/hash/artifact paths do not resolve to one file",
        )

    file_ok = summary_path is not None and summary_path.is_file()
    actual_sha: str | None = None
    actual_bytes: int | None = None
    if file_ok:
        try:
            actual_sha = _sha256(summary_path)
            actual_bytes = summary_path.stat().st_size
        except OSError as exc:
            file_ok = False
            _append_reason(reasons, f"unable to read Mission journal: {exc}")
    else:
        _append_reason(reasons, "Mission journal file does not exist")

    declared_sha = hash_entry.get("sha256")
    hash_ok = (
        file_ok
        and isinstance(declared_sha, str)
        and actual_sha is not None
        and declared_sha.strip().lower() == actual_sha.lower()
    )
    checks["mission_journal_hash_matches"] = hash_ok
    if not hash_ok:
        _append_reason(reasons, "Mission journal SHA-256 does not match its hash binding")
    declared_bytes = hash_entry.get("bytes")
    if declared_bytes is not None:
        bytes_ok = type(declared_bytes) is int and actual_bytes == declared_bytes
        checks["mission_journal_bytes_match"] = bytes_ok
        if not bytes_ok:
            _append_reason(reasons, "Mission journal byte count does not match its hash binding")

    if not file_ok or summary_path is None:
        checks["mission_journal_integrity"] = False
        checks["mission_journal_summary_matches"] = False
        checks["mission_journal_sequences_complete"] = False
        _append_reason(
            reasons, "Mission journal integrity cannot be checked without a readable file"
        )
        return

    journal = None
    try:
        journal = DurableMissionJournal.open(summary_path)
        recovery = journal.recover()
        integrity_ok = not recovery.recovery_required
        checks["mission_journal_integrity"] = integrity_ok
        if not integrity_ok:
            _append_reason(reasons, "Mission journal requires recovery")

        requested = _strict_int_tuple(summary.get("requested_sequences"))
        summary_matches = (
            summary.get("identity_hash") == journal.identity.canonical_hash
            and summary.get("state") == journal.state.value
            and type(summary.get("recovery_required")) is bool
            and summary.get("recovery_required") is recovery.recovery_required
            and type(summary.get("record_count")) is int
            and summary.get("record_count") == len(journal.records)
        )
        checks["mission_journal_summary_matches"] = summary_matches
        if not summary_matches:
            _append_reason(
                reasons, "Mission journal summary does not match the validated JSONL records"
            )

        sequences_ok = requested == journal.requested_sequences
        protocol = mission_mapping.get("protocol")
        if isinstance(protocol, Mapping) and type(protocol.get("mission_item_count")) is int:
            item_count = protocol["mission_item_count"]
            sequences_ok = sequences_ok and requested == tuple(range(item_count))
        checks["mission_journal_sequences_complete"] = sequences_ok
        if not sequences_ok:
            _append_reason(
                reasons, "Mission journal does not contain the complete requested sequence set"
            )
    except (MissionJournalError, OSError, TypeError, ValueError) as exc:
        checks["mission_journal_integrity"] = False
        checks["mission_journal_summary_matches"] = False
        checks["mission_journal_sequences_complete"] = False
        _append_reason(reasons, f"Mission journal failed integrity validation: {exc}")
    finally:
        if journal is not None:
            journal.close()


def verify_evidence(
    document: Mapping[str, Any],
    *,
    expected_release_label: str | None = None,
    combined_path: Path | None = None,
    combined_sha256: str | None = None,
    release_mode_evidence_path: Path | None = None,
    require_release_mode: bool = False,
) -> dict[str, Any]:
    """Verify one loaded combined-SIM document and return an audit report."""

    reasons: list[str] = []
    checks: dict[str, bool] = {}

    checks["schema_identity"] = document.get("schema") == COMBINED_SIM_SCHEMA
    if not checks["schema_identity"]:
        _append_reason(reasons, f"schema must be {COMBINED_SIM_SCHEMA!r}")
    checks["schema_version"] = document.get("schema_version") == COMBINED_SIM_SCHEMA_VERSION
    if not checks["schema_version"]:
        _append_reason(reasons, f"schema_version must be {COMBINED_SIM_SCHEMA_VERSION}")
    checks["runtime_mode"] = document.get("runtime_mode") == "sim"
    if not checks["runtime_mode"]:
        _append_reason(reasons, "runtime_mode must be 'sim'")
    checks["source_gate_passed"] = document.get("gate_passed") is True
    if not checks["source_gate_passed"]:
        _append_reason(reasons, "combined SIM source gate_passed must be true")

    release_label = _extract_release_label(document)
    status = document.get("release_status")
    status_ok = isinstance(status, str) and bool(status.strip())
    if expected_release_label is not None:
        status_ok = status_ok and expected_release_label in str(status)
    checks["release_status_bound"] = status_ok
    if not status_ok:
        if expected_release_label is None:
            _append_reason(reasons, "release_status must be a non-empty string")
        else:
            _append_reason(reasons, f"release_status must contain {expected_release_label!r}")

    ports = document.get("ports")
    ports_mapping = ports if isinstance(ports, Mapping) else {}
    checks["port_allocation_mode"] = ports_mapping.get("allocation_mode") == "dynamic_loopback"
    if not checks["port_allocation_mode"]:
        _append_reason(reasons, "ports.allocation_mode must be 'dynamic_loopback'")

    dynamic_range = ports_mapping.get("dynamic_range")
    range_ok = (
        isinstance(dynamic_range, Mapping)
        and dynamic_range.get("minimum") == PORT_RANGE_MIN
        and dynamic_range.get("maximum") == PORT_RANGE_MAX
    )
    checks["port_range"] = range_ok
    if not range_ok:
        _append_reason(reasons, "ports.dynamic_range must be 30000..60000")

    allocated = ports_mapping.get("allocated_host_ports")
    allocated_ok = isinstance(allocated, list) and all(type(item) is int for item in allocated)
    count_ok = allocated_ok and len(allocated) == EXPECTED_PORT_COUNT
    unique_ok = count_ok and len(set(allocated)) == EXPECTED_PORT_COUNT
    in_range_ok = unique_ok and all(PORT_RANGE_MIN <= item <= PORT_RANGE_MAX for item in allocated)
    checks["ports_count"] = count_ok
    checks["ports_unique"] = unique_ok
    checks["ports_in_range"] = in_range_ok
    if not count_ok:
        _append_reason(
            reasons,
            f"ports.allocated_host_ports must contain exactly {EXPECTED_PORT_COUNT} integer ports",
        )
    if count_ok and not unique_ok:
        _append_reason(reasons, "ports.allocated_host_ports must be unique")
    if unique_ok and not in_range_ok:
        _append_reason(reasons, "ports.allocated_host_ports must stay in 30000..60000")

    named_values = [ports_mapping.get(field) for field in PORT_FIELDS]
    named_ok = (
        allocated_ok
        and len(named_values) == EXPECTED_PORT_COUNT
        and all(type(item) is int for item in named_values)
        and list(named_values) == allocated
    )
    checks["named_ports_match"] = named_ok
    if not named_ok:
        _append_reason(
            reasons, "named dynamic port fields must match allocated_host_ports in order"
        )

    before = ports_mapping.get("protected_udp_14550_before")
    after = ports_mapping.get("protected_udp_14550_after")
    protected_ok = (
        type(ports_mapping.get("protected_udp_14550_unchanged")) is bool
        and ports_mapping.get("protected_udp_14550_unchanged") is True
        and isinstance(before, list)
        and isinstance(after, list)
        and before == after
        and (not allocated_ok or PROTECTED_GROUND_STATION_PORT not in allocated)
    )
    checks["protected_port_unchanged"] = protected_ok
    if not protected_ok:
        _append_reason(
            reasons, "protected UDP 14550 must be unchanged and excluded from dynamic ports"
        )
    released_ok = ports_mapping.get("all_test_ports_released") is True
    checks["test_ports_released"] = released_ok
    if not released_ok:
        _append_reason(reasons, "all_test_ports_released must be true")

    router = document.get("router")
    router_mapping = router if isinstance(router, Mapping) else {}
    _bool_check(
        checks, reasons, "router_loopback", router_mapping.get("all_udp_endpoints_loopback")
    )
    _bool_check(checks, reasons, "router_ports_distinct", router_mapping.get("all_ports_distinct"))
    _bool_check(checks, reasons, "router_software_only", router_mapping.get("software_only"))
    _bool_check(
        checks,
        reasons,
        "router_no_protected_port",
        router_mapping.get("protected_udp_14550_used"),
        expected=False,
    )
    _bool_check(
        checks,
        reasons,
        "router_no_real_pixhawk",
        router_mapping.get("real_pixhawk_contacted"),
        expected=False,
    )
    _bool_check(
        checks,
        reasons,
        "router_no_physical_actuators",
        router_mapping.get("physical_actuator_outputs_present"),
        expected=False,
    )
    _bool_check(
        checks,
        reasons,
        "router_no_qgc_flight_commands",
        router_mapping.get("qgc_flight_commands_allowed"),
        expected=False,
    )

    docker = document.get("docker")
    docker_mapping = docker if isinstance(docker, Mapping) else {}
    _bool_check(
        checks, reasons, "docker_container_started", docker_mapping.get("container_started")
    )
    _bool_check(
        checks,
        reasons,
        "docker_container_cleaned",
        docker_mapping.get("container_stopped_and_removed"),
    )
    _bool_check(
        checks,
        reasons,
        "docker_exactly_one_container",
        docker_mapping.get("exactly_one_px4_gazebo_container"),
    )

    integrated = document.get("integrated_sim_qgc")
    attached = document.get("vtol_attached")
    mission = document.get("vtol_mission")
    sensor_topics = document.get("gazebo_sensor_topics")
    _bool_check(
        checks,
        reasons,
        "integrated_sim_gate",
        integrated.get("gate_passed") if isinstance(integrated, Mapping) else None,
    )
    _bool_check(
        checks,
        reasons,
        "vtol_attached_gate",
        attached.get("gate_passed") if isinstance(attached, Mapping) else None,
    )
    _bool_check(
        checks,
        reasons,
        "vtol_mission_gate",
        mission.get("gate_passed") if isinstance(mission, Mapping) else None,
    )
    _bool_check(
        checks,
        reasons,
        "required_sensor_topics_ready",
        sensor_topics.get("required_ready") if isinstance(sensor_topics, Mapping) else None,
    )
    integrated_mapping = integrated if isinstance(integrated, Mapping) else {}
    _verify_target_evidence_cards(
        integrated_mapping,
        checks=checks,
        reasons=reasons,
    )
    sensor_transport = integrated_mapping.get("sensor_transport")
    sensor_transport_mapping = sensor_transport if isinstance(sensor_transport, Mapping) else {}
    camera_info_semantics_ok = (
        sensor_transport_mapping.get("camera_info_intrinsics_semantic_validation")
        == EXPECTED_CAMERA_INFO_SEMANTIC_STATUS
    )
    checks["camera_info_intrinsics_semantics_validated"] = camera_info_semantics_ok
    if not camera_info_semantics_ok:
        _append_reason(
            reasons,
            "integrated_sim_qgc.sensor_transport CameraInfo K/P/R/D semantics "
            f"must be {EXPECTED_CAMERA_INFO_SEMANTIC_STATUS!r}",
        )
    expected_sensor_frames = sensor_transport_mapping.get("expected_frame_count")
    received_sensor_frames = sensor_transport_mapping.get("received_frame_count")
    sensor_frames_complete = (
        type(expected_sensor_frames) is int
        and expected_sensor_frames > 0
        and received_sensor_frames == expected_sensor_frames
        and sensor_transport_mapping.get("complete") is True
    )
    checks["sensor_frames_complete"] = sensor_frames_complete
    if not sensor_frames_complete:
        _append_reason(
            reasons,
            "integrated_sim_qgc.sensor_transport must contain a positive and complete "
            "expected/received frame count",
        )
    odometry_frames_complete = (
        sensor_transport_mapping.get("odometry_required") is True
        and type(sensor_transport_mapping.get("odometry_timestamp_frame_count")) is int
        and sensor_transport_mapping.get("odometry_timestamp_frame_count") == expected_sensor_frames
        and sensor_transport_mapping.get("full_odometry_pose_velocity_fusion_claimed") is True
    )
    checks["odometry_frames_complete"] = odometry_frames_complete
    if not odometry_frames_complete:
        _append_reason(
            reasons,
            "integrated_sim_qgc.sensor_transport must bind full odometry pose/velocity "
            "to every expected frame",
        )
    _verify_optional_mission_journal(
        document,
        combined_path=combined_path,
        checks=checks,
        reasons=reasons,
    )

    hardware_claims, visual_claims = _walk_for_claims(document)
    checks["no_hardware_claims"] = not hardware_claims
    checks["no_unproved_visual_identity_claims"] = not visual_claims
    # Preserve the original check name for historical verifier consumers while
    # applying the stricter recursive visual/QML/source-identity policy.
    checks["no_operator_visual_claim"] = not visual_claims
    if hardware_claims:
        _append_reason(
            reasons,
            "software-only combined SIM contains hardware claims: " + ", ".join(hardware_claims),
        )
    if visual_claims:
        _append_reason(
            reasons,
            "combined SIM contains an unproved operator/QML/source-frame visual claim: "
            + ", ".join(visual_claims),
        )

    envelope: dict[str, Any] | None = None
    if combined_path is not None and combined_sha256 is not None:
        envelope = _check_release_mode_envelope(
            combined_path,
            combined_sha256,
            release_mode_evidence_path,
            require=require_release_mode,
            reasons=reasons,
            checks=checks,
        )
    else:
        checks["release_mode_envelope_present"] = not require_release_mode
        checks["release_mode_source_path_matches"] = not require_release_mode
        checks["release_mode_source_hash_matches"] = not require_release_mode
        checks["release_mode_gate_passed"] = not require_release_mode
        if require_release_mode:
            _append_reason(
                reasons,
                "combined SIM verifier requires a hashed source path for the release-mode envelope",
            )

    return {
        "event": "combined_sim_evidence_verified",
        "schema_version": VERIFIER_SCHEMA_VERSION,
        "runtime_mode": "sim" if checks["runtime_mode"] else document.get("runtime_mode"),
        "release_label": release_label,
        "gate_passed": not reasons,
        "checks": checks,
        "reasons": reasons,
        "release_mode": envelope,
    }


def verify_file(
    path: str | Path,
    *,
    expected_release_label: str | None = None,
    release_mode_evidence_path: str | Path | None = None,
    require_release_mode: bool = False,
) -> dict[str, Any]:
    """Load and verify a combined-SIM evidence file."""

    try:
        evidence_path = _resolve_file(path, name="combined SIM evidence")
        evidence_sha = _sha256(evidence_path)
        document = _load_object(evidence_path, name="combined SIM evidence")
    except CombinedSimulatorEvidenceError as exc:
        return {
            "event": "combined_sim_evidence_verified",
            "schema_version": VERIFIER_SCHEMA_VERSION,
            "runtime_mode": None,
            "release_label": None,
            "gate_passed": False,
            "checks": {},
            "reasons": [str(exc)],
            "evidence": {"path": str(Path(path).expanduser().resolve())},
            "release_mode": None,
        }

    envelope_path: Path | None = None
    if release_mode_evidence_path is not None:
        try:
            envelope_path = _resolve_file(
                release_mode_evidence_path,
                name="release-mode evidence envelope",
            )
        except CombinedSimulatorEvidenceError as exc:
            result = verify_evidence(
                document,
                expected_release_label=expected_release_label,
                combined_path=evidence_path,
                combined_sha256=evidence_sha,
                release_mode_evidence_path=None,
                require_release_mode=True,
            )
            result["reasons"].append(str(exc))
            result["gate_passed"] = False
            result["evidence"] = {
                "path": str(evidence_path),
                "sha256": evidence_sha,
                "bytes": evidence_path.stat().st_size,
            }
            return result

    result = verify_evidence(
        document,
        expected_release_label=expected_release_label,
        combined_path=evidence_path,
        combined_sha256=evidence_sha,
        release_mode_evidence_path=envelope_path,
        require_release_mode=require_release_mode,
    )
    result["evidence"] = {
        "path": str(evidence_path),
        "sha256": evidence_sha,
        "bytes": evidence_path.stat().st_size,
    }
    return result


def build_binding(
    evidence_path: str | Path,
    *,
    release_mode_evidence_path: str | Path,
    manifest_directory: str | Path,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic E7 binding for one combined-SIM artifact."""

    evidence = _resolve_file(evidence_path, name="combined SIM evidence")
    envelope = _resolve_file(
        release_mode_evidence_path,
        name="release-mode evidence envelope",
    )
    result = verify_file(
        evidence,
        expected_release_label=expected_release_label,
        release_mode_evidence_path=envelope,
        require_release_mode=True,
    )
    if result.get("gate_passed") is not True:
        reasons = "; ".join(str(item) for item in result.get("reasons", []) if item)
        raise CombinedSimulatorEvidenceError(
            f"combined SIM evidence verifier did not pass{': ' + reasons if reasons else ''}"
        )
    output_directory = Path(manifest_directory).expanduser().resolve()
    try:
        evidence_relative = Path(os.path.relpath(evidence, output_directory)).as_posix()
        envelope_relative = Path(os.path.relpath(envelope, output_directory)).as_posix()
    except ValueError as exc:
        raise CombinedSimulatorEvidenceError(
            "combined SIM evidence and release-gate output must be on the same filesystem drive"
        ) from exc
    return {
        "evidence_path": evidence_relative,
        "evidence_sha256": _sha256(evidence),
        "release_mode_path": envelope_relative,
        "release_mode_sha256": _sha256(envelope),
        "runtime_mode": "sim",
        "release_label": result.get("release_label"),
        "gate_passed": True,
        "checks": dict(result.get("checks", {})),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expected-release-label")
    parser.add_argument("--release-mode-evidence", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        help="also write the JSON verification report to this path",
    )
    parser.add_argument(
        "--require-release-mode",
        action="store_true",
        help="require and verify the hash-bound E7 sim release-mode envelope",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify_file(
        args.evidence,
        expected_release_label=args.expected_release_label,
        release_mode_evidence_path=args.release_mode_evidence,
        require_release_mode=args.require_release_mode,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        output_path = args.out.expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result.get("gate_passed") is True else 1


__all__ = [
    "COMBINED_SIM_SCHEMA",
    "COMBINED_SIM_SCHEMA_VERSION",
    "CombinedSimulatorEvidenceError",
    "EXPECTED_PORT_COUNT",
    "PORT_FIELDS",
    "PORT_RANGE_MAX",
    "PORT_RANGE_MIN",
    "PROTECTED_GROUND_STATION_PORT",
    "build_binding",
    "build_parser",
    "main",
    "sha256_file",
    "verify_evidence",
    "verify_file",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
