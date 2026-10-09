"""Create a hash-bound receipt for an existing shared-policy campaign.

This command is deliberately read-only with respect to PX4, MAVLink and
hardware.  It audits an already completed campaign directory and binds each
Mission report, transaction journal and policy decision to one canonical
receipt.  It does not infer cleanup, HIL, REAL or outdoor-GNSS acceptance.
"""

from __future__ import annotations

# ruff: noqa: E402 -- repository-local CLI bootstrap before package imports
import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "src", _ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads
from multidetect.sensor_transport_binding import (
    SensorTransportManifest,
    load_sensor_transport_manifest,
)
from scripts.vtol_selected_task_campaign import (
    _load_sensor_frame_map,
    _sensor_transport_for_task,
)
from scripts.vtol_sitl_mission_acceptance import (
    _validate_sensor_transport_binding_window,
)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(path: Path, label: str) -> dict[str, Any]:
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _sha256_text(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a SHA-256 string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a SHA-256 string") from exc
    return value.lower()


def _release_parity_binding(path: Path) -> dict[str, Any]:
    document = _object(path, "release parity")
    hashes = document.get("parity")
    required = {
        "core_algorithm_hash",
        "px4_mission_path_hash",
        "safety_policy_hash",
        "task_contract_hash",
        "ui_contract_hash",
    }
    if not isinstance(hashes, dict) or set(hashes) != required:
        raise ValueError("release parity mapping is incomplete")
    normalized = {
        key: _sha256_text(hashes[key], f"release parity {key}") for key in sorted(required)
    }
    return {
        "verified": True,
        "scope": "receipt_generation_file_binding",
        "path": str(path),
        "bytes": path.stat().st_size,
        "file_sha256": _sha256_path(path),
        "hashes": normalized,
        "release_label": document.get("release_label"),
    }


def _contained_path(value: object, *, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is required")
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve(strict=True)
    try:
        resolved.relative_to(base.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"{label} path escapes campaign directory") from exc
    return resolved


def _mission_receipt(
    entry: dict[str, Any],
    *,
    ordinal: int,
    summary: dict[str, Any],
    base: Path,
    shared_policy: bool,
    policy_artifact_sha256: str | None,
    ranker_sha256: str | None,
    sensor_transport_manifest: SensorTransportManifest | None,
    sensor_frame_ids: Mapping[str, str] | None,
) -> dict[str, Any]:
    evidence_path = _contained_path(entry.get("evidence_path"), base=base, label="Mission evidence")
    journal_path = evidence_path.with_suffix(".transaction.jsonl")
    if not journal_path.is_file():
        raise ValueError(f"missing transaction journal for sortie {ordinal}")
    report = _object(evidence_path, f"Mission evidence {ordinal}")
    if report.get("gate_passed") is not True:
        raise ValueError(f"Mission evidence {ordinal} did not pass its gate")
    if report.get("container", {}).get("id") != summary.get("session_id"):
        raise ValueError(f"Mission evidence {ordinal} has a different session identity")
    mission_hash = entry.get("mission_hash")
    document_hash = report.get("documents", {}).get("executable_mission", {}).get("sha256")
    outcome_hash = report.get("shared_outcome_shadow", {}).get("outcome", {}).get("mission_hash")
    if (
        not isinstance(mission_hash, str)
        or mission_hash != document_hash
        or mission_hash != outcome_hash
    ):
        raise ValueError(f"Mission hash mismatch in sortie {ordinal}")
    if report.get("shared_outcome_shadow", {}).get("outcome", {}).get("status") != "completed":
        raise ValueError(f"Mission outcome {ordinal} is not completed")

    sensor_transport: dict[str, Any] | None = None
    if sensor_transport_manifest is not None or sensor_frame_ids is not None:
        if sensor_transport_manifest is None or sensor_frame_ids is None:
            raise ValueError("sensor transport manifest and frame map must be paired")
        expected_sensor = _sensor_transport_for_task(
            sensor_transport_manifest,
            sensor_frame_ids,
            str(entry.get("task_id")),
        )
        report_sensor = report.get("sensor_transport")
        entry_sensor = entry.get("sensor_transport")
        if (
            expected_sensor is None
            or not isinstance(report_sensor, Mapping)
            or not isinstance(entry_sensor, Mapping)
            or any(report_sensor.get(key) != value for key, value in expected_sensor.items())
            or dict(entry_sensor) != dict(report_sensor)
        ):
            raise ValueError(f"sensor transport binding {ordinal} failed identity checks")
        mission_receive_window = report.get("mission_receive_window")
        if not isinstance(mission_receive_window, Mapping):
            raise ValueError(f"sensor transport binding {ordinal} has no Mission receive window")
        try:
            sensor_transport = _validate_sensor_transport_binding_window(
                report_sensor,
                mission_receive_window,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"sensor transport binding {ordinal} failed time checks: {exc}"
            ) from exc
    elif report.get("sensor_transport") is not None or entry.get("sensor_transport") is not None:
        raise ValueError(f"unexpected sensor transport binding in legacy sortie {ordinal}")

    policy_path: Path | None = None
    policy_sha256: str | None = None
    decision_sha256: str | None = None
    if shared_policy:
        policy_path = _contained_path(
            entry.get("policy_evidence_path"), base=base, label="Policy evidence"
        )
        policy = _object(policy_path, f"Policy evidence {ordinal}")
        decision = policy.get("decision")
        if not isinstance(decision, dict):
            raise ValueError(f"policy decision {ordinal} is missing")
        if (
            decision.get("action") != "select"
            or decision.get("task_id") != entry.get("task_id")
            or decision.get("planner_revalidated") is not True
            or decision.get("truth_free") is not True
            or decision.get("policy_artifact_sha256") != policy_artifact_sha256
            or policy.get("ranker_sha256") != ranker_sha256
            or policy.get("mission_hash") != mission_hash
            or policy.get("policy_applied_to_actuator") is not False
        ):
            raise ValueError(f"policy decision {ordinal} failed identity checks")
        policy_sha256 = _sha256_path(policy_path)
        decision_sha256 = canonical_sha256(decision)

    return {
        "ordinal": ordinal,
        "task_id": entry.get("task_id"),
        "mission_hash": mission_hash,
        "mission_evidence_path": str(evidence_path),
        "mission_evidence_sha256": _sha256_path(evidence_path),
        "transaction_journal_path": str(journal_path),
        "transaction_journal_sha256": _sha256_path(journal_path),
        "policy_evidence_path": str(policy_path) if policy_path is not None else None,
        "policy_evidence_sha256": policy_sha256,
        "policy_decision_sha256": decision_sha256,
        "planner_revalidated": entry.get("planner_revalidated") is True,
        "sensor_transport": sensor_transport,
    }


def _validate_completed_sensor_frame_map(
    manifest: SensorTransportManifest,
    completed: list[object],
    frame_ids: Mapping[str, str],
) -> None:
    task_ids = {
        entry.get("task_id")
        for entry in completed
        if isinstance(entry, Mapping)
    }
    if any(not isinstance(task_id, str) or not task_id for task_id in task_ids):
        raise ValueError("completed sorties must have non-empty task IDs")
    if set(frame_ids) != task_ids:
        raise ValueError("sensor frame map task IDs must match completed sorties")
    resolved = []
    for task_id in sorted(task_ids):
        frame = manifest.frame_for_ids(frame_ids[task_id])
        if frame is None:
            raise ValueError(f"sensor frame map references unknown frame: {frame_ids[task_id]}")
        resolved.append(frame.frame_id)
    if len(set(resolved)) != len(resolved):
        raise ValueError("sensor frame map must assign a unique frame to each task")


def build_receipt(
    summary_path: Path,
    campaign_path: Path,
    output_path: Path,
    runtime_evidence_path: Path | None = None,
    release_parity_path: Path | None = None,
    sensor_transport_manifest_path: Path | None = None,
    sensor_frame_map_path: Path | None = None,
) -> dict[str, Any]:
    summary_path = summary_path.resolve(strict=True)
    campaign_path = campaign_path.resolve(strict=True)
    summary = _object(summary_path, "campaign summary")
    if summary.get("status") != "all_tasks_completed":
        raise ValueError("campaign summary is not complete")
    completed = summary.get("completed")
    if not isinstance(completed, list) or not completed:
        raise ValueError("campaign summary has no completed sorties")
    if (sensor_transport_manifest_path is None) != (sensor_frame_map_path is None):
        raise ValueError("sensor transport manifest and frame map must be paired")
    sensor_transport_manifest: SensorTransportManifest | None = None
    sensor_frame_ids: dict[str, str] | None = None
    sensor_transport_enabled = sensor_transport_manifest_path is not None
    if sensor_transport_enabled:
        if summary.get("sensor_transport_used") is not True:
            raise ValueError("campaign sensor transport usage flag mismatch")
        try:
            sensor_transport_manifest = load_sensor_transport_manifest(
                sensor_transport_manifest_path.resolve(strict=True)
            )
            sensor_frame_ids = _load_sensor_frame_map(sensor_frame_map_path)
            _validate_completed_sensor_frame_map(
                sensor_transport_manifest,
                completed,
                sensor_frame_ids,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(f"sensor transport input is invalid: {exc}") from exc
    elif summary.get("sensor_transport_used") not in (None, False):
        raise ValueError("legacy campaign sensor usage flag mismatch")
    shared_policy = summary.get("shared_policy_used") is True
    if not shared_policy:
        raise ValueError("shared-policy campaign receipt requires shared_policy_used=true")
    policy_artifact_sha256 = summary.get("policy_artifact_sha256")
    ranker_sha256 = summary.get("ranker_sha256")
    if not isinstance(policy_artifact_sha256, str) or not isinstance(ranker_sha256, str):
        raise ValueError("campaign summary is missing policy/ranker identity")
    base = summary_path.parent.resolve(strict=True)
    sorties = [
        _mission_receipt(
            entry,
            ordinal=index,
            summary=summary,
            base=base,
            shared_policy=shared_policy,
            policy_artifact_sha256=policy_artifact_sha256,
            ranker_sha256=ranker_sha256,
            sensor_transport_manifest=sensor_transport_manifest,
            sensor_frame_ids=sensor_frame_ids,
        )
        for index, entry in enumerate(completed, 1)
        if isinstance(entry, dict)
    ]
    if len(sorties) != len(completed):
        raise ValueError("campaign completed entries must be objects")
    if not all(item["planner_revalidated"] for item in sorties):
        raise ValueError("campaign contains a sortie without planner revalidation")
    cleanup: dict[str, Any] = {
        "verified": False,
        "scope": (
            "receipt audits retained files; outer session cleanup must be "
            "verified separately"
        ),
    }
    runtime_binding: dict[str, Any] = {"verified": False}
    runtime_parity_snapshot_verified = False
    release_parity = (
        _release_parity_binding(release_parity_path.resolve(strict=True))
        if release_parity_path is not None
        else {"verified": False}
    )
    if runtime_evidence_path is not None:
        runtime_evidence_path = runtime_evidence_path.resolve(strict=True)
        runtime = _object(runtime_evidence_path, "runtime evidence")
        runtime_cleanup = runtime.get("container", {}).get("cleanup", {})
        if (
            runtime.get("schema") != "multidetect.vtol-selected-campaign"
            or runtime.get("gate_passed") is not True
            or runtime.get("container", {}).get("id") != summary.get("session_id")
            or runtime.get("cleanup_failures") != []
            or runtime_cleanup.get("attempted") is not True
            or runtime_cleanup.get("cleanup_succeeded") is not True
            or runtime_cleanup.get("removed") is not True
            or runtime_cleanup.get("post_remove_absence_proven") is not True
            or runtime_cleanup.get("errors") != []
            or runtime.get("campaign", {}).get("summary_sha256") != _sha256_path(summary_path)
        ):
            raise ValueError("runtime evidence cleanup binding failed")
        runtime_identity = runtime.get("runtime_identity")
        descriptor = (
            runtime_identity.get("descriptor") if isinstance(runtime_identity, dict) else None
        )
        provenance = (
            runtime_identity.get("provenance") if isinstance(runtime_identity, dict) else None
        )
        scenario = runtime.get("scenario")
        if not isinstance(runtime_identity, dict) or not isinstance(descriptor, dict):
            raise ValueError("runtime evidence identity binding is missing")
        if not isinstance(provenance, dict) or not isinstance(scenario, dict):
            raise ValueError("runtime evidence scenario/provenance binding is missing")
        runtime_mode = descriptor.get("runtime_mode")
        scenario_hash = _sha256_text(
            descriptor.get("scenario_manifest_sha256"), "scenario manifest hash"
        )
        declared_scenario_hash = _sha256_text(
            scenario.get("manifest_sha256"), "scenario manifest hash"
        )
        airframe_hash = _sha256_text(
            runtime_identity.get("airframe_profile_sha256"), "airframe profile hash"
        )
        if (
            runtime_mode != "SIM"
            or descriptor.get("scenario_id") != scenario.get("scenario_id_expected")
            or scenario_hash != declared_scenario_hash
            or provenance.get("container_id") != summary.get("session_id")
        ):
            raise ValueError("runtime evidence identity binding failed")
        runtime_snapshot = runtime.get("campaign", {}).get("release_parity_snapshot")
        if runtime_snapshot is not None:
            end_snapshot = (
                runtime_snapshot.get("end")
                if isinstance(runtime_snapshot, dict)
                else None
            )
            runtime_parity_snapshot_verified = bool(
                isinstance(runtime_snapshot, dict)
                and runtime_snapshot.get("requested") is True
                and runtime_snapshot.get("stable") is True
                and runtime_snapshot.get("error") is None
                and isinstance(runtime_snapshot.get("start"), dict)
                and runtime_snapshot.get("start") == end_snapshot
                and isinstance(end_snapshot, dict)
                and end_snapshot.get("file_sha256") == release_parity.get("file_sha256")
                and end_snapshot.get("hashes") == release_parity.get("hashes")
                and end_snapshot.get("release_label") == release_parity.get("release_label")
            )
            if not runtime_parity_snapshot_verified:
                raise ValueError("runtime evidence release parity snapshot is not stable")
        elif release_parity_path is not None:
            raise ValueError("runtime evidence has no release parity snapshot")
        runtime_binding = {
            "verified": True,
            "runtime_mode": runtime_mode,
            "scenario_id": descriptor["scenario_id"],
            "scenario_manifest_sha256": scenario_hash,
            "airframe_profile_sha256": airframe_hash,
            "runtime_descriptor_sha256": _sha256_text(
                runtime_identity.get("descriptor_sha256"), "runtime descriptor hash"
            ),
            "runtime_provenance_sha256": _sha256_text(
                runtime_identity.get("provenance_sha256"), "runtime provenance hash"
            ),
            "stack_plan_sha256": _sha256_text(
                descriptor.get("stack_plan_sha256"), "stack plan hash"
            ),
            "runtime_parity_snapshot_verified": runtime_parity_snapshot_verified,
        }
        verified_sorties = runtime.get("campaign", {}).get("verified_sorties")
        if not isinstance(verified_sorties, list) or len(verified_sorties) != len(sorties):
            raise ValueError("runtime evidence sortie binding failed")
        for receipt_sortie, runtime_sortie in zip(sorties, verified_sorties, strict=True):
            if (
                runtime_sortie.get("task_id") != receipt_sortie.get("task_id")
                or runtime_sortie.get("report_sha256")
                != receipt_sortie.get("mission_evidence_sha256")
                or runtime_sortie.get("journal_sha256")
                != receipt_sortie.get("transaction_journal_sha256")
                or runtime_sortie.get("policy_evidence_sha256")
                != receipt_sortie.get("policy_evidence_sha256")
            ):
                raise ValueError("runtime evidence sortie hash binding failed")
            if sensor_transport_enabled:
                if runtime_sortie.get("sensor_transport") != receipt_sortie.get("sensor_transport"):
                    raise ValueError("runtime evidence sensor transport binding failed")
            elif runtime_sortie.get("sensor_transport") not in (None,):
                raise ValueError("runtime evidence unexpectedly contains sensor transport")
        cleanup = {
            "verified": True,
            "runtime_evidence_path": str(runtime_evidence_path),
            "runtime_evidence_sha256": _sha256_path(runtime_evidence_path),
            "container_id": runtime["container"]["id"],
            "cleanup_succeeded": True,
            "removed": True,
            "post_remove_absence_proven": True,
        }
    payload: dict[str, Any] = {
        "schema": "multidetect.shared-policy-campaign-receipt",
        "schema_version": 1,
        "producer": "create_shared_policy_campaign_receipt",
        "evidence_scope": "retained_files_audit",
        "runtime_source_parity_verified": False,
        "external_sim_rerun_performed": False,
        "summary": {
            "path": str(summary_path),
            "sha256": _sha256_path(summary_path),
            "status": summary["status"],
        },
        "campaign": {"path": str(campaign_path), "sha256": _sha256_path(campaign_path)},
        "release_parity": release_parity,
        "runtime_binding": runtime_binding,
        "runtime_parity_snapshot_verified": runtime_parity_snapshot_verified,
        "session_id": summary["session_id"],
        "shared_policy_used": True,
        "policy_artifact_sha256": policy_artifact_sha256,
        "ranker_sha256": ranker_sha256,
        "planner_revalidated": True,
        "sensor_transport_used": sensor_transport_enabled,
        "sensor_transport": (
            {
                "manifest_path": str(sensor_transport_manifest.path),
                "manifest_file_sha256": sensor_transport_manifest.file_sha256,
                "manifest_sha256": sensor_transport_manifest.manifest_sha256,
                "frame_count": len(sensor_transport_manifest.frames),
                "truth_free": True,
            }
            if sensor_transport_manifest is not None
            else None
        ),
        "truth_free": True,
        "policy_applied_to_actuator": False,
        "sortie_count": len(sorties),
        "sorties": sorties,
        "cleanup": cleanup,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    payload["receipt_sha256"] = canonical_sha256(payload)
    if output_path.exists():
        raise ValueError("receipt output must be new")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--release-parity", type=Path)
    parser.add_argument("--sensor-transport-manifest", type=Path)
    parser.add_argument("--sensor-frame-map", type=Path)
    args = parser.parse_args(argv)
    receipt = build_receipt(
        args.summary,
        args.campaign,
        args.output,
        args.runtime_evidence,
        args.release_parity,
        args.sensor_transport_manifest,
        args.sensor_frame_map,
    )
    print(json.dumps({"status": "passed", "receipt_sha256": receipt["receipt_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
