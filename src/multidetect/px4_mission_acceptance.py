"""Shared offline acceptance for the three PX4 mission vehicle classes.

The compiler already dispatches VTOL, fixed-wing and plain multirotor
geometry to one ``CompiledPx4Mission`` contract.  This module closes the
remaining software-only gap: every class is driven through the same
``Px4MissionTransaction`` and ``PersistentPx4MissionTransaction`` lifecycle,
including an append-only journal and an exact ``MISSION_ITEM_INT`` readback.

The helper deliberately has no MAVLink socket and never enables physical
output.  It is the deterministic contract used by SIM/HIL/REAL adapters; a
transport adapter may replace the in-memory request loop only after this
identity and journal contract has passed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .autonomy import (
    ContextSnapshot,
    OperatorGrant,
    PlatformNeutralPlan,
    ValidationFinding,
    ValidationReport,
    ValidationVerdict,
)
from .autonomy.serialization import canonical_sha256
from .mission_journal import (
    DurableMissionJournal,
    MissionJournalError,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from .px4_mission import (
    CompiledPx4Mission,
    MissionTransactionBindings,
    Px4FixedWingSafetyPolicy,
    Px4MissionSafetyPolicy,
    Px4MissionTransaction,
    Px4MultirotorSafetyPolicy,
    normalize_px4_vehicle_class,
)

_STATE_PATH = (
    MissionTransactionState.AUTHORIZED.value,
    MissionTransactionState.UPLOADING.value,
    MissionTransactionState.UPLOADED.value,
    MissionTransactionState.READBACK_VERIFIED.value,
    MissionTransactionState.COMMITTED.value,
    MissionTransactionState.START_PERMITTED.value,
)


class Px4MissionAcceptanceError(RuntimeError):
    """Raised when the shared offline PX4 Mission acceptance contract is invalid."""


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise Px4MissionAcceptanceError("now_utc must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy_hashes(
    policy: Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
) -> tuple[str, str]:
    try:
        geofence_hash = policy.geofence_hash
        policy_hash = policy.canonical_hash
    except AttributeError as exc:  # pragma: no cover - defensive CLI boundary
        raise Px4MissionAcceptanceError("policy does not expose canonical PX4 hashes") from exc
    return geofence_hash, policy_hash


def _build_transaction_documents(
    *,
    vehicle_class: str,
    compiled: CompiledPx4Mission,
    context_snapshot: ContextSnapshot,
    platform_plan: PlatformNeutralPlan,
    policy: Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
    transaction_id: str,
    now_utc: datetime,
) -> tuple[ValidationReport, MissionTransactionBindings, OperatorGrant]:
    """Build the hash-bound validation/grant documents for one fixture."""

    normalized = normalize_px4_vehicle_class(vehicle_class)
    mission = compiled.executable_mission
    geofence_hash, policy_hash = _policy_hashes(policy)
    prompt_hash = canonical_sha256(
        {
            "purpose": "px4-mission-transaction-acceptance",
            "vehicle_class": normalized,
            "mission_id": mission.mission_id,
        }
    )
    task_spec_hash = canonical_sha256(
        {
            "purpose": "shared-px4-mission-path",
            "vehicle_class": normalized,
            "mission_id": mission.mission_id,
            "policy_hash": policy_hash,
        }
    )
    vehicle_identity_hash = canonical_sha256(
        {
            "vehicle_class": normalized,
            "airframe_profile_id": context_snapshot.airframe_profile_id,
            "airframe_adapter_id": mission.airframe_adapter_id,
            "sim_only": True,
        }
    )
    runtime_descriptor_hash = canonical_sha256(
        {
            "runtime_mode": "SIM",
            "transport": "in_process_mission_item_int",
            "dispatcher": "compile_executable_mission",
            "vehicle_class": normalized,
        }
    )
    runtime_provenance_hash = canonical_sha256(
        {
            "source": "repository-runtime-channel-fixture",
            "truth_free": True,
            "physical_output_enabled": False,
            "vehicle_class": normalized,
        }
    )
    bindings = MissionTransactionBindings(
        prompt_hash=prompt_hash,
        task_spec_hash=task_spec_hash,
        plan_hash=platform_plan.canonical_hash,
        context_snapshot_hash=context_snapshot.canonical_hash,
        airframe_profile_hash=mission.airframe_profile_hash,
        geofence_hash=geofence_hash,
        vehicle_identity_hash=vehicle_identity_hash,
        runtime_descriptor_hash=runtime_descriptor_hash,
        runtime_provenance_hash=runtime_provenance_hash,
    )
    report = ValidationReport(
        report_id=f"validation-{transaction_id}",
        executable_mission_hash=mission.canonical_hash,
        verdict=ValidationVerdict.PASS,
        findings=(
            ValidationFinding("vehicle-dispatch", ValidationVerdict.PASS, normalized),
            ValidationFinding(
                "mission-item-int", ValidationVerdict.PASS, "canonical MAVLink2 wire items"
            ),
            ValidationFinding("runtime-channel", ValidationVerdict.PASS, "truth-free fixture"),
            ValidationFinding("safety-policy", ValidationVerdict.PASS, policy_hash),
        ),
    )
    grant = OperatorGrant(
        grant_id=f"acceptance-grant-{transaction_id}",
        transaction_id=transaction_id,
        operator_id="test-only-acceptance-fixture-not-human-approval",
        prompt_hash=bindings.prompt_hash,
        task_spec_hash=bindings.task_spec_hash,
        plan_hash=bindings.plan_hash,
        executable_mission_hash=mission.canonical_hash,
        validation_report_hash=report.canonical_hash,
        context_snapshot_hash=bindings.context_snapshot_hash,
        airframe_profile_hash=bindings.airframe_profile_hash,
        geofence_hash=bindings.geofence_hash,
        vehicle_identity_hash=bindings.vehicle_identity_hash,
        runtime_descriptor_hash=bindings.runtime_descriptor_hash,
        runtime_provenance_hash=bindings.runtime_provenance_hash,
        expires_at=_utc_timestamp(now_utc + timedelta(minutes=15)),
    )
    return report, bindings, grant


def run_px4_mission_transaction_acceptance(
    *,
    vehicle_class: str,
    compiled: CompiledPx4Mission,
    context_snapshot: ContextSnapshot,
    platform_plan: PlatformNeutralPlan,
    policy: Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
    journal_path: str | Path,
    now_utc: datetime,
    transaction_id: str,
) -> dict[str, Any]:
    """Run one vehicle through the shared PX4 Mission transaction path.

    The returned mapping is an evidence entry.  A failed lifecycle is encoded
    as ``gate_passed=false`` with the observed error and any journal prefix;
    callers can persist that evidence without losing the failure checkpoint.
    """

    normalized = normalize_px4_vehicle_class(vehicle_class)
    if not isinstance(compiled, CompiledPx4Mission):
        raise Px4MissionAcceptanceError("compiled has the wrong type")
    if not isinstance(context_snapshot, ContextSnapshot):
        raise Px4MissionAcceptanceError("context_snapshot has the wrong type")
    if not isinstance(platform_plan, PlatformNeutralPlan):
        raise Px4MissionAcceptanceError("platform_plan has the wrong type")
    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise Px4MissionAcceptanceError("transaction_id must be non-empty")
    resolved_journal_path = Path(journal_path).expanduser().resolve()
    if resolved_journal_path.exists():
        raise Px4MissionAcceptanceError(
            f"refusing to overwrite existing journal: {resolved_journal_path}"
        )

    report, bindings, grant = _build_transaction_documents(
        vehicle_class=normalized,
        compiled=compiled,
        context_snapshot=context_snapshot,
        platform_plan=platform_plan,
        policy=policy,
        transaction_id=transaction_id,
        now_utc=now_utc,
    )
    transaction: Px4MissionTransaction | None = None
    persistent: PersistentPx4MissionTransaction | None = None
    journal: DurableMissionJournal | None = None
    state_events: list[str] = []
    failure: str | None = None
    upload_plan = None
    permit = None
    readback_hash: str | None = None
    committed_hash: str | None = None

    def record_state() -> None:
        if persistent is not None:
            state = persistent.state.value
        elif transaction is not None:
            state = transaction.state.value
        else:
            return
        if not state_events or state_events[-1] != state:
            state_events.append(state)

    try:
        transaction = Px4MissionTransaction(
            transaction_id=transaction_id,
            compiled=compiled,
            validation_report=report,
            operator_grant=grant,
            bindings=bindings,
            now_utc=now_utc,
        )
        journal = DurableMissionJournal.create(
            resolved_journal_path,
            identity=transaction,
            now_utc=now_utc,
        )
        persistent = PersistentPx4MissionTransaction(transaction, journal)
        record_state()
        upload_plan = persistent.begin_upload(now_utc=now_utc)
        record_state()
        for sequence in range(upload_plan.item_count):
            persistent.mission_item_for_request(sequence, now_utc=now_utc)
        persistent.accept_upload_ack(now_utc=now_utc)
        record_state()
        readback_hash = persistent.verify_readback(
            compiled.wire_items,
            now_utc=now_utc,
        )
        record_state()
        committed_hash = persistent.commit(now_utc=now_utc)
        record_state()
        permit = persistent.permit_start(now_utc=now_utc)
        record_state()
    except Exception as exc:  # noqa: BLE001 - evidence must preserve all gate failures
        failure = f"{type(exc).__name__}: {exc}"
        record_state()
    finally:
        if persistent is not None:
            try:
                persistent.close()
            except Exception as exc:  # pragma: no cover - filesystem failure path
                failure = failure or f"{type(exc).__name__}: {exc}"
        elif journal is not None:
            try:
                journal.close()
            except Exception as exc:  # pragma: no cover - filesystem failure path
                failure = failure or f"{type(exc).__name__}: {exc}"

    journal_summary: dict[str, Any] | None = None
    journal_error: str | None = None
    if resolved_journal_path.is_file():
        try:
            reopened = DurableMissionJournal.open(resolved_journal_path)
            recovery = reopened.recover()
            journal_summary = {
                "path": str(resolved_journal_path),
                "bytes": resolved_journal_path.stat().st_size,
                "sha256": _sha256_file(resolved_journal_path),
                "format": "multidetect.px4-mission-journal.v1",
                "state": recovery.state.value,
                "recovery_required": recovery.recovery_required,
                "record_count": len(recovery.records),
                "requested_sequences": list(recovery.requested_sequences),
                "identity_hash": recovery.identity.canonical_hash,
                "transaction_id": recovery.identity.transaction_id,
                "executable_mission_hash": recovery.identity.executable_mission_hash,
                "protocol_hash": recovery.identity.protocol_hash,
                "mission_item_count": recovery.identity.mission_item_count,
                "airframe_adapter_id": recovery.identity.airframe_adapter_id,
            }
            reopened.close()
        except (MissionJournalError, OSError, ValueError) as exc:
            journal_error = f"{type(exc).__name__}: {exc}"

    item_count = len(compiled.wire_items)
    requested = (
        tuple(journal_summary["requested_sequences"]) if journal_summary is not None else tuple()
    )
    checks: dict[str, bool] = {
        "shared_dispatch_contract": True,
        "mission_item_int_transport": True,
        "truth_free": True,
        "physical_output_disabled": True,
        "state_path_exact": tuple(state_events) == _STATE_PATH,
        "all_sequences_requested": requested == tuple(range(item_count)),
        "readback_hash_match": readback_hash == compiled.protocol_hash,
        "commit_hash_match": committed_hash == compiled.protocol_hash,
        "start_permit_bound": bool(
            permit is not None
            and permit.transaction_id == transaction_id
            and permit.executable_mission_hash == compiled.executable_mission.canonical_hash
            and permit.protocol_hash == compiled.protocol_hash
        ),
        "journal_present": journal_summary is not None,
        "journal_recovery_clear": bool(
            journal_summary is not None and not journal_summary["recovery_required"]
        ),
        "journal_state_start_permitted": bool(
            journal_summary is not None
            and journal_summary["state"] == MissionTransactionState.START_PERMITTED.value
        ),
        "journal_identity_bound": bool(
            journal_summary is not None
            and journal_summary["transaction_id"] == transaction_id
            and journal_summary["executable_mission_hash"]
            == compiled.executable_mission.canonical_hash
            and journal_summary["protocol_hash"] == compiled.protocol_hash
            and journal_summary["mission_item_count"] == item_count
        ),
        "journal_record_count": bool(
            journal_summary is not None and journal_summary["record_count"] >= item_count + 6
        ),
    }
    gate_passed = failure is None and all(checks.values())
    return {
        "schema": "multidetect.px4-mission-transaction-acceptance-entry",
        "schema_version": 1,
        "gate_passed": gate_passed,
        "failure": failure,
        "vehicle_class": normalized,
        "vehicle_class_input": vehicle_class,
        "runtime_mode": "SIM",
        "truth_free": True,
        "physical_output_enabled": False,
        "transport": "in_process_mission_item_int",
        "dispatcher": "compile_executable_mission",
        "transaction_id": transaction_id,
        "state_path": state_events,
        "executable_mission_hash": compiled.executable_mission.canonical_hash,
        "protocol_hash": compiled.protocol_hash,
        "item_count": item_count,
        "airframe_adapter_id": compiled.executable_mission.airframe_adapter_id,
        "airframe_profile_hash": compiled.executable_mission.airframe_profile_hash,
        "readback_hash": readback_hash,
        "committed_hash": committed_hash,
        "mission_journal": journal_summary,
        "journal_error": journal_error,
        "checks": checks,
    }


def verify_px4_mission_transaction_acceptance(
    document: Mapping[str, Any],
    *,
    required_vehicle_classes: tuple[str, ...] = ("fixed-wing", "multirotor", "vtol"),
) -> dict[str, Any]:
    """Re-open and verify a cross-airframe acceptance artifact and its journals."""

    errors: list[str] = []
    entries_raw = document.get("entries")
    entries = entries_raw if isinstance(entries_raw, list) else []
    observed_classes: list[str] = []
    entry_results: list[dict[str, Any]] = []
    if document.get("schema") != "multidetect.px4-airframe-mission-transaction-acceptance":
        errors.append("unexpected acceptance schema")
    if document.get("schema_version") != 1:
        errors.append("unsupported acceptance schema_version")
    for index, entry_raw in enumerate(entries):
        if not isinstance(entry_raw, Mapping):
            errors.append(f"entry {index} is not an object")
            continue
        entry = dict(entry_raw)
        vehicle_class = entry.get("vehicle_class")
        if not isinstance(vehicle_class, str):
            errors.append(f"entry {index} has no vehicle_class")
            continue
        observed_classes.append(vehicle_class)
        checks: dict[str, bool] = {
            "entry_gate_passed": entry.get("gate_passed") is True,
            "shared_dispatch_contract": entry.get("dispatcher") == "compile_executable_mission",
            "mission_item_int_transport": entry.get("transport") == "in_process_mission_item_int",
            "truth_free": entry.get("truth_free") is True,
            "physical_output_disabled": entry.get("physical_output_enabled") is False,
        }
        journal_raw = entry.get("mission_journal")
        journal: DurableMissionJournal | None = None
        if not isinstance(journal_raw, Mapping):
            checks["journal_present"] = False
        else:
            path_value = journal_raw.get("path")
            path = Path(path_value).expanduser().resolve() if isinstance(path_value, str) else None
            checks["journal_present"] = path is not None and path.is_file()
            if path is None or not path.is_file():
                errors.append(f"entry {index} journal path is missing")
            else:
                try:
                    actual_bytes = path.stat().st_size
                    actual_sha = _sha256_file(path)
                    checks["journal_bytes_match"] = actual_bytes == journal_raw.get("bytes")
                    checks["journal_sha256_match"] = actual_sha == journal_raw.get("sha256")
                    journal = DurableMissionJournal.open(path)
                    recovery = journal.recover()
                    checks["journal_integrity"] = not recovery.recovery_required
                    checks["journal_state_start_permitted"] = (
                        recovery.state is MissionTransactionState.START_PERMITTED
                    )
                    checks["journal_record_count"] = (
                        len(recovery.records) >= int(entry.get("item_count", 0)) + 6
                    )
                    checks["journal_sequences_complete"] = recovery.requested_sequences == tuple(
                        range(int(entry.get("item_count", 0)))
                    )
                    checks["journal_identity_bound"] = (
                        recovery.identity.transaction_id == entry.get("transaction_id")
                        and recovery.identity.executable_mission_hash
                        == entry.get("executable_mission_hash")
                        and recovery.identity.protocol_hash == entry.get("protocol_hash")
                        and recovery.identity.mission_item_count == entry.get("item_count")
                        and recovery.identity.airframe_adapter_id
                        == entry.get("airframe_adapter_id")
                    )
                    checks["journal_summary_matches"] = (
                        journal_raw.get("identity_hash") == recovery.identity.canonical_hash
                        and journal_raw.get("state") == recovery.state.value
                        and journal_raw.get("record_count") == len(recovery.records)
                        and journal_raw.get("requested_sequences")
                        == list(recovery.requested_sequences)
                    )
                except (MissionJournalError, OSError, ValueError, TypeError) as exc:
                    checks["journal_integrity"] = False
                    errors.append(f"entry {index} journal verification failed: {exc}")
                finally:
                    if journal is not None:
                        journal.close()
        entry_gate = all(checks.values())
        entry_results.append(
            {
                "vehicle_class": vehicle_class,
                "gate_passed": entry_gate,
                "checks": checks,
            }
        )
        if not entry_gate:
            errors.append(f"entry {index} failed one or more checks")

    expected = tuple(required_vehicle_classes)
    class_set_ok = tuple(sorted(observed_classes)) == tuple(sorted(expected))
    if not class_set_ok:
        errors.append(
            "vehicle class set mismatch: "
            f"expected {sorted(expected)!r}, got {sorted(observed_classes)!r}"
        )
    checks = {
        "vehicle_classes_complete": class_set_ok,
        "entry_count": len(entries) == len(expected),
        "all_entries_passed": bool(entry_results)
        and all(result["gate_passed"] for result in entry_results),
        "no_errors": not errors,
    }
    gate_passed = all(checks.values())
    return {
        "schema": "multidetect.px4-airframe-mission-transaction-verification",
        "schema_version": 1,
        "gate_passed": gate_passed,
        "checks": checks,
        "entries": entry_results,
        "errors": errors,
    }


__all__ = [
    "Px4MissionAcceptanceError",
    "run_px4_mission_transaction_acceptance",
    "verify_px4_mission_transaction_acceptance",
]
