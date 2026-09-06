"""Machine-readable four-mode release gate for the v0.3.0 evaluator.

The project has deliberately separate evidence runners for REPLAY, SIM, HIL
and REAL.  This module is the final *aggregation* boundary: it checks that
all four modes have independently hashed evidence, that every shared
contract hash is identical, and that the mode-specific human/hardware gates
are explicitly complete before a release can be enabled.  A release manifest
may additionally bind the stratified deployment-domain replay set; when the
product channel is final, that binding is required and is revalidated here.
It may also bind a saved REAL field-link preflight report; that file is
rehashed and passed through the strict field-link verifier on every evaluation,
and the final release channel requires the binding.
It may also bind an offline REAL target-geolocation accuracy report; the report
is rehashed and its surveyed WGS-84 samples are recomputed on every evaluation,
and the final release channel requires a passing binding.
It may also bind the deterministic SIM S4 safety artifact; the source file is
rehashed and its independent verifier is rerun on every evaluation, and the
final release channel requires this binding as well.
It may also bind the deterministic actuator safety software-bench artifact;
the source file is rehashed and its independent verifier is rerun on every
evaluation, and the final release channel requires this shared executioner
contract as well.
It may also bind the deterministic three-airframe SIM scenario matrix; the
source JSON, embedded canonical digest and scenario summaries are re-verified
on every evaluation, and the final release channel requires this binding as
well.  Final-channel evaluation additionally requires the matrix's explicit
full fault-coverage opt-in and automatic frame-budget extension, so a stale
nominal-only matrix cannot be promoted.
It may also bind the deterministic three-airframe PX4 Mission transaction
artifact; the source JSON and all three durable journals are rehashed and
reopened on every evaluation, and the final release channel requires this
binding as well.  This keeps the fixed-wing, multirotor and VTOL dispatchers on
one journalized ``MISSION_ITEM_INT``/commit/start-permit contract.
It may also bind six receive-only PX4 Mission feedback observations: one for
every HIL/REAL and fixed-wing/multirotor/VTOL combination.  Every source report
signed runtime profile and executable Mission is rehashed, re-opened and
compared with the current four-mode ``px4_mission_path_hash``.  This is a
feedback sub-gate and never claims complete hardware, HIL, REAL or
operator-visual acceptance by itself.
It may also bind the three deterministic 3-D control records; each source
record is independently re-opened, its scenario/assets and stack-plan digest
are checked, and the final release channel requires one VTOL, fixed-wing and
multirotor record.
It may also bind the three deterministic scene-stream sources; the aggregate
and each source stream are independently re-opened and revalidated on every
evaluation, and the final release channel requires a passing binding.
It may also bind the six-source external SIM runtime/live-AI join; the join,
its independent ``--join`` revalidation, all six source hashes and current
parity are re-opened on every evaluation, and the final release channel
requires a passing binding.  This remains software-only evidence and never
claims hardware or operator visual acceptance.
It may also bind the six-strata REAL field-evidence bundle; the source JSON,
its input hashes and every serialized summary are re-evaluated on each read,
and the final release channel requires a passing real-domain bundle.
It may also bind a REAL authenticated alert-link bench report; the source JSON,
transport domain and threshold summary are re-opened on every evaluation, and
the final release channel requires a passing non-loopback authenticated ACK
binding.

The evaluator never upgrades missing or ``not_claimed`` evidence.  A
prerelease may therefore have all evidence gates green while
``release_allowed`` remains false until the product version is changed to the
final ``0.3.0`` channel after review.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .actuator_safety import verify_actuator_safety_evidence
from .alert_link_bench import verify_alert_link_bench_report
from .field_evidence_bundle import verify_field_evidence_bundle_report
from .field_link_preflight import verify_field_link_report
from .field_session_capture import verify_field_session_capture
from .geolocation_accuracy import verify_geolocation_accuracy_report
from .gr01_bench import verify_gr01_link_bench_report
from .model_review import verify_model_review_bundle
from .product_version import ProductVersion, load_product_version
from .qgc_visual_acceptance import verify_record as verify_qgc_visual_acceptance
from .replay_set import REPLAY_SET_STRATA, load_replay_set_manifest
from .replay_set_calibration import verify_replay_set_calibration_report
from .replay_set_evaluation import (
    accuracy_metrics_are_defined,
    positive_strata_coverage_is_complete,
)
from .replay_set_readiness import check_replay_set_readiness
from .runtime import RuntimeMode
from .simulator.safety_evidence import verify_file as verify_simulator_safety_file
from .simulator.scenario_matrix import (
    verify_file as verify_simulator_scenario_matrix_file,
)
from .simulator_external_evidence import (
    REQUIRED_VEHICLES as EXTERNAL_REQUIRED_VEHICLES,
)
from .simulator_external_evidence import (
    revalidate_simulator_external_evidence,
)

RELEASE_GATE_SCHEMA_VERSION = 1
REQUIRED_MODES = tuple(mode.value for mode in RuntimeMode)
PARITY_FIELDS = (
    "ui_contract_hash",
    "core_algorithm_hash",
    "task_contract_hash",
    "safety_policy_hash",
    "px4_mission_path_hash",
)
_STATUS_VALUES = frozenset({"passed", "not_required", "not_claimed", "pending"})


def _canonical_evaluation_timestamp(value: str | None) -> str:
    """Return a canonical UTC timestamp for a release-gate report.

    The evaluator uses the current time by default so normal CLI invocations
    retain their existing audit semantics.  Revalidation jobs can provide an
    explicit ISO-8601 timestamp (``Z`` or an offset) to make the complete
    JSON report byte-stable and therefore reproducible from the same inputs.
    Naive timestamps are rejected because an evidence report must not depend
    on the evaluator machine's local timezone.
    """

    if value is None:
        timestamp = datetime.now(timezone.utc)
    else:
        raw = value.strip()
        if not raw:
            raise ValueError("evaluated_at_utc must be a non-empty ISO-8601 timestamp")
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            timestamp = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(
                "evaluated_at_utc must be an ISO-8601 timestamp with timezone"
            ) from exc
        if timestamp.tzinfo is None:
            raise ValueError("evaluated_at_utc must include a timezone (use 'Z' for UTC)")
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


_MODE_RECORD_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "gate_passed",
        "parity",
        "operator_visual_acceptance",
        "hardware_acceptance",
        "real_control_acceptance",
        "mission_path_acceptance",
        "artifact_event",
        "notes",
    }
)
_REPLAY_SET_KEYS = frozenset(
    {
        "manifest_path",
        "manifest_sha256",
        "canonical_sha256",
        "gate_passed",
        "accuracy_validated",
        "accuracy_evidence",
        "calibration_verification",
    }
)
_REPLAY_SET_ACCURACY_KEYS = frozenset({"path", "sha256", "gate_passed", "accuracy_validated"})
_REPLAY_SET_CALIBRATION_KEYS = frozenset(
    {
        "path",
        "sha256",
        "event",
        "schema_version",
        "report_path",
        "report_sha256",
        "manifest_sha256",
        "canonical_sha256",
        "report_promotion_ready",
        "recomputed_promotion_ready",
        "report_provenance_complete",
        "recomputed_provenance_complete",
        "gate_passed",
    }
)
_REPLAY_SET_CALIBRATION_CHECKS = frozenset(
    {
        "report_json_valid",
        "report_event_valid",
        "report_schema_version_valid",
        "manifest_available",
        "manifest_sha256_matches",
        "canonical_sha256_matches",
        "recomputed_report_matches",
    }
)
_REAL_FIELD_LINK_KEYS = frozenset(
    {
        "report_path",
        "report_sha256",
        "report_bytes",
        "max_age_s",
        "report_timestamp_epoch_s",
        "hardware_link_ready",
        "route_checks_requested",
        "route_checks_passed",
        "route_source_cidr",
        "address_state_bound",
        "gate_passed",
        "reasons",
    }
)
_REAL_ALERT_LINK_KEYS = frozenset(
    {
        "report_path",
        "report_sha256",
        "report_bytes",
        "event",
        "schema_version",
        "hardware_observed",
        "simulation_only",
        "remote_host",
        "remote_port",
        "remote_is_loopback",
        "expected_source_cidr",
        "local_source_address",
        "source_route_verified",
        "transport_source_address",
        "transport_source_address_bound",
        "observed_transport_source_addresses",
        "transport_source_observation_count",
        "transport_source_observations_complete",
        "topology",
        "topology_sha256",
        "topology_binding_verified",
        "hardware_id",
        "requested_round_trips",
        "round_trip_samples",
        "failed_round_trips",
        "transmission_attempts",
        "missing_ack_attempts",
        "packet_loss_rate",
        "ack_latency_p50_ms",
        "ack_latency_p95_ms",
        "correlated_ack_verified",
        "authenticated_ack_verified",
        "checks",
        "thresholds",
        "gate_passed",
        "reasons",
    }
)
_REAL_ALERT_LINK_BOUND_FIELDS = (
    "event",
    "schema_version",
    "hardware_observed",
    "simulation_only",
    "remote_host",
    "remote_port",
    "remote_is_loopback",
    "hardware_id",
    "requested_round_trips",
    "round_trip_samples",
    "failed_round_trips",
    "transmission_attempts",
    "missing_ack_attempts",
    "packet_loss_rate",
    "ack_latency_p50_ms",
    "ack_latency_p95_ms",
    "correlated_ack_verified",
    "authenticated_ack_verified",
    "checks",
    "thresholds",
    "reasons",
)
_REAL_ALERT_LINK_V2_BOUND_FIELDS = (
    "expected_source_cidr",
    "local_source_address",
    "source_route_verified",
)
_REAL_ALERT_LINK_V3_BOUND_FIELDS = (
    "transport_source_address",
    "transport_source_address_bound",
)
_REAL_ALERT_LINK_V4_BOUND_FIELDS = (
    "observed_transport_source_addresses",
    "transport_source_observation_count",
    "transport_source_observations_complete",
)
_REAL_ALERT_LINK_V5_BOUND_FIELDS = (
    "topology",
    "topology_sha256",
    "topology_binding_verified",
)
_REAL_GR01_OPERATOR_LINK_BOUND_FIELDS = (
    "event",
    "schema_version",
    "hardware_observed",
    "simulation_only",
    "passed",
    "reasons",
    "hardware_model",
    "hardware_id",
    "remote_host",
    "remote_port",
    "remote_is_loopback",
    "expected_source_cidr",
    "local_source_address",
    "source_route_verified",
    "requested_round_trips",
    "round_trip_samples",
    "rejected_round_trips",
    "timed_out_round_trips",
    "transmission_attempts",
    "missing_ack_attempts",
    "packet_loss_rate",
    "ack_latency_p50_ms",
    "ack_latency_p95_ms",
    "bidirectional_ip_verified",
    "signed_operator_round_trip",
    "application_hmac_verified",
    "mavlink2_signature_verified",
    "checks",
    "thresholds",
    "payload_release_requested",
    "flight_control_enabled",
    "physical_release_enabled",
    "hardware_control_enabled",
)
_REAL_GR01_OPERATOR_LINK_KEYS = frozenset(
    {
        "report_path",
        "report_sha256",
        "report_bytes",
        "observed_at_utc",
        "report_timestamp_epoch_s",
        "max_age_s",
        *_REAL_GR01_OPERATOR_LINK_BOUND_FIELDS,
        "gate_passed",
    }
)
_REAL_TARGET_ACCURACY_KEYS = frozenset(
    {
        "report_path",
        "report_sha256",
        "report_bytes",
        "evidence_type",
        "schema_version",
        "ground_truth_isolated",
        "advisory_only",
        "contains_flight_control",
        "gate_passed",
        "sample_count",
        "p95_error_m",
        "one_sigma_coverage",
        "two_sigma_coverage",
        "reasons",
    }
)
_REAL_TARGET_STATE_EVIDENCE_KEYS = frozenset(
    {
        "report_path",
        "report_sha256",
        "report_bytes",
        "schema_version",
        "require_target_state_evidence",
        "gate_passed",
        "captured_records",
        "state_path",
        "gps_qualified_records",
        "range_qualified_records",
        "reasons",
    }
)
_FIELD_EVIDENCE_BUNDLE_KEYS = frozenset(
    {
        "report_path",
        "report_sha256",
        "report_bytes",
        "evidence_type",
        "schema_version",
        "release_label",
        "bundle_id",
        "gate_passed",
        "release_eligible",
        "hardware_acceptance",
        "real_flight_acceptance",
        "software_only",
        "read_only_evidence",
        "advisory_only",
        "ground_truth_isolated",
        "contains_flight_control",
        "contains_truth",
        "all_inputs_present",
        "all_strata_passed",
        "cross_bindings_passed",
        "hashes_complete",
        "real_domain_required",
        "strata",
    }
)
_FIELD_EVIDENCE_BUNDLE_STRATA = (
    "field_link",
    "gnss",
    "execution",
    "target_event",
    "media_timeline",
    "accuracy",
)
_SIMULATOR_SAFETY_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "event",
        "runtime_mode",
        "release_label",
        "gate_passed",
        "verifier_event",
        "verifier_exit_code",
        "checks",
    }
)
_ACTUATOR_SAFETY_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "event",
        "runtime_mode",
        "release_label",
        "gate_passed",
        "software_only",
        "physical_outputs_present",
        "propulsion_disconnected",
        "verifier_event",
        "verifier_exit_code",
        "checks",
    }
)
_SIMULATOR_SCENARIO_MATRIX_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "evidence_bytes",
        "canonical_sha256",
        "schema_version",
        "event",
        "product",
        "release_label",
        "gate_passed",
        "deterministic_gate_passed",
        "scenario_count",
        "scenario_ids",
        "modes",
        "required_modes",
        "enable_ai",
        "arm_on_start",
        "repeatability_checked",
        "truth_loaded",
        "truth_free",
        "claims",
        "verifier_event",
        "verifier_exit_code",
        "checks",
    }
)
_SIMULATOR_COMBINED_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "release_mode_path",
        "release_mode_sha256",
        "runtime_mode",
        "release_label",
        "gate_passed",
        "checks",
    }
)
_SIMULATOR_EXTERNAL_EVIDENCE_KEYS = frozenset(
    {
        "join_path",
        "join_sha256",
        "join_bytes",
        "revalidation_path",
        "revalidation_sha256",
        "revalidation_bytes",
        "release_label",
        "gate_passed",
        "source_recomputed",
        "parity",
        "source_hashes",
        "checks",
    }
)
_SIMULATOR_EXTERNAL_PARITY_KEYS = frozenset({"path", "sha256", "bytes", "release_label", "hashes"})
_SIMULATOR_EXTERNAL_SOURCE_KEYS = frozenset({"sha256", "bytes"})
_SIMULATOR_EXTERNAL_CHECKS = frozenset(
    {
        "join_schema_valid",
        "join_event_valid",
        "join_release_label_matches",
        "join_gate_passed",
        "join_shared_boundaries_passed",
        "join_claims_passed",
        "join_parity_matches",
        "six_sources_distinct",
        "six_sources_hash_bound",
        "revalidation_schema_valid",
        "revalidation_join_matches",
        "revalidation_source_recomputed",
        "revalidation_gate_passed",
    }
)
_PX4_AIRFRAME_TRANSACTION_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "evidence_bytes",
        "schema",
        "schema_version",
        "runtime_mode",
        "release_label",
        "gate_passed",
        "shared_dispatcher",
        "shared_transport",
        "truth_free",
        "physical_output_enabled",
        "vehicle_classes",
        "checks",
    }
)
_PX4_REPORTED_POSITION_RESPONSE_MATRIX_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "evidence_bytes",
        "schema",
        "schema_version",
        "release_label",
        "gate_passed",
        "matrix_sha256",
        "vehicle_classes",
        "truth_free",
        "hardware_acceptance",
        "hil_acceptance",
        "real_acceptance",
        "checks",
    }
)
_PX4_REPORTED_POSITION_RESPONSE_CAMPAIGN_KEYS = frozenset(
    {
        "evidence_path",
        "evidence_sha256",
        "evidence_bytes",
        "schema",
        "schema_version",
        "release_label",
        "gate_passed",
        "campaign_sha256",
        "positive_run_count",
        "fault_run_count",
        "vehicle_classes",
        "truth_free",
        "hardware_acceptance",
        "hil_acceptance",
        "real_acceptance",
        "checks",
    }
)
_SIMULATOR_3D_CONTROL_KEYS = frozenset(
    {
        "schema_version",
        "evidence",
        "scenario_ids",
        "modes",
        "gate_passed",
        "deterministic_gate_passed",
        "external_runtime_gate_passed",
        "external_runtime_acceptance",
        "visual_acceptance",
        "hardware_acceptance",
        "checks",
    }
)
_SIMULATOR_3D_CONTROL_EVIDENCE_KEYS = frozenset(
    {
        "path",
        "sha256",
        "bytes",
        "scenario_id",
        "mode",
        "stack_plan_sha256",
        "gate_passed",
        "deterministic_gate_passed",
        "external_runtime_gate_passed",
    }
)
_SIMULATOR_SCENE_STREAM_SET_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "release_label",
        "runtime_mode",
        "evidence",
        "scenario_ids",
        "modes",
        "gate_passed",
        "deterministic",
        "truth_free",
        "external_runtime_acceptance",
        "visual_acceptance",
        "hardware_acceptance",
        "checks",
    }
)
_MODEL_REVIEW_KEYS = frozenset(
    {
        "path",
        "sha256",
        "model_id",
        "model_version",
        "model_artifact_sha256",
        "review_status",
        "production_approval_eligible",
        "production_approved",
        "flight_control_enabled",
        "physical_release_enabled",
        "gate_passed",
        "reasons",
    }
)
_QGC_OPERATOR_VISUAL_ACCEPTANCE_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "records",
        "covered_modes",
        "required_modes",
        "complete",
        "gate_passed",
    }
)
_QGC_OPERATOR_VISUAL_ACCEPTANCE_ENTRY_KEYS = frozenset(
    {
        "runtime_mode",
        "path",
        "sha256",
        "bytes",
        "review_sha256",
        "reviewer",
        "reviewed_at_utc",
        "machine_run_id",
        "machine_report_sha256",
        "screenshot_sha256",
        "qgc_executable_sha256",
        "ui_contract_hash",
        "gate_passed",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "product",
        "release_label",
        "modes",
        "replay_set",
        "real_field_link",
        "real_alert_link",
        "real_gr01_operator_link",
        "real_target_accuracy",
        "real_target_state_evidence",
        "field_evidence_bundle",
        "simulator_safety",
        "actuator_safety",
        "simulator_scenario_matrix",
        "simulator_combined",
        "simulator_external_evidence",
        "px4_airframe_transaction",
        "px4_reported_position_response_matrix",
        "px4_reported_position_response_campaign",
        "qgc_operator_visual_acceptance",
        "px4_mission_feedback",
        "pixhawk_covariance",
        "simulator_3d_control",
        "simulator_scene_stream_set",
        "model_review",
    }
)
_REQUIRED_TOP_LEVEL_KEYS = frozenset({"schema_version", "product", "release_label", "modes"})


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        # PowerShell may emit a UTF-8 BOM for evidence written on Windows;
        # accepting it keeps the evaluator aligned with the manifest writer.
        raw = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot load JSON object {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return raw


def _reject_json_constant(value: str) -> None:
    """Reject JavaScript-only non-finite numbers in hash-bound evidence."""

    raise ValueError(f"non-finite JSON number {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject last-key-wins ambiguity in release-gate JSON objects."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _product_version_mapping(version: ProductVersion) -> dict[str, object]:
    return {
        "schema_version": version.schema_version,
        "product": version.product,
        "release_label": version.release_label,
        "numeric": version.numeric,
        "python": version.python,
        "channel": version.channel,
    }


def _artifact_runtime_mode(artifact: Mapping[str, Any]) -> str | None:
    for key in ("runtime_mode", "mode"):
        value = artifact.get(key)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized:
                return normalized
    runtime = artifact.get("runtime")
    if isinstance(runtime, Mapping):
        value = runtime.get("mode")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized:
                return normalized
    return None


def _status_reason(status: object, *, field: str, required: bool = True) -> str | None:
    if not isinstance(status, str) or status not in _STATUS_VALUES:
        return f"{field} must be one of {sorted(_STATUS_VALUES)!r}"
    if required and status != "passed":
        return f"{field} is {status!r}, required value is 'passed'"
    return None


def _record_result(
    mode: str,
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    reasons: list[str] = []
    result: dict[str, Any] = {
        "mode": mode,
        "passed": False,
        "reasons": reasons,
        "evidence": None,
        "parity": None,
    }
    if not isinstance(raw_record, Mapping):
        reasons.append("mode evidence record is missing or not an object")
        return result

    unknown = sorted(set(raw_record).difference(_MODE_RECORD_KEYS))
    if unknown:
        reasons.append(f"unknown mode evidence field(s): {unknown!r}")

    evidence_value = raw_record.get("evidence_path")
    evidence_path: Path | None = None
    if not isinstance(evidence_value, str) or not evidence_value.strip():
        reasons.append("evidence_path is missing or empty")
    else:
        evidence_path = Path(evidence_value)
        if not evidence_path.is_absolute():
            evidence_path = manifest_directory / evidence_path
        evidence_path = evidence_path.resolve()

    expected_sha = raw_record.get("evidence_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("evidence_sha256 is missing or invalid")

    gate_passed = raw_record.get("gate_passed")
    if type(gate_passed) is not bool:
        reasons.append("gate_passed must be a boolean")
    elif not gate_passed:
        reasons.append("mode gate_passed is false")

    parity = raw_record.get("parity")
    normalized_parity: dict[str, str] | None = None
    if not isinstance(parity, Mapping):
        reasons.append("parity is missing or not an object")
    else:
        unknown_parity = sorted(set(parity).difference(PARITY_FIELDS))
        if unknown_parity:
            reasons.append(f"unknown parity field(s): {unknown_parity!r}")
        normalized_parity = {}
        for field in PARITY_FIELDS:
            value = parity.get(field)
            if not _is_sha256(value):
                reasons.append(f"parity.{field} is missing or invalid")
            else:
                normalized_parity[field] = value.lower()

    # All modes must show the same task and safety path.  Hardware and real
    # control are only meaningful for HIL/REAL; replay/SIM explicitly declare
    # them not required instead of silently inheriting a claim.
    for field in ("operator_visual_acceptance", "mission_path_acceptance"):
        reason = _status_reason(raw_record.get(field), field=field)
        if reason is not None:
            reasons.append(reason)
    for field in ("hardware_acceptance", "real_control_acceptance"):
        status = raw_record.get(field)
        reason = _status_reason(status, field=field, required=False)
        if reason is not None:
            reasons.append(reason)
        elif mode in {"hil", "real"} and status != "passed":
            reasons.append(f"{field} is {status!r}, required for {mode.upper()}")
        elif mode in {"replay", "sim"} and status not in {"not_required", "passed"}:
            reasons.append(f"{field} must be 'not_required' for {mode.upper()}")

    if evidence_path is not None:
        evidence_info: dict[str, Any] = {"path": str(evidence_path)}
        if not evidence_path.is_file():
            reasons.append("evidence file does not exist")
        else:
            actual_sha = _sha256(evidence_path)
            evidence_info["sha256"] = actual_sha
            if isinstance(expected_sha, str) and _is_sha256(expected_sha):
                if actual_sha.lower() != expected_sha.lower():
                    reasons.append("evidence SHA-256 does not match")
            try:
                artifact = _load_object(evidence_path)
            except ValueError as exc:
                reasons.append(str(exc))
            else:
                artifact_event_value = artifact.get("event")
                artifact_event = (
                    artifact_event_value.strip()
                    if isinstance(artifact_event_value, str)
                    else artifact_event_value
                )
                evidence_info["event"] = artifact_event
                evidence_info["gate_passed"] = artifact.get("gate_passed")
                artifact_gate = artifact.get("gate_passed")
                if artifact_gate is not True:
                    reasons.append("referenced artifact gate_passed is not true")
                artifact_mode = _artifact_runtime_mode(artifact)
                if artifact_mode is not None:
                    evidence_info["runtime_mode"] = artifact_mode
                    if artifact_mode != mode:
                        reasons.append(
                            "referenced artifact runtime mode is "
                            f"{artifact_mode!r}, expected {mode!r}"
                        )
                else:
                    reasons.append("referenced artifact runtime mode is missing")
                expected_event = raw_record.get("artifact_event")
                if expected_event is not None:
                    if not isinstance(expected_event, str) or not expected_event.strip():
                        reasons.append("artifact_event must be a non-empty string")
                    elif artifact_event != expected_event.strip():
                        reasons.append("referenced artifact event does not match artifact_event")
        result["evidence"] = evidence_info

    result["parity"] = normalized_parity
    result["gate_passed"] = gate_passed
    result["statuses"] = {
        field: raw_record.get(field)
        for field in (
            "operator_visual_acceptance",
            "hardware_acceptance",
            "real_control_acceptance",
            "mission_path_acceptance",
        )
    }
    result["passed"] = not reasons
    return result


def _replay_set_calibration_verification_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    replay_manifest_path: Path | None,
    expected_manifest_sha: object,
    expected_canonical_sha: object,
) -> dict[str, Any]:
    """Re-validate the optional independent calibration-verification binding.

    Calibration does not replace replay-set accuracy evidence. It remains
    advisory on prereleases, while the final release channel requires complete
    prediction-run and accuracy-report provenance. When a caller binds the
    independent verifier artifact, every read
    must prove that the artifact itself, its calibration report and the replay
    manifest all still refer to the same bytes.  A pending calibration result
    (``report_promotion_ready=false``) remains valid here: this binding proves
    integrity of the advisory calculation, not model-promotion approval.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "promotion_ready": False,
        "provenance_complete": False,
        "reasons": [],
        "artifact": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        # Optional on the prerelease line; old manifests remain readable.
        result["passed"] = True
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("replay_set.calibration_verification must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_REPLAY_SET_CALIBRATION_KEYS))
    if unknown:
        reasons.append(f"unknown replay_set calibration_verification field(s): {unknown!r}")

    artifact_path: Path | None = None
    path_value = raw_record.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("replay_set.calibration_verification.path is missing or empty")
    else:
        artifact_path = Path(path_value)
        if not artifact_path.is_absolute():
            artifact_path = manifest_directory / artifact_path
        try:
            artifact_path = artifact_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"replay_set calibration verification path cannot be resolved: {exc}")
            artifact_path = None

    expected_artifact_sha = raw_record.get("sha256")
    if not _is_sha256(expected_artifact_sha):
        reasons.append("replay_set.calibration_verification.sha256 is missing or invalid")
    expected_event = raw_record.get("event")
    if expected_event != "replay_set_threshold_calibration_verified":
        reasons.append("replay_set.calibration_verification.event does not match verifier contract")
    expected_schema_version = raw_record.get("schema_version")
    if expected_schema_version != 1:
        reasons.append("replay_set.calibration_verification.schema_version must be 1")

    report_path: Path | None = None
    report_path_value = raw_record.get("report_path")
    if not isinstance(report_path_value, str) or not report_path_value.strip():
        reasons.append("replay_set.calibration_verification.report_path is missing or empty")
    else:
        report_path = Path(report_path_value)
        if not report_path.is_absolute():
            report_path = manifest_directory / report_path
        try:
            report_path = report_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"replay_set calibration report path cannot be resolved: {exc}")
            report_path = None

    expected_report_sha = raw_record.get("report_sha256")
    if not _is_sha256(expected_report_sha):
        reasons.append("replay_set.calibration_verification.report_sha256 is missing or invalid")
    expected_manifest_record_sha = raw_record.get("manifest_sha256")
    if not _is_sha256(expected_manifest_record_sha):
        reasons.append("replay_set.calibration_verification.manifest_sha256 is missing or invalid")
    expected_canonical_record_sha = raw_record.get("canonical_sha256")
    if not _is_sha256(expected_canonical_record_sha):
        reasons.append("replay_set.calibration_verification.canonical_sha256 is missing or invalid")
    if (
        _is_sha256(expected_manifest_sha)
        and _is_sha256(expected_manifest_record_sha)
        and expected_manifest_sha.lower() != expected_manifest_record_sha.lower()
    ):
        reasons.append(
            "replay_set calibration verification manifest SHA-256 does not match "
            "the replay_set binding"
        )
    if (
        _is_sha256(expected_canonical_sha)
        and _is_sha256(expected_canonical_record_sha)
        and expected_canonical_sha.lower() != expected_canonical_record_sha.lower()
    ):
        reasons.append(
            "replay_set calibration verification canonical SHA-256 does not match "
            "the replay_set binding"
        )

    for field in ("report_promotion_ready", "recomputed_promotion_ready", "gate_passed"):
        if type(raw_record.get(field)) is not bool:
            reasons.append(f"replay_set.calibration_verification.{field} must be a boolean")
    for field in ("report_provenance_complete", "recomputed_provenance_complete"):
        value = raw_record.get(field, False)
        if type(value) is not bool:
            reasons.append(f"replay_set.calibration_verification.{field} must be a boolean")
    expected_gate = raw_record.get("gate_passed")
    if expected_gate is False:
        reasons.append("replay_set calibration verification gate_passed is false")

    artifact_document: Mapping[str, Any] | None = None
    artifact_info: dict[str, Any] | None = None
    if artifact_path is not None:
        artifact_info = {"path": str(artifact_path)}
        if not artifact_path.is_file():
            reasons.append("replay_set calibration verification file does not exist")
        else:
            try:
                actual_artifact_sha = _sha256(artifact_path)
            except OSError as exc:
                reasons.append(f"replay_set calibration verification file cannot be hashed: {exc}")
            else:
                artifact_info["sha256"] = actual_artifact_sha
                if (
                    _is_sha256(expected_artifact_sha)
                    and actual_artifact_sha.lower() != expected_artifact_sha.lower()
                ):
                    reasons.append("replay_set calibration verification SHA-256 does not match")
            try:
                artifact_document = _load_object(artifact_path)
            except ValueError as exc:
                reasons.append(f"replay_set calibration verification JSON is invalid: {exc}")

    if artifact_document is not None:
        for field, expected in (
            ("event", expected_event),
            ("schema_version", expected_schema_version),
            ("report_sha256", expected_report_sha),
            ("report_promotion_ready", raw_record.get("report_promotion_ready")),
            (
                "recomputed_promotion_ready",
                raw_record.get("recomputed_promotion_ready"),
            ),
            (
                "report_provenance_complete",
                raw_record.get("report_provenance_complete", False),
            ),
            (
                "recomputed_provenance_complete",
                raw_record.get("recomputed_provenance_complete", False),
            ),
            ("gate_passed", expected_gate),
        ):
            observed = (
                artifact_document.get(field, False)
                if field.endswith("provenance_complete")
                else artifact_document.get(field)
            )
            if observed != expected:
                reasons.append(
                    f"replay_set calibration verification {field} does not match the bound record"
                )

        artifact_report_path_value = artifact_document.get("report_path")
        if not isinstance(artifact_report_path_value, str) or not artifact_report_path_value:
            reasons.append("replay_set calibration verification artifact report_path is missing")
        elif artifact_path is not None:
            artifact_report_path = Path(artifact_report_path_value)
            if not artifact_report_path.is_absolute():
                artifact_report_path = artifact_path.parent / artifact_report_path
            try:
                artifact_report_path = artifact_report_path.resolve()
            except (OSError, RuntimeError) as exc:
                reasons.append(
                    "replay_set calibration verification artifact report path "
                    f"cannot be resolved: {exc}"
                )
            else:
                if report_path is not None and artifact_report_path != report_path:
                    reasons.append(
                        "replay_set calibration verification report_path does not "
                        "match the bound record"
                    )

        checks_value = artifact_document.get("checks")
        if not isinstance(checks_value, Mapping):
            reasons.append("replay_set calibration verification artifact checks must be an object")
        else:
            unknown_checks = sorted(set(checks_value).difference(_REPLAY_SET_CALIBRATION_CHECKS))
            missing_checks = sorted(set(_REPLAY_SET_CALIBRATION_CHECKS).difference(checks_value))
            if unknown_checks:
                reasons.append(
                    "replay_set calibration verification artifact has unknown checks: "
                    f"{unknown_checks!r}"
                )
            if missing_checks:
                reasons.append(
                    "replay_set calibration verification artifact is missing checks: "
                    f"{missing_checks!r}"
                )
            if any(value is not True for value in checks_value.values()):
                reasons.append(
                    "replay_set calibration verification artifact contains a failed check"
                )

    report_document: Mapping[str, Any] | None = None
    actual_report_sha: str | None = None
    actual_manifest_sha: str | None = None
    actual_canonical_sha: str | None = None
    if report_path is not None:
        if not report_path.is_file():
            reasons.append("replay_set calibration report file does not exist")
        else:
            try:
                actual_report_sha = _sha256(report_path)
            except OSError as exc:
                reasons.append(f"replay_set calibration report cannot be hashed: {exc}")
            else:
                if (
                    _is_sha256(expected_report_sha)
                    and actual_report_sha.lower() != expected_report_sha.lower()
                ):
                    reasons.append("replay_set calibration report SHA-256 does not match")
            try:
                report_document = _load_object(report_path)
            except ValueError as exc:
                reasons.append(f"replay_set calibration report JSON is invalid: {exc}")
            if isinstance(report_document, Mapping):
                raw_report_manifest_sha = report_document.get("manifest_sha256")
                raw_report_canonical_sha = report_document.get("canonical_sha256")
                if _is_sha256(raw_report_manifest_sha):
                    actual_manifest_sha = raw_report_manifest_sha
                if _is_sha256(raw_report_canonical_sha):
                    actual_canonical_sha = raw_report_canonical_sha
                if (
                    not _is_sha256(raw_report_manifest_sha)
                    or not _is_sha256(expected_manifest_sha)
                    or raw_report_manifest_sha.lower() != expected_manifest_sha.lower()
                ):
                    reasons.append(
                        "replay_set calibration report manifest SHA-256 does not match "
                        "the replay_set binding"
                    )
                if (
                    not _is_sha256(raw_report_canonical_sha)
                    or not _is_sha256(expected_canonical_sha)
                    or raw_report_canonical_sha.lower() != expected_canonical_sha.lower()
                ):
                    reasons.append(
                        "replay_set calibration report canonical SHA-256 does not match "
                        "the replay_set binding"
                    )

    if replay_manifest_path is None or not replay_manifest_path.is_file():
        reasons.append(
            "replay_set calibration verification cannot bind without a replay_set manifest"
        )
    elif report_path is not None:
        try:
            verification = verify_replay_set_calibration_report(
                report_path,
                manifest_path=replay_manifest_path,
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            reasons.append(f"replay_set calibration verifier failed: {exc}")
        else:
            result["verification"] = verification
            if verification.get("gate_passed") is not True:
                reasons.append("replay_set calibration verifier gate_passed is not true")
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str):
                        reasons.append(f"replay_set calibration verifier: {reason}")
            if (
                _is_sha256(expected_report_sha)
                and verification.get("report_sha256") != expected_report_sha
            ):
                reasons.append("replay_set calibration verifier report SHA-256 does not match")
            for field in (
                "report_promotion_ready",
                "recomputed_promotion_ready",
                "report_provenance_complete",
                "recomputed_provenance_complete",
            ):
                bound_value = raw_record.get(field, False)
                if verification.get(field) != bound_value:
                    reasons.append(
                        f"replay_set calibration verifier {field} does not match the bound record"
                    )
            result["provenance_complete"] = bool(
                verification.get("report_provenance_complete") is True
                and verification.get("recomputed_provenance_complete") is True
            )
            result["promotion_ready"] = bool(
                verification.get("report_promotion_ready") is True
                and verification.get("recomputed_promotion_ready") is True
            )

    if artifact_info is not None:
        artifact_info.update(
            {
                "event": expected_event,
                "schema_version": expected_schema_version,
                "report_path": str(report_path) if report_path is not None else None,
                "report_sha256": actual_report_sha or expected_report_sha,
                "manifest_sha256": actual_manifest_sha or expected_manifest_record_sha,
                "canonical_sha256": actual_canonical_sha or expected_canonical_record_sha,
                "report_promotion_ready": raw_record.get("report_promotion_ready"),
                "recomputed_promotion_ready": raw_record.get("recomputed_promotion_ready"),
                "report_provenance_complete": raw_record.get("report_provenance_complete", False),
                "recomputed_provenance_complete": raw_record.get(
                    "recomputed_provenance_complete", False
                ),
                "gate_passed": raw_record.get("gate_passed"),
            }
        )
    result["artifact"] = artifact_info
    result["passed"] = not reasons
    return result


def _replay_set_result(
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Validate an optional deployment-domain replay-set binding.

    Four-mode manifests created before the stratified replay-set contract are
    still readable during the prerelease line.  When a record is present,
    however, it is checked all the way back to the referenced manifest and its
    raw files; a pending or tampered replay set therefore cannot be promoted
    into a passing release gate.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "manifest": None,
        "readiness": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("replay_set record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("replay_set must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_REPLAY_SET_KEYS))
    if unknown:
        reasons.append(f"unknown replay_set field(s): {unknown!r}")

    path_value = raw_record.get("manifest_path")
    manifest_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("replay_set.manifest_path is missing or empty")
    else:
        manifest_path = Path(path_value)
        if not manifest_path.is_absolute():
            manifest_path = manifest_directory / manifest_path
        try:
            manifest_path = manifest_path.resolve()
        except OSError as exc:
            reasons.append(f"replay_set manifest path cannot be resolved: {exc}")
            manifest_path = None

    expected_sha = raw_record.get("manifest_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("replay_set.manifest_sha256 is missing or invalid")
    expected_canonical = raw_record.get("canonical_sha256")
    if not _is_sha256(expected_canonical):
        reasons.append("replay_set.canonical_sha256 is missing or invalid")

    gate_value = raw_record.get("gate_passed")
    if type(gate_value) is not bool:
        reasons.append("replay_set.gate_passed must be a boolean")
    elif not gate_value:
        reasons.append("replay_set gate_passed is false")

    accuracy_value = raw_record.get("accuracy_validated")
    if type(accuracy_value) is not bool:
        reasons.append("replay_set.accuracy_validated must be a boolean")
    elif accuracy_value:
        reasons.append("replay_set.accuracy_validated must remain false until IoU review")

    accuracy_evidence_value = raw_record.get("accuracy_evidence")
    accuracy_path: Path | None = None
    accuracy_evidence_info: dict[str, Any] | None = None
    if accuracy_evidence_value is not None:
        if not isinstance(accuracy_evidence_value, Mapping):
            reasons.append("replay_set.accuracy_evidence must be an object")
        else:
            unknown_accuracy = sorted(
                set(accuracy_evidence_value).difference(_REPLAY_SET_ACCURACY_KEYS)
            )
            if unknown_accuracy:
                reasons.append(
                    f"unknown replay_set accuracy_evidence field(s): {unknown_accuracy!r}"
                )
            accuracy_path_value = accuracy_evidence_value.get("path")
            if not isinstance(accuracy_path_value, str) or not accuracy_path_value.strip():
                reasons.append("replay_set.accuracy_evidence.path is missing or empty")
            else:
                accuracy_path = Path(accuracy_path_value)
                if not accuracy_path.is_absolute():
                    accuracy_path = manifest_directory / accuracy_path
                try:
                    accuracy_path = accuracy_path.resolve()
                except OSError as exc:
                    reasons.append(f"replay_set accuracy evidence path cannot be resolved: {exc}")
                    accuracy_path = None
            expected_accuracy_sha = accuracy_evidence_value.get("sha256")
            if not _is_sha256(expected_accuracy_sha):
                reasons.append("replay_set.accuracy_evidence.sha256 is missing or invalid")
            accuracy_gate = accuracy_evidence_value.get("gate_passed")
            accuracy_claim = accuracy_evidence_value.get("accuracy_validated")
            if type(accuracy_gate) is not bool:
                reasons.append("replay_set.accuracy_evidence.gate_passed must be a boolean")
            elif not accuracy_gate:
                reasons.append("replay_set accuracy evidence gate_passed is false")
            if type(accuracy_claim) is not bool:
                reasons.append("replay_set.accuracy_evidence.accuracy_validated must be a boolean")
            elif not accuracy_claim:
                reasons.append("replay_set accuracy evidence accuracy_validated is false")
            if accuracy_path is not None:
                accuracy_evidence_info = {"path": str(accuracy_path)}
                if not accuracy_path.is_file():
                    reasons.append("replay_set accuracy evidence file does not exist")
                else:
                    actual_accuracy_sha = _sha256(accuracy_path)
                    accuracy_evidence_info["sha256"] = actual_accuracy_sha
                    if (
                        isinstance(expected_accuracy_sha, str)
                        and _is_sha256(expected_accuracy_sha)
                        and actual_accuracy_sha.lower() != expected_accuracy_sha.lower()
                    ):
                        reasons.append("replay_set accuracy evidence SHA-256 does not match")
                    try:
                        accuracy_document = _load_object(accuracy_path)
                    except ValueError as exc:
                        reasons.append(str(exc))
                    else:
                        if accuracy_document.get("event") != (
                            "replay_set_accuracy_evaluation_completed"
                        ):
                            reasons.append("replay_set accuracy evidence has the wrong event type")
                        if accuracy_document.get("schema_version") != 1:
                            reasons.append("replay_set accuracy evidence schema_version must be 1")
                        accuracy_evidence_info["gate_passed"] = accuracy_document.get("gate_passed")
                        accuracy_evidence_info["accuracy_validated"] = accuracy_document.get(
                            "accuracy_validated"
                        )
                        accuracy_manifest_sha = accuracy_document.get("manifest_sha256")
                        if (
                            not isinstance(accuracy_manifest_sha, str)
                            or not isinstance(expected_sha, str)
                            or accuracy_manifest_sha.lower() != expected_sha.lower()
                        ):
                            reasons.append(
                                "replay_set accuracy evidence manifest SHA-256 does not match"
                            )
                        accuracy_canonical_sha = accuracy_document.get("canonical_sha256")
                        if (
                            not isinstance(accuracy_canonical_sha, str)
                            or not isinstance(expected_canonical, str)
                            or accuracy_canonical_sha.lower() != expected_canonical.lower()
                        ):
                            reasons.append(
                                "replay_set accuracy evidence canonical SHA-256 does not match"
                            )
                        if accuracy_document.get("gate_passed") is not True:
                            reasons.append(
                                "replay_set accuracy evidence artifact gate_passed is not true"
                            )
                        if accuracy_document.get("accuracy_validated") is not True:
                            reasons.append(
                                "replay_set accuracy evidence artifact "
                                "accuracy_validated is not true"
                            )
                        if (
                            accuracy_document.get("gate_passed") is True
                            or accuracy_document.get("accuracy_validated") is True
                        ) and not accuracy_metrics_are_defined(accuracy_document.get("metrics")):
                            reasons.append(
                                "replay_set accuracy evidence aggregate "
                                "precision/recall are undefined"
                            )
                        if (
                            accuracy_document.get("gate_passed") is True
                            or accuracy_document.get("accuracy_validated") is True
                        ) and not positive_strata_coverage_is_complete(accuracy_document):
                            reasons.append(
                                "replay_set accuracy evidence positive-stratum "
                                "coverage is incomplete"
                            )
                        for field in (
                            "structurally_valid",
                            "deployment_domain_evidence_complete",
                            "predictions_complete",
                            "metrics_evaluated",
                            "metrics_gate_passed",
                            "reviewed",
                        ):
                            if accuracy_document.get(field) is not True:
                                reasons.append(
                                    f"replay_set accuracy evidence artifact {field} is not true"
                                )
                        failure_reasons_value = accuracy_document.get("failure_reasons")
                        if failure_reasons_value != []:
                            reasons.append(
                                "replay_set accuracy evidence artifact contains failure_reasons"
                            )

    if manifest_path is not None:
        manifest_info: dict[str, Any] = {"path": str(manifest_path)}
        if not manifest_path.is_file():
            reasons.append("replay_set manifest file does not exist")
        else:
            payload = manifest_path.read_bytes()
            actual_sha = hashlib.sha256(payload).hexdigest()
            manifest_info["sha256"] = actual_sha
            if isinstance(expected_sha, str) and _is_sha256(expected_sha):
                if actual_sha.lower() != expected_sha.lower():
                    reasons.append("replay_set manifest SHA-256 does not match")
            try:
                replay_set = load_replay_set_manifest(manifest_path)
            except (OSError, ValueError, RuntimeError) as exc:
                reasons.append(f"replay_set manifest is invalid: {exc}")
            else:
                manifest_info.update(
                    {
                        "dataset_id": replay_set.dataset_id,
                        "dataset_version": replay_set.dataset_version,
                        "canonical_sha256": replay_set.canonical_hash,
                        "coverage": replay_set.coverage(),
                        "missing_strata": list(replay_set.missing_strata),
                        "gate_passed": replay_set.is_complete,
                        "accuracy_validated": replay_set.claims["accuracy_validated"],
                    }
                )
                if isinstance(expected_canonical, str) and _is_sha256(expected_canonical):
                    if replay_set.canonical_hash.lower() != expected_canonical.lower():
                        reasons.append("replay_set canonical SHA-256 does not match")
                if type(gate_value) is bool and gate_value != replay_set.is_complete:
                    reasons.append("replay_set.gate_passed does not match manifest claims")
                if replay_set.claims["accuracy_validated"] is not False:
                    reasons.append("replay_set manifest claims accuracy_validated=true")
                if set(replay_set.required_strata) != set(REPLAY_SET_STRATA):
                    reasons.append(
                        "replay_set required_strata must cover all deployment strata: "
                        + ", ".join(REPLAY_SET_STRATA)
                    )
                if not replay_set.is_complete:
                    reasons.append("replay_set deployment coverage is incomplete")
        result["manifest"] = manifest_info

    if manifest_path is not None and manifest_path.is_file():
        try:
            readiness = check_replay_set_readiness(
                manifest_path,
                accuracy_report_path=(
                    accuracy_path if accuracy_path is not None and accuracy_path.is_file() else None
                ),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            reasons.append(f"replay_set readiness could not be recomputed: {exc}")
        else:
            result["readiness"] = readiness

    calibration_verification = _replay_set_calibration_verification_result(
        raw_record.get("calibration_verification"),
        manifest_directory=manifest_directory,
        replay_manifest_path=manifest_path,
        expected_manifest_sha=expected_sha,
        expected_canonical_sha=expected_canonical,
    )
    if calibration_verification["present"] and not calibration_verification["passed"]:
        reasons.extend(calibration_verification["reasons"])
    result["calibration_verification"] = (
        calibration_verification if calibration_verification["present"] else None
    )
    result["accuracy_evidence"] = accuracy_evidence_info
    result["passed"] = not reasons
    return result


def _real_field_link_result(
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Re-validate the optional REAL field-link preflight binding.

    The release manifest stores a path, byte count, SHA-256 and (for new
    bindings) an explicit maximum age for the saved field-link report.  On
    every evaluation we recompute those values and run the same strict
    verifier used by field-session capture.  This prevents a stale, replaced
    or merely structural preflight from being promoted into a REAL release
    claim.  The record remains optional for the prerelease line; if it is
    present, however, it is authoritative and any mismatch fails the evidence
    gate.  Historical prerelease records without freshness fields remain
    readable, while the final release channel requires a freshness-bound
    record.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "report": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("real_field_link record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("real_field_link must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_REAL_FIELD_LINK_KEYS))
    if unknown:
        reasons.append(f"unknown real_field_link field(s): {unknown!r}")

    path_value = raw_record.get("report_path")
    report_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("real_field_link.report_path is missing or empty")
    else:
        report_path = Path(path_value)
        if not report_path.is_absolute():
            report_path = manifest_directory / report_path
        try:
            report_path = report_path.resolve()
        except OSError as exc:
            reasons.append(f"real_field_link report path cannot be resolved: {exc}")
            report_path = None

    expected_sha = raw_record.get("report_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("real_field_link.report_sha256 is missing or invalid")
    expected_bytes = raw_record.get("report_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("real_field_link.report_bytes must be a non-negative integer")
    freshness_bound = "max_age_s" in raw_record
    timestamp_bound = "report_timestamp_epoch_s" in raw_record
    if timestamp_bound and not freshness_bound:
        reasons.append("real_field_link.report_timestamp_epoch_s requires max_age_s")
    configured_max_age_s: float | None = None
    expected_report_timestamp: float | None = None
    if freshness_bound:
        raw_max_age = raw_record.get("max_age_s")
        if not _is_finite_number(raw_max_age) or float(raw_max_age) < 0.0:
            reasons.append("real_field_link.max_age_s must be a finite non-negative number")
        else:
            configured_max_age_s = float(raw_max_age)
        if "report_timestamp_epoch_s" not in raw_record:
            reasons.append(
                "real_field_link.report_timestamp_epoch_s is required when max_age_s is bound"
            )
        else:
            raw_timestamp = raw_record.get("report_timestamp_epoch_s")
            if raw_timestamp is not None and not _is_finite_number(raw_timestamp):
                reasons.append(
                    "real_field_link.report_timestamp_epoch_s must be a finite number or null"
                )
            elif raw_timestamp is not None:
                expected_report_timestamp = float(raw_timestamp)
    expected_ready = raw_record.get("hardware_link_ready")
    if type(expected_ready) is not bool:
        reasons.append("real_field_link.hardware_link_ready must be a boolean")
    expected_address_state_bound = raw_record.get("address_state_bound")
    if expected_address_state_bound is not None and type(expected_address_state_bound) is not bool:
        reasons.append("real_field_link.address_state_bound must be a boolean")
    route_requested = raw_record.get("route_checks_requested")
    route_passed = raw_record.get("route_checks_passed")
    route_cidr = raw_record.get("route_source_cidr")
    route_passed_needs_boolean = False
    if route_requested is not None and type(route_requested) is not bool:
        reasons.append("real_field_link.route_checks_requested must be a boolean")
    if route_passed is not None and type(route_passed) is not bool:
        reasons.append("real_field_link.route_checks_passed must be a boolean or null")
    if route_requested is True and type(route_passed) is not bool:
        if route_passed is None:
            # A fail-fast watch uses null until every requested sample has
            # been observed.  Defer this type invariant until the hash-bound
            # report is opened so a partial watch can be distinguished from a
            # malformed one-shot record.
            route_passed_needs_boolean = True
        else:
            reasons.append(
                "real_field_link.route_checks_passed must be a boolean when "
                "route checks are requested"
            )
    elif route_requested is False and route_passed not in (None, False):
        reasons.append(
            "real_field_link.route_checks_passed must be null or false when "
            "route checks are not requested"
        )
    if route_cidr is not None:
        if not isinstance(route_cidr, str) or not route_cidr.strip():
            reasons.append(
                "real_field_link.route_source_cidr must be a non-empty IPv4 CIDR or null"
            )
        else:
            try:
                parsed_route_cidr = ipaddress.ip_network(route_cidr.strip(), strict=False)
                if parsed_route_cidr.version != 4:
                    raise ValueError("route_source_cidr must be an IPv4 CIDR")
            except ValueError as exc:
                reasons.append(f"real_field_link.{exc}")
    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("real_field_link.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("real_field_link gate_passed is false")
    raw_reasons = raw_record.get("reasons")
    if raw_reasons is not None:
        if not isinstance(raw_reasons, list) or any(
            not isinstance(item, str) for item in raw_reasons
        ):
            reasons.append("real_field_link.reasons must be a list of strings")

    if report_path is not None:
        report_info: dict[str, Any] = {"path": str(report_path)}
        if not report_path.is_file():
            reasons.append("real_field_link report file does not exist")
        else:
            actual_sha = _sha256(report_path)
            actual_bytes = report_path.stat().st_size
            report_info.update({"sha256": actual_sha, "bytes": actual_bytes})
            if (
                isinstance(expected_sha, str)
                and _is_sha256(expected_sha)
                and actual_sha.lower() != expected_sha.lower()
            ):
                reasons.append("real_field_link report SHA-256 does not match")
            if type(expected_bytes) is int and actual_bytes != expected_bytes:
                reasons.append("real_field_link report byte count does not match")
            try:
                verification = verify_field_link_report(
                    report_path,
                    require_ready=True,
                    max_age_s=configured_max_age_s,
                )
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                reasons.append(f"real_field_link verifier failed: {exc}")
            else:
                report_info.update(
                    {
                        "hardware_link_ready": verification.get("hardware_link_ready"),
                        "gate_passed": verification.get("gate_passed"),
                        "max_age_s": verification.get("max_age_s"),
                        "report_timestamp_epoch_s": verification.get("report_timestamp_epoch_s"),
                        "report_age_s": verification.get("report_age_s"),
                        "checks": verification.get("checks"),
                    }
                )
                verification_checks = verification.get("checks")
                address_state_bound = (
                    isinstance(verification_checks, Mapping)
                    and "address_state" in verification_checks
                )
                report_info["address_state_bound"] = address_state_bound
                if (
                    expected_address_state_bound is not None
                    and expected_address_state_bound is not address_state_bound
                ):
                    reasons.append("real_field_link address_state_bound does not match report")
                if freshness_bound:
                    verified_max_age = verification.get("max_age_s")
                    if (
                        configured_max_age_s is not None
                        and verified_max_age != configured_max_age_s
                    ):
                        reasons.append("real_field_link.max_age_s does not match verifier")
                    verified_timestamp = verification.get("report_timestamp_epoch_s")
                    if expected_report_timestamp is None:
                        if verified_timestamp is not None:
                            reasons.append(
                                "real_field_link.report_timestamp_epoch_s does not match verifier"
                            )
                    elif verified_timestamp != expected_report_timestamp:
                        reasons.append(
                            "real_field_link.report_timestamp_epoch_s does not match verifier"
                        )
                verification_summary = verification.get("summary")
                if isinstance(verification_summary, Mapping):
                    report_mode = verification.get("mode")
                    report_samples_requested = verification_summary.get("samples_requested")
                    report_samples_collected = verification_summary.get("samples_collected")
                    partial_watch = (
                        report_mode == "watch"
                        and type(report_samples_requested) is int
                        and not isinstance(report_samples_requested, bool)
                        and report_samples_requested > 0
                        and type(report_samples_collected) is int
                        and not isinstance(report_samples_collected, bool)
                        and 0 <= report_samples_collected < report_samples_requested
                    )
                    if route_passed_needs_boolean and partial_watch:
                        route_passed_needs_boolean = False
                    report_route_requested = bool(
                        verification_summary.get("route_checks_requested", False)
                    )
                    report_route_passed = verification_summary.get("route_checks_passed")
                    report_route_cidr = verification_summary.get("route_source_cidr")
                    report_info.update(
                        {
                            "route_checks_requested": report_route_requested,
                            "route_checks_passed": report_route_passed,
                            "route_source_cidr": report_route_cidr,
                        }
                    )
                    if (
                        route_requested is not None
                        and route_requested is not report_route_requested
                    ):
                        reasons.append(
                            "real_field_link route_checks_requested does not match report"
                        )
                    if "route_checks_passed" in raw_record and route_passed != report_route_passed:
                        reasons.append("real_field_link route_checks_passed does not match report")
                    if route_cidr is not None and route_cidr != report_route_cidr:
                        reasons.append("real_field_link route_source_cidr does not match report")
                if verification.get("hardware_link_ready") is not expected_ready:
                    reasons.append("real_field_link hardware_link_ready does not match report")
                if verification.get("gate_passed") is not expected_gate:
                    reasons.append("real_field_link gate_passed does not match report")
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str) and reason not in reasons:
                        reasons.append(f"real_field_link: {reason}")
        result["report"] = report_info

    if route_passed_needs_boolean:
        reasons.append(
            "real_field_link.route_checks_passed must be a boolean when route checks are requested"
        )

    result["passed"] = not reasons
    return result


def _real_alert_link_result(
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Re-validate the optional REAL authenticated alert-link binding.

    The manifest stores a compact summary of a completed authenticated UDP
    alert ACK bench.  The complete report remains the source of truth: every
    evaluation re-hashes it, parses it with the strict JSON loader and runs the
    independent bench verifier again.  A software/loopback report can never be
    promoted by changing only the manifest summary; the hardware event,
    endpoint domain and authenticated/correlated ACK bits are all checked
    against the reopened source.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "report": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("real_alert_link record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("real_alert_link must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_REAL_ALERT_LINK_KEYS))
    if unknown:
        reasons.append(f"unknown real_alert_link field(s): {unknown!r}")

    # Every source summary field is required in a generated binding.  Keeping
    # this explicit makes hand-written or truncated records fail closed before
    # any comparison with the source report.
    schema_version = raw_record.get("schema_version")
    bound_fields = _REAL_ALERT_LINK_BOUND_FIELDS
    if schema_version in {2, 3, 4, 5}:
        bound_fields += _REAL_ALERT_LINK_V2_BOUND_FIELDS
    if schema_version in {3, 4, 5}:
        bound_fields += _REAL_ALERT_LINK_V3_BOUND_FIELDS
    if schema_version in {4, 5}:
        bound_fields += _REAL_ALERT_LINK_V4_BOUND_FIELDS
    if schema_version == 5:
        bound_fields += _REAL_ALERT_LINK_V5_BOUND_FIELDS
    for field in bound_fields:
        if field not in raw_record:
            reasons.append(f"real_alert_link.{field} is missing")

    path_value = raw_record.get("report_path")
    report_path: Path | None = None
    requested_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("real_alert_link.report_path is missing or empty")
    else:
        requested_path = Path(path_value)
        if not requested_path.is_absolute():
            requested_path = manifest_directory / requested_path
        if requested_path.is_symlink():
            reasons.append("real_alert_link report must not be a symbolic link")
        try:
            report_path = requested_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"real_alert_link report path cannot be resolved: {exc}")

    expected_sha = raw_record.get("report_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("real_alert_link.report_sha256 is missing or invalid")
    expected_bytes = raw_record.get("report_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("real_alert_link.report_bytes must be a non-negative integer")

    expected_contract: dict[str, object] = {
        "event": "alert_link_bench_passed",
        "hardware_observed": True,
        "simulation_only": False,
        "remote_is_loopback": False,
        "correlated_ack_verified": True,
        "authenticated_ack_verified": True,
    }
    if schema_version != 5:
        reasons.append("real_alert_link.schema_version must be 5")
    for field, expected in expected_contract.items():
        actual = raw_record.get(field)
        if type(actual) is not type(expected) or actual != expected:
            reasons.append(f"real_alert_link.{field} does not match hardware verifier contract")

    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("real_alert_link.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("real_alert_link gate_passed is false")

    raw_reasons = raw_record.get("reasons")
    if not isinstance(raw_reasons, list) or any(not isinstance(item, str) for item in raw_reasons):
        reasons.append("real_alert_link.reasons must be a list of strings")

    if report_path is not None:
        report_info: dict[str, Any] = {"path": str(report_path)}
        if not report_path.is_file():
            reasons.append("real_alert_link report file does not exist")
        else:
            try:
                actual_sha = _sha256(report_path)
                actual_bytes = report_path.stat().st_size
            except OSError as exc:
                reasons.append(f"real_alert_link report cannot be hashed: {exc}")
            else:
                report_info.update({"sha256": actual_sha, "bytes": actual_bytes})
                if (
                    isinstance(expected_sha, str)
                    and _is_sha256(expected_sha)
                    and actual_sha.lower() != expected_sha.lower()
                ):
                    reasons.append("real_alert_link report SHA-256 does not match")
                if type(expected_bytes) is int and actual_bytes != expected_bytes:
                    reasons.append("real_alert_link report byte count does not match")

            try:
                report_document = _load_object(report_path)
            except ValueError as exc:
                reasons.append(f"real_alert_link report JSON is invalid: {exc}")
                report_document = None
            if isinstance(report_document, Mapping):
                verification = verify_alert_link_bench_report(
                    report_document,
                    require_passed=True,
                )
                result["verification"] = verification
                report_info.update({field: report_document.get(field) for field in bound_fields})
                report_info["transport_protocol"] = report_document.get("transport_protocol")
                report_info["failure_types"] = report_document.get("failure_types")
                report_info["passed"] = report_document.get("passed")

                # Compare every bound summary field, including nested checks,
                # thresholds and the ordered reasons list.  Numeric values use
                # the same tolerance as the rest of the evaluator so JSON
                # integer/float spelling cannot create a false mismatch.
                for field in bound_fields:
                    expected = raw_record.get(field)
                    actual = report_document.get(field)
                    if field in {
                        "packet_loss_rate",
                        "ack_latency_p50_ms",
                        "ack_latency_p95_ms",
                    }:
                        matches = _same_json_number_or_value(actual, expected)
                    else:
                        matches = actual == expected
                    if not matches:
                        reasons.append(f"real_alert_link {field} does not match report")

                for field, expected in expected_contract.items():
                    actual = report_document.get(field)
                    if type(actual) is not type(expected) or actual != expected:
                        reasons.append(
                            "real_alert_link report "
                            f"{field} does not match hardware verifier contract"
                        )
                if verification.get("gate_passed") is not True:
                    for reason in verification.get("reasons", []):
                        if isinstance(reason, str) and reason:
                            reasons.append(f"real_alert_link: {reason}")
            elif report_document is not None:
                reasons.append("real_alert_link report must contain a JSON object")
        result["report"] = report_info

    result["passed"] = not reasons
    return result


def _real_gr01_operator_link_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    evaluated_at_utc: str,
) -> dict[str, Any]:
    """Re-open and independently verify the REAL GR01 selection/ACK report."""

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "report": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("real_gr01_operator_link record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("real_gr01_operator_link must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_REAL_GR01_OPERATOR_LINK_KEYS))
    if unknown:
        reasons.append(f"unknown real_gr01_operator_link field(s): {unknown!r}")
    for field in _REAL_GR01_OPERATOR_LINK_BOUND_FIELDS:
        if field not in raw_record:
            reasons.append(f"real_gr01_operator_link.{field} is missing")

    path_value = raw_record.get("report_path")
    report_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("real_gr01_operator_link.report_path is missing or empty")
    else:
        requested_path = Path(path_value)
        if not requested_path.is_absolute():
            requested_path = manifest_directory / requested_path
        if requested_path.is_symlink():
            reasons.append("real_gr01_operator_link report must not be a symbolic link")
        try:
            report_path = requested_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"real_gr01_operator_link report path cannot be resolved: {exc}")

    expected_sha = raw_record.get("report_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("real_gr01_operator_link.report_sha256 is missing or invalid")
    expected_bytes = raw_record.get("report_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("real_gr01_operator_link.report_bytes must be a non-negative integer")

    freshness_fields = ("observed_at_utc", "report_timestamp_epoch_s", "max_age_s")
    freshness_present = [field in raw_record for field in freshness_fields]
    freshness_bound = all(freshness_present)
    if any(freshness_present) and not freshness_bound:
        reasons.append(
            "real_gr01_operator_link freshness binding is partial; "
            "observed_at_utc, report_timestamp_epoch_s and max_age_s are all required"
        )
    expected_observed_at: str | None = None
    expected_report_timestamp: float | None = None
    configured_max_age_s: float | None = None
    if freshness_bound:
        raw_observed_at = raw_record.get("observed_at_utc")
        if not isinstance(raw_observed_at, str) or not raw_observed_at.strip():
            reasons.append("real_gr01_operator_link.observed_at_utc is missing or empty")
        else:
            expected_observed_at = raw_observed_at.strip()
        raw_timestamp = raw_record.get("report_timestamp_epoch_s")
        if not _is_finite_number(raw_timestamp):
            reasons.append(
                "real_gr01_operator_link.report_timestamp_epoch_s must be a finite number"
            )
        else:
            expected_report_timestamp = float(raw_timestamp)
        raw_max_age = raw_record.get("max_age_s")
        if not _is_finite_number(raw_max_age) or float(raw_max_age) < 0.0:
            reasons.append("real_gr01_operator_link.max_age_s must be a finite non-negative number")
        else:
            configured_max_age_s = float(raw_max_age)
        if expected_observed_at is not None and expected_report_timestamp is not None:
            parse_value = (
                expected_observed_at[:-1] + "+00:00"
                if expected_observed_at.endswith(("Z", "z"))
                else expected_observed_at
            )
            try:
                parsed_observed_at = datetime.fromisoformat(parse_value)
            except ValueError:
                reasons.append("real_gr01_operator_link.observed_at_utc must be ISO-8601")
            else:
                if parsed_observed_at.tzinfo is None:
                    reasons.append(
                        "real_gr01_operator_link.observed_at_utc must include a timezone"
                    )
                elif not math.isclose(
                    parsed_observed_at.timestamp(),
                    expected_report_timestamp,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                ):
                    reasons.append(
                        "real_gr01_operator_link report_timestamp_epoch_s does not match "
                        "observed_at_utc"
                    )
        if expected_report_timestamp is not None and configured_max_age_s is not None:
            evaluation_epoch_s = datetime.fromisoformat(
                evaluated_at_utc[:-1] + "+00:00"
                if evaluated_at_utc.endswith("Z")
                else evaluated_at_utc
            ).timestamp()
            age_s = evaluation_epoch_s - expected_report_timestamp
            result["age_s"] = age_s
            result["max_age_s"] = configured_max_age_s
            if age_s < -5.0:
                reasons.append("real_gr01_operator_link observation is in the future")
            elif age_s > configured_max_age_s:
                reasons.append(
                    "real_gr01_operator_link observation is stale: "
                    f"age {age_s:.3f}s exceeds max_age_s {configured_max_age_s:.3f}s"
                )

    expected_contract: dict[str, object] = {
        "event": "gr01_bench_passed",
        "schema_version": 1,
        "hardware_observed": True,
        "simulation_only": False,
        "passed": True,
        "hardware_model": "GR01",
        "remote_is_loopback": False,
        "source_route_verified": True,
        "bidirectional_ip_verified": True,
        "signed_operator_round_trip": True,
        "application_hmac_verified": True,
        "mavlink2_signature_verified": True,
        "payload_release_requested": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "hardware_control_enabled": False,
    }
    for field, expected in expected_contract.items():
        actual = raw_record.get(field)
        if type(actual) is not type(expected) or actual != expected:
            reasons.append(
                f"real_gr01_operator_link.{field} does not match hardware verifier contract"
            )
    if raw_record.get("gate_passed") is not True:
        reasons.append("real_gr01_operator_link gate_passed is not true")

    if report_path is not None:
        report_info: dict[str, Any] = {"path": str(report_path)}
        if not report_path.is_file():
            reasons.append("real_gr01_operator_link report file does not exist")
        else:
            try:
                actual_sha = _sha256(report_path)
                actual_bytes = report_path.stat().st_size
            except OSError as exc:
                reasons.append(f"real_gr01_operator_link report cannot be hashed: {exc}")
            else:
                report_info.update({"sha256": actual_sha, "bytes": actual_bytes})
                if (
                    isinstance(expected_sha, str)
                    and _is_sha256(expected_sha)
                    and actual_sha.lower() != expected_sha.lower()
                ):
                    reasons.append("real_gr01_operator_link report SHA-256 does not match")
                if type(expected_bytes) is int and actual_bytes != expected_bytes:
                    reasons.append("real_gr01_operator_link report byte count does not match")
            try:
                report_document = _load_object(report_path)
            except ValueError as exc:
                reasons.append(f"real_gr01_operator_link report JSON is invalid: {exc}")
                report_document = None
            if isinstance(report_document, Mapping):
                verification = verify_gr01_link_bench_report(
                    report_document,
                    require_passed=True,
                )
                result["verification"] = verification
                report_info.update(
                    {
                        field: report_document.get(field)
                        for field in _REAL_GR01_OPERATOR_LINK_BOUND_FIELDS
                    }
                )
                for field in _REAL_GR01_OPERATOR_LINK_BOUND_FIELDS:
                    expected = raw_record.get(field)
                    actual = report_document.get(field)
                    if field in {
                        "packet_loss_rate",
                        "ack_latency_p50_ms",
                        "ack_latency_p95_ms",
                    }:
                        matches = _same_json_number_or_value(actual, expected)
                    else:
                        matches = actual == expected
                    if not matches:
                        reasons.append(f"real_gr01_operator_link {field} does not match report")
                if freshness_bound and expected_observed_at is not None:
                    if report_document.get("observed_at_utc") != expected_observed_at:
                        reasons.append(
                            "real_gr01_operator_link observed_at_utc does not match report"
                        )
                if verification.get("gate_passed") is not True:
                    for reason in verification.get("reasons", []):
                        if isinstance(reason, str) and reason:
                            reasons.append(f"real_gr01_operator_link: {reason}")
            elif report_document is not None:
                reasons.append("real_gr01_operator_link report must contain a JSON object")
        result["report"] = report_info

    result["passed"] = not reasons
    return result


def _real_target_accuracy_result(
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Re-validate the optional REAL target-geolocation accuracy binding.

    The report is an offline, truth-bearing artifact and must never enter the
    runtime control path.  The manifest stores its identity and a compact
    metric summary; every evaluation re-hashes the JSON and recomputes the
    WGS-84 errors from the serialized samples.  A failed prerelease report is
    retained as an explicit diagnostic, while a present binding is always
    authoritative and therefore closes the evidence gate until it passes.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "report": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("real_target_accuracy record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("real_target_accuracy must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_REAL_TARGET_ACCURACY_KEYS))
    if unknown:
        reasons.append(f"unknown real_target_accuracy field(s): {unknown!r}")

    path_value = raw_record.get("report_path")
    report_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("real_target_accuracy.report_path is missing or empty")
    else:
        report_path = Path(path_value)
        if not report_path.is_absolute():
            report_path = manifest_directory / report_path
        try:
            report_path = report_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"real_target_accuracy report path cannot be resolved: {exc}")
            report_path = None

    expected_sha = raw_record.get("report_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("real_target_accuracy.report_sha256 is missing or invalid")
    expected_bytes = raw_record.get("report_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("real_target_accuracy.report_bytes must be a non-negative integer")

    expected_contract: dict[str, object] = {
        "evidence_type": "multidetect.target-geolocation-accuracy.v1",
        "schema_version": 1,
        "ground_truth_isolated": True,
        "advisory_only": True,
        "contains_flight_control": False,
    }
    for field, expected in expected_contract.items():
        if type(raw_record.get(field)) is not type(expected) or raw_record.get(field) != expected:
            reasons.append(f"real_target_accuracy.{field} does not match verifier contract")

    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("real_target_accuracy.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("real_target_accuracy gate_passed is false")

    expected_sample_count = raw_record.get("sample_count")
    if type(expected_sample_count) is not int or expected_sample_count < 1:
        reasons.append("real_target_accuracy.sample_count must be a positive integer")
    expected_p95 = raw_record.get("p95_error_m")
    if not _finite_non_negative(expected_p95):
        reasons.append("real_target_accuracy.p95_error_m must be finite and non-negative")
    expected_one_sigma = raw_record.get("one_sigma_coverage")
    if not _valid_coverage_or_none(expected_one_sigma):
        reasons.append("real_target_accuracy.one_sigma_coverage must be within [0, 1] or null")
    expected_two_sigma = raw_record.get("two_sigma_coverage")
    if not _valid_coverage_or_none(expected_two_sigma):
        reasons.append("real_target_accuracy.two_sigma_coverage must be within [0, 1] or null")
    raw_reasons = raw_record.get("reasons")
    if raw_reasons is not None and (
        not isinstance(raw_reasons, list) or any(not isinstance(item, str) for item in raw_reasons)
    ):
        reasons.append("real_target_accuracy.reasons must be a list of strings")

    if report_path is not None:
        report_info: dict[str, Any] = {"path": str(report_path)}
        if not report_path.is_file():
            reasons.append("real_target_accuracy report file does not exist")
        else:
            try:
                actual_sha = _sha256(report_path)
                actual_bytes = report_path.stat().st_size
            except OSError as exc:
                reasons.append(f"real_target_accuracy report cannot be hashed: {exc}")
            else:
                report_info.update({"sha256": actual_sha, "bytes": actual_bytes})
                if (
                    isinstance(expected_sha, str)
                    and _is_sha256(expected_sha)
                    and actual_sha.lower() != expected_sha.lower()
                ):
                    reasons.append("real_target_accuracy report SHA-256 does not match")
                if type(expected_bytes) is int and actual_bytes != expected_bytes:
                    reasons.append("real_target_accuracy report byte count does not match")
            try:
                report_document = _load_object(report_path)
            except ValueError as exc:
                reasons.append(f"real_target_accuracy report JSON is invalid: {exc}")
                report_document = None
            if isinstance(report_document, Mapping):
                verification = verify_geolocation_accuracy_report(
                    report_document,
                    require_gate_passed=True,
                )
                result["verification"] = verification
                report_info.update(
                    {
                        "evidence_type": report_document.get("evidence_type"),
                        "schema_version": report_document.get("schema_version"),
                        "ground_truth_isolated": report_document.get("ground_truth_isolated"),
                        "advisory_only": report_document.get("advisory_only"),
                        "contains_flight_control": report_document.get("contains_flight_control"),
                        "gate_passed": report_document.get("gate_passed"),
                    }
                )
                measurement = report_document.get("measurement")
                distance_metrics = report_document.get("distance_error_m")
                actual_summary = {
                    "sample_count": (
                        measurement.get("sample_count")
                        if isinstance(measurement, Mapping)
                        else None
                    ),
                    "p95_error_m": (
                        distance_metrics.get("p95_m")
                        if isinstance(distance_metrics, Mapping)
                        else None
                    ),
                    "one_sigma_coverage": (
                        measurement.get("one_sigma_coverage")
                        if isinstance(measurement, Mapping)
                        else None
                    ),
                    "two_sigma_coverage": (
                        measurement.get("two_sigma_coverage")
                        if isinstance(measurement, Mapping)
                        else None
                    ),
                }
                report_info["measurement"] = measurement
                report_info["distance_error_m"] = distance_metrics
                requirements = report_document.get("requirements")
                if not isinstance(requirements, Mapping) or not any(
                    requirements.get(field) is not None
                    for field in (
                        "maximum_p95_m",
                        "minimum_one_sigma_coverage",
                        "minimum_two_sigma_coverage",
                    )
                ):
                    reasons.append("real_target_accuracy report has no explicit accuracy threshold")
                for field, expected in expected_contract.items():
                    if (
                        type(report_document.get(field)) is not type(expected)
                        or report_document.get(field) != expected
                    ):
                        reasons.append(
                            f"real_target_accuracy report {field} does not match binding"
                        )
                for field, expected in (
                    ("gate_passed", expected_gate),
                    ("sample_count", expected_sample_count),
                    ("p95_error_m", expected_p95),
                    ("one_sigma_coverage", expected_one_sigma),
                    ("two_sigma_coverage", expected_two_sigma),
                ):
                    actual = (
                        report_document.get(field)
                        if field == "gate_passed"
                        else actual_summary[field]
                    )
                    if not _same_json_number_or_value(actual, expected):
                        reasons.append(f"real_target_accuracy {field} does not match report")
                if raw_reasons is not None and report_document.get("reasons") != raw_reasons:
                    reasons.append("real_target_accuracy reasons do not match report")
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str) and reason not in reasons:
                        reasons.append(f"real_target_accuracy: {reason}")
            elif report_document is not None:
                reasons.append("real_target_accuracy report must contain a JSON object")
        result["report"] = report_info

    result["passed"] = not reasons
    return result


def _real_target_state_evidence_result(
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Re-validate the optional REAL target-state capture binding.

    A target-state capture is a different proof from surveyed accuracy: it
    shows that one operator session actually reached ``TGT`` with fresh GPS
    and ``RANGE_STATUS`` samples.  The complete capture report remains the
    source of truth; this function only checks its hash-bound compact summary
    and re-runs :func:`verify_field_session_capture` on every evaluation.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "report": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("real_target_state_evidence record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("real_target_state_evidence must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_REAL_TARGET_STATE_EVIDENCE_KEYS))
    if unknown:
        reasons.append(f"unknown real_target_state_evidence field(s): {unknown!r}")

    path_value = raw_record.get("report_path")
    report_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("real_target_state_evidence.report_path is missing or empty")
    else:
        report_path = Path(path_value)
        if not report_path.is_absolute():
            report_path = manifest_directory / report_path
        try:
            report_path = report_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"real_target_state_evidence report path cannot be resolved: {exc}")
            report_path = None

    expected_sha = raw_record.get("report_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("real_target_state_evidence.report_sha256 is missing or invalid")
    expected_bytes = raw_record.get("report_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("real_target_state_evidence.report_bytes must be a non-negative integer")
    expected_schema = raw_record.get("schema_version")
    if type(expected_schema) is not int or expected_schema != 1:
        reasons.append("real_target_state_evidence.schema_version must be 1")
    expected_required = raw_record.get("require_target_state_evidence")
    if expected_required is not True:
        reasons.append("real_target_state_evidence.require_target_state_evidence must be true")
    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("real_target_state_evidence.gate_passed must be a boolean")
    elif expected_gate is not True:
        reasons.append("real_target_state_evidence gate_passed is false")
    expected_count = raw_record.get("captured_records")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        reasons.append("real_target_state_evidence.captured_records must be a positive integer")

    def _valid_index_list(value: object) -> bool:
        return isinstance(value, list) and all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value
        )

    def _valid_state_path(value: object) -> bool:
        return isinstance(value, list) and all(
            isinstance(item, str) and bool(item.strip()) for item in value
        )

    expected_state_path = raw_record.get("state_path")
    expected_gps_records = raw_record.get("gps_qualified_records")
    expected_range_records = raw_record.get("range_qualified_records")
    for name, value in (
        ("state_path", expected_state_path),
        ("gps_qualified_records", expected_gps_records),
        ("range_qualified_records", expected_range_records),
    ):
        valid = _valid_state_path(value) if name == "state_path" else _valid_index_list(value)
        if not valid:
            if name == "state_path":
                reasons.append(
                    "real_target_state_evidence.state_path must be an array of state strings"
                )
            else:
                reasons.append(
                    f"real_target_state_evidence.{name} must be an array of non-negative integers"
                )
    raw_reasons = raw_record.get("reasons")
    if raw_reasons is not None and (
        not isinstance(raw_reasons, list) or any(not isinstance(item, str) for item in raw_reasons)
    ):
        reasons.append("real_target_state_evidence.reasons must be a list of strings")

    if report_path is not None:
        report_info: dict[str, Any] = {"path": str(report_path)}
        if not report_path.is_file():
            reasons.append("real_target_state_evidence report file does not exist")
        else:
            try:
                actual_sha = _sha256(report_path)
                actual_bytes = report_path.stat().st_size
            except OSError as exc:
                reasons.append(f"real_target_state_evidence report cannot be hashed: {exc}")
                actual_sha = None
                actual_bytes = 0
            report_info.update({"sha256": actual_sha, "bytes": actual_bytes})
            if (
                isinstance(expected_sha, str)
                and _is_sha256(expected_sha)
                and actual_sha is not None
                and actual_sha.lower() != expected_sha.lower()
            ):
                reasons.append("real_target_state_evidence report SHA-256 does not match")
            if type(expected_bytes) is int and actual_bytes != expected_bytes:
                reasons.append("real_target_state_evidence report byte count does not match")
            try:
                report_document = _load_object(report_path)
            except ValueError as exc:
                reasons.append(f"real_target_state_evidence report JSON is invalid: {exc}")
                report_document = None
            if isinstance(report_document, Mapping):
                verification = verify_field_session_capture(report_path)
                result["verification"] = verification
                report_info["capture_gate_passed"] = report_document.get("gate_passed")
                capture = report_document.get("capture")
                validation = report_document.get("validation")
                target_state = (
                    validation.get("target_state_evidence")
                    if isinstance(validation, Mapping)
                    else None
                )
                report_info["require_target_state_evidence"] = (
                    capture.get("require_target_state_evidence")
                    if isinstance(capture, Mapping)
                    else None
                )
                report_info["captured_records"] = (
                    capture.get("captured_records") if isinstance(capture, Mapping) else None
                )
                report_info["state_path"] = (
                    validation.get("state_path") if isinstance(validation, Mapping) else None
                )
                report_info["target_state_evidence"] = target_state
                actual_gate = report_document.get("gate_passed")
                actual_required = (
                    capture.get("require_target_state_evidence")
                    if isinstance(capture, Mapping)
                    else None
                )
                actual_count = (
                    capture.get("captured_records") if isinstance(capture, Mapping) else None
                )
                actual_state_path = (
                    validation.get("state_path") if isinstance(validation, Mapping) else None
                )
                actual_gps_records = (
                    target_state.get("gps_qualified_records")
                    if isinstance(target_state, Mapping)
                    else None
                )
                actual_range_records = (
                    target_state.get("range_qualified_records")
                    if isinstance(target_state, Mapping)
                    else None
                )
                actual_target_required = (
                    target_state.get("required") if isinstance(target_state, Mapping) else None
                )
                actual_target_state = (
                    target_state.get("state") if isinstance(target_state, Mapping) else None
                )
                actual_target_valid = (
                    target_state.get("valid") if isinstance(target_state, Mapping) else None
                )
                if actual_required is not True or actual_target_required is not True:
                    reasons.append(
                        "real_target_state_evidence report does not enable the strict TGT gate"
                    )
                if actual_target_state != "TGT":
                    reasons.append("real_target_state_evidence report target state must be TGT")
                if actual_target_valid is not True:
                    reasons.append(
                        "real_target_state_evidence report target-state gate is not valid"
                    )
                if actual_gate is not expected_gate:
                    reasons.append("real_target_state_evidence gate_passed does not match report")
                for name, actual, expected in (
                    ("captured_records", actual_count, expected_count),
                    ("state_path", actual_state_path, expected_state_path),
                    ("gps_qualified_records", actual_gps_records, expected_gps_records),
                    ("range_qualified_records", actual_range_records, expected_range_records),
                ):
                    if actual != expected:
                        reasons.append(f"real_target_state_evidence {name} does not match report")
                if verification.get("gate_passed") is not True:
                    reasons.append("real_target_state_evidence capture verifier did not pass")
                if raw_reasons is not None:
                    report_validation_reasons = (
                        target_state.get("reasons") if isinstance(target_state, Mapping) else None
                    )
                    if report_validation_reasons != raw_reasons:
                        reasons.append("real_target_state_evidence reasons do not match report")
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str) and reason not in reasons:
                        reasons.append(f"real_target_state_evidence: {reason}")
            elif report_document is not None:
                reasons.append("real_target_state_evidence report must contain a JSON object")
        result["report"] = report_info

    result["passed"] = not reasons
    return result


def _finite_non_negative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _valid_coverage_or_none(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _same_json_number_or_value(left: object, right: object) -> bool:
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        if isinstance(right, (int, float)) and not isinstance(right, bool):
            return math.isclose(float(left), float(right), rel_tol=1.0e-12, abs_tol=1.0e-9)
    return left == right


def _field_evidence_bundle_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and re-run the optional six-strata field-evidence bundle.

    The field bundle is deliberately advisory and read-only, but once a
    release manifest binds it the binding is authoritative.  The evaluator
    re-opens the report, re-hashes it, and re-evaluates every source artifact
    recorded in its input map.  A prerelease may retain a valid diagnostic
    report with ``gate_passed=false``; the final channel requires a passing
    real-domain bundle.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "report": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("field_evidence_bundle record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("field_evidence_bundle must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_FIELD_EVIDENCE_BUNDLE_KEYS))
    if unknown:
        reasons.append(f"unknown field_evidence_bundle field(s): {unknown!r}")

    path_value = raw_record.get("report_path")
    report_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("field_evidence_bundle.report_path is missing or empty")
    else:
        report_path = Path(path_value)
        if not report_path.is_absolute():
            report_path = manifest_directory / report_path
        if report_path.is_symlink():
            reasons.append("field_evidence_bundle report must not be a symbolic link")
        try:
            report_path = report_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"field_evidence_bundle report path cannot be resolved: {exc}")
            report_path = None

    expected_sha = raw_record.get("report_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("field_evidence_bundle.report_sha256 is missing or invalid")
    expected_bytes = raw_record.get("report_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("field_evidence_bundle.report_bytes must be a non-negative integer")

    expected_contract: dict[str, object] = {
        "evidence_type": "multidetect.field-evidence-bundle.v1",
        "schema_version": 1,
        "release_eligible": False,
        "hardware_acceptance": "not_claimed",
        "real_flight_acceptance": "not_claimed",
        "read_only_evidence": True,
        "advisory_only": True,
        "ground_truth_isolated": True,
        "contains_flight_control": False,
        "contains_truth": True,
    }
    if expected_release_label is not None:
        expected_contract["release_label"] = expected_release_label
    for field, expected in expected_contract.items():
        if raw_record.get(field) != expected:
            reasons.append(f"field_evidence_bundle.{field} does not match verifier contract")

    expected_bundle_id = raw_record.get("bundle_id")
    if (
        not isinstance(expected_bundle_id, str)
        or len(expected_bundle_id) != len("field-evidence-") + 24
        or not expected_bundle_id.startswith("field-evidence-")
        or any(character not in "0123456789abcdef" for character in expected_bundle_id[-24:])
    ):
        reasons.append("field_evidence_bundle.bundle_id is invalid")

    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("field_evidence_bundle.gate_passed must be a boolean")
    elif expected_gate is not True:
        reasons.append("field_evidence_bundle gate_passed is false")

    summary_fields = (
        "all_inputs_present",
        "all_strata_passed",
        "cross_bindings_passed",
        "hashes_complete",
        "real_domain_required",
    )
    for field in summary_fields:
        if type(raw_record.get(field)) is not bool:
            reasons.append(f"field_evidence_bundle.{field} must be a boolean")
    if raw_record.get("real_domain_required") is not True:
        reasons.append("field_evidence_bundle must require the real deployment domain")

    raw_strata = raw_record.get("strata")
    if not isinstance(raw_strata, Mapping):
        reasons.append("field_evidence_bundle.strata must be an object")
        raw_strata = {}
    else:
        unknown_strata = sorted(set(raw_strata).difference(_FIELD_EVIDENCE_BUNDLE_STRATA))
        missing_strata = sorted(set(_FIELD_EVIDENCE_BUNDLE_STRATA).difference(raw_strata))
        if unknown_strata:
            reasons.append(f"unknown field_evidence_bundle strata field(s): {unknown_strata!r}")
        if missing_strata:
            reasons.append(f"missing field_evidence_bundle strata field(s): {missing_strata!r}")
        for name in _FIELD_EVIDENCE_BUNDLE_STRATA:
            if type(raw_strata.get(name)) is not bool:
                reasons.append(f"field_evidence_bundle.strata.{name} must be a boolean")

    if report_path is not None:
        report_info: dict[str, Any] = {"path": str(report_path)}
        if not report_path.is_file():
            reasons.append("field_evidence_bundle report file does not exist")
        else:
            try:
                actual_sha = _sha256(report_path)
                actual_bytes = report_path.stat().st_size
            except OSError as exc:
                reasons.append(f"field_evidence_bundle report cannot be hashed: {exc}")
            else:
                report_info.update({"sha256": actual_sha, "bytes": actual_bytes})
                if (
                    isinstance(expected_sha, str)
                    and _is_sha256(expected_sha)
                    and actual_sha.lower() != expected_sha.lower()
                ):
                    reasons.append("field_evidence_bundle report SHA-256 does not match")
                if type(expected_bytes) is int and actual_bytes != expected_bytes:
                    reasons.append("field_evidence_bundle report byte count does not match")

            try:
                verification = verify_field_evidence_bundle_report(
                    report_path,
                    require_gate_passed=False,
                    require_real_domain=True,
                    expected_release_label=expected_release_label,
                )
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                reasons.append(f"field_evidence_bundle verifier failed: {exc}")
                verification = None
            if verification is not None:
                result["verification"] = verification
                report_info.update(
                    {
                        "bundle_id": (
                            verification.get("report", {}).get("bundle_id")
                            if isinstance(verification.get("report"), Mapping)
                            else None
                        ),
                        "gate_passed": verification.get("gate_passed"),
                        "verifier_passed": verification.get("passed"),
                    }
                )
                if verification.get("passed") is not True:
                    for reason in verification.get("reasons", []):
                        if isinstance(reason, str) and reason not in reasons:
                            reasons.append(f"field_evidence_bundle: {reason}")
                report_document = verification.get("report")
                if not isinstance(report_document, Mapping):
                    reasons.append("field_evidence_bundle report must contain a JSON object")
                else:
                    for field in (
                        "evidence_type",
                        "schema_version",
                        "release_label",
                        "bundle_id",
                        "gate_passed",
                        "release_eligible",
                        "hardware_acceptance",
                        "real_flight_acceptance",
                        "software_only",
                        "read_only_evidence",
                        "advisory_only",
                        "ground_truth_isolated",
                        "contains_flight_control",
                        "contains_truth",
                    ):
                        if report_document.get(field) != raw_record.get(field):
                            reasons.append(f"field_evidence_bundle {field} does not match report")
                    report_checks = report_document.get("checks")
                    if isinstance(report_checks, Mapping):
                        for field in summary_fields:
                            if report_checks.get(field) != raw_record.get(field):
                                reasons.append(
                                    f"field_evidence_bundle {field} does not match report"
                                )
                    else:
                        reasons.append("field_evidence_bundle report checks must be an object")
                    report_strata = report_document.get("strata")
                    if isinstance(report_strata, Mapping):
                        for name in _FIELD_EVIDENCE_BUNDLE_STRATA:
                            entry = report_strata.get(name)
                            actual = (
                                entry.get("gate_passed") if isinstance(entry, Mapping) else None
                            )
                            if actual != raw_strata.get(name):
                                reasons.append(
                                    f"field_evidence_bundle strata.{name} does not match report"
                                )
                    else:
                        reasons.append("field_evidence_bundle report strata must be an object")
        result["report"] = report_info

    result["passed"] = not reasons
    return result


def _simulator_safety_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and re-run the deterministic SIM S4 safety evidence.

    The binding is optional for old prerelease manifests, but once supplied it
    is authoritative.  The source JSON and its verifier result are both
    checked so a copied ``gate_passed`` flag or stale check map cannot promote
    an altered safety artifact.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("simulator_safety record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("simulator_safety must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_SIMULATOR_SAFETY_KEYS))
    if unknown:
        reasons.append(f"unknown simulator_safety field(s): {unknown!r}")

    path_value = raw_record.get("evidence_path")
    evidence_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("simulator_safety.evidence_path is missing or empty")
    else:
        evidence_path = Path(path_value)
        if not evidence_path.is_absolute():
            evidence_path = manifest_directory / evidence_path
        try:
            evidence_path = evidence_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"simulator_safety evidence path cannot be resolved: {exc}")
            evidence_path = None

    expected_sha = raw_record.get("evidence_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("simulator_safety.evidence_sha256 is missing or invalid")
    expected_event = raw_record.get("event")
    if expected_event != "simulator_safety_acceptance_finished":
        reasons.append("simulator_safety.event must be simulator_safety_acceptance_finished")
    if raw_record.get("runtime_mode") != "sim":
        reasons.append("simulator_safety.runtime_mode must be 'sim'")
    expected_release = raw_record.get("release_label")
    if not isinstance(expected_release, str) or not expected_release.strip():
        reasons.append("simulator_safety.release_label is missing or empty")
    elif expected_release_label is not None and expected_release != expected_release_label:
        reasons.append("simulator_safety.release_label does not match the product manifest")
    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("simulator_safety.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("simulator_safety gate_passed is false")
    verifier_event = raw_record.get("verifier_event")
    if verifier_event != "simulator_safety_evidence_verified":
        reasons.append("simulator_safety.verifier_event must be simulator_safety_evidence_verified")
    verifier_exit = raw_record.get("verifier_exit_code")
    if type(verifier_exit) is not int or verifier_exit != 0:
        reasons.append("simulator_safety.verifier_exit_code must be zero")
    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("simulator_safety.checks must be an object")

    if evidence_path is not None:
        evidence_info: dict[str, Any] = {"path": str(evidence_path)}
        if not evidence_path.is_file():
            reasons.append("simulator_safety evidence file does not exist")
        else:
            try:
                actual_sha = _sha256(evidence_path)
            except OSError as exc:
                reasons.append(f"simulator_safety evidence cannot be hashed: {exc}")
                actual_sha = None
            if actual_sha is not None:
                evidence_info["sha256"] = actual_sha
                if (
                    isinstance(expected_sha, str)
                    and _is_sha256(expected_sha)
                    and actual_sha.lower() != expected_sha.lower()
                ):
                    reasons.append("simulator_safety evidence SHA-256 does not match")
            try:
                artifact = _load_object(evidence_path)
            except ValueError as exc:
                reasons.append(f"simulator_safety evidence JSON is invalid: {exc}")
                artifact = None
            if not isinstance(artifact, Mapping):
                reasons.append("simulator_safety evidence must contain a JSON object")
                artifact = {}
            evidence_info.update(
                {
                    "event": artifact.get("event"),
                    "runtime_mode": artifact.get("runtime_mode"),
                    "release_label": artifact.get("release_label"),
                    "gate_passed": artifact.get("gate_passed"),
                }
            )
            if artifact.get("event") != expected_event:
                reasons.append("simulator_safety evidence event does not match binding")
            if artifact.get("runtime_mode") != "sim":
                reasons.append("simulator_safety evidence runtime_mode is not 'sim'")
            if artifact.get("release_label") != expected_release:
                reasons.append("simulator_safety evidence release_label does not match binding")
            if (
                expected_release_label is not None
                and artifact.get("release_label") != expected_release_label
            ):
                reasons.append(
                    "simulator_safety evidence release_label does not match product manifest"
                )
            if artifact.get("gate_passed") is not True:
                reasons.append("simulator_safety evidence gate_passed is not true")
            try:
                verification = verify_simulator_safety_file(
                    evidence_path,
                    expected_release_label=expected_release_label,
                )
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                reasons.append(f"simulator_safety verifier failed: {exc}")
                verification = None
            if verification is not None:
                result["verification"] = verification
                evidence_info["verifier_event"] = verification.get("event")
                evidence_info["verifier_gate_passed"] = verification.get("gate_passed")
                evidence_info["verifier_checks"] = verification.get("checks")
                if verification.get("event") != "simulator_safety_evidence_verified":
                    reasons.append("simulator_safety verifier returned the wrong event")
                if verification.get("runtime_mode") != "sim":
                    reasons.append("simulator_safety verifier runtime_mode is not 'sim'")
                if verification.get("release_label") != expected_release:
                    reasons.append("simulator_safety verifier release_label does not match binding")
                if (
                    expected_release_label is not None
                    and verification.get("release_label") != expected_release_label
                ):
                    reasons.append(
                        "simulator_safety verifier release_label does not match product manifest"
                    )
                if verification.get("gate_passed") is not True:
                    reasons.append("simulator_safety verifier gate_passed is not true")
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str) and reason not in reasons:
                        reasons.append(f"simulator_safety: {reason}")
                verified_checks = verification.get("checks")
                if not isinstance(verified_checks, Mapping):
                    reasons.append("simulator_safety verifier checks must be an object")
                elif any(value is not True for value in verified_checks.values()):
                    reasons.append("simulator_safety verifier checks must all be true")
                if (
                    isinstance(declared_checks, Mapping)
                    and isinstance(verified_checks, Mapping)
                    and dict(declared_checks) != dict(verified_checks)
                ):
                    reasons.append("simulator_safety checks do not match verifier output")
            result["evidence"] = evidence_info

    result["passed"] = not reasons
    return result


def _actuator_safety_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and re-run the deterministic actuator safety software bench.

    The binding is optional for existing prerelease manifests, but once it is
    present it is authoritative.  Both the source artifact and every summary
    field in the manifest are checked so a copied ``gate_passed`` bit or a
    stale check map cannot promote an altered executioner contract.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("actuator_safety record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("actuator_safety must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_ACTUATOR_SAFETY_KEYS))
    if unknown:
        reasons.append(f"unknown actuator_safety field(s): {unknown!r}")

    path_value = raw_record.get("evidence_path")
    evidence_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("actuator_safety.evidence_path is missing or empty")
    else:
        evidence_path = Path(path_value)
        if not evidence_path.is_absolute():
            evidence_path = manifest_directory / evidence_path
        try:
            evidence_path = evidence_path.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"actuator_safety evidence path cannot be resolved: {exc}")
            evidence_path = None

    expected_sha = raw_record.get("evidence_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("actuator_safety.evidence_sha256 is missing or invalid")
    expected_event = raw_record.get("event")
    if expected_event != "actuator_safety_software_acceptance_finished":
        reasons.append("actuator_safety.event must be actuator_safety_software_acceptance_finished")
    if raw_record.get("runtime_mode") != "HIL":
        reasons.append("actuator_safety.runtime_mode must be 'HIL'")
    expected_release = raw_record.get("release_label")
    if not isinstance(expected_release, str) or not expected_release.strip():
        reasons.append("actuator_safety.release_label is missing or empty")
    elif expected_release_label is not None and expected_release != expected_release_label:
        reasons.append("actuator_safety.release_label does not match the product manifest")
    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("actuator_safety.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("actuator_safety gate_passed is false")
    if raw_record.get("software_only") is not True:
        reasons.append("actuator_safety.software_only must be true")
    if raw_record.get("physical_outputs_present") is not False:
        reasons.append("actuator_safety.physical_outputs_present must be false")
    if raw_record.get("propulsion_disconnected") != "not_applicable_in_software_fixture":
        reasons.append("actuator_safety.propulsion_disconnected must identify the software fixture")
    verifier_event = raw_record.get("verifier_event")
    if verifier_event != "actuator_safety_software_evidence_verified":
        reasons.append(
            "actuator_safety.verifier_event must be actuator_safety_software_evidence_verified"
        )
    verifier_exit = raw_record.get("verifier_exit_code")
    if type(verifier_exit) is not int or verifier_exit != 0:
        reasons.append("actuator_safety.verifier_exit_code must be zero")
    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("actuator_safety.checks must be an object")

    if evidence_path is not None:
        evidence_info: dict[str, Any] = {"path": str(evidence_path)}
        if not evidence_path.is_file():
            reasons.append("actuator_safety evidence file does not exist")
        else:
            try:
                actual_sha = _sha256(evidence_path)
            except OSError as exc:
                reasons.append(f"actuator_safety evidence cannot be hashed: {exc}")
                actual_sha = None
            if actual_sha is not None:
                evidence_info["sha256"] = actual_sha
                if (
                    isinstance(expected_sha, str)
                    and _is_sha256(expected_sha)
                    and actual_sha.lower() != expected_sha.lower()
                ):
                    reasons.append("actuator_safety evidence SHA-256 does not match")
            try:
                artifact = _load_object(evidence_path)
            except ValueError as exc:
                reasons.append(f"actuator_safety evidence JSON is invalid: {exc}")
                artifact = {}
            evidence_info.update(
                {
                    "event": artifact.get("event"),
                    "runtime_mode": artifact.get("runtime_mode"),
                    "release_label": artifact.get("release_label"),
                    "gate_passed": artifact.get("gate_passed"),
                    "software_only": artifact.get("software_only"),
                    "physical_outputs_present": artifact.get("physical_outputs_present"),
                    "propulsion_disconnected": artifact.get("propulsion_disconnected"),
                }
            )
            if artifact.get("event") != expected_event:
                reasons.append("actuator_safety evidence event does not match binding")
            if artifact.get("runtime_mode") != "HIL":
                reasons.append("actuator_safety evidence runtime_mode is not 'HIL'")
            if artifact.get("release_label") != expected_release:
                reasons.append("actuator_safety evidence release_label does not match binding")
            if (
                expected_release_label is not None
                and artifact.get("release_label") != expected_release_label
            ):
                reasons.append(
                    "actuator_safety evidence release_label does not match product manifest"
                )
            if artifact.get("gate_passed") is not True:
                reasons.append("actuator_safety evidence gate_passed is not true")
            if artifact.get("software_only") is not True:
                reasons.append("actuator_safety evidence software_only is not true")
            if artifact.get("physical_outputs_present") is not False:
                reasons.append("actuator_safety evidence physical_outputs_present is not false")
            if artifact.get("propulsion_disconnected") != "not_applicable_in_software_fixture":
                reasons.append(
                    "actuator_safety evidence propulsion_disconnected is not the software fixture"
                )
            try:
                verification = verify_actuator_safety_evidence(
                    artifact,
                    expected_release_label=expected_release_label,
                )
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                reasons.append(f"actuator_safety verifier failed: {exc}")
                verification = None
            if verification is not None:
                result["verification"] = verification
                evidence_info["verifier_event"] = verification.get("event")
                evidence_info["verifier_gate_passed"] = verification.get("gate_passed")
                evidence_info["verifier_checks"] = verification.get("checks")
                if verification.get("event") != "actuator_safety_software_evidence_verified":
                    reasons.append("actuator_safety verifier returned the wrong event")
                if verification.get("runtime_mode") != "HIL":
                    reasons.append("actuator_safety verifier runtime_mode is not 'HIL'")
                if verification.get("release_label") != expected_release:
                    reasons.append("actuator_safety verifier release_label does not match binding")
                if (
                    expected_release_label is not None
                    and verification.get("release_label") != expected_release_label
                ):
                    reasons.append(
                        "actuator_safety verifier release_label does not match product manifest"
                    )
                if verification.get("gate_passed") is not True:
                    reasons.append("actuator_safety verifier gate_passed is not true")
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str) and reason not in reasons:
                        reasons.append(f"actuator_safety: {reason}")
                verified_checks = verification.get("checks")
                if not isinstance(verified_checks, Mapping):
                    reasons.append("actuator_safety verifier checks must be an object")
                elif any(value is not True for value in verified_checks.values()):
                    reasons.append("actuator_safety verifier checks must all be true")
                if (
                    isinstance(declared_checks, Mapping)
                    and isinstance(verified_checks, Mapping)
                    and dict(declared_checks) != dict(verified_checks)
                ):
                    reasons.append("actuator_safety checks do not match verifier output")
            result["evidence"] = evidence_info

    result["passed"] = not reasons
    return result


def _simulator_scenario_matrix_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
    require_fault_coverage: bool = False,
) -> dict[str, Any]:
    """Re-hash and independently verify the three-airframe SIM matrix.

    The final release channel passes ``require_fault_coverage=True`` so an
    older nominal-only matrix cannot be promoted merely because its hashes are
    internally consistent.  Prerelease manifests retain the backward-
    compatible optional coverage policy.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("simulator_scenario_matrix record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("simulator_scenario_matrix must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_SIMULATOR_SCENARIO_MATRIX_KEYS))
    if unknown:
        reasons.append(f"unknown simulator_scenario_matrix field(s): {unknown!r}")

    path_value = raw_record.get("evidence_path")
    evidence_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("simulator_scenario_matrix.evidence_path is missing or empty")
    else:
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = manifest_directory / candidate
        try:
            evidence_path = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"simulator_scenario_matrix evidence path cannot be resolved: {exc}")

    expected_sha = raw_record.get("evidence_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("simulator_scenario_matrix.evidence_sha256 is missing or invalid")
    expected_bytes = raw_record.get("evidence_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("simulator_scenario_matrix.evidence_bytes must be a non-negative integer")

    expected_canonical_sha = raw_record.get("canonical_sha256")
    if not _is_sha256(expected_canonical_sha):
        reasons.append("simulator_scenario_matrix.canonical_sha256 is missing or invalid")
    if raw_record.get("schema_version") != 1:
        reasons.append("simulator_scenario_matrix.schema_version must be 1")
    if raw_record.get("event") != "simulator_scenario_matrix_finished":
        reasons.append("simulator_scenario_matrix.event must be simulator_scenario_matrix_finished")
    if raw_record.get("product") != "Multi-Detect":
        reasons.append("simulator_scenario_matrix.product must be 'Multi-Detect'")
    declared_release = raw_record.get("release_label")
    if not isinstance(declared_release, str) or not declared_release.strip():
        reasons.append("simulator_scenario_matrix.release_label is missing or empty")
    elif expected_release_label is not None and declared_release != expected_release_label:
        reasons.append(
            "simulator_scenario_matrix.release_label does not match the product manifest"
        )
    if raw_record.get("gate_passed") is not True:
        reasons.append("simulator_scenario_matrix.gate_passed must be true")
    if raw_record.get("deterministic_gate_passed") is not True:
        reasons.append("simulator_scenario_matrix.deterministic_gate_passed must be true")
    if raw_record.get("verifier_event") != "simulator_scenario_matrix_verified":
        reasons.append(
            "simulator_scenario_matrix.verifier_event must be simulator_scenario_matrix_verified"
        )
    if raw_record.get("verifier_exit_code") != 0:
        reasons.append("simulator_scenario_matrix.verifier_exit_code must be zero")
    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("simulator_scenario_matrix.checks must be an object")

    if evidence_path is None:
        result["passed"] = not reasons
        return result
    evidence_info: dict[str, Any] = {"path": str(evidence_path)}
    if not evidence_path.is_file():
        reasons.append("simulator_scenario_matrix evidence file does not exist")
        result["evidence"] = evidence_info
        result["passed"] = not reasons
        return result
    try:
        actual_sha = _sha256(evidence_path)
        actual_bytes = evidence_path.stat().st_size
    except OSError as exc:
        reasons.append(f"simulator_scenario_matrix evidence cannot be read: {exc}")
        result["evidence"] = evidence_info
        result["passed"] = not reasons
        return result
    evidence_info.update({"sha256": actual_sha, "bytes": actual_bytes})
    if _is_sha256(expected_sha) and actual_sha.lower() != str(expected_sha).lower():
        reasons.append("simulator_scenario_matrix evidence SHA-256 does not match")
    if type(expected_bytes) is int and actual_bytes != expected_bytes:
        reasons.append("simulator_scenario_matrix evidence byte count does not match")
    result["evidence"] = evidence_info

    try:
        artifact = _load_object(evidence_path)
    except ValueError as exc:
        reasons.append(f"simulator_scenario_matrix evidence JSON is invalid: {exc}")
        result["passed"] = not reasons
        return result

    scalar_fields = (
        "schema_version",
        "event",
        "product",
        "release_label",
        "gate_passed",
        "deterministic_gate_passed",
        "scenario_count",
        "enable_ai",
        "arm_on_start",
        "repeatability_checked",
        "truth_loaded",
        "truth_free",
    )
    for field in scalar_fields:
        if artifact.get(field) != raw_record.get(field):
            reasons.append(f"simulator_scenario_matrix evidence {field} does not match binding")
    for field in ("scenario_ids", "modes", "required_modes"):
        if artifact.get(field) != raw_record.get(field):
            reasons.append(f"simulator_scenario_matrix evidence {field} does not match binding")
    artifact_claims = artifact.get("claims")
    declared_claims = raw_record.get("claims")
    if not isinstance(artifact_claims, Mapping) or not isinstance(declared_claims, Mapping):
        reasons.append("simulator_scenario_matrix claims must be objects")
    else:
        for field in (
            "shared_truth_free_scenario_runner",
            "shared_ai_authority_watchdog_path",
            "all_selected_scenarios_passed",
        ):
            if artifact_claims.get(field) != declared_claims.get(field):
                reasons.append(
                    f"simulator_scenario_matrix evidence claims.{field} does not match binding"
                )
    artifact_canonical_sha = artifact.get("sha256")
    if not _is_sha256(artifact_canonical_sha):
        reasons.append("simulator_scenario_matrix evidence sha256 is missing or invalid")
    elif (
        _is_sha256(expected_canonical_sha)
        and str(artifact_canonical_sha).lower() != str(expected_canonical_sha).lower()
    ):
        reasons.append("simulator_scenario_matrix canonical SHA-256 does not match binding")

    try:
        verification = verify_simulator_scenario_matrix_file(
            evidence_path,
            expected_release_label=expected_release_label,
            require_all_airframes=True,
            require_fault_coverage=require_fault_coverage,
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        reasons.append(f"simulator_scenario_matrix verifier failed: {exc}")
        verification = None
    if verification is not None:
        result["verification"] = verification
        if verification.get("event") != "simulator_scenario_matrix_verified":
            reasons.append("simulator_scenario_matrix verifier returned the wrong event")
        if verification.get("gate_passed") is not True:
            verifier_reasons = verification.get("reasons", [])
            if isinstance(verifier_reasons, list):
                reasons.extend(
                    f"simulator_scenario_matrix: {reason}"
                    for reason in verifier_reasons
                    if isinstance(reason, str) and reason
                )
            else:
                reasons.append("simulator_scenario_matrix verifier gate_passed is not true")
        if (
            _is_sha256(expected_canonical_sha)
            and verification.get("recomputed_sha256") != str(expected_canonical_sha).lower()
        ):
            reasons.append("simulator_scenario_matrix canonical digest does not match verifier")
        verified_checks = verification.get("checks")
        if not isinstance(verified_checks, Mapping):
            reasons.append("simulator_scenario_matrix verifier checks must be an object")
        elif any(value is not True for value in verified_checks.values()):
            reasons.append("simulator_scenario_matrix verifier checks must all be true")
        if (
            isinstance(declared_checks, Mapping)
            and isinstance(verified_checks, Mapping)
            and dict(declared_checks) != dict(verified_checks)
        ):
            reasons.append("simulator_scenario_matrix checks do not match verifier output")

    result["passed"] = not reasons
    return result


def _simulator_combined_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and re-verify the combined Windows SIM acceptance binding.

    The four-mode E7 manifest historically only bound a small SIM envelope.
    The combined PX4/Gazebo/QGC runner has a second, stricter contract for
    dynamic loopback ports and software-only claims.  When this optional
    binding is present it is authoritative on prereleases; a final release
    additionally requires it to be present and passing.
    """

    # Keep the combined verifier lazy.  The package root imports this module
    # for the normal release-gate API; importing it eagerly would make
    # ``python -m multidetect.simulator_combined_evidence`` see its own module
    # in ``sys.modules`` before runpy executes it.
    from .simulator_combined_evidence import verify_file as verify_combined_sim_file

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "release_mode": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("simulator_combined record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("simulator_combined must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_SIMULATOR_COMBINED_KEYS))
    if unknown:
        reasons.append(f"unknown simulator_combined field(s): {unknown!r}")

    def resolve_record_path(field: str) -> Path | None:
        value = raw_record.get(field)
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"simulator_combined.{field} is missing or empty")
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = manifest_directory / candidate
        try:
            return candidate.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"simulator_combined {field} cannot be resolved: {exc}")
            return None

    evidence_path = resolve_record_path("evidence_path")
    release_mode_path = resolve_record_path("release_mode_path")
    expected_sha = raw_record.get("evidence_sha256")
    expected_release_mode_sha = raw_record.get("release_mode_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("simulator_combined.evidence_sha256 is missing or invalid")
    if not _is_sha256(expected_release_mode_sha):
        reasons.append("simulator_combined.release_mode_sha256 is missing or invalid")
    if (
        evidence_path is not None
        and release_mode_path is not None
        and evidence_path == release_mode_path
    ):
        reasons.append("simulator_combined evidence and release-mode paths must be different files")

    if raw_record.get("runtime_mode") != "sim":
        reasons.append("simulator_combined.runtime_mode must be 'sim'")
    declared_release = raw_record.get("release_label")
    if not isinstance(declared_release, str) or not declared_release.strip():
        reasons.append("simulator_combined.release_label is missing or empty")
    elif expected_release_label is not None and declared_release != expected_release_label:
        reasons.append("simulator_combined.release_label does not match the product manifest")
    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("simulator_combined.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("simulator_combined gate_passed is false")
    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("simulator_combined.checks must be an object")

    if evidence_path is not None:
        evidence_info: dict[str, Any] = {"path": str(evidence_path)}
        if not evidence_path.is_file():
            reasons.append("simulator_combined evidence file does not exist")
        else:
            actual_sha = _sha256(evidence_path)
            evidence_info.update({"sha256": actual_sha, "bytes": evidence_path.stat().st_size})
            if (
                isinstance(expected_sha, str)
                and _is_sha256(expected_sha)
                and actual_sha.lower() != expected_sha.lower()
            ):
                reasons.append("simulator_combined evidence SHA-256 does not match")
            result["evidence"] = evidence_info

    if release_mode_path is not None:
        release_mode_info: dict[str, Any] = {"path": str(release_mode_path)}
        if not release_mode_path.is_file():
            reasons.append("simulator_combined release-mode evidence file does not exist")
        else:
            actual_release_mode_sha = _sha256(release_mode_path)
            release_mode_info.update(
                {"sha256": actual_release_mode_sha, "bytes": release_mode_path.stat().st_size}
            )
            if (
                isinstance(expected_release_mode_sha, str)
                and _is_sha256(expected_release_mode_sha)
                and actual_release_mode_sha.lower() != expected_release_mode_sha.lower()
            ):
                reasons.append("simulator_combined release-mode evidence SHA-256 does not match")
            result["release_mode"] = release_mode_info

    if (
        evidence_path is not None
        and release_mode_path is not None
        and evidence_path.is_file()
        and release_mode_path.is_file()
    ):
        verification = verify_combined_sim_file(
            evidence_path,
            expected_release_label=expected_release_label,
            release_mode_evidence_path=release_mode_path,
            require_release_mode=True,
        )
        result["verification"] = verification
        if verification.get("gate_passed") is not True:
            for reason in verification.get("reasons", []):
                if isinstance(reason, str):
                    reasons.append(f"simulator_combined: {reason}")
        if verification.get("runtime_mode") != "sim":
            reasons.append("simulator_combined verifier runtime_mode must be 'sim'")
        if (
            expected_release_label is not None
            and verification.get("release_label") != expected_release_label
        ):
            reasons.append(
                "simulator_combined verifier release_label does not match the product manifest"
            )
        verified_checks = verification.get("checks")
        if not isinstance(verified_checks, Mapping):
            reasons.append("simulator_combined verifier checks must be an object")
        elif any(value is not True for value in verified_checks.values()):
            reasons.append("simulator_combined verifier checks must all be true")
        if isinstance(declared_checks, Mapping) and isinstance(verified_checks, Mapping):
            if dict(declared_checks) != dict(verified_checks):
                reasons.append("simulator_combined checks do not match verifier output")

    result["passed"] = not reasons
    return result


def _simulator_external_evidence_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
    expected_parity: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Re-hash and re-verify the six-source external SIM binding.

    The external Docker/live-AI join is intentionally independent from the
    native combined-QGC envelope.  This evaluator keeps both seams strict:
    the stored join and revalidation files must match their hashes, the
    revalidator must recompute all six source files, and the join parity must
    equal the parity shared by the four release-gate mode records.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "join": None,
        "revalidation": None,
        "parity": None,
        "source_hashes": None,
        "checks": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("simulator_external_evidence record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("simulator_external_evidence must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_SIMULATOR_EXTERNAL_EVIDENCE_KEYS))
    if unknown:
        reasons.append(f"unknown simulator_external_evidence field(s): {unknown!r}")

    def resolve_record_path(field: str) -> Path | None:
        value = raw_record.get(field)
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"simulator_external_evidence.{field} is missing or empty")
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = manifest_directory / candidate
        try:
            return candidate.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"simulator_external_evidence {field} cannot be resolved: {exc}")
            return None

    join_path = resolve_record_path("join_path")
    revalidation_path = resolve_record_path("revalidation_path")
    if join_path is not None and revalidation_path is not None and join_path == revalidation_path:
        reasons.append("simulator_external_evidence join and revalidation must be different files")

    declared_join_sha = raw_record.get("join_sha256")
    declared_revalidation_sha = raw_record.get("revalidation_sha256")
    if not _is_sha256(declared_join_sha):
        reasons.append("simulator_external_evidence.join_sha256 is missing or invalid")
    if not _is_sha256(declared_revalidation_sha):
        reasons.append("simulator_external_evidence.revalidation_sha256 is missing or invalid")
    declared_join_bytes = raw_record.get("join_bytes")
    declared_revalidation_bytes = raw_record.get("revalidation_bytes")
    if type(declared_join_bytes) is not int or declared_join_bytes < 1:
        reasons.append("simulator_external_evidence.join_bytes must be a positive integer")
    if type(declared_revalidation_bytes) is not int or declared_revalidation_bytes < 1:
        reasons.append("simulator_external_evidence.revalidation_bytes must be a positive integer")

    declared_release = raw_record.get("release_label")
    if not isinstance(declared_release, str) or not declared_release.strip():
        reasons.append("simulator_external_evidence.release_label is missing or empty")
    elif expected_release_label is not None and declared_release != expected_release_label:
        reasons.append(
            "simulator_external_evidence.release_label does not match the product manifest"
        )
    if raw_record.get("gate_passed") is not True:
        reasons.append("simulator_external_evidence gate_passed must be true")
    if raw_record.get("source_recomputed") is not True:
        reasons.append("simulator_external_evidence source_recomputed must be true")

    join_document: dict[str, Any] | None = None
    join_info: dict[str, Any] | None = None
    if join_path is not None:
        join_info = {"path": str(join_path)}
        if not join_path.is_file():
            reasons.append("simulator_external_evidence join file does not exist")
        else:
            actual_sha = _sha256(join_path)
            actual_bytes = join_path.stat().st_size
            join_info.update({"sha256": actual_sha, "bytes": actual_bytes})
            if (
                _is_sha256(declared_join_sha)
                and actual_sha.lower() != str(declared_join_sha).lower()
            ):
                reasons.append("simulator_external_evidence join SHA-256 does not match")
            if type(declared_join_bytes) is int and actual_bytes != declared_join_bytes:
                reasons.append("simulator_external_evidence join byte count does not match")
            try:
                join_document = _load_object(join_path)
            except ValueError as exc:
                reasons.append(f"simulator_external_evidence join cannot be loaded: {exc}")
        result["join"] = join_info

    join_schema_valid = False
    join_event_valid = False
    join_release_label_matches = False
    join_gate_passed = False
    join_shared_boundaries_passed = False
    join_claims_passed = False
    join_parity_matches = False
    six_sources_distinct = False
    six_sources_hash_bound = False
    source_hashes_from_join: dict[str, dict[str, dict[str, Any]]] = {}
    join_parity: dict[str, Any] | None = None

    if join_document is not None:
        join_schema_valid = join_document.get("schema_version") == 1
        join_event_valid = (
            join_document.get("event") == "simulator_external_runtime_ai_evidence_verified"
        )
        join_release_label_matches = (
            isinstance(expected_release_label, str)
            and join_document.get("release_label") == expected_release_label
        )
        join_gate_passed = join_document.get("gate_passed") is True
        shared = join_document.get("shared")
        join_shared_boundaries_passed = (
            isinstance(shared, Mapping)
            and all(
                shared.get(field) is True
                for field in (
                    "image_digest_consistent",
                    "release_label_consistent",
                    "truth_free_perception",
                    "hardware_control_disabled",
                )
            )
            and all(
                shared.get(field) == "not_claimed" for field in ("visual_accuracy", "real_hardware")
            )
        )
        claims = join_document.get("claims")
        join_claims_passed = isinstance(claims, Mapping) and claims == {
            "external_runtime_gate": "passed",
            "ai_target_policy_gate": "passed",
            "hardware_acceptance": "not_claimed",
            "operator_visual_acceptance": "not_claimed",
            "truth_free": True,
        }

        parity_document = join_document.get("parity")
        if isinstance(parity_document, Mapping):
            parity_hashes = parity_document.get("hashes")
            parity_path_value = parity_document.get("path")
            if isinstance(parity_hashes, Mapping) and isinstance(parity_path_value, str):
                try:
                    normalized_hashes = {
                        field: str(parity_hashes[field]).lower() for field in PARITY_FIELDS
                    }
                except (KeyError, TypeError):
                    normalized_hashes = {}
                join_parity_path = Path(parity_path_value).expanduser()
                if not join_parity_path.is_absolute() and join_path is not None:
                    join_parity_path = join_path.parent / join_parity_path
                try:
                    join_parity_path = join_parity_path.resolve()
                except (OSError, RuntimeError):
                    join_parity_path = Path()
                parity_file_ok = join_parity_path.is_file()
                actual_parity_sha = _sha256(join_parity_path) if parity_file_ok else None
                parity_bytes = join_parity_path.stat().st_size if parity_file_ok else None
                parity_file_ok = parity_file_ok and (
                    isinstance(parity_document.get("sha256"), str)
                    and actual_parity_sha is not None
                    and actual_parity_sha.lower() == parity_document["sha256"].lower()
                    and type(parity_document.get("bytes")) is int
                    and parity_bytes == parity_document.get("bytes")
                )
                join_parity = {
                    "path": str(join_parity_path),
                    "sha256": actual_parity_sha,
                    "bytes": parity_bytes,
                    "release_label": parity_document.get("release_label"),
                    "hashes": normalized_hashes,
                }
                join_parity_matches = (
                    parity_file_ok
                    and parity_document.get("release_label") == expected_release_label
                    and set(normalized_hashes) == set(PARITY_FIELDS)
                    and (
                        expected_parity is None
                        or normalized_hashes
                        == {field: str(expected_parity[field]).lower() for field in PARITY_FIELDS}
                    )
                )
        result["parity"] = join_parity

        vehicles = join_document.get("vehicles")
        if isinstance(vehicles, Mapping) and set(vehicles) == set(EXTERNAL_REQUIRED_VEHICLES):
            all_paths: set[Path] = set()
            source_shape_valid = True
            for vehicle in EXTERNAL_REQUIRED_VEHICLES:
                pair = vehicles.get(vehicle)
                if not isinstance(pair, Mapping):
                    source_shape_valid = False
                    continue
                source_hashes_from_join[vehicle] = {}
                for role in ("runtime", "ai"):
                    source = pair.get(role)
                    if not isinstance(source, Mapping):
                        source_shape_valid = False
                        continue
                    source_path_value = source.get("path")
                    if not isinstance(source_path_value, str) or join_path is None:
                        source_shape_valid = False
                        continue
                    source_path = Path(source_path_value).expanduser()
                    if not source_path.is_absolute():
                        source_path = join_path.parent / source_path
                    try:
                        source_path = source_path.resolve()
                    except (OSError, RuntimeError):
                        source_shape_valid = False
                        continue
                    all_paths.add(source_path)
                    source_sha = source.get("sha256")
                    source_bytes = source.get("bytes")
                    source_hashes_from_join[vehicle][role] = {
                        "sha256": source_sha,
                        "bytes": source_bytes,
                    }
                    if (
                        not source_path.is_file()
                        or not _is_sha256(source_sha)
                        or type(source_bytes) is not int
                        or _sha256(source_path).lower() != str(source_sha).lower()
                        or source_path.stat().st_size != source_bytes
                        or source.get("gate_passed") is not True
                        or (
                            role == "ai"
                            and any(
                                source.get(field) is not True
                                for field in (
                                    "ai_policy_gate_passed",
                                    "target_policy_gate_passed",
                                    "target_exercise_accepted",
                                )
                            )
                        )
                    ):
                        source_shape_valid = False
            six_sources_distinct = len(all_paths) == 6
            six_sources_hash_bound = source_shape_valid and len(source_hashes_from_join) == 3
        result["source_hashes"] = source_hashes_from_join or None

    declared_parity = raw_record.get("parity")
    if not isinstance(declared_parity, Mapping):
        reasons.append("simulator_external_evidence.parity must be an object")
    else:
        unknown_parity = sorted(set(declared_parity).difference(_SIMULATOR_EXTERNAL_PARITY_KEYS))
        if unknown_parity:
            reasons.append(
                f"unknown simulator_external_evidence parity field(s): {unknown_parity!r}"
            )
        expected_join_parity = result.get("parity")
        parity_matches = False
        if isinstance(expected_join_parity, Mapping):
            declared_path = declared_parity.get("path")
            expected_path = expected_join_parity.get("path")
            declared_path_resolved: Path | None = None
            if isinstance(declared_path, str):
                candidate = Path(declared_path).expanduser()
                if not candidate.is_absolute():
                    candidate = manifest_directory / candidate
                try:
                    declared_path_resolved = candidate.resolve()
                except (OSError, RuntimeError):
                    declared_path_resolved = None
            expected_path_resolved: Path | None = None
            if isinstance(expected_path, str):
                try:
                    expected_path_resolved = Path(expected_path).resolve()
                except (OSError, RuntimeError):
                    expected_path_resolved = None
            parity_matches = (
                declared_path_resolved is not None
                and expected_path_resolved is not None
                and declared_path_resolved == expected_path_resolved
                and all(
                    declared_parity.get(field) == expected_join_parity.get(field)
                    for field in ("sha256", "bytes", "release_label", "hashes")
                )
            )
        if not parity_matches:
            reasons.append("simulator_external_evidence parity does not match the join")

    declared_sources = raw_record.get("source_hashes")
    if not isinstance(declared_sources, Mapping):
        reasons.append("simulator_external_evidence.source_hashes must be an object")
    elif declared_sources != source_hashes_from_join:
        reasons.append("simulator_external_evidence source hashes do not match the join")

    verification: dict[str, Any] | None = None
    if join_path is not None and join_path.is_file():
        try:
            verification = revalidate_simulator_external_evidence(
                join_path,
                expected_release_label=expected_release_label,
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            reasons.append(f"simulator_external_evidence revalidation failed: {exc}")
        else:
            result["verification"] = verification
            if verification.get("source_recomputed") is not True:
                reasons.append(
                    "simulator_external_evidence revalidation source_recomputed is not true"
                )
            if verification.get("gate_passed") is not True:
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str):
                        reasons.append(f"simulator_external_evidence: {reason}")

    revalidation_info: dict[str, Any] | None = None
    revalidation_document: dict[str, Any] | None = None
    if revalidation_path is not None:
        revalidation_info = {"path": str(revalidation_path)}
        if not revalidation_path.is_file():
            reasons.append("simulator_external_evidence revalidation file does not exist")
        else:
            actual_sha = _sha256(revalidation_path)
            actual_bytes = revalidation_path.stat().st_size
            revalidation_info.update({"sha256": actual_sha, "bytes": actual_bytes})
            if (
                _is_sha256(declared_revalidation_sha)
                and actual_sha.lower() != str(declared_revalidation_sha).lower()
            ):
                reasons.append("simulator_external_evidence revalidation SHA-256 does not match")
            if (
                type(declared_revalidation_bytes) is int
                and actual_bytes != declared_revalidation_bytes
            ):
                reasons.append("simulator_external_evidence revalidation byte count does not match")
            try:
                revalidation_document = _load_object(revalidation_path)
            except ValueError as exc:
                reasons.append(f"simulator_external_evidence revalidation cannot be loaded: {exc}")
        result["revalidation"] = revalidation_info

    revalidation_schema_valid = False
    revalidation_join_matches = False
    revalidation_source_recomputed = False
    revalidation_gate_passed = False
    if revalidation_document is not None:
        revalidation_schema_valid = (
            revalidation_document.get("schema_version") == 1
            and revalidation_document.get("event")
            == "simulator_external_runtime_ai_evidence_revalidated"
        )
        report_path_value = revalidation_document.get("report_path")
        bound_report_path: Path | None = None
        if isinstance(report_path_value, str) and revalidation_path is not None:
            bound_report_path = Path(report_path_value).expanduser()
            if not bound_report_path.is_absolute():
                bound_report_path = revalidation_path.parent / bound_report_path
            try:
                bound_report_path = bound_report_path.resolve()
            except (OSError, RuntimeError):
                bound_report_path = None
        revalidation_join_matches = (
            join_path is not None
            and bound_report_path == join_path
            and revalidation_document.get("report_sha256") == raw_record.get("join_sha256")
            and revalidation_document.get("report_bytes") == raw_record.get("join_bytes")
        )
        revalidation_source_recomputed = revalidation_document.get("source_recomputed") is True
        revalidation_gate_passed = (
            revalidation_document.get("gate_passed") is True
            and revalidation_document.get("release_label") == expected_release_label
            and revalidation_document.get("expected_release_label") == expected_release_label
            and revalidation_document.get("reasons") == []
        )
        if verification is not None:
            expected_revalidation_fields = {
                "schema_version": verification.get("schema_version"),
                "event": verification.get("event"),
                "report_path": verification.get("report_path"),
                "report_sha256": verification.get("report_sha256"),
                "report_bytes": verification.get("report_bytes"),
                "source_recomputed": verification.get("source_recomputed"),
                "release_label": verification.get("release_label"),
                "expected_release_label": verification.get("expected_release_label"),
                "gate_passed": verification.get("gate_passed"),
                "reasons": verification.get("reasons"),
            }
            actual_revalidation_fields = {
                field: revalidation_document.get(field) for field in expected_revalidation_fields
            }
            if actual_revalidation_fields != expected_revalidation_fields:
                reasons.append(
                    "simulator_external_evidence revalidation summary does not match recomputation"
                )

    checks = {
        "join_schema_valid": join_schema_valid,
        "join_event_valid": join_event_valid,
        "join_release_label_matches": join_release_label_matches,
        "join_gate_passed": join_gate_passed,
        "join_shared_boundaries_passed": join_shared_boundaries_passed,
        "join_claims_passed": join_claims_passed,
        "join_parity_matches": join_parity_matches,
        "six_sources_distinct": six_sources_distinct,
        "six_sources_hash_bound": six_sources_hash_bound,
        "revalidation_schema_valid": revalidation_schema_valid,
        "revalidation_join_matches": revalidation_join_matches,
        "revalidation_source_recomputed": revalidation_source_recomputed,
        "revalidation_gate_passed": revalidation_gate_passed,
    }
    result["checks"] = checks
    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("simulator_external_evidence.checks must be an object")
    else:
        unknown_checks = sorted(set(declared_checks).difference(_SIMULATOR_EXTERNAL_CHECKS))
        missing_checks = sorted(set(_SIMULATOR_EXTERNAL_CHECKS).difference(declared_checks))
        if unknown_checks:
            reasons.append(f"unknown simulator_external_evidence check(s): {unknown_checks!r}")
        if missing_checks:
            reasons.append(f"missing simulator_external_evidence check(s): {missing_checks!r}")
        if dict(declared_checks) != checks:
            reasons.append("simulator_external_evidence checks do not match recomputed checks")
    if any(value is not True for value in checks.values()):
        reasons.append("simulator_external_evidence checks must all be true")

    result["passed"] = not reasons and raw_record.get("gate_passed") is True
    return result


def _simulator_3d_control_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and independently verify the three 3-D control records.

    The runner emits one record per airframe.  This optional aggregate binding
    is authoritative whenever supplied and is required for the final channel,
    but it intentionally treats external PX4/Gazebo execution as a separate
    claim.  A deterministic fixture cannot promote itself to hardware evidence
    by copying a ``gate_passed`` flag.
    """

    # Import lazily so ``python -m multidetect.simulator.three_d_control`` can
    # execute its own CLI without the package root preloading the same module
    # through the release-gate aggregator (which would trigger runpy's
    # duplicate-module warning).
    from .simulator.three_d_control import (
        verify_file as verify_simulator_3d_control_file,
    )

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": [],
        "verification": [],
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("simulator_3d_control record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("simulator_3d_control must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_SIMULATOR_3D_CONTROL_KEYS))
    if unknown:
        reasons.append(f"unknown simulator_3d_control field(s): {unknown!r}")
    if raw_record.get("schema_version") != 1:
        reasons.append("simulator_3d_control.schema_version must be 1")
    if raw_record.get("gate_passed") is not True:
        reasons.append("simulator_3d_control.gate_passed must be true")
    if raw_record.get("deterministic_gate_passed") is not True:
        reasons.append("simulator_3d_control.deterministic_gate_passed must be true")
    if raw_record.get("external_runtime_gate_passed") is not False:
        reasons.append("simulator_3d_control.external_runtime_gate_passed must remain false")
    for field in ("external_runtime_acceptance", "visual_acceptance", "hardware_acceptance"):
        if raw_record.get(field) != "not_claimed":
            reasons.append(f"simulator_3d_control.{field} must remain not_claimed")

    raw_evidence = raw_record.get("evidence")
    if not isinstance(raw_evidence, list):
        reasons.append("simulator_3d_control.evidence must be an array")
        raw_evidence = []
    if len(raw_evidence) != 3:
        reasons.append("simulator_3d_control.evidence must contain exactly three records")
    declared_scenario_ids = raw_record.get("scenario_ids")
    declared_modes = raw_record.get("modes")
    if not isinstance(declared_scenario_ids, list):
        reasons.append("simulator_3d_control.scenario_ids must be an array")
        declared_scenario_ids = []
    if not isinstance(declared_modes, list):
        reasons.append("simulator_3d_control.modes must be an array")
        declared_modes = []
    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("simulator_3d_control.checks must be an object")

    seen_paths: set[Path] = set()
    seen_ids: set[str] = set()
    seen_modes: set[str] = set()
    evidence_info: list[dict[str, Any]] = []
    verification_info: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(raw_evidence):
        prefix = f"simulator_3d_control.evidence[{index}]"
        if not isinstance(raw_entry, Mapping):
            reasons.append(f"{prefix} must be an object")
            continue
        unknown_entry = sorted(set(raw_entry).difference(_SIMULATOR_3D_CONTROL_EVIDENCE_KEYS))
        if unknown_entry:
            reasons.append(f"unknown {prefix} field(s): {unknown_entry!r}")
        path_value = raw_entry.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            reasons.append(f"{prefix}.path is missing or empty")
            continue
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = manifest_directory / candidate
        try:
            evidence_path = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"{prefix}.path cannot be resolved: {exc}")
            continue
        if evidence_path in seen_paths:
            reasons.append(f"{prefix}.path is reused")
        seen_paths.add(evidence_path)
        expected_sha = raw_entry.get("sha256")
        expected_bytes = raw_entry.get("bytes")
        if not _is_sha256(expected_sha):
            reasons.append(f"{prefix}.sha256 must be a SHA-256 digest")
        if type(expected_bytes) is not int or expected_bytes < 0:
            reasons.append(f"{prefix}.bytes must be a non-negative integer")
        if not evidence_path.is_file():
            reasons.append(f"{prefix} evidence file does not exist")
            continue
        try:
            actual_sha = _sha256(evidence_path)
            actual_bytes = evidence_path.stat().st_size
        except OSError as exc:
            reasons.append(f"{prefix} evidence cannot be read: {exc}")
            continue
        info = {"path": str(evidence_path), "sha256": actual_sha, "bytes": actual_bytes}
        evidence_info.append(info)
        if _is_sha256(expected_sha) and actual_sha.lower() != str(expected_sha).lower():
            reasons.append(f"{prefix} evidence SHA-256 does not match")
        if type(expected_bytes) is int and actual_bytes != expected_bytes:
            reasons.append(f"{prefix} evidence byte count does not match")
        try:
            verification = verify_simulator_3d_control_file(
                evidence_path,
                expected_release_label=expected_release_label,
            )
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            reasons.append(f"{prefix} verifier failed: {exc}")
            continue
        verification_info.append(verification)
        if verification.get("gate_passed") is not True:
            verifier_reasons = verification.get("reasons", [])
            if isinstance(verifier_reasons, list):
                reasons.extend(
                    f"{prefix}: {reason}"
                    for reason in verifier_reasons
                    if isinstance(reason, str) and reason
                )
            else:
                reasons.append(f"{prefix} verifier gate_passed is not true")
        scenario_id = raw_entry.get("scenario_id")
        mode = raw_entry.get("mode")
        if not isinstance(scenario_id, str) or not scenario_id:
            reasons.append(f"{prefix}.scenario_id must be a non-empty string")
        elif scenario_id in seen_ids:
            reasons.append(f"duplicate simulator_3d_control scenario_id: {scenario_id}")
        else:
            seen_ids.add(scenario_id)
        if not isinstance(mode, str) or not mode:
            reasons.append(f"{prefix}.mode must be a non-empty string")
        elif mode in seen_modes:
            reasons.append(f"duplicate simulator_3d_control mode: {mode}")
        else:
            seen_modes.add(mode)
        if verification.get("scenario_id") != scenario_id:
            reasons.append(f"{prefix}.scenario_id does not match evidence")
        if verification.get("mode") != mode:
            reasons.append(f"{prefix}.mode does not match evidence")
        for field in ("gate_passed", "deterministic_gate_passed", "external_runtime_gate_passed"):
            if raw_entry.get(field) != _load_object(evidence_path).get(field):
                reasons.append(f"{prefix}.{field} does not match evidence")
        if raw_entry.get("stack_plan_sha256") != _load_object(evidence_path).get(
            "stack_plan_sha256"
        ):
            reasons.append(f"{prefix}.stack_plan_sha256 does not match evidence")

    expected_scenario_ids = sorted(value for value in seen_ids)
    expected_modes = sorted(value for value in seen_modes)
    if declared_scenario_ids != expected_scenario_ids:
        reasons.append("simulator_3d_control scenario_ids do not match evidence")
    if declared_modes != expected_modes:
        reasons.append("simulator_3d_control modes do not match evidence")

    computed_checks = {
        "evidence_count": len(raw_evidence) == 3,
        "all_airframes": expected_modes == ["fixed-wing", "multirotor", "vtol"],
        "unique_scenarios": len(seen_ids) == 3,
        "unique_modes": len(seen_modes) == 3,
        "all_gate_passed": all(
            isinstance(entry, Mapping) and entry.get("gate_passed") is True
            for entry in raw_evidence
        ),
        "all_deterministic_gate_passed": all(
            isinstance(entry, Mapping) and entry.get("deterministic_gate_passed") is True
            for entry in raw_evidence
        ),
        "external_runtime_not_claimed": all(
            isinstance(entry, Mapping) and entry.get("external_runtime_gate_passed") is False
            for entry in raw_evidence
        ),
        "records_hash_bound": all(
            isinstance(entry, Mapping)
            and _is_sha256(entry.get("sha256"))
            and _is_sha256(entry.get("stack_plan_sha256"))
            for entry in raw_evidence
        ),
    }
    if isinstance(declared_checks, Mapping) and dict(declared_checks) != computed_checks:
        reasons.append("simulator_3d_control checks do not match bound evidence")
    if any(value is not True for value in computed_checks.values()):
        reasons.append("simulator_3d_control computed checks must all be true")

    result["evidence"] = evidence_info
    result["verification"] = verification_info
    result["checks"] = computed_checks
    result["passed"] = not reasons
    return result


def _simulator_scene_stream_set_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-open and independently verify the three-airframe scene-stream set."""

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("simulator_scene_stream_set record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("simulator_scene_stream_set must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_SIMULATOR_SCENE_STREAM_SET_KEYS))
    if unknown:
        reasons.append(f"unknown simulator_scene_stream_set field(s): {unknown!r}")
    try:
        # Keep this import lazy for clean standalone scene-stream CLI execution.
        from .simulator.scene_stream_set import verify_binding

        verification = verify_binding(
            raw_record,
            manifest_directory=manifest_directory,
            expected_release_label=expected_release_label,
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        reasons.append(f"simulator_scene_stream_set verifier failed: {exc}")
        verification = None
    if verification is not None:
        result["verification"] = verification
        result["evidence"] = verification.get("evidence")
        result["checks"] = verification.get("checks")
        if verification.get("event") != "simulator_scene_stream_set_revalidated":
            reasons.append("simulator_scene_stream_set verifier returned the wrong event")
        if verification.get("gate_passed") is not True:
            verifier_reasons = verification.get("reasons", [])
            if isinstance(verifier_reasons, list):
                reasons.extend(
                    f"simulator_scene_stream_set: {reason}"
                    for reason in verifier_reasons
                    if isinstance(reason, str) and reason
                )
            else:
                reasons.append("simulator_scene_stream_set verifier gate_passed is not true")
    result["passed"] = not reasons and raw_record.get("gate_passed") is True
    return result


def _px4_reported_position_response_matrix_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and reproduce the three-airframe external PX4 motion matrix."""

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("px4_reported_position_response_matrix record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("px4_reported_position_response_matrix must be an object")
        return result
    unknown = sorted(set(raw_record).difference(_PX4_REPORTED_POSITION_RESPONSE_MATRIX_KEYS))
    if unknown:
        reasons.append(f"unknown px4_reported_position_response_matrix field(s): {unknown!r}")
    path_value = raw_record.get("evidence_path")
    evidence_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("px4_reported_position_response_matrix.evidence_path is missing or empty")
    else:
        evidence_path = Path(path_value)
        if not evidence_path.is_absolute():
            evidence_path = manifest_directory / evidence_path
        evidence_path = evidence_path.resolve()
    expected_sha = raw_record.get("evidence_sha256")
    expected_bytes = raw_record.get("evidence_bytes")
    if not _is_sha256(expected_sha):
        reasons.append("px4_reported_position_response_matrix.evidence_sha256 is invalid")
    if type(expected_bytes) is not int or expected_bytes < 1:
        reasons.append("px4_reported_position_response_matrix.evidence_bytes must be positive")
    expected_values = {
        "schema": "multidetect.px4-reported-position-response-matrix",
        "schema_version": 1,
        "gate_passed": True,
        "truth_free": True,
        "hardware_acceptance": "not_claimed",
        "hil_acceptance": "not_claimed",
        "real_acceptance": "not_claimed",
    }
    for field, expected in expected_values.items():
        if raw_record.get(field) != expected:
            reasons.append(f"px4_reported_position_response_matrix.{field} must be {expected!r}")
    release_label = raw_record.get("release_label")
    if expected_release_label is not None and release_label != expected_release_label:
        reasons.append("px4_reported_position_response_matrix.release_label does not match product")
    if not _is_sha256(raw_record.get("matrix_sha256")):
        reasons.append("px4_reported_position_response_matrix.matrix_sha256 is invalid")
    if raw_record.get("vehicle_classes") != ["fixed-wing", "multirotor", "vtol"]:
        reasons.append("px4_reported_position_response_matrix.vehicle_classes is incomplete")
    checks = raw_record.get("checks")
    if not isinstance(checks, Mapping) or any(value is not True for value in checks.values()):
        reasons.append("px4_reported_position_response_matrix.checks must all be true")
    if evidence_path is None or not evidence_path.is_file():
        reasons.append("px4_reported_position_response_matrix evidence file does not exist")
        return result
    actual_sha = _sha256(evidence_path)
    actual_bytes = evidence_path.stat().st_size
    result["evidence"] = {
        "path": str(evidence_path),
        "sha256": actual_sha,
        "bytes": actual_bytes,
    }
    if _is_sha256(expected_sha) and actual_sha.lower() != str(expected_sha).lower():
        reasons.append("px4_reported_position_response_matrix evidence SHA-256 mismatch")
    if type(expected_bytes) is int and actual_bytes != expected_bytes:
        reasons.append("px4_reported_position_response_matrix evidence byte count mismatch")
    try:
        from scripts.verify_px4_reported_position_response_matrix import verify_file

        verification = verify_file(evidence_path)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        reasons.append(f"px4_reported_position_response_matrix verifier failed: {exc}")
        verification = None
    if verification is not None:
        result["verification"] = verification
        if verification.get("gate_passed") is not True:
            reasons.extend(
                f"px4_reported_position_response_matrix: {reason}"
                for reason in verification.get("reasons", [])
                if isinstance(reason, str) and reason
            )
        if verification.get("matrix_sha256") != raw_record.get("matrix_sha256"):
            reasons.append("px4_reported_position_response_matrix digest does not match verifier")
        verified_checks = verification.get("checks")
        if not isinstance(verified_checks, Mapping) or dict(verified_checks) != dict(
            checks if isinstance(checks, Mapping) else {}
        ):
            reasons.append("px4_reported_position_response_matrix checks do not match verifier")
    result["passed"] = not reasons
    return result


def _px4_reported_position_response_campaign_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and reproduce PX4 response repeats and injected faults."""

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("px4_reported_position_response_campaign record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("px4_reported_position_response_campaign must be an object")
        return result
    unknown = sorted(set(raw_record).difference(_PX4_REPORTED_POSITION_RESPONSE_CAMPAIGN_KEYS))
    if unknown:
        reasons.append(f"unknown px4_reported_position_response_campaign field(s): {unknown!r}")
    path_value = raw_record.get("evidence_path")
    evidence_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("px4 response campaign evidence_path is missing or empty")
    else:
        evidence_path = Path(path_value)
        if not evidence_path.is_absolute():
            evidence_path = manifest_directory / evidence_path
        evidence_path = evidence_path.resolve()
    expected_sha = raw_record.get("evidence_sha256")
    expected_bytes = raw_record.get("evidence_bytes")
    if not _is_sha256(expected_sha):
        reasons.append("px4 response campaign evidence_sha256 is invalid")
    if type(expected_bytes) is not int or expected_bytes < 1:
        reasons.append("px4 response campaign evidence_bytes must be positive")
    expected_values = {
        "schema": "multidetect.px4-reported-position-response-campaign",
        "schema_version": 1,
        "gate_passed": True,
        "truth_free": True,
        "hardware_acceptance": "not_claimed",
        "hil_acceptance": "not_claimed",
        "real_acceptance": "not_claimed",
        "positive_run_count": 6,
        "fault_run_count": 9,
        "vehicle_classes": ["fixed-wing", "multirotor", "vtol"],
    }
    for field, expected in expected_values.items():
        if raw_record.get(field) != expected:
            reasons.append(f"px4 response campaign {field} must be {expected!r}")
    if (
        expected_release_label is not None
        and raw_record.get("release_label") != expected_release_label
    ):
        reasons.append("px4 response campaign release_label does not match product")
    if not _is_sha256(raw_record.get("campaign_sha256")):
        reasons.append("px4 response campaign campaign_sha256 is invalid")
    checks = raw_record.get("checks")
    if not isinstance(checks, Mapping) or any(value is not True for value in checks.values()):
        reasons.append("px4 response campaign checks must all be true")
    if evidence_path is None or not evidence_path.is_file():
        reasons.append("px4 response campaign evidence file does not exist")
        return result
    actual_sha = _sha256(evidence_path)
    actual_bytes = evidence_path.stat().st_size
    result["evidence"] = {
        "path": str(evidence_path),
        "sha256": actual_sha,
        "bytes": actual_bytes,
    }
    if _is_sha256(expected_sha) and actual_sha.lower() != str(expected_sha).lower():
        reasons.append("px4 response campaign evidence SHA-256 mismatch")
    if type(expected_bytes) is int and actual_bytes != expected_bytes:
        reasons.append("px4 response campaign evidence byte count mismatch")
    try:
        from scripts.verify_px4_reported_position_response_campaign import verify_file

        verification = verify_file(evidence_path)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        reasons.append(f"px4 response campaign verifier failed: {exc}")
        verification = None
    if verification is not None:
        result["verification"] = verification
        if verification.get("gate_passed") is not True:
            reasons.extend(
                f"px4_reported_position_response_campaign: {reason}"
                for reason in verification.get("reasons", [])
                if isinstance(reason, str) and reason
            )
        if verification.get("campaign_sha256") != raw_record.get("campaign_sha256"):
            reasons.append("px4 response campaign digest does not match verifier")
        verified_checks = verification.get("checks")
        if not isinstance(verified_checks, Mapping) or dict(verified_checks) != dict(
            checks if isinstance(checks, Mapping) else {}
        ):
            reasons.append("px4 response campaign checks do not match verifier")
    result["passed"] = not reasons
    return result


def _qgc_operator_visual_acceptance_result(
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Reopen the four-mode set of human QGC visual-review records."""

    reasons: list[str] = []
    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": reasons,
        "records": [],
    }
    if raw_record is None:
        reasons.append("qgc_operator_visual_acceptance record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("qgc_operator_visual_acceptance must be an object")
        return result
    unknown = sorted(set(raw_record).difference(_QGC_OPERATOR_VISUAL_ACCEPTANCE_KEYS))
    missing = sorted(_QGC_OPERATOR_VISUAL_ACCEPTANCE_KEYS.difference(raw_record))
    if unknown:
        reasons.append(f"unknown qgc_operator_visual_acceptance field(s): {unknown!r}")
    if missing:
        reasons.append(f"missing qgc_operator_visual_acceptance field(s): {missing!r}")
    if raw_record.get("schema") != "multidetect.qgc-operator-visual-acceptance-set":
        reasons.append("qgc_operator_visual_acceptance schema is invalid")
    if raw_record.get("schema_version") != 1:
        reasons.append("qgc_operator_visual_acceptance schema_version is invalid")

    required_modes = raw_record.get("required_modes")
    covered_modes = raw_record.get("covered_modes")
    if required_modes != list(REQUIRED_MODES):
        reasons.append("qgc_operator_visual_acceptance required_modes do not match E7")
    if not isinstance(covered_modes, list) or any(
        not isinstance(mode, str) for mode in covered_modes
    ):
        reasons.append("qgc_operator_visual_acceptance covered_modes must be strings")
        covered_modes = []

    raw_entries = raw_record.get("records")
    if not isinstance(raw_entries, list):
        reasons.append("qgc_operator_visual_acceptance records must be a list")
        raw_entries = []
    observed_modes: list[str] = []
    seen_paths: set[Path] = set()
    seen_machine_reports: set[str] = set()
    seen_screenshots: set[str] = set()
    entry_results: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_entries):
        entry_reasons: list[str] = []
        entry_result: dict[str, Any] = {
            "index": index,
            "passed": False,
            "reasons": entry_reasons,
        }
        if not isinstance(entry, Mapping):
            entry_reasons.append("entry must be an object")
            entry_results.append(entry_result)
            continue
        unknown_entry = sorted(set(entry).difference(_QGC_OPERATOR_VISUAL_ACCEPTANCE_ENTRY_KEYS))
        missing_entry = sorted(_QGC_OPERATOR_VISUAL_ACCEPTANCE_ENTRY_KEYS.difference(entry))
        if unknown_entry:
            entry_reasons.append(f"unknown field(s): {unknown_entry!r}")
        if missing_entry:
            entry_reasons.append(f"missing field(s): {missing_entry!r}")
        mode_value = entry.get("runtime_mode")
        mode = mode_value.strip().lower() if isinstance(mode_value, str) else ""
        entry_result["runtime_mode"] = mode_value
        if mode not in REQUIRED_MODES:
            entry_reasons.append("runtime_mode is invalid")
        elif mode in observed_modes:
            entry_reasons.append("runtime_mode is duplicated")
        else:
            observed_modes.append(mode)

        path_value = entry.get("path")
        evidence_path: Path | None = None
        if not isinstance(path_value, str) or not path_value.strip():
            entry_reasons.append("path is missing or empty")
        else:
            evidence_path = Path(path_value)
            if not evidence_path.is_absolute():
                evidence_path = manifest_directory / evidence_path
            if evidence_path.is_symlink():
                entry_reasons.append("evidence must not be a symbolic link")
            try:
                evidence_path = evidence_path.resolve(strict=True)
            except OSError as exc:
                entry_reasons.append(f"evidence path cannot be resolved: {exc}")
                evidence_path = None
        if evidence_path is not None:
            if evidence_path in seen_paths:
                entry_reasons.append("evidence path is duplicated")
            seen_paths.add(evidence_path)
        expected_sha = entry.get("sha256")
        expected_bytes = entry.get("bytes")
        if not _is_sha256(expected_sha):
            entry_reasons.append("sha256 is missing or invalid")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 1
        ):
            entry_reasons.append("bytes must be a positive integer")
        if not _is_sha256(entry.get("review_sha256")):
            entry_reasons.append("review_sha256 is missing or invalid")
        if not isinstance(entry.get("reviewer"), str) or not entry["reviewer"].strip():
            entry_reasons.append("reviewer is missing or empty")
        if (
            not isinstance(entry.get("reviewed_at_utc"), str)
            or not entry["reviewed_at_utc"].strip()
        ):
            entry_reasons.append("reviewed_at_utc is missing or empty")
        if not isinstance(entry.get("machine_run_id"), str) or not entry["machine_run_id"].strip():
            entry_reasons.append("machine_run_id is missing or empty")
        for identity_field in (
            "machine_report_sha256",
            "screenshot_sha256",
            "qgc_executable_sha256",
            "ui_contract_hash",
        ):
            if not _is_sha256(entry.get(identity_field)):
                entry_reasons.append(f"{identity_field} is missing or invalid")
        machine_report_identity = str(entry.get("machine_report_sha256", ""))
        screenshot_identity = str(entry.get("screenshot_sha256", ""))
        if machine_report_identity in seen_machine_reports:
            entry_reasons.append("machine report is reused across runtime modes")
        if screenshot_identity in seen_screenshots:
            entry_reasons.append("screenshot is reused across runtime modes")
        seen_machine_reports.add(machine_report_identity)
        seen_screenshots.add(screenshot_identity)
        if entry.get("gate_passed") is not True:
            entry_reasons.append("gate_passed is not true")

        if evidence_path is not None and evidence_path.is_file():
            raw = evidence_path.read_bytes()
            actual_sha = _sha256(evidence_path)
            entry_result["path"] = str(evidence_path)
            entry_result["sha256"] = actual_sha
            entry_result["bytes"] = len(raw)
            if isinstance(expected_sha, str) and actual_sha.lower() != expected_sha.lower():
                entry_reasons.append("evidence SHA-256 does not match")
            if isinstance(expected_bytes, int) and len(raw) != expected_bytes:
                entry_reasons.append("evidence byte count does not match")
            try:
                artifact = _load_object(evidence_path)
                verification = verify_qgc_visual_acceptance(evidence_path)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                entry_reasons.append(f"visual verifier failed: {exc}")
            else:
                if artifact.get("runtime_mode") != mode:
                    entry_reasons.append("runtime_mode does not match evidence")
                if artifact.get("review_sha256") != entry.get("review_sha256"):
                    entry_reasons.append("review_sha256 does not match evidence")
                positive = artifact.get("positive_visual_evidence")
                if not isinstance(positive, Mapping):
                    entry_reasons.append("positive_visual_evidence is unavailable")
                else:
                    bound_fields = {
                        "machine_run_id": ("machine_report", "run_id"),
                        "machine_report_sha256": ("machine_report", "sha256"),
                        "screenshot_sha256": ("screenshot", "sha256"),
                        "qgc_executable_sha256": ("qgc_executable", "sha256"),
                        "ui_contract_hash": ("ui_contract", "ui_contract_hash"),
                    }
                    for field, (section_name, value_name) in bound_fields.items():
                        section = positive.get(section_name)
                        actual = section.get(value_name) if isinstance(section, Mapping) else None
                        if actual != entry.get(field):
                            entry_reasons.append(f"{field} does not match evidence")
                review = artifact.get("review")
                if not isinstance(review, Mapping):
                    entry_reasons.append("evidence review is unavailable")
                else:
                    if review.get("reviewer") != entry.get("reviewer"):
                        entry_reasons.append("reviewer does not match evidence")
                    if review.get("reviewed_at_utc") != entry.get("reviewed_at_utc"):
                        entry_reasons.append("reviewed_at_utc does not match evidence")
                if verification.get("gate_passed") is not True:
                    entry_reasons.extend(
                        f"visual verifier: {reason}" for reason in verification.get("reasons", [])
                    )
        elif evidence_path is not None:
            entry_reasons.append("evidence file does not exist")
        entry_result["passed"] = not entry_reasons
        entry_results.append(entry_result)
        reasons.extend(f"records[{index}]: {reason}" for reason in entry_reasons)

    if observed_modes != list(covered_modes):
        reasons.append("covered_modes do not match record order")
    complete = observed_modes == list(REQUIRED_MODES)
    if raw_record.get("complete") is not complete:
        reasons.append("complete does not match reproduced mode coverage")
    if raw_record.get("gate_passed") is not complete:
        reasons.append("gate_passed does not match reproduced mode coverage")
    if not complete:
        reasons.append("all four runtime-mode visual reviews are required")
    result.update(
        {
            "records": entry_results,
            "covered_modes": observed_modes,
            "required_modes": list(REQUIRED_MODES),
            "complete": complete,
            "gate_passed": raw_record.get("gate_passed"),
            "passed": not reasons,
        }
    )
    return result


def _px4_airframe_transaction_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-hash and re-open the shared three-airframe Mission transaction.

    The binding is intentionally separate from the four mode records.  Those
    records prove parity hashes and per-mode evidence, while this artifact
    proves that VTOL, fixed-wing and multirotor all traversed the same
    journalized ``MISSION_ITEM_INT`` transaction.  A present binding is
    authoritative on prereleases; the final release channel additionally
    requires it to be present and passing.
    """

    # Keep the import lazy for the same reason as the combined-SIM verifier:
    # the package root exposes the release-gate API and the acceptance module
    # imports mission primitives that should not be initialized by every
    # ordinary ``multidetect.release_gate`` import.
    from .px4_mission_acceptance import (
        verify_px4_mission_transaction_acceptance,
    )

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "evidence": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("px4_airframe_transaction record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("px4_airframe_transaction must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_PX4_AIRFRAME_TRANSACTION_KEYS))
    if unknown:
        reasons.append(f"unknown px4_airframe_transaction field(s): {unknown!r}")

    def resolve_evidence_path() -> Path | None:
        value = raw_record.get("evidence_path")
        if not isinstance(value, str) or not value.strip():
            reasons.append("px4_airframe_transaction.evidence_path is missing or empty")
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = manifest_directory / candidate
        try:
            return candidate.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"px4_airframe_transaction evidence_path cannot be resolved: {exc}")
            return None

    evidence_path = resolve_evidence_path()
    expected_sha = raw_record.get("evidence_sha256")
    if not _is_sha256(expected_sha):
        reasons.append("px4_airframe_transaction.evidence_sha256 is missing or invalid")
    expected_bytes = raw_record.get("evidence_bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        reasons.append("px4_airframe_transaction.evidence_bytes must be a non-negative integer")

    if raw_record.get("schema") != "multidetect.px4-airframe-mission-transaction-acceptance":
        reasons.append(
            "px4_airframe_transaction.schema is not the shared Mission acceptance schema"
        )
    if raw_record.get("schema_version") != 1:
        reasons.append("px4_airframe_transaction.schema_version must be 1")
    runtime_mode = raw_record.get("runtime_mode")
    if not isinstance(runtime_mode, str) or runtime_mode.strip().lower() != "sim":
        reasons.append("px4_airframe_transaction.runtime_mode must be 'sim'")
    declared_release = raw_record.get("release_label")
    if not isinstance(declared_release, str) or not declared_release.strip():
        reasons.append("px4_airframe_transaction.release_label is missing or empty")
    elif expected_release_label is not None and declared_release != expected_release_label:
        reasons.append("px4_airframe_transaction.release_label does not match the product manifest")
    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("px4_airframe_transaction.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("px4_airframe_transaction.gate_passed is false")
    if raw_record.get("shared_dispatcher") != "compile_executable_mission":
        reasons.append(
            "px4_airframe_transaction.shared_dispatcher is not compile_executable_mission"
        )
    if raw_record.get("shared_transport") != "in_process_mission_item_int":
        reasons.append(
            "px4_airframe_transaction.shared_transport is not in_process_mission_item_int"
        )
    if raw_record.get("truth_free") is not True:
        reasons.append("px4_airframe_transaction.truth_free must be true")
    if raw_record.get("physical_output_enabled") is not False:
        reasons.append("px4_airframe_transaction.physical_output_enabled must be false")

    expected_classes = ("fixed-wing", "multirotor", "vtol")
    declared_classes = raw_record.get("vehicle_classes")
    if (
        not isinstance(declared_classes, list)
        or any(not isinstance(value, str) for value in declared_classes)
        or tuple(sorted(declared_classes)) != tuple(sorted(expected_classes))
    ):
        reasons.append(
            "px4_airframe_transaction.vehicle_classes must contain fixed-wing, "
            "multirotor and vtol exactly once"
        )

    declared_checks = raw_record.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("px4_airframe_transaction.checks must be an object")
    elif any(value is not True for value in declared_checks.values()):
        reasons.append("px4_airframe_transaction.checks must all be true")

    if evidence_path is None:
        result["passed"] = not reasons
        return result
    evidence_info: dict[str, Any] = {"path": str(evidence_path)}
    if not evidence_path.is_file():
        reasons.append("px4_airframe_transaction evidence file does not exist")
        result["evidence"] = evidence_info
        result["passed"] = not reasons
        return result

    try:
        actual_sha = _sha256(evidence_path)
        actual_bytes = evidence_path.stat().st_size
    except OSError as exc:
        reasons.append(f"px4_airframe_transaction evidence cannot be read: {exc}")
        result["passed"] = not reasons
        return result
    evidence_info.update({"sha256": actual_sha, "bytes": actual_bytes})
    if _is_sha256(expected_sha) and actual_sha.lower() != str(expected_sha).lower():
        reasons.append("px4_airframe_transaction evidence SHA-256 does not match")
    if type(expected_bytes) is int and actual_bytes != expected_bytes:
        reasons.append("px4_airframe_transaction evidence byte count does not match")
    result["evidence"] = evidence_info

    try:
        artifact = _load_object(evidence_path)
    except ValueError as exc:
        reasons.append(f"px4_airframe_transaction evidence JSON is invalid: {exc}")
        result["passed"] = not reasons
        return result

    artifact_runtime = _artifact_runtime_mode(artifact)
    if artifact.get("schema") != "multidetect.px4-airframe-mission-transaction-acceptance":
        reasons.append("px4_airframe_transaction evidence schema does not match binding")
    if artifact.get("schema_version") != 1:
        reasons.append("px4_airframe_transaction evidence schema_version must be 1")
    if artifact_runtime != "sim":
        reasons.append("px4_airframe_transaction evidence runtime_mode is not 'sim'")
    if artifact.get("release_label") != declared_release:
        reasons.append("px4_airframe_transaction evidence release_label does not match binding")
    if artifact.get("gate_passed") is not True:
        reasons.append("px4_airframe_transaction evidence gate_passed is not true")
    if artifact.get("shared_dispatcher") != "compile_executable_mission":
        reasons.append("px4_airframe_transaction evidence dispatcher does not match binding")
    if artifact.get("shared_transport") != "in_process_mission_item_int":
        reasons.append("px4_airframe_transaction evidence transport does not match binding")
    if artifact.get("truth_free") is not True:
        reasons.append("px4_airframe_transaction evidence truth_free is not true")
    if artifact.get("physical_output_enabled") is not False:
        reasons.append("px4_airframe_transaction evidence physical output is enabled")
    artifact_classes = artifact.get("entries")
    artifact_class_names: list[str] = []
    if isinstance(artifact_classes, list):
        for entry in artifact_classes:
            if isinstance(entry, Mapping) and isinstance(entry.get("vehicle_class"), str):
                artifact_class_names.append(entry["vehicle_class"])
    if tuple(sorted(artifact_class_names)) != tuple(sorted(expected_classes)):
        reasons.append("px4_airframe_transaction evidence does not contain all three airframes")

    try:
        verification = verify_px4_mission_transaction_acceptance(artifact)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        reasons.append(f"px4_airframe_transaction verifier failed: {exc}")
        verification = None
    if verification is not None:
        result["verification"] = verification
        if verification.get("gate_passed") is not True:
            for reason in verification.get("errors", []):
                if isinstance(reason, str):
                    reasons.append(f"px4_airframe_transaction: {reason}")
            if not verification.get("errors"):
                reasons.append("px4_airframe_transaction verifier gate_passed is not true")
        verified_checks = verification.get("checks")
        if not isinstance(verified_checks, Mapping):
            reasons.append("px4_airframe_transaction verifier checks must be an object")
        elif any(value is not True for value in verified_checks.values()):
            reasons.append("px4_airframe_transaction verifier checks must all be true")
        if (
            isinstance(declared_checks, Mapping)
            and isinstance(verified_checks, Mapping)
            and dict(declared_checks) != dict(verified_checks)
        ):
            reasons.append("px4_airframe_transaction checks do not match verifier output")
        artifact_checks = artifact.get("checks")
        if (
            isinstance(artifact_checks, Mapping)
            and isinstance(verified_checks, Mapping)
            and dict(artifact_checks) != dict(verified_checks)
        ):
            reasons.append("px4_airframe_transaction evidence checks do not match verifier output")

    result["passed"] = not reasons
    return result


def _px4_mission_feedback_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-open the HIL/REAL x three-airframe receive-only feedback matrix."""

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "checks": None,
        "observations": [],
        "px4_mission_path_hash": None,
        "verification": None,
    }
    if raw_record is None:
        result["reasons"].append("px4_mission_feedback record is absent")
        return result
    try:
        # The field observer imports Pixhawk/Mission modules; keep it lazy so
        # ordinary release-gate inspection stays lightweight.
        from .px4_mission_feedback_binding import (
            verify_px4_mission_feedback_binding,
        )

        verification = verify_px4_mission_feedback_binding(
            raw_record,
            manifest_directory=manifest_directory,
            expected_release_label=expected_release_label,
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result["reasons"].append(f"px4_mission_feedback verifier failed: {exc}")
        return result
    result["verification"] = verification
    result["passed"] = verification.get("passed") is True
    result["reasons"] = [
        str(reason)
        for reason in verification.get("reasons", [])
        if isinstance(reason, str) and reason
    ]
    result["checks"] = verification.get("checks")
    result["observations"] = verification.get("observations", [])
    result["px4_mission_path_hash"] = verification.get("px4_mission_path_hash")
    if not result["passed"] and not result["reasons"]:
        result["reasons"].append("px4_mission_feedback verifier gate_passed is not true")
    return result


def _pixhawk_covariance_result(
    raw_record: object,
    *,
    manifest_directory: Path,
    expected_release_label: str | None,
) -> dict[str, Any]:
    """Re-open all six current-link covariance observations."""

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "checks": None,
        "observations": [],
        "px4_mission_path_hash": None,
        "verification": None,
    }
    if raw_record is None:
        result["reasons"].append("pixhawk_covariance record is absent")
        return result
    try:
        from .pixhawk_covariance_binding import verify_pixhawk_covariance_binding

        verification = verify_pixhawk_covariance_binding(
            raw_record,
            manifest_directory=manifest_directory,
            expected_release_label=expected_release_label,
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result["reasons"].append(f"pixhawk_covariance verifier failed: {exc}")
        return result
    result["verification"] = verification
    result["passed"] = verification.get("passed") is True
    result["reasons"] = list(verification.get("reasons", []))
    result["checks"] = verification.get("checks")
    result["observations"] = verification.get("observations", [])
    result["px4_mission_path_hash"] = verification.get("px4_mission_path_hash")
    return result


def _model_review_result(
    raw_record: object,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Re-validate an optional hash-bound model-review envelope.

    A model-review bundle is authoritative once referenced by a release
    manifest.  The complete bundle is reopened and its source model/reports
    are recomputed; the compact release-manifest summary is compared with the
    reopened document so a hand-edited boolean cannot promote a candidate.
    """

    result: dict[str, Any] = {
        "present": raw_record is not None,
        "passed": False,
        "reasons": [],
        "bundle": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]
    if raw_record is None:
        reasons.append("model_review record is absent")
        return result
    if not isinstance(raw_record, Mapping):
        reasons.append("model_review must be an object")
        return result

    unknown = sorted(set(raw_record).difference(_MODEL_REVIEW_KEYS))
    if unknown:
        reasons.append(f"unknown model_review field(s): {unknown!r}")
    for field in (
        "path",
        "sha256",
        "model_id",
        "model_version",
        "model_artifact_sha256",
        "review_status",
        "production_approval_eligible",
        "production_approved",
        "flight_control_enabled",
        "physical_release_enabled",
        "gate_passed",
        "reasons",
    ):
        if field not in raw_record:
            reasons.append(f"model_review.{field} is missing")

    path_value = raw_record.get("path")
    bundle_path: Path | None = None
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("model_review.path is missing or empty")
    else:
        requested = Path(path_value)
        if not requested.is_absolute():
            requested = manifest_directory / requested
        if requested.is_symlink():
            reasons.append("model_review bundle must not be a symbolic link")
        try:
            bundle_path = requested.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"model_review bundle path cannot be resolved: {exc}")

    expected_sha = raw_record.get("sha256")
    if not _is_sha256(expected_sha):
        reasons.append("model_review.sha256 is missing or invalid")
    expected_gate = raw_record.get("gate_passed")
    if type(expected_gate) is not bool:
        reasons.append("model_review.gate_passed must be a boolean")
    elif not expected_gate:
        reasons.append("model_review gate_passed is false")
    for field in (
        "production_approval_eligible",
        "production_approved",
        "flight_control_enabled",
        "physical_release_enabled",
    ):
        if type(raw_record.get(field)) is not bool:
            reasons.append(f"model_review.{field} must be a boolean")
    if raw_record.get("production_approved") is not False:
        reasons.append("model_review.production_approved must remain false")
    if raw_record.get("flight_control_enabled") is not False:
        reasons.append("model_review.flight_control_enabled must remain false")
    if raw_record.get("physical_release_enabled") is not False:
        reasons.append("model_review.physical_release_enabled must remain false")
    if not isinstance(raw_record.get("reasons"), list) or any(
        not isinstance(reason, str) for reason in raw_record.get("reasons", [])
    ):
        reasons.append("model_review.reasons must be a list of strings")

    if bundle_path is not None:
        bundle_info: dict[str, Any] = {"path": str(bundle_path)}
        if not bundle_path.is_file():
            reasons.append("model_review bundle file does not exist")
        else:
            try:
                actual_sha = _sha256(bundle_path)
                bundle_info["sha256"] = actual_sha
            except OSError as exc:
                reasons.append(f"model_review bundle cannot be hashed: {exc}")
                actual_sha = None
            if (
                actual_sha is not None
                and isinstance(expected_sha, str)
                and _is_sha256(expected_sha)
                and actual_sha.lower() != expected_sha.lower()
            ):
                reasons.append("model_review bundle SHA-256 does not match")
            verification = verify_model_review_bundle(bundle_path, require_eligible=True)
            result["verification"] = verification
            if verification.get("passed") is not True:
                for reason in verification.get("reasons", []):
                    if isinstance(reason, str) and reason:
                        reasons.append(f"model_review: {reason}")
            document = verification.get("document")
            if isinstance(document, Mapping):
                model = document.get("model")
                if isinstance(model, Mapping):
                    bundle_info.update(
                        {
                            "model_id": model.get("model_id"),
                            "model_version": model.get("model_version"),
                            "model_artifact_sha256": model.get("artifact_sha256"),
                        }
                    )
                    for field in ("model_id", "model_version", "model_artifact_sha256"):
                        if raw_record.get(field) != bundle_info.get(field):
                            reasons.append(f"model_review.{field} does not match bundle")
                else:
                    reasons.append("model_review bundle model must be an object")
                for field in (
                    "review_status",
                    "production_approval_eligible",
                    "production_approved",
                    "flight_control_enabled",
                    "physical_release_enabled",
                ):
                    actual = document.get(field)
                    bundle_info[field] = actual
                    if raw_record.get(field) != actual:
                        reasons.append(f"model_review.{field} does not match bundle")
                bundle_info["gate_passed"] = (
                    verification.get("production_approval_eligible") is True
                    and verification.get("passed") is True
                )
                if expected_gate is True and bundle_info["gate_passed"] is not True:
                    reasons.append("model_review gate_passed does not match verified eligibility")
            else:
                reasons.append("model_review bundle document must be an object")
        result["bundle"] = bundle_info

    result["passed"] = not reasons
    return result


def evaluate_release_gate(
    manifest_path: str | Path,
    *,
    version_manifest_path: str | Path | None = None,
    evaluated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Evaluate a four-mode release-gate manifest without mutating the repo.

    ``evaluated_at_utc`` is optional and accepts an ISO-8601 timestamp with a
    timezone.  Supplying it makes the returned report deterministic; omitting
    it records the current UTC time as before.
    """

    path = Path(manifest_path).expanduser().resolve()
    evaluation_timestamp = _canonical_evaluation_timestamp(evaluated_at_utc)
    top_reasons: list[str] = []
    try:
        manifest = _load_object(path)
    except ValueError as exc:
        return {
            "event": "multidetect_release_gate_evaluated",
            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
            "manifest_path": str(path),
            "gate_passed": False,
            "release_allowed": False,
            "reasons": [str(exc)],
            "evaluated_at_utc": evaluation_timestamp,
        }

    unknown_top = sorted(set(manifest).difference(_TOP_LEVEL_KEYS))
    missing_top = sorted(_REQUIRED_TOP_LEVEL_KEYS.difference(manifest))
    if unknown_top:
        top_reasons.append(f"unknown top-level field(s): {unknown_top!r}")
    if missing_top:
        top_reasons.append(f"missing top-level field(s): {missing_top!r}")
    if manifest.get("schema_version") != RELEASE_GATE_SCHEMA_VERSION:
        top_reasons.append("unsupported release-gate schema_version")
    if manifest.get("product") != "Multi-Detect":
        top_reasons.append("manifest product must be 'Multi-Detect'")

    version_error: str | None = None
    try:
        version = load_product_version(version_manifest_path)
    except (OSError, RuntimeError, ValueError) as exc:
        version = None
        version_error = str(exc)
        top_reasons.append(f"product version manifest is invalid: {exc}")
    if version is not None:
        if (
            manifest.get("product") == "Multi-Detect"
            and manifest.get("release_label") != version.release_label
        ):
            top_reasons.append("release-gate release_label does not match product version manifest")

    raw_modes = manifest.get("modes")
    if not isinstance(raw_modes, Mapping):
        top_reasons.append("modes must be an object")
        raw_modes = {}
    unknown_modes = sorted(set(raw_modes).difference(REQUIRED_MODES))
    missing_modes = sorted(set(REQUIRED_MODES).difference(raw_modes))
    if unknown_modes:
        top_reasons.append(f"unknown runtime mode(s): {unknown_modes!r}")
    if missing_modes:
        top_reasons.append(f"missing required runtime mode(s): {missing_modes!r}")

    raw_replay_set = manifest.get("replay_set")
    replay_set_result = _replay_set_result(
        raw_replay_set,
        manifest_directory=path.parent,
    )
    # Keep old prerelease manifests readable while treating an explicitly
    # supplied replay-set binding as authoritative.  A present but pending or
    # tampered binding must fail the evidence gate instead of being ignored.
    if raw_replay_set is not None and not replay_set_result["passed"]:
        top_reasons.extend(f"replay_set: {reason}" for reason in replay_set_result["reasons"])

    raw_real_field_link = manifest.get("real_field_link")
    real_field_link_result = _real_field_link_result(
        raw_real_field_link,
        manifest_directory=path.parent,
    )
    # An explicitly supplied field-link binding is authoritative even on the
    # prerelease line: a stale/tampered report must never make a manifest look
    # valid.  Absence remains backward-compatible until the product channel is
    # switched to the final release.
    if raw_real_field_link is not None and not real_field_link_result["passed"]:
        top_reasons.extend(
            f"real_field_link: {reason}" for reason in real_field_link_result["reasons"]
        )

    raw_real_alert_link = manifest.get("real_alert_link")
    real_alert_link_result = _real_alert_link_result(
        raw_real_alert_link,
        manifest_directory=path.parent,
    )
    # An explicitly supplied alert-link binding is authoritative on every
    # channel.  Absence stays backward-compatible for existing prerelease
    # manifests; the final release branch below requires the record.
    if raw_real_alert_link is not None and not real_alert_link_result["passed"]:
        top_reasons.extend(
            f"real_alert_link: {reason}" for reason in real_alert_link_result["reasons"]
        )

    raw_real_gr01_operator_link = manifest.get("real_gr01_operator_link")
    real_gr01_operator_link_result = _real_gr01_operator_link_result(
        raw_real_gr01_operator_link,
        manifest_directory=path.parent,
        evaluated_at_utc=evaluation_timestamp,
    )
    if raw_real_gr01_operator_link is not None and not real_gr01_operator_link_result["passed"]:
        top_reasons.extend(
            f"real_gr01_operator_link: {reason}"
            for reason in real_gr01_operator_link_result["reasons"]
        )

    raw_real_target_accuracy = manifest.get("real_target_accuracy")
    real_target_accuracy_result = _real_target_accuracy_result(
        raw_real_target_accuracy,
        manifest_directory=path.parent,
    )
    # A present accuracy report is authoritative on every channel.  Absence
    # remains backward-compatible for old prerelease manifests, but the final
    # release channel requires a passing, hash-bound report.
    if raw_real_target_accuracy is not None and not real_target_accuracy_result["passed"]:
        top_reasons.extend(
            f"real_target_accuracy: {reason}" for reason in real_target_accuracy_result["reasons"]
        )

    raw_real_target_state_evidence = manifest.get("real_target_state_evidence")
    real_target_state_evidence_result = _real_target_state_evidence_result(
        raw_real_target_state_evidence,
        manifest_directory=path.parent,
    )
    # A present target-state capture is authoritative on every channel.  Its
    # absence remains backward-compatible for old prerelease manifests, while
    # the final release channel requires a passing strict TGT evidence binding.
    if (
        raw_real_target_state_evidence is not None
        and not real_target_state_evidence_result["passed"]
    ):
        top_reasons.extend(
            f"real_target_state_evidence: {reason}"
            for reason in real_target_state_evidence_result["reasons"]
        )

    raw_field_evidence_bundle = manifest.get("field_evidence_bundle")
    field_evidence_bundle_result = _field_evidence_bundle_result(
        raw_field_evidence_bundle,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # A present field-evidence binding is authoritative on every channel.
    # Prerelease manifests may retain a valid report whose six strata are
    # still pending, but a stale or tampered report must close the aggregate
    # evidence gate immediately.
    if raw_field_evidence_bundle is not None and not field_evidence_bundle_result["passed"]:
        top_reasons.extend(
            f"field_evidence_bundle: {reason}" for reason in field_evidence_bundle_result["reasons"]
        )

    raw_simulator_safety = manifest.get("simulator_safety")
    simulator_safety_result = _simulator_safety_result(
        raw_simulator_safety,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # Old prerelease manifests remain readable without this optional binding;
    # an explicitly supplied binding is authoritative and fail-closed.
    if raw_simulator_safety is not None and not simulator_safety_result["passed"]:
        top_reasons.extend(
            f"simulator_safety: {reason}" for reason in simulator_safety_result["reasons"]
        )

    raw_actuator_safety = manifest.get("actuator_safety")
    actuator_safety_result = _actuator_safety_result(
        raw_actuator_safety,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # Old prerelease manifests remain readable without this optional binding;
    # an explicitly supplied executioner contract is authoritative and
    # fail-closed on every channel.
    if raw_actuator_safety is not None and not actuator_safety_result["passed"]:
        top_reasons.extend(
            f"actuator_safety: {reason}" for reason in actuator_safety_result["reasons"]
        )

    raw_simulator_scenario_matrix = manifest.get("simulator_scenario_matrix")
    simulator_scenario_matrix_result = _simulator_scenario_matrix_result(
        raw_simulator_scenario_matrix,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
        require_fault_coverage=(version is not None and version.channel == "release"),
    )
    # An explicitly supplied matrix binding is authoritative on every
    # channel.  Its source digest and per-airframe summaries are re-verified
    # above; absence remains compatible with older prerelease manifests.
    if raw_simulator_scenario_matrix is not None and not simulator_scenario_matrix_result["passed"]:
        top_reasons.extend(
            f"simulator_scenario_matrix: {reason}"
            for reason in simulator_scenario_matrix_result["reasons"]
        )

    raw_px4_response_matrix = manifest.get("px4_reported_position_response_matrix")
    px4_response_matrix_result = _px4_reported_position_response_matrix_result(
        raw_px4_response_matrix,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    if raw_px4_response_matrix is not None and not px4_response_matrix_result["passed"]:
        top_reasons.extend(
            f"px4_reported_position_response_matrix: {reason}"
            for reason in px4_response_matrix_result["reasons"]
        )

    raw_px4_response_campaign = manifest.get("px4_reported_position_response_campaign")
    px4_response_campaign_result = _px4_reported_position_response_campaign_result(
        raw_px4_response_campaign,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    if raw_px4_response_campaign is not None and not px4_response_campaign_result["passed"]:
        top_reasons.extend(
            f"px4_reported_position_response_campaign: {reason}"
            for reason in px4_response_campaign_result["reasons"]
        )

    raw_qgc_operator_visual_acceptance = manifest.get("qgc_operator_visual_acceptance")
    qgc_operator_visual_acceptance_result = _qgc_operator_visual_acceptance_result(
        raw_qgc_operator_visual_acceptance,
        manifest_directory=path.parent,
    )
    if (
        raw_qgc_operator_visual_acceptance is not None
        and not qgc_operator_visual_acceptance_result["passed"]
    ):
        top_reasons.extend(
            f"qgc_operator_visual_acceptance: {reason}"
            for reason in qgc_operator_visual_acceptance_result["reasons"]
        )

    raw_simulator_scene_stream_set = manifest.get("simulator_scene_stream_set")
    simulator_scene_stream_set_result = _simulator_scene_stream_set_result(
        raw_simulator_scene_stream_set,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # An explicitly supplied scene-stream binding is authoritative on every
    # channel.  The verifier re-opens all three source streams, so a changed
    # stream, path, scenario or aggregate check closes the evidence gate.
    if (
        raw_simulator_scene_stream_set is not None
        and not simulator_scene_stream_set_result["passed"]
    ):
        top_reasons.extend(
            f"simulator_scene_stream_set: {reason}"
            for reason in simulator_scene_stream_set_result["reasons"]
        )

    raw_simulator_combined = manifest.get("simulator_combined")
    simulator_combined_result = _simulator_combined_result(
        raw_simulator_combined,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # An explicitly supplied combined-SIM binding is authoritative on every
    # channel.  Its source hash, dynamic-port contract and software-only
    # claims must remain valid after the manifest is written.
    if raw_simulator_combined is not None and not simulator_combined_result["passed"]:
        top_reasons.extend(
            f"simulator_combined: {reason}" for reason in simulator_combined_result["reasons"]
        )

    raw_simulator_external = manifest.get("simulator_external_evidence")
    simulator_external_result = _simulator_external_evidence_result(
        raw_simulator_external,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # An explicitly supplied external runtime/live-AI binding is authoritative
    # on every channel.  Absence remains compatible with historical
    # prerelease manifests; the final channel requires the six-source join.
    if raw_simulator_external is not None and not simulator_external_result["passed"]:
        top_reasons.extend(
            f"simulator_external_evidence: {reason}"
            for reason in simulator_external_result["reasons"]
        )

    raw_simulator_3d_control = manifest.get("simulator_3d_control")
    simulator_3d_control_result = _simulator_3d_control_result(
        raw_simulator_3d_control,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # An explicitly supplied three-airframe binding is authoritative on every
    # channel.  Absence remains compatible with older prerelease manifests;
    # the final channel requires all three deterministic records.
    if raw_simulator_3d_control is not None and not simulator_3d_control_result["passed"]:
        top_reasons.extend(
            f"simulator_3d_control: {reason}" for reason in simulator_3d_control_result["reasons"]
        )

    raw_px4_airframe_transaction = manifest.get("px4_airframe_transaction")
    px4_airframe_transaction_result = _px4_airframe_transaction_result(
        raw_px4_airframe_transaction,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # An explicitly supplied three-airframe binding is authoritative on every
    # channel.  Its source JSON and durable journals are re-opened above, so
    # a stale or edited journal can never be accepted by the aggregate gate.
    if raw_px4_airframe_transaction is not None and not px4_airframe_transaction_result["passed"]:
        top_reasons.extend(
            f"px4_airframe_transaction: {reason}"
            for reason in px4_airframe_transaction_result["reasons"]
        )

    raw_px4_mission_feedback = manifest.get("px4_mission_feedback")
    px4_mission_feedback_result = _px4_mission_feedback_result(
        raw_px4_mission_feedback,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    # A supplied matrix is authoritative on every channel.  The final channel
    # additionally requires all six HIL/REAL airframe observations.
    if raw_px4_mission_feedback is not None and not px4_mission_feedback_result["passed"]:
        top_reasons.extend(
            f"px4_mission_feedback: {reason}" for reason in px4_mission_feedback_result["reasons"]
        )

    raw_pixhawk_covariance = manifest.get("pixhawk_covariance")
    pixhawk_covariance_result = _pixhawk_covariance_result(
        raw_pixhawk_covariance,
        manifest_directory=path.parent,
        expected_release_label=(
            version.release_label if version is not None else manifest.get("release_label")
        ),
    )
    if raw_pixhawk_covariance is not None and not pixhawk_covariance_result["passed"]:
        top_reasons.extend(
            f"pixhawk_covariance: {reason}" for reason in pixhawk_covariance_result["reasons"]
        )

    raw_model_review = manifest.get("model_review")
    model_review_result = _model_review_result(
        raw_model_review,
        manifest_directory=path.parent,
    )
    # A referenced model-review bundle is authoritative on every channel.  A
    # pending or tampered bundle therefore closes the aggregate gate instead
    # of being silently treated as advisory metadata.
    if raw_model_review is not None and not model_review_result["passed"]:
        top_reasons.extend(f"model_review: {reason}" for reason in model_review_result["reasons"])

    mode_results: dict[str, Any] = {}
    for mode in REQUIRED_MODES:
        mode_results[mode] = _record_result(
            mode,
            raw_modes.get(mode),
            manifest_directory=path.parent,
        )

    # Bind the six raw feedback observations to the same PX4 Mission contract
    # used by every E7 runtime mode.  Replacing all six reports without also
    # matching the current path hash must still fail closed.
    if raw_px4_mission_feedback is not None:
        px4_hashes = {
            str(result["parity"]["px4_mission_path_hash"]).lower()
            for result in mode_results.values()
            if isinstance(result.get("parity"), Mapping)
            and "px4_mission_path_hash" in result["parity"]
        }
        if len(px4_hashes) == 1:
            expected_px4_hash = next(iter(px4_hashes))
            if px4_mission_feedback_result.get("px4_mission_path_hash") != expected_px4_hash:
                reason = "px4_mission_path_hash does not match the four-mode E7 parity"
                if reason not in px4_mission_feedback_result["reasons"]:
                    px4_mission_feedback_result["reasons"].append(reason)
                checks = px4_mission_feedback_result.get("checks")
                if isinstance(checks, dict):
                    checks["px4_mission_path_bound"] = False
                verification = px4_mission_feedback_result.get("verification")
                if isinstance(verification, dict):
                    verification_checks = verification.get("checks")
                    if isinstance(verification_checks, dict):
                        verification_checks["px4_mission_path_bound"] = False
                    verification["passed"] = False
                px4_mission_feedback_result["passed"] = False
                prefixed = f"px4_mission_feedback: {reason}"
                if prefixed not in top_reasons:
                    top_reasons.append(prefixed)

    if raw_pixhawk_covariance is not None:
        px4_hashes = {
            str(result["parity"]["px4_mission_path_hash"]).lower()
            for result in mode_results.values()
            if isinstance(result.get("parity"), Mapping)
            and "px4_mission_path_hash" in result["parity"]
        }
        if len(px4_hashes) == 1:
            expected_px4_hash = next(iter(px4_hashes))
            if pixhawk_covariance_result.get("px4_mission_path_hash") != expected_px4_hash:
                reason = "px4_mission_path_hash does not match the four-mode E7 parity"
                if reason not in pixhawk_covariance_result["reasons"]:
                    pixhawk_covariance_result["reasons"].append(reason)
                checks = pixhawk_covariance_result.get("checks")
                if isinstance(checks, dict):
                    checks["px4_mission_path_bound"] = False
                pixhawk_covariance_result["passed"] = False
                prefixed = f"pixhawk_covariance: {reason}"
                if prefixed not in top_reasons:
                    top_reasons.append(prefixed)

    # The external join carries its own parity manifest.  Compare it with the
    # four mode records so changing every mode hash together cannot detach the
    # six-source SIM evidence from the current E7 contract.
    if raw_simulator_external is not None:
        external_parity_record = simulator_external_result.get("parity")
        external_hashes = (
            external_parity_record.get("hashes")
            if isinstance(external_parity_record, Mapping)
            else None
        )
        mode_parity: dict[str, str] | None = None
        mode_parity_records = [result.get("parity") for result in mode_results.values()]
        if all(isinstance(record, Mapping) for record in mode_parity_records):
            first = mode_parity_records[0]
            if isinstance(first, Mapping) and all(
                record == first for record in mode_parity_records
            ):
                mode_parity = {
                    field: str(first[field]).lower() for field in PARITY_FIELDS if field in first
                }
        if (
            mode_parity is not None
            and isinstance(external_hashes, Mapping)
            and {field: str(external_hashes.get(field)).lower() for field in PARITY_FIELDS}
            != mode_parity
        ):
            reason = (
                "simulator_external_evidence parity hashes do not match the four-mode E7 parity"
            )
            if reason not in simulator_external_result["reasons"]:
                simulator_external_result["reasons"].append(reason)
            checks = simulator_external_result.get("checks")
            if isinstance(checks, dict):
                checks["join_parity_matches"] = False
            simulator_external_result["passed"] = False
            top_reasons.append(f"simulator_external_evidence: {reason}")

    parity_results: dict[str, bool] = {}
    for field in PARITY_FIELDS:
        values = {
            result["parity"][field]
            for result in mode_results.values()
            if isinstance(result.get("parity"), Mapping) and field in result["parity"]
        }
        parity_results[field] = len(values) == 1 and len(mode_results) == len(REQUIRED_MODES)
        if not parity_results[field]:
            top_reasons.append(f"shared parity field {field} is not identical across all modes")

    mode_reasons = [reason for result in mode_results.values() for reason in result["reasons"]]
    gate_passed = (
        not top_reasons
        and not mode_reasons
        and all(bool(result.get("passed")) for result in mode_results.values())
    )

    release_reasons = list(top_reasons) + list(mode_reasons)
    if version is None:
        release_reasons.append("product version could not be loaded")
    else:
        if version.channel == "release":
            if not replay_set_result["present"]:
                release_reasons.append(
                    "final release requires a complete deployment-domain replay_set manifest"
                )
            elif not replay_set_result["passed"]:
                release_reasons.extend(
                    f"replay_set: {reason}" for reason in replay_set_result["reasons"]
                )
            elif replay_set_result.get("accuracy_evidence") is None:
                release_reasons.append(
                    "final release requires hash-bound replay_set accuracy evidence"
                )
            calibration_verification = replay_set_result.get("calibration_verification")
            if not isinstance(calibration_verification, Mapping):
                release_reasons.append(
                    "final release requires provenance-complete replay_set calibration verification"
                )
            elif not calibration_verification.get("passed"):
                release_reasons.append(
                    "final release requires passing replay_set calibration verification"
                )
            else:
                if not calibration_verification.get("provenance_complete"):
                    release_reasons.append(
                        "final release requires provenance-complete replay_set "
                        "calibration verification"
                    )
                if not calibration_verification.get("promotion_ready"):
                    release_reasons.append(
                        "final release requires promotion-ready replay_set calibration verification"
                    )
            if not real_field_link_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound REAL field-link preflight"
                )
            elif not real_field_link_result["passed"]:
                release_reasons.extend(
                    f"real_field_link: {reason}" for reason in real_field_link_result["reasons"]
                )
            else:
                # A final REAL claim must prove which local interface reached
                # the aircraft network.  Historical prerelease reports may
                # omit route provenance, but accepting one on the release
                # channel would allow a VPN/TAP route to satisfy only the TCP
                # reachability checks.
                route_requested = (
                    raw_real_field_link.get("route_checks_requested")
                    if isinstance(raw_real_field_link, Mapping)
                    else None
                )
                route_passed = (
                    raw_real_field_link.get("route_checks_passed")
                    if isinstance(raw_real_field_link, Mapping)
                    else None
                )
                route_cidr = (
                    raw_real_field_link.get("route_source_cidr")
                    if isinstance(raw_real_field_link, Mapping)
                    else None
                )
                if (
                    route_requested is not True
                    or route_passed is not True
                    or not isinstance(route_cidr, str)
                    or not route_cidr.strip()
                ):
                    release_reasons.append(
                        "final release requires route-bound REAL field-link provenance "
                        "(route_checks_requested=true, route_checks_passed=true, "
                        "route_source_cidr set)"
                    )
            if real_field_link_result["present"] and (
                not isinstance(raw_real_field_link, Mapping)
                or "max_age_s" not in raw_real_field_link
                or "report_timestamp_epoch_s" not in raw_real_field_link
            ):
                release_reasons.append(
                    "final release requires freshness-bound REAL field-link provenance "
                    "(max_age_s and report_timestamp_epoch_s)"
                )
            if real_field_link_result["present"] and (
                not isinstance(raw_real_field_link, Mapping)
                or "address_state_bound" not in raw_real_field_link
            ):
                release_reasons.append(
                    "final release requires an explicit REAL field-link address-state "
                    "binding projection (address_state_bound)"
                )
            if not real_alert_link_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound REAL authenticated alert-link bench"
                )
            elif not real_alert_link_result["passed"]:
                release_reasons.extend(
                    f"real_alert_link: {reason}" for reason in real_alert_link_result["reasons"]
                )
            if not real_gr01_operator_link_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound REAL GR01 operator selection/ACK bench"
                )
            elif not real_gr01_operator_link_result["passed"]:
                release_reasons.extend(
                    f"real_gr01_operator_link: {reason}"
                    for reason in real_gr01_operator_link_result["reasons"]
                )
            if real_gr01_operator_link_result["present"] and (
                not isinstance(raw_real_gr01_operator_link, Mapping)
                or "observed_at_utc" not in raw_real_gr01_operator_link
                or "report_timestamp_epoch_s" not in raw_real_gr01_operator_link
                or "max_age_s" not in raw_real_gr01_operator_link
            ):
                release_reasons.append(
                    "final release requires freshness-bound REAL GR01 operator-link provenance "
                    "(observed_at_utc, report_timestamp_epoch_s and max_age_s)"
                )
            if not real_target_accuracy_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound REAL target geolocation accuracy report"
                )
            elif not real_target_accuracy_result["passed"]:
                release_reasons.extend(
                    f"real_target_accuracy: {reason}"
                    for reason in real_target_accuracy_result["reasons"]
                )
            if not real_target_state_evidence_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound REAL target-state evidence capture"
                )
            elif not real_target_state_evidence_result["passed"]:
                release_reasons.extend(
                    f"real_target_state_evidence: {reason}"
                    for reason in real_target_state_evidence_result["reasons"]
                )
            if not field_evidence_bundle_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound six-strata field evidence bundle"
                )
            elif not field_evidence_bundle_result["passed"]:
                release_reasons.extend(
                    f"field_evidence_bundle: {reason}"
                    for reason in field_evidence_bundle_result["reasons"]
                )
            if not simulator_safety_result["present"]:
                release_reasons.append("final release requires a hash-bound SIM S4 safety evidence")
            elif not simulator_safety_result["passed"]:
                release_reasons.extend(
                    f"simulator_safety: {reason}" for reason in simulator_safety_result["reasons"]
                )
            if not actuator_safety_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound actuator safety evidence"
                )
            elif not actuator_safety_result["passed"]:
                release_reasons.extend(
                    f"actuator_safety: {reason}" for reason in actuator_safety_result["reasons"]
                )
            if not simulator_scenario_matrix_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound three-airframe SIM scenario matrix"
                )
            elif not simulator_scenario_matrix_result["passed"]:
                release_reasons.extend(
                    f"simulator_scenario_matrix: {reason}"
                    for reason in simulator_scenario_matrix_result["reasons"]
                )
            if not px4_response_matrix_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound three-airframe PX4 "
                    "reported-position response matrix"
                )
            elif not px4_response_matrix_result["passed"]:
                release_reasons.extend(
                    f"px4_reported_position_response_matrix: {reason}"
                    for reason in px4_response_matrix_result["reasons"]
                )
            if not px4_response_campaign_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound three-airframe PX4 "
                    "response repeatability and fault campaign"
                )
            elif not px4_response_campaign_result["passed"]:
                release_reasons.extend(
                    f"px4_reported_position_response_campaign: {reason}"
                    for reason in px4_response_campaign_result["reasons"]
                )
            if not qgc_operator_visual_acceptance_result["present"]:
                release_reasons.append(
                    "final release requires source-bound human QGC visual reviews "
                    "for REPLAY, SIM, HIL and REAL"
                )
            elif not qgc_operator_visual_acceptance_result["passed"]:
                release_reasons.extend(
                    f"qgc_operator_visual_acceptance: {reason}"
                    for reason in qgc_operator_visual_acceptance_result["reasons"]
                )
            if not simulator_combined_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound combined SIM dynamic-port evidence"
                )
            elif not simulator_combined_result["passed"]:
                release_reasons.extend(
                    f"simulator_combined: {reason}"
                    for reason in simulator_combined_result["reasons"]
                )
            if not simulator_external_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound six-source external SIM "
                    "runtime/live-AI evidence"
                )
            elif not simulator_external_result["passed"]:
                release_reasons.extend(
                    f"simulator_external_evidence: {reason}"
                    for reason in simulator_external_result["reasons"]
                )
            if not simulator_3d_control_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound three-airframe SIM 3-D control binding"
                )
            elif not simulator_3d_control_result["passed"]:
                release_reasons.extend(
                    f"simulator_3d_control: {reason}"
                    for reason in simulator_3d_control_result["reasons"]
                )
            if not simulator_scene_stream_set_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound three-airframe SIM scene-stream set"
                )
            elif not simulator_scene_stream_set_result["passed"]:
                release_reasons.extend(
                    f"simulator_scene_stream_set: {reason}"
                    for reason in simulator_scene_stream_set_result["reasons"]
                )
            if not px4_airframe_transaction_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound three-airframe PX4 Mission "
                    "transaction acceptance"
                )
            elif not px4_airframe_transaction_result["passed"]:
                release_reasons.extend(
                    f"px4_airframe_transaction: {reason}"
                    for reason in px4_airframe_transaction_result["reasons"]
                )
            if not px4_mission_feedback_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound HIL/REAL x three-airframe "
                    "PX4 Mission feedback matrix"
                )
            elif not px4_mission_feedback_result["passed"]:
                release_reasons.extend(
                    f"px4_mission_feedback: {reason}"
                    for reason in px4_mission_feedback_result["reasons"]
                )
            if not pixhawk_covariance_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound HIL/REAL x three-airframe "
                    "Pixhawk covariance matrix"
                )
            elif not pixhawk_covariance_result["passed"]:
                release_reasons.extend(
                    f"pixhawk_covariance: {reason}"
                    for reason in pixhawk_covariance_result["reasons"]
                )
            if not model_review_result["present"]:
                release_reasons.append(
                    "final release requires a hash-bound eligible model-review bundle"
                )
            elif not model_review_result["passed"]:
                release_reasons.extend(
                    f"model_review: {reason}" for reason in model_review_result["reasons"]
                )
        if version.channel != "release":
            release_reasons.append(
                f"product channel is {version.channel!r}; final release requires channel 'release'"
            )
        if version.release_label != version.numeric or version.python != version.numeric:
            release_reasons.append("final release version surfaces do not equal numeric version")

    return {
        "event": "multidetect_release_gate_evaluated",
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "manifest_path": str(path),
        "product_version": (_product_version_mapping(version) if version is not None else None),
        "version_error": version_error,
        "required_modes": list(REQUIRED_MODES),
        "modes": mode_results,
        "replay_set": replay_set_result,
        "replay_set_required_for_release": True,
        "real_field_link": real_field_link_result,
        "real_field_link_required_for_release": True,
        "real_alert_link": real_alert_link_result,
        "real_alert_link_required_for_release": True,
        "real_gr01_operator_link": real_gr01_operator_link_result,
        "real_gr01_operator_link_required_for_release": True,
        "real_target_accuracy": real_target_accuracy_result,
        "real_target_accuracy_required_for_release": True,
        "real_target_state_evidence": real_target_state_evidence_result,
        "real_target_state_evidence_required_for_release": True,
        "field_evidence_bundle": field_evidence_bundle_result,
        "field_evidence_bundle_required_for_release": True,
        "simulator_safety": simulator_safety_result,
        "simulator_safety_required_for_release": True,
        "actuator_safety": actuator_safety_result,
        "actuator_safety_required_for_release": True,
        "simulator_scenario_matrix": simulator_scenario_matrix_result,
        "simulator_scenario_matrix_required_for_release": True,
        "px4_reported_position_response_matrix": px4_response_matrix_result,
        "px4_reported_position_response_matrix_required_for_release": True,
        "px4_reported_position_response_campaign": px4_response_campaign_result,
        "px4_reported_position_response_campaign_required_for_release": True,
        "qgc_operator_visual_acceptance": qgc_operator_visual_acceptance_result,
        "qgc_operator_visual_acceptance_required_for_release": True,
        "simulator_combined": simulator_combined_result,
        "simulator_combined_required_for_release": True,
        "simulator_external_evidence": simulator_external_result,
        "simulator_external_evidence_required_for_release": True,
        "simulator_3d_control": simulator_3d_control_result,
        "simulator_3d_control_required_for_release": True,
        "simulator_scene_stream_set": simulator_scene_stream_set_result,
        "simulator_scene_stream_set_required_for_release": True,
        "px4_airframe_transaction": px4_airframe_transaction_result,
        "px4_airframe_transaction_required_for_release": True,
        "px4_mission_feedback": px4_mission_feedback_result,
        "px4_mission_feedback_required_for_release": True,
        "pixhawk_covariance": pixhawk_covariance_result,
        "pixhawk_covariance_required_for_release": True,
        "model_review": model_review_result,
        "model_review_required_for_release": True,
        "parity": parity_results,
        "gate_passed": gate_passed,
        "release_allowed": gate_passed and not release_reasons,
        "release_blocked_reasons": release_reasons,
        "final_release_label": version.numeric if version is not None else "0.3.0",
        "evaluated_at_utc": evaluation_timestamp,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="four-mode release-gate manifest JSON")
    parser.add_argument("--version-manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--evaluated-at-utc",
        metavar="ISO8601",
        help=(
            "explicit timezone-aware ISO-8601 evaluation time (for byte-stable "
            "reports; defaults to current UTC)"
        ),
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even when the release gate is blocked",
    )
    args = parser.parse_args(argv)
    try:
        report = evaluate_release_gate(
            args.manifest,
            version_manifest_path=args.version_manifest,
            evaluated_at_utc=args.evaluated_at_utc,
        )
    except ValueError as exc:
        parser.error(str(exc))
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report.get("release_allowed") or args.report_only else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "PARITY_FIELDS",
    "RELEASE_GATE_SCHEMA_VERSION",
    "REQUIRED_MODES",
    "evaluate_release_gate",
    "main",
]
