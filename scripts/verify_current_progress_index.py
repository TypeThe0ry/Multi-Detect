"""Verify the authoritative current-progress index without mutating the repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Keep direct CLI invocation independent of an editable install or caller
# managed PYTHONPATH.  The verifier imports the policy artifact model during
# current-session revalidation.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from multidetect.release_parity import build_release_parity_document  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty repository-relative path")
    result = (root / value).resolve()
    try:
        result.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes repository root") from exc
    if not result.is_file():
        raise ValueError(f"{label} does not exist: {value}")
    return result


def _verify_v030_pre1_foundation(
    root: Path,
    progress: dict[str, Any],
    *,
    parity_path: Path,
    parity_sha: str,
    release_label: str,
) -> None:
    """Re-open the current-source receipt for the pre.1 runtime foundation."""

    entry = progress.get("v030_pre1_foundation_acceptance_20261003_r5")
    if not isinstance(entry, dict):
        raise ValueError("v0.3.0-pre.1 foundation acceptance reference is missing")
    evidence_path = _path(root, entry.get("evidence"), "v0.3.0-pre.1 foundation evidence")
    if entry.get("evidence_bytes") != evidence_path.stat().st_size:
        raise ValueError("v0.3.0-pre.1 foundation evidence byte count drift")
    evidence_sha = _sha256(evidence_path)
    if entry.get("evidence_file_sha256") != evidence_sha:
        raise ValueError("v0.3.0-pre.1 foundation evidence file SHA-256 drift")
    evidence = _json(evidence_path)
    declared = evidence.get("sha256")
    unsigned = {key: value for key, value in evidence.items() if key != "sha256"}
    if declared != _canonical_sha256(unsigned) or entry.get("evidence_sha256") != declared:
        raise ValueError("v0.3.0-pre.1 foundation canonical SHA-256 drift")
    if (
        evidence.get("schema_version") != 1
        or evidence.get("event") != "v030_pre1_foundation_acceptance"
        or evidence.get("foundation_baseline") != "0.3.0-pre.1"
        or evidence.get("current_release_label") != release_label
        or evidence.get("gate_passed") is not True
    ):
        raise ValueError("v0.3.0-pre.1 foundation identity or gate drift")
    required_checks = (
        "runtime_mode",
        "clock",
        "runtime_profile_isolation",
        "runtime_backend",
        "scenario_manifest_runtime_channel",
        "core_task_contract",
        "deterministic_replay",
        "deterministic_simulator",
    )
    checks = evidence.get("checks")
    if not isinstance(checks, dict) or any(
        not isinstance(checks.get(name), dict) or checks[name].get("passed") is not True
        for name in required_checks
    ):
        raise ValueError("v0.3.0-pre.1 foundation check drift")
    if any(
        evidence.get(key) is not expected
        for key, expected in {
            "truth_free": True,
            "policy_applied_to_actuator": False,
            "flight_control_enabled": False,
            "candidate_promoted": False,
            "final_release_allowed": False,
        }.items()
    ):
        raise ValueError("v0.3.0-pre.1 foundation safety boundary drift")
    parity = evidence.get("parity_file")
    if not isinstance(parity, dict) or (
        parity.get("path") != "configs/release-parity.json"
        or parity.get("bytes") != parity_path.stat().st_size
        or parity.get("sha256") != parity_sha
        or parity.get("release_label") != release_label
    ):
        raise ValueError("v0.3.0-pre.1 foundation parity binding drift")
    source_hashes = evidence.get("source_hashes")
    if not isinstance(source_hashes, list) or not source_hashes:
        raise ValueError("v0.3.0-pre.1 foundation source hashes are missing")
    for item in source_hashes:
        if not isinstance(item, dict):
            raise ValueError("v0.3.0-pre.1 foundation source hash entry is malformed")
        source_path = _path(root, item.get("path"), "v0.3.0-pre.1 foundation source")
        if (
            item.get("bytes") != source_path.stat().st_size
            or item.get("sha256") != _sha256(source_path)
        ):
            raise ValueError("v0.3.0-pre.1 foundation source hash drift")
    if entry.get("release_label") != release_label:
        raise ValueError("v0.3.0-pre.1 foundation progress release label drift")
    for key in (
        "clock_contract_passed",
        "runtime_backend_contract_passed",
        "scenario_manifest_runtime_channel_passed",
        "core_task_contract_passed",
        "runtime_profile_isolation_passed",
        "deterministic_replay_passed",
        "deterministic_simulator_passed",
    ):
        if entry.get(key) is not True:
            raise ValueError(f"v0.3.0-pre.1 foundation progress check drift: {key}")


def verify_release_parity_sources(root: Path) -> dict[str, Any]:
    """Recompute the canonical parity document from its explicit source list.

    The progress index historically checked only that its recorded parity
    bytes/hash matched ``configs/release-parity.json``.  That is insufficient:
    the parity document itself can be stale while remaining internally
    consistent with the index.  This read-only check closes that gap without
    modifying either the canonical document or any evidence.
    """

    root = root.resolve()
    parity_path = root / "configs/release-parity.json"
    parity = _json(parity_path)
    source_files = parity.get("source_files")
    if not isinstance(source_files, dict):
        raise ValueError("release parity source_files mapping is missing")
    try:
        generated = build_release_parity_document(
            root,
            file_list=source_files,
            version_manifest_path=root / "configs/product-version.json",
            output_path=parity_path,
        )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"release parity source recomputation failed: {exc}") from exc
    if generated != parity:
        mismatches = [
            key
            for key in ("schema_version", "product", "release_label", "source_files", "parity")
            if generated.get(key) != parity.get(key)
        ]
        detail = ", ".join(mismatches) or "document"
        raise ValueError(f"release parity source recomputation drift: {detail}")
    return {
        "path": str(parity_path.relative_to(root)).replace("\\", "/"),
        "file_sha256": _sha256(parity_path),
        "source_files": source_files,
        "parity": parity["parity"],
    }


def _verify_current_combined_sim_qgc(root: Path, progress: dict[str, Any]) -> None:
    """Bind the active QGC combined-SIM summary to immutable raw evidence."""

    entry = progress.get("combined_sim_qgc_external_vtol_pass_20261003_r4")
    if not isinstance(entry, dict):
        raise ValueError("current QGC combined-SIM reference is missing")
    source_path = _path(
        root, entry.get("source_evidence"), "current QGC combined-SIM source evidence"
    )
    if entry.get("source_evidence_bytes") != source_path.stat().st_size:
        raise ValueError("current QGC combined-SIM source evidence byte count drift")
    source_sha = _sha256(source_path)
    if str(entry.get("source_evidence_sha256", "")).lower() != source_sha:
        raise ValueError("current QGC combined-SIM source evidence SHA-256 drift")
    summary_path = _path(root, entry.get("evidence"), "current QGC combined-SIM summary")
    if entry.get("evidence_bytes") != summary_path.stat().st_size:
        raise ValueError("current QGC combined-SIM summary byte count drift")
    if str(entry.get("evidence_sha256", "")).lower() != _sha256(summary_path):
        raise ValueError("current QGC combined-SIM summary SHA-256 drift")

    source = _json(source_path)
    summary = _json(summary_path)
    if source.get("schema") != "multidetect.px4-gazebo-qgc-combined-sim-acceptance":
        raise ValueError("current QGC combined-SIM source schema drift")
    if source.get("gate_passed") is not True or source.get("failure") is not None:
        raise ValueError("current QGC combined-SIM gate is not passed")
    if source.get("release_status") != "v0.3.0-pre.8; final v0.3.0 remains blocked":
        raise ValueError("current QGC combined-SIM release boundary drift")
    if summary.get("source_evidence") != entry.get("source_evidence"):
        raise ValueError("current QGC combined-SIM summary source path drift")
    if summary.get("source_evidence_bytes") != source_path.stat().st_size:
        raise ValueError("current QGC combined-SIM summary source byte count drift")
    if str(summary.get("source_evidence_sha256", "")).lower() != source_sha:
        raise ValueError("current QGC combined-SIM summary source SHA-256 drift")
    if summary.get("combined_gate_passed") is not True:
        raise ValueError("current QGC combined-SIM summary gate drift")

    hashes = source.get("hashes")
    if not isinstance(hashes, dict):
        raise ValueError("current QGC combined-SIM hashes are missing")
    parity_path = root / "configs/release-parity.json"
    parity_sha = _sha256(parity_path)
    parity_hashes = hashes.get("release_parity")
    if (
        not isinstance(parity_hashes, dict)
        or str(parity_hashes.get("sha256", "")).lower() != parity_sha
        or parity_hashes.get("bytes") != parity_path.stat().st_size
        or str(entry.get("release_parity_sha256", "")).lower() != parity_sha
    ):
        raise ValueError("current QGC combined-SIM release parity binding drift")
    qgc_hash = hashes.get("qgc_exe")
    build_entry = progress.get("qgc_build_20261003_qml_hitfix")
    if not isinstance(qgc_hash, dict) or not isinstance(build_entry, dict):
        raise ValueError("current QGC combined-SIM executable binding is missing")
    if str(qgc_hash.get("sha256", "")).lower() != str(
        build_entry.get("release_executable_sha256", "")
    ).lower():
        raise ValueError("current QGC combined-SIM executable SHA-256 drift")
    if str(entry.get("qgc_executable_sha256", "")).lower() != str(
        qgc_hash.get("sha256", "")
    ).lower():
        raise ValueError("current QGC combined-SIM progress executable SHA-256 drift")

    qgc = source.get("qgc")
    selection = qgc.get("native_target_selection") if isinstance(qgc, dict) else None
    if (
        not isinstance(qgc, dict)
        or qgc.get("machine_visual_toolbar_smoke") != "passed"
        or qgc.get("native_qquickitem_video_pixels_proved") is not True
        or qgc.get("complete_qml_rendering_semantics_proved") is not False
        or qgc.get("operator_visual_acceptance") != "not_claimed"
        or not isinstance(selection, dict)
        or selection.get("selection_action") != "SELECT_TRK"
        or selection.get("selection_ack_correlated") is not True
        or selection.get("qgc_pid_matches") is not True
        or selection.get("qgc_hwnd_matches") is not True
        or selection.get("qgc_operator_tracked_adoption_proved") is not True
    ):
        raise ValueError("current QGC combined-SIM native selection/video binding drift")
    if (
        source.get("integrated_sim_qgc", {}).get("gate_passed") is not True
        or source.get("integrated_sim_qgc", {}).get("result", {}).get("processed_frames") != 900
        or source.get("vtol_attached", {}).get("gate_passed") is not True
        or source.get("vtol_mission", {}).get("gate_passed") is not True
        or source.get("docker", {}).get("container_stopped_and_removed") is not True
    ):
        raise ValueError("current QGC combined-SIM Mission/cleanup binding drift")
    if (
        source.get("safety", {}).get("real_pixhawk_contacted") is not False
        or source.get("safety", {}).get("real_jetson_contacted") is not False
        or source.get("safety", {}).get("product_runtime_actuator_output_enabled") is not False
    ):
        raise ValueError("current QGC combined-SIM safety boundary drift")


def _verify_gazebo_fault_runs(root: Path, progress: dict[str, Any]) -> None:
    legacy_entry = progress.get("external_gazebo_sensor_fault_contract_20260927_r1")
    if isinstance(legacy_entry, dict):
        legacy_runs = legacy_entry.get("external_runs")
        if not isinstance(legacy_runs, list) or len(legacy_runs) != 3:
            raise ValueError("historical external Gazebo sensor fault baseline is incomplete")
        for legacy_run in legacy_runs:
            if not isinstance(legacy_run, dict):
                raise ValueError("historical external Gazebo sensor fault baseline is invalid")
            source = legacy_run.get("source")
            label = f"external Gazebo {source} fault evidence"
            path = _path(root, legacy_run.get("evidence"), label)
            if legacy_run.get("evidence_bytes") != path.stat().st_size:
                raise ValueError(f"{label} byte count drift")
            if legacy_run.get("evidence_sha256") != _sha256(path):
                raise ValueError(f"{label} SHA-256 drift")
    entry = progress.get("external_gazebo_sensor_fault_contract_20261001_r2")
    if not isinstance(entry, dict):
        raise ValueError("current external Gazebo sensor fault reference is missing")
    runs = entry.get("external_runs")
    if not isinstance(runs, list) or len(runs) != 3:
        raise ValueError("external Gazebo sensor fault source coverage is incomplete")
    if {run.get("source") for run in runs if isinstance(run, dict)} != {
        "image",
        "navsat",
        "odometry",
    }:
        raise ValueError("external Gazebo sensor fault source coverage drift")
    for run in runs:
        source = run["source"]
        label = f"external Gazebo {source} fault evidence"
        path = _path(root, run.get("evidence"), label)
        if run.get("evidence_bytes") != path.stat().st_size:
            raise ValueError(f"{label} byte count drift")
        if run.get("evidence_sha256") != _sha256(path):
            raise ValueError(f"{label} SHA-256 drift")
        report = _json(path)
        sensor = report.get("sensor_transport")
        if not isinstance(sensor, dict):
            raise ValueError(f"{label} sensor transport is missing")
        fault = sensor.get("fault_injection")
        transport = sensor.get("transport")
        diagnostic = fault.get("diagnostic") if isinstance(fault, dict) else None
        expected_frames = 2 if source == "image" else 4
        if (
            run.get("frames") != expected_frames
            or run.get("gate_passed") is not True
            or run.get("recovery_observed") is not True
            or report.get("gate_passed") is not True
            or report.get("runtime_gate_passed") is not True
            or report.get("sensor_transport_gate_passed") is not True
            or report.get("failure") is not None
            or sensor.get("qualified") is not True
            or sensor.get("requested_frame_count") != expected_frames
            or not isinstance(sensor.get("frames"), list)
            or len(sensor["frames"]) != expected_frames
            or not isinstance(transport, dict)
            or transport.get("complete") is not True
            or transport.get("helper_exit_code") != 0
            or transport.get("received_frame_count") != expected_frames
            or not isinstance(diagnostic, dict)
            or diagnostic.get("source") != source
            or diagnostic.get("enabled") is not True
            or diagnostic.get("window_key") != "received_image_index"
            or diagnostic.get("dynamic_source_policy")
            != "reject_during_window_and_until_post_window_fresh_sample"
            or diagnostic.get("cache_boundary_policy")
            != "clear_source_history_on_first_dropped_callback_keep_stamp_order"
            or diagnostic.get("dropped_source_messages", 0) < 1
            or diagnostic.get("recovered_frame_count", 0) < 1
            or diagnostic.get("recovery_observed") is not True
            or report.get("telemetry", {}).get("qualified") is not True
            or report.get("container_runtime", {}).get("cleanup", {}).get("removed") is not True
            or report.get("container_runtime", {}).get("cleanup", {}).get("errors") != []
            or report.get("safety", {}).get("flight_control_commands_sent") != 0
            or report.get("hardware", {}).get("status") != "not_claimed"
            or report.get("visual", {}).get("status") != "not_claimed"
        ):
            raise ValueError(f"{label} recovery/safety contract drift")
        timeline = diagnostic.get("timeline")
        if not isinstance(timeline, list) or not timeline:
            raise ValueError(f"{label} fault timeline is missing")
        if diagnostic.get("timeline_overflow") is not False:
            raise ValueError(f"{label} fault timeline overflowed")
        timeline_events = [item for item in timeline if isinstance(item, dict)]
        if len(timeline_events) != len(timeline):
            raise ValueError(f"{label} fault timeline contains invalid records")
        start_image = run.get("start_image")
        duration_images = run.get("duration_images")
        if type(start_image) is not int or type(duration_images) is not int:
            raise ValueError(f"{label} fault window identity is missing")
        window_end = start_image + duration_images
        if (
            diagnostic.get("start_image") != start_image
            or diagnostic.get("duration_images") != duration_images
        ):
            raise ValueError(f"{label} fault window identity drift")
        received_indices = [item.get("received_image_index") for item in timeline_events]
        if any(type(index) is not int or index < 1 for index in received_indices):
            raise ValueError(f"{label} fault timeline image index is invalid")
        if received_indices != sorted(received_indices):
            raise ValueError(f"{label} fault timeline image index regressed")
        if any(
            type(item.get("fault_active")) is not bool
            or item["fault_active"] != (start_image <= item["received_image_index"] < window_end)
            for item in timeline_events
        ):
            raise ValueError(f"{label} fault timeline window boundary drift")
        dropped_stamps = [
            item.get("source_stamp_ns")
            for item in timeline_events
            if item.get("event") == "source_dropped"
        ]
        if any(type(stamp) is not int or stamp <= 0 for stamp in dropped_stamps):
            raise ValueError(f"{label} dropped source timestamp is invalid")
        observed_drop_events = (
            len(dropped_stamps)
            if source != "image"
            else sum(item.get("event") == "frame_rejected_fault_window" for item in timeline_events)
        )
        if diagnostic.get("dropped_source_messages") != observed_drop_events:
            raise ValueError(f"{label} dropped source count drift")
        dropped_max = max(dropped_stamps, default=None)
        source_epoch = diagnostic.get("source_epoch")
        if source != "image" and (
            type(source_epoch) is not int
            or source_epoch < 1
            or diagnostic.get("cache_cleared") is not True
        ):
            raise ValueError(f"{label} dynamic source cache epoch is missing")
        emitted_wires = [item for item in timeline_events if item.get("event") == "wire_emitted"]
        wire_sequences = [item.get("wire_sequence") for item in emitted_wires]
        if any(type(sequence) is not int or sequence < 0 for sequence in wire_sequences):
            raise ValueError(f"{label} wire sequence is invalid")
        if wire_sequences != list(range(len(wire_sequences))):
            raise ValueError(f"{label} wire sequence is not contiguous")
        recovery_wires = [
            item for item in timeline_events if item.get("event") == "wire_emitted_after_recovery"
        ]
        emitted_by_sequence = {item["wire_sequence"]: item for item in emitted_wires}
        if len(recovery_wires) != 1 or any(
            item["received_image_index"] < window_end
            or type(item.get("wire_sequence")) is not int
            or item["wire_sequence"] not in emitted_by_sequence
            or any(
                item.get(key) != emitted_by_sequence[item["wire_sequence"]].get(key)
                for key in (
                    "received_image_index",
                    "source_stamp_ns",
                    "source_epoch",
                    "fault_active",
                )
            )
            for item in recovery_wires
        ):
            raise ValueError(f"{label} recovery wire is not bound to an emitted wire")
        recovery_index = timeline_events.index(recovery_wires[0])
        matching_wire_index = next(
            index
            for index, item in enumerate(timeline_events)
            if item.get("event") == "wire_emitted"
            and item.get("wire_sequence") == recovery_wires[0].get("wire_sequence")
        )
        if matching_wire_index >= recovery_index:
            raise ValueError(f"{label} recovery marker precedes its emitted wire")
        if source != "image" and not any(
            index < recovery_index
            and item.get("event") == "source_fresh_after_window"
            and type(item.get("source_stamp_ns")) is int
            and dropped_max is not None
            and item["source_stamp_ns"] > dropped_max
            for index, item in enumerate(timeline_events)
        ):
            raise ValueError(f"{label} recovery wire precedes fresh source callback")
        fresh_events = [
            item for item in timeline_events if item.get("event") == "source_fresh_after_window"
        ]
        if source != "image" and (
            dropped_max is None
            or not any(
                type(item.get("source_stamp_ns")) is int and item["source_stamp_ns"] > dropped_max
                for item in fresh_events
            )
        ):
            raise ValueError(f"{label} fresh source recovery is missing")
        for item in emitted_wires:
            source_stamp = item.get("source_stamp_ns")
            if source == "image":
                if source_stamp is not None:
                    raise ValueError(f"{label} image fault wire has a dynamic source stamp")
            elif (
                type(source_stamp) is not int
                or source_stamp <= 0
                or (
                    item["received_image_index"] >= window_end
                    and (dropped_max is None or source_stamp <= dropped_max)
                )
            ):
                raise ValueError(f"{label} wire used a stale dynamic source")
        if source != "image":
            epoch_events = {
                "source_dropped",
                "source_fresh_after_window",
                "frame_rejected_cached_source",
                "frame_rejected_waiting_for_fresh_source",
                "wire_emitted",
                "wire_emitted_after_recovery",
            }
            if any(
                item.get("event") in epoch_events
                and (
                    type(item.get("source_epoch")) is not int
                    or item["source_epoch"]
                    != (0 if item["received_image_index"] < start_image else source_epoch)
                )
                for item in timeline_events
            ):
                raise ValueError(f"{label} source epoch drift")


def _verify_current_external_policy_campaign(
    root: Path,
    progress: dict[str, Any],
    parity_path: Path,
    parity_sha: str,
    parity: dict[str, Any],
) -> None:
    entry = progress.get("shared_policy_external_parent_campaign_20260927_r14")
    if not isinstance(entry, dict):
        raise ValueError("current shared-policy external campaign reference is missing")
    evidence_path = _path(root, entry.get("evidence"), "current campaign evidence")
    summary_path = _path(root, entry.get("campaign_summary"), "current campaign summary")
    receipt_path = _path(root, entry.get("receipt"), "current campaign receipt")
    for label, path, bytes_key, sha_key in (
        ("current campaign evidence", evidence_path, "evidence_bytes", "evidence_sha256"),
        (
            "current campaign summary",
            summary_path,
            "campaign_summary_bytes",
            "campaign_summary_sha256",
        ),
        ("current campaign receipt", receipt_path, "receipt_bytes", "receipt_file_sha256"),
    ):
        if entry.get(bytes_key) != path.stat().st_size:
            raise ValueError(f"{label} byte count drift")
        if entry.get(sha_key) != _sha256(path):
            raise ValueError(f"{label} SHA-256 drift")
    evidence = _json(evidence_path)
    summary = _json(summary_path)
    receipt = _json(receipt_path)
    if (
        entry.get("evidence_scope") != "retained_files_audit"
        or entry.get("current_runtime_parity_proven") is not False
        or entry.get("external_sim_rerun_performed") is not False
        or receipt.get("evidence_scope") != "retained_files_audit"
        or receipt.get("runtime_source_parity_verified") is not False
        or receipt.get("external_sim_rerun_performed") is not False
        or receipt.get("release_parity", {}).get("scope") != "receipt_generation_file_binding"
    ):
        raise ValueError("current campaign retained-file audit scope drift")
    if (
        evidence.get("gate_passed") is not True
        or evidence.get("execution", {}).get("status") != "passed"
        or evidence.get("execution", {}).get("headless") is not True
        or evidence.get("execution", {}).get("qgc_process_started") is not False
        or evidence.get("policy_artifact_input", {}).get("canonical_sha256")
        != entry.get("policy_artifact_canonical_sha256")
        or evidence.get("policy_artifact_input", {}).get("file_sha256")
        != entry.get("policy_artifact_file_sha256")
        or evidence.get("container", {}).get("cleanup", {}).get("cleanup_succeeded") is not True
        or evidence.get("container", {}).get("cleanup", {}).get("post_remove_absence_proven")
        is not True
        or evidence.get("safety", {}).get("sim_only") is not True
        or evidence.get("safety", {}).get("real_pixhawk_contacted") is not False
        or evidence.get("safety", {}).get("outdoor_gnss") != "not_claimed"
    ):
        raise ValueError("current campaign evidence safety or policy binding drift")
    if (
        summary.get("status") != "all_tasks_completed"
        or summary.get("shared_policy_used") is not True
        or summary.get("planner_revalidated") is not True
        or summary.get("policy_artifact_sha256") != entry.get("policy_artifact_canonical_sha256")
        or not isinstance(summary.get("completed"), list)
        or len(summary["completed"]) != entry.get("sortie_count")
    ):
        raise ValueError("current campaign summary completion drift")
    if (
        receipt.get("schema") != "multidetect.shared-policy-campaign-receipt"
        or receipt.get("schema_version") != 1
        or receipt.get("shared_policy_used") is not True
        or receipt.get("planner_revalidated") is not True
        or receipt.get("truth_free") is not True
        or receipt.get("policy_applied_to_actuator") is not False
        or receipt.get("sortie_count") != entry.get("sortie_count")
        or receipt.get("hil_real_evidence") is not False
        or receipt.get("outdoor_gnss_evidence") is not False
        or receipt.get("final_release_allowed") is not False
    ):
        raise ValueError("current campaign receipt safety binding drift")
    receipt_parity = receipt.get("release_parity")
    if (
        not isinstance(receipt_parity, dict)
        or Path(str(receipt_parity.get("path", ""))).resolve() != parity_path.resolve()
        or receipt_parity.get("bytes") != parity_path.stat().st_size
        or receipt_parity.get("file_sha256") != parity_sha
        or receipt_parity.get("hashes") != parity.get("parity")
    ):
        raise ValueError("current campaign receipt release parity drift")
    summary_binding = receipt.get("summary")
    cleanup = receipt.get("cleanup")
    if (
        not isinstance(summary_binding, dict)
        or summary_binding.get("sha256") != _sha256(summary_path)
        or summary_binding.get("status") != "all_tasks_completed"
        or not isinstance(cleanup, dict)
        or cleanup.get("runtime_evidence_sha256") != _sha256(evidence_path)
        or cleanup.get("cleanup_succeeded") is not True
        or cleanup.get("post_remove_absence_proven") is not True
    ):
        raise ValueError("current campaign receipt evidence binding drift")
    declared = receipt.pop("receipt_sha256", None)
    if declared != _canonical_sha256(receipt) or declared != entry.get("receipt_canonical_sha256"):
        raise ValueError("current campaign receipt canonical hash drift")


def _verify_current_shared_policy_sensor_campaign(
    root: Path,
    progress: dict[str, Any],
    parity_path: Path,
    parity_sha: str,
    parity: dict[str, Any],
) -> None:
    """Verify the fresh same-container campaign plus wire-v3 capture evidence."""
    entry = progress.get("shared_policy_external_vtol_sensor_campaign_20261003_r14")
    if entry is None:
        # Keep older progress snapshots readable while preferring the newest
        # source-bound campaign entry in the live index.
        entry = progress.get("shared_policy_external_vtol_sensor_campaign_20261003_r9")
    if not isinstance(entry, dict):
        raise ValueError("current shared-policy sensor campaign reference is missing")
    evidence_path = _path(root, entry.get("evidence"), "shared-policy sensor campaign evidence")
    if entry.get("evidence_bytes") != evidence_path.stat().st_size or entry.get(
        "evidence_sha256"
    ) != _sha256(evidence_path):
        raise ValueError("shared-policy sensor campaign evidence identity drift")
    campaign_path = _path(root, entry.get("campaign_input"), "shared-policy sensor campaign input")
    if entry.get("campaign_input_sha256") != _sha256(campaign_path):
        raise ValueError("shared-policy sensor campaign input identity drift")
    evidence = _json(evidence_path)
    gates = evidence.get("gates")
    campaign = evidence.get("campaign")
    sensor_capture = evidence.get("sensor_capture")
    sensor_document = sensor_capture.get("document") if isinstance(sensor_capture, dict) else None
    transport = sensor_document.get("transport") if isinstance(sensor_document, dict) else None
    parity_snapshot = (
        campaign.get("release_parity_snapshot") if isinstance(campaign, dict) else None
    )
    if (
        evidence.get("gate_passed") is not True
        or evidence.get("execution", {}).get("status") != "passed"
        or evidence.get("execution", {}).get("qgc_process_started") is not False
        or evidence.get("campaign_sensor_capture_requested") is not True
        or evidence.get("campaign_input", {}).get("sha256") != entry.get("campaign_input_sha256")
        or evidence.get("policy_artifact_input", {}).get("canonical_sha256")
        != entry.get("policy_artifact_sha256")
        or evidence.get("policy_artifact_input", {}).get("file_sha256")
        != entry.get("policy_artifact_file_sha256")
        or not isinstance(gates, dict)
        or any(
            gates.get(name) is not True
            for name in (
                "docker_plan",
                "container_identity",
                "router_ready",
                "flight_capture_ready",
                "flight_capture_complete_marker",
                "flight_capture_interval",
                "same_container_sensor_capture",
                "wire_v3_pixels",
                "complete_vtol_mission",
                "mission_journal_integrity",
                "campaign_completed",
                "task_binding",
                "router_graceful_stop",
                "exact_id_cleanup",
                "final_artifact_integrity",
            )
        )
        or evidence.get("container", {}).get("cleanup", {}).get("removed") is not True
        or evidence.get("container", {}).get("cleanup", {}).get("post_remove_absence_proven")
        is not True
        or evidence.get("safety", {}).get("sim_only") is not True
        or evidence.get("safety", {}).get("real_pixhawk_contacted") is not False
        or evidence.get("safety", {}).get("real_jetson_contacted") is not False
        or evidence.get("safety", {}).get("real_camera_contacted") is not False
        or evidence.get("safety", {}).get("outdoor_gnss") != "not_claimed"
        or evidence.get("safety", {}).get("ground_truth_used_for_runtime_or_capture") is not False
    ):
        raise ValueError("shared-policy sensor campaign safety or gate binding drift")
    if (
        entry.get("release_parity_file_sha256") != parity_sha
        or entry.get("release_parity_snapshot_stable") is not True
        or not isinstance(parity_snapshot, dict)
        or parity_snapshot.get("requested") is not True
        or parity_snapshot.get("stable") is not True
        or parity_snapshot.get("start", {}).get("file_sha256") != parity_sha
        or parity_snapshot.get("end", {}).get("file_sha256") != parity_sha
        or parity_snapshot.get("start", {}).get("hashes") != parity.get("parity")
        or parity_snapshot.get("end", {}).get("hashes") != parity.get("parity")
    ):
        raise ValueError("shared-policy sensor campaign parity binding drift")
    if (
        not isinstance(campaign, dict)
        or campaign.get("camera_capture_claimed") is not False
        or campaign.get("sensor_transport_used") is not False
        or campaign.get("sensor_frame_count") != 0
        or campaign.get("release_parity_snapshot_gate_passed") is not True
        or campaign.get("summary_sha256") is None
        or [item.get("task_id") for item in campaign.get("verified_sorties", [])]
        != entry.get("tasks")
    ):
        raise ValueError("shared-policy sensor campaign Mission summary drift")
    if (
        not isinstance(sensor_capture, dict)
        or sensor_capture.get("same_container_as_flight_barrier") is not True
        or sensor_capture.get("docker_exec_target_is_exact_id") is not True
        or sensor_capture.get("campaign_first_sortie_binding", {}).get("task_binding_claimed")
        is not True
        or sensor_capture.get("campaign_first_sortie_binding", {}).get("task_id")
        != entry.get("binding_task_id")
        or sensor_capture.get("campaign_first_sortie_binding", {}).get("mission_hash")
        != entry.get("binding_mission_sha256")
        or sensor_capture.get("campaign_first_sortie_binding", {}).get("transaction_id")
        != entry.get("binding_transaction_id")
        or sensor_capture.get("completion_marker", {}).get("document", {}).get("task_id")
        != entry.get("binding_task_id")
        or sensor_capture.get("completion_marker", {}).get("document", {}).get(
            "executable_mission_sha256"
        )
        != entry.get("binding_mission_sha256")
        or sensor_capture.get("completion_marker", {}).get("document", {}).get(
            "transaction_id"
        )
        != entry.get("binding_transaction_id")
        or not isinstance(sensor_document, dict)
        or sensor_document.get("qualified") is not True
        or sensor_document.get("truth_free") is not True
        or sensor_document.get("requested_frame_count") != entry.get("sensor_frame_count")
        or not isinstance(sensor_document.get("frames"), list)
        or len(sensor_document["frames"]) != entry.get("sensor_frame_count")
        or not isinstance(transport, dict)
        or transport.get("received_frame_count") != entry.get("sensor_frame_count")
        or transport.get("complete") is not True
        or sensor_document.get("wire_version") != 3
        or transport.get("helper_exit_code") != 0
    ):
        raise ValueError("shared-policy sensor capture binding drift")


def _verify_current_external_shared_policy_pair(
    root: Path, progress: dict[str, Any], parity: dict[str, Any]
) -> None:
    entry = progress.get("external_shared_policy_pair_20260927_r1")
    if not isinstance(entry, dict):
        raise ValueError("external shared-policy pair reference is missing")
    receipt_path = _path(root, entry.get("evidence"), "external shared-policy pair receipt")
    if entry.get("evidence_bytes") != receipt_path.stat().st_size:
        raise ValueError("external shared-policy pair receipt byte count drift")
    if entry.get("evidence_sha256") != _sha256(receipt_path):
        raise ValueError("external shared-policy pair receipt SHA-256 drift")
    receipt = _json(receipt_path)
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        receipt.get("schema") != "multidetect.external-shared-policy-pair-receipt"
        or receipt.get("receipt_sha256") != _canonical_sha256(unsigned)
        or receipt.get("release_label") != "0.3.0-pre.8"
        or receipt.get("same_ranker") is not True
        or receipt.get("same_policy_artifact") is not True
        or receipt.get("ranker_sha256") != entry.get("ranker_sha256")
        or receipt.get("policy_artifact_sha256") != entry.get("policy_artifact_sha256")
        or receipt.get("current_release_parity_bound") is not True
        or receipt.get("truth_free") is not True
        or receipt.get("policy_applied_to_actuator") is not False
        or receipt.get("flight_started") is not False
        or receipt.get("physical_output_enabled") is not False
        or receipt.get("final_release_allowed") is not False
    ):
        raise ValueError("external shared-policy pair receipt binding drift")
    vehicles = receipt.get("vehicles")
    if not isinstance(vehicles, dict) or set(vehicles) != {"fixed-wing", "multirotor"}:
        raise ValueError("external shared-policy pair vehicle coverage drift")
    for vehicle, source in vehicles.items():
        if (
            not isinstance(source, dict)
            or source.get("vehicle_class") != vehicle
            or source.get("ranker_sha256") != receipt.get("ranker_sha256")
            or source.get("policy_artifact_sha256") != receipt.get("policy_artifact_sha256")
            or source.get("planner_revalidated") is not True
            or source.get("policy_applied_to_actuator") is not False
            or source.get("truth_free") is not True
            or source.get("flight_started") is not False
            or source.get("physical_output_enabled") is not False
            or source.get("current_release_parity_bound") is not True
        ):
            raise ValueError(f"external shared-policy pair {vehicle} binding drift")
        evidence = _path(root, source.get("evidence"), f"external shared-policy {vehicle} evidence")
        if source.get("evidence_bytes") != evidence.stat().st_size or source.get(
            "evidence_sha256"
        ) != _sha256(evidence):
            raise ValueError(f"external shared-policy {vehicle} evidence identity drift")
        document = _json(evidence)
        if (
            document.get("shared_policy_used") is not True
            or document.get("ranker_sha256") != receipt.get("ranker_sha256")
            or document.get("policy_artifact_sha256") != receipt.get("policy_artifact_sha256")
            or document.get("current_release_parity_bound") is not True
        ):
            raise ValueError(f"external shared-policy {vehicle} source drift")


def _verify_current_shared_policy_vehicle_matrix(root: Path, progress: dict[str, Any]) -> None:
    entry = progress.get("shared_policy_vehicle_matrix_20260927_r2")
    if not isinstance(entry, dict):
        raise ValueError("shared-policy vehicle matrix reference is missing")
    receipt_path = _path(root, entry.get("evidence"), "shared-policy vehicle matrix")
    if entry.get("evidence_bytes") != receipt_path.stat().st_size or entry.get(
        "evidence_sha256"
    ) != _sha256(receipt_path):
        raise ValueError("shared-policy vehicle matrix file identity drift")
    receipt = _json(receipt_path)
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        receipt.get("receipt_sha256") != _canonical_sha256(unsigned)
        or receipt.get("release_label") != "0.3.0-pre.8"
        or receipt.get("same_external_mission_path") is not True
        or receipt.get("same_core_compiler_and_transaction") is not True
        or receipt.get("same_high_level_ranker") is not True
        or receipt.get("same_policy_artifact") is not False
        or receipt.get("unified_policy_model_proven") is not True
        or receipt.get("policy_identity_scope")
        != "shared_ranker_with_airframe_bound_policy_artifacts"
        or receipt.get("current_release_parity_bound") is not True
        or receipt.get("truth_free") is not True
        or receipt.get("flight_started") is not False
        or receipt.get("physical_output_enabled") is not False
        or receipt.get("final_release_allowed") is not False
    ):
        raise ValueError("shared-policy vehicle matrix identity or safety drift")
    expected_ranker = entry.get("ranker_sha256")
    if receipt.get("ranker_sha256_by_vehicle") != {
        "fixed-wing": expected_ranker,
        "multirotor": expected_ranker,
        "vtol": expected_ranker,
    }:
        raise ValueError("shared-policy vehicle matrix ranker identity drift")
    if receipt.get("policy_artifact_sha256_by_vehicle") != entry.get(
        "policy_artifact_sha256_by_vehicle"
    ):
        raise ValueError("shared-policy vehicle matrix artifact identity drift")
    for key in ("campaign_file", "policy_artifact_file"):
        path = _path(root, entry.get(key), f"shared-policy {key}")
        if entry.get(f"{key}_sha256") != _sha256(path):
            raise ValueError(f"shared-policy {key} SHA-256 drift")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(root / "src") not in sys.path:
        sys.path.insert(0, str(root / "src"))
    try:
        from scripts.verify_shared_policy_vehicle_matrix_receipt import verify_receipt

        result = verify_receipt(root=root, receipt_path=receipt_path)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"shared-policy vehicle matrix revalidation failed: {exc}") from exc
    if result.get("status") != "passed" or result.get("receipt_sha256") != receipt.get(
        "receipt_sha256"
    ):
        raise ValueError("shared-policy vehicle matrix source revalidation drift")


def _verify_current_shared_policy_vtol_campaign(
    root: Path, progress: dict[str, Any], parity_path: Path, parity: dict[str, Any]
) -> None:
    """Keep the indexed VTOL campaign and its current-parity receipt aligned.

    The VTOL campaign receipt is also used as a source by the three-vehicle
    matrix.  Checking it here prevents the progress index from silently
    retaining a receipt produced against an older release-parity document.
    """

    entry = progress.get("shared_policy_current_vtol_campaign_20260928_r2")
    if not isinstance(entry, dict):
        raise ValueError("current shared-policy VTOL campaign reference is missing")
    evidence_path = _path(root, entry.get("evidence"), "current shared-policy VTOL evidence")
    summary_path = _path(root, entry.get("summary"), "current shared-policy VTOL summary")
    receipt_path = _path(root, entry.get("receipt"), "current shared-policy VTOL receipt")
    for label, path, bytes_key, sha_key in (
        ("evidence", evidence_path, "evidence_bytes", "evidence_sha256"),
        ("summary", summary_path, None, "summary_sha256"),
        ("receipt", receipt_path, "receipt_bytes", "receipt_file_sha256"),
    ):
        if bytes_key is not None and entry.get(bytes_key) != path.stat().st_size:
            raise ValueError(f"current shared-policy VTOL {label} byte count drift")
        if entry.get(sha_key) != _sha256(path):
            raise ValueError(f"current shared-policy VTOL {label} SHA-256 drift")

    evidence = _json(evidence_path)
    summary = _json(summary_path)
    receipt = _json(receipt_path)
    if (
        evidence.get("schema") != "multidetect.vtol-selected-campaign"
        or evidence.get("gate_passed") is not True
        or evidence.get("campaign", {}).get("summary_sha256") != _sha256(summary_path)
        or evidence.get("cleanup_failures") != []
        or evidence.get("container", {}).get("cleanup", {}).get("removed") is not True
        or evidence.get("container", {}).get("cleanup", {}).get("post_remove_absence_proven")
        is not True
    ):
        raise ValueError("current shared-policy VTOL runtime evidence drift")
    if (
        summary.get("status") != "all_tasks_completed"
        or summary.get("shared_policy_used") is not True
        or summary.get("planner_revalidated") is not True
        or summary.get("policy_artifact_sha256") != entry.get("policy_artifact_canonical_sha256")
        or summary.get("ranker_sha256") != entry.get("ranker_sha256")
    ):
        raise ValueError("current shared-policy VTOL campaign summary drift")
    release = receipt.get("release_parity")
    if (
        not isinstance(release, dict)
        or release.get("verified") is not True
        or release.get("bytes") != parity_path.stat().st_size
        or release.get("file_sha256") != _sha256(parity_path)
        or release.get("hashes") != parity.get("parity")
        or release.get("release_label") != "0.3.0-pre.8"
    ):
        raise ValueError("current shared-policy VTOL release parity binding drift")
    if (
        receipt.get("schema") != "multidetect.shared-policy-campaign-receipt"
        or receipt.get("receipt_sha256") != entry.get("receipt_canonical_sha256")
        or receipt.get("summary", {}).get("sha256") != _sha256(summary_path)
        or receipt.get("campaign", {}).get("sha256") != entry.get("campaign_sha256")
        or receipt.get("shared_policy_used") is not True
        or receipt.get("planner_revalidated") is not True
        or receipt.get("truth_free") is not True
        or receipt.get("policy_applied_to_actuator") is not False
        or receipt.get("final_release_allowed") is not False
        or receipt.get("policy_artifact_sha256") != entry.get("policy_artifact_canonical_sha256")
        or receipt.get("ranker_sha256") != entry.get("ranker_sha256")
        or receipt.get("sortie_count") != entry.get("sortie_count")
    ):
        raise ValueError("current shared-policy VTOL receipt identity or safety drift")
    artifact_path = _path(
        root, entry.get("policy_artifact"), "current shared-policy VTOL policy artifact"
    )
    if entry.get("policy_artifact_file_sha256") != _sha256(artifact_path):
        raise ValueError("current shared-policy VTOL policy artifact file identity drift")


def _verify_current_shared_policy_runtime_matrix(
    root: Path, progress: dict[str, Any], parity: dict[str, Any]
) -> None:
    entry = progress.get("shared_policy_runtime_matrix_20260927_r3")
    if not isinstance(entry, dict):
        raise ValueError("current shared-policy runtime matrix reference is missing")
    evidence_path = _path(root, entry.get("evidence"), "shared-policy runtime matrix")
    if entry.get("evidence_bytes") != evidence_path.stat().st_size or entry.get(
        "evidence_sha256"
    ) != _sha256(evidence_path):
        raise ValueError("shared-policy runtime matrix file identity drift")
    artifact_path = _path(root, entry.get("policy_artifact_file"), "shared-policy runtime artifact")
    if entry.get("policy_artifact_file_sha256") != _sha256(artifact_path):
        raise ValueError("shared-policy runtime artifact file identity drift")
    evidence = _json(evidence_path)
    expected_modes = {"replay", "sim", "hil", "real"}
    modes = evidence.get("modes")
    if (
        evidence.get("schema_version") != 1
        or evidence.get("status") != "passed_shared_policy_runtime_matrix"
        or evidence.get("same_policy_artifact") is not True
        or evidence.get("same_airframe_profile") is not True
        or evidence.get("same_release_parity") is not True
        or evidence.get("same_ui_contract") is not True
        or evidence.get("actuator_capability_granted") is not False
        or evidence.get("backend_started") is not False
        or evidence.get("mavlink_sent") is not False
        or evidence.get("hil_real_evidence") is not False
        or evidence.get("outdoor_gnss_evidence") is not False
        or evidence.get("final_release_allowed") is not False
        or evidence.get("release_parity") != parity.get("parity")
        or set(entry.get("modes", [])) != expected_modes
    ):
        raise ValueError("shared-policy runtime matrix contract drift")
    if not isinstance(modes, dict) or set(modes) != expected_modes:
        raise ValueError("shared-policy runtime matrix mode coverage drift")
    artifact_document = _json(artifact_path)
    if evidence.get("policy_artifact_sha256") != entry.get("policy_artifact_canonical_sha256"):
        raise ValueError("shared-policy runtime artifact canonical identity drift")
    if evidence.get("airframe_profile_hash") != entry.get("airframe_profile_sha256"):
        raise ValueError("shared-policy runtime airframe identity drift")
    if evidence.get("policy_artifact_sha256") != entry.get("policy_artifact_canonical_sha256"):
        raise ValueError("shared-policy runtime policy identity drift")
    if artifact_document.get("airframe_profile_hash") != entry.get("airframe_profile_sha256"):
        raise ValueError("shared-policy runtime artifact profile drift")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from multidetect.autonomy.policy_artifact import PolicyArtifact

        artifact = PolicyArtifact.from_mapping(artifact_document)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("shared-policy runtime artifact parsing failed") from exc
    if artifact.canonical_hash != evidence.get("policy_artifact_sha256"):
        raise ValueError("shared-policy runtime artifact canonical hash drift")
    for mode, row in modes.items():
        if (
            not isinstance(row, dict)
            or row.get("policy_artifact_sha256") != evidence.get("policy_artifact_sha256")
            or row.get("airframe_profile_hash") != evidence.get("airframe_profile_hash")
            or row.get("release_parity") != parity.get("parity")
            or row.get("flight_control_output") is not False
        ):
            raise ValueError(f"shared-policy runtime {mode} row drift")


def _verify_current_airframe_transaction_acceptance(
    root: Path,
    progress: dict[str, Any],
    parity: dict[str, Any],
) -> None:
    entry = progress.get("px4_airframe_transaction_acceptance_20260927_r2")
    if not isinstance(entry, dict):
        raise ValueError("current three-airframe transaction reference is missing")
    evidence_path = _path(root, entry.get("evidence"), "three-airframe transaction evidence")
    if entry.get("evidence_bytes") != evidence_path.stat().st_size:
        raise ValueError("three-airframe transaction evidence byte count drift")
    if entry.get("evidence_sha256") != _sha256(evidence_path):
        raise ValueError("three-airframe transaction evidence SHA-256 drift")
    document = _json(evidence_path)
    expected_classes = {"vtol", "fixed-wing", "multirotor"}
    entries = document.get("entries")
    if (
        document.get("schema") != "multidetect.px4-airframe-mission-transaction-acceptance"
        or document.get("schema_version") != 1
        or document.get("release_label") != progress.get("product_version", {}).get("release_label")
        or document.get("runtime_mode") != "SIM"
        or document.get("truth_free") is not True
        or document.get("physical_output_enabled") is not False
        or document.get("gate_passed") is not True
        or document.get("shared_dispatcher") != entry.get("shared_dispatcher")
        or document.get("shared_transport") != entry.get("shared_transport")
        or not isinstance(entries, list)
        or {item.get("vehicle_class") for item in entries if isinstance(item, dict)}
        != expected_classes
        or len(entries) != len(expected_classes)
    ):
        raise ValueError("three-airframe transaction top-level binding drift")
    for item in entries:
        if (
            not isinstance(item, dict)
            or item.get("gate_passed") is not True
            or item.get("truth_free") is not True
            or item.get("physical_output_enabled") is not False
            or not isinstance(item.get("checks"), dict)
            or not all(value is True for value in item["checks"].values())
        ):
            raise ValueError("three-airframe transaction entry safety drift")
        journal = item.get("mission_journal")
        if not isinstance(journal, dict):
            raise ValueError("three-airframe transaction journal binding is missing")
        journal_path = _path(root, journal.get("path"), "three-airframe transaction journal")
        if journal.get("sha256") != _sha256(journal_path):
            raise ValueError("three-airframe transaction journal SHA-256 drift")
        if (
            journal.get("state") != "start_permitted"
            or journal.get("recovery_required") is not False
        ):
            raise ValueError("three-airframe transaction journal state drift")
    if set(entry.get("vehicle_classes", [])) != expected_classes:
        raise ValueError("three-airframe transaction progress class order drift")
    if entry.get("gate_passed") is not True or entry.get("all_entries_passed") is not True:
        raise ValueError("three-airframe transaction progress gate drift")


def _verify_current_shared_policy_observed_session(
    root: Path,
    progress: dict[str, Any],
    parity_path: Path,
    parity_sha: str,
    parity: dict[str, Any],
) -> None:
    """Revalidate the latest truth-free observed session against current policy identity."""
    entry = progress.get("shared_policy_observed_session_20260927_r17")
    if not isinstance(entry, dict):
        raise ValueError("current shared-policy observed session reference is missing")
    evidence_path = _path(root, entry.get("evidence"), "current observed session evidence")
    if entry.get("evidence_bytes") != evidence_path.stat().st_size or entry.get(
        "evidence_sha256"
    ) != _sha256(evidence_path):
        raise ValueError("current observed session evidence file identity drift")
    evidence = _json(evidence_path)
    campaign_path = _path(root, evidence.get("campaign"), "current observed session campaign")
    if (
        entry.get("campaign") != evidence.get("campaign")
        or entry.get("campaign_sha256") != _sha256(campaign_path)
        or evidence.get("campaign_sha256") != entry.get("campaign_sha256")
    ):
        raise ValueError("current observed session campaign identity drift")
    artifact_path = _path(
        root, evidence.get("policy_artifact"), "current observed session policy artifact"
    )
    if (
        entry.get("policy_artifact") != evidence.get("policy_artifact")
        or entry.get("policy_artifact_file_sha256") != _sha256(artifact_path)
        or evidence.get("policy_artifact_file_sha256") != entry.get("policy_artifact_file_sha256")
        or evidence.get("policy_artifact_canonical_sha256")
        != entry.get("policy_artifact_canonical_sha256")
    ):
        raise ValueError("current observed session policy artifact identity drift")
    if (
        evidence.get("release_parity", {}).get("path") != "configs/release-parity.json"
        or evidence.get("release_parity", {}).get("bytes") != parity_path.stat().st_size
        or evidence.get("release_parity", {}).get("file_sha256") != parity_sha
        or evidence.get("release_parity", {}).get("hashes") != parity.get("parity")
        or entry.get("release_parity_file_sha256") != parity_sha
    ):
        raise ValueError("current observed session release parity binding drift")
    required = {
        "all_tasks_completed": True,
        "deterministic_repeat_passed": True,
        "planner_revalidated": True,
        "truth_free": True,
        "backend_started": False,
        "mavlink_sent": False,
        "actuator_capability_granted": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    if any(evidence.get(key) is not value for key, value in required.items()):
        raise ValueError("current observed session safety/completion contract drift")
    if any(entry.get(key) is not value for key, value in required.items()):
        raise ValueError("current observed session progress safety/completion drift")
    if evidence.get("ranker_sha256") != entry.get("ranker_sha256"):
        raise ValueError("current observed session ranker identity drift")
    artifact_document = _json(artifact_path)
    if artifact_document.get("airframe_profile_hash") != evidence.get("airframe_profile_sha256"):
        raise ValueError("current observed session airframe profile drift")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from multidetect.autonomy.policy_artifact import PolicyArtifact

        artifact = PolicyArtifact.from_mapping(artifact_document)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("current observed session policy artifact parsing failed") from exc
    if artifact.canonical_hash != evidence.get(
        "policy_artifact_canonical_sha256"
    ) or artifact.ranker_sha256 != evidence.get("ranker_sha256"):
        raise ValueError("current observed session policy artifact canonical hash drift")
    observed_session = evidence.get("session")
    if not isinstance(observed_session, dict):
        raise ValueError("current observed session details are missing")
    if evidence.get("session_sha256") != _canonical_sha256(observed_session):
        raise ValueError("current observed session canonical hash drift")
    selected_ids = ["synthetic-east", "synthetic-west"]
    if (
        observed_session.get("stop_reason") != "all_tasks_completed"
        or observed_session.get("remaining_task_ids") != []
        or observed_session.get("selected_task_ids") != selected_ids
        or observed_session.get("planner_revalidated") is not True
        or observed_session.get("step_count") != 2
        or not isinstance(observed_session.get("steps"), list)
        or len(observed_session["steps"]) != 2
        or entry.get("selected_task_ids") != selected_ids
        or entry.get("completed_task_count") != 2
    ):
        raise ValueError("current observed session task sequence drift")
    for step, expected_task_id in zip(observed_session["steps"], selected_ids, strict=True):
        if (
            not isinstance(step, dict)
            or step.get("task_id") != expected_task_id
            or step.get("planner_revalidated") is not True
            or step.get("policy_artifact_sha256")
            != evidence.get("policy_artifact_canonical_sha256")
            or step.get("ranker_sha256") != evidence.get("ranker_sha256")
            or step.get("outcome", {}).get("status") != "completed"
        ):
            raise ValueError("current observed session step policy/outcome drift")


def _recompute_external_gazebo_sensor_binding_hash(
    root: Path,
    entry: dict[str, Any],
    evidence: dict[str, Any],
    *,
    model_name: str,
) -> str:
    """Rebuild the backend's sensor-binding identity from repository inputs."""

    plan = evidence.get("plan")
    sensor = evidence.get("sensor_transport")
    if not isinstance(plan, dict) or not isinstance(sensor, dict):
        raise ValueError("sensor binding provenance is incomplete")
    transport = sensor.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("sensor binding transport is incomplete")
    scenario_path = _path(root, entry.get("scenario"), "external scenario")
    scenario_document = _json(scenario_path)
    runtime_reference = scenario_document.get("channels", {}).get("runtime_input")
    if not isinstance(runtime_reference, dict):
        raise ValueError("sensor binding runtime channel is missing")
    runtime_path = _path(
        scenario_path.parent,
        runtime_reference.get("path"),
        "external runtime channel",
    )
    runtime_document = _json(runtime_path)
    rgb_sensors = [
        item
        for item in runtime_document.get("sensors", [])
        if isinstance(item, dict) and item.get("kind") == "rgb_camera"
    ]
    depth_sensors = [
        item
        for item in runtime_document.get("sensors", [])
        if isinstance(item, dict) and item.get("kind") == "depth_camera"
    ]
    if len(rgb_sensors) != 1 or len(depth_sensors) != 1:
        raise ValueError("sensor binding requires one RGB and one depth camera")
    rgb_sensor = rgb_sensors[0]
    depth_sensor = depth_sensors[0]
    model_instance = f"{model_name}_0"
    world = "default"
    topics = {
        "schema_version": 1,
        "world": world,
        "model_instance": model_instance,
        "image_topic": (
            f"/world/{world}/model/{model_instance}/link/camera_link/sensor/camera/image"
        ),
        "camera_info_topic": (
            f"/world/{world}/model/{model_instance}/link/camera_link/sensor/camera/camera_info"
        ),
        "navsat_topic": (
            f"/world/{world}/model/{model_instance}/link/base_link/sensor/navsat_sensor/navsat"
        ),
        "odometry_topic": f"/model/{model_instance}/odometry_with_covariance",
        "depth_topic": (
            f"/world/{world}/model/{model_instance}/link/depth_camera_link/"
            "sensor/depth_camera/depth_image"
        ),
    }
    topic_hash = _canonical_sha256(topics)
    camera_calibration_evidence = sensor.get("camera_calibration")
    if not isinstance(camera_calibration_evidence, dict):
        raise ValueError("sensor binding camera calibration is missing")
    calibration_path = _path(
        root,
        f"simulation/gazebo/calibration/{rgb_sensor.get('calibration_id')}.json",
        "external RGB calibration",
    )
    calibration_document = _json(calibration_path).get("calibration")
    if (
        not isinstance(calibration_document, dict)
        or camera_calibration_evidence.get("calibration") != calibration_document
        or camera_calibration_evidence.get("file_sha256") != _sha256(calibration_path)
    ):
        raise ValueError("sensor binding camera calibration provenance drift")
    rgb_binding = {
        "schema_version": 1,
        "sensor_id": rgb_sensor.get("sensor_id"),
        "kind": rgb_sensor.get("kind"),
        "calibration_id": rgb_sensor.get("calibration_id"),
        "stream_uri": rgb_sensor.get("stream_uri"),
        "topic_binding": topics,
        "topic_binding_sha256": topic_hash,
        "source_row_order": sensor.get("source_row_order"),
        "wire_row_order": sensor.get("wire_row_order"),
        "vertical_flip_applied": sensor.get("vertical_flip_applied"),
        "camera_calibration_sha256": _canonical_sha256(calibration_document),
        "camera_info_intrinsics_semantic_validation": sensor.get("transport", {}).get(
            "camera_info_intrinsics_semantic_validation"
        ),
    }
    depth_calibration_document = {
        "schema_version": 1,
        "calibration_id": depth_sensor.get("calibration_id"),
        "sensor_id": depth_sensor.get("sensor_id"),
        "width_px": calibration_document["width_px"],
        "height_px": calibration_document["height_px"],
        "fx_px": calibration_document["fx_px"],
        "fy_px": calibration_document["fy_px"],
        "cx_px": calibration_document["cx_px"],
        "cy_px": calibration_document["cy_px"],
        "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
        "rectification": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "extrinsics_rgb_to_depth": {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        "registration_mode": "registered_to_rgb",
        "coordinate_space": "rgb_pixel_aligned",
        "encoding": "FLOAT32_M",
        "distance_semantics": "optical_axis_z_m",
        "near_clip_m": 0.1,
        "far_clip_m": 3000.0,
    }
    depth_binding = {
        "sensor_id": depth_sensor.get("sensor_id"),
        "kind": "depth_camera",
        "calibration_id": depth_sensor.get("calibration_id"),
        "stream_uri": depth_sensor.get("stream_uri"),
        "topic": topics["depth_topic"],
        "calibration_sha256": _canonical_sha256(depth_calibration_document),
        "registration_mode": "registered_to_rgb",
        "coordinate_space": "rgb_pixel_aligned",
        "encoding": "FLOAT32_M",
        "distance_semantics": "optical_axis_z_m",
    }
    depth_contract = {
        "sensor_id": depth_sensor.get("sensor_id"),
        "source_topic": topics["depth_topic"],
        "calibration_id": depth_sensor.get("calibration_id"),
        "calibration_sha256": _canonical_sha256(depth_calibration_document),
        "registration_mode": "registered_to_rgb",
        "coordinate_space": "rgb_pixel_aligned",
        "encoding": "FLOAT32_M",
        "distance_semantics": "optical_axis_z_m",
        "width": calibration_document["width_px"],
        "height": calibration_document["height_px"],
        "minimum_depth_m": 0.1,
        "maximum_depth_m": 3000.0,
        "fx_px": calibration_document["fx_px"],
        "fy_px": calibration_document["fy_px"],
        "cx_px": calibration_document["cx_px"],
        "cy_px": calibration_document["cy_px"],
    }
    sensor_binding = dict(rgb_binding)
    sensor_binding["depth_sensor"] = depth_binding
    image_reference = plan.get("image")
    if not isinstance(image_reference, str) or "@sha256:" not in image_reference:
        raise ValueError("sensor binding image digest is missing")
    image_hash = image_reference.rsplit("@sha256:", 1)[1]
    environment = plan.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("sensor binding simulator environment is missing")
    try:
        scenario_seed = int(environment["MD_SIM_SEED"])
        frame_count = int(sensor["requested_frame_count"])
        width = int(calibration_document["width_px"])
        height = int(calibration_document["height_px"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("sensor binding numeric provenance is invalid") from exc
    document = {
        "schema_version": 3,
        "wire_version": sensor.get("wire_version"),
        "scenario_manifest_hash": plan.get("scenario_manifest_hash"),
        "scenario_seed": scenario_seed,
        "container_image_sha256": image_hash,
        "stream_script_sha256": sensor.get("stream_script_sha256"),
        "model_assets_sha256": sensor.get("model_assets_sha256"),
        "model_instance": model_instance,
        "world": world,
        "scenario_rgb_camera": sensor_binding,
        "scenario_depth_camera": depth_contract,
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "simulated_gps_quality": {
            "fix_type": transport.get("simulated_gps_fix_type"),
            "satellites_visible": transport.get("simulated_satellites_visible"),
            "horizontal_accuracy_m": transport.get("simulated_gps_horizontal_accuracy_m"),
            "vertical_accuracy_m": transport.get("simulated_gps_vertical_accuracy_m"),
        },
        "terrain_agl": {
            "terrain_dem_sha256": sensor.get("terrain_dem_sha256"),
            "altitude_reference": "GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL",
            "odometry_local_up_used_as_agl": False,
        },
    }
    return _canonical_sha256(document)


def _verify_current_external_multirotor_shared_policy_depth_mission(
    root: Path,
    progress: dict[str, Any],
    parity_path: Path,
    parity_sha: str,
    parity: dict[str, Any],
    *,
    entry_key: str = "external_multirotor_shared_policy_depth_mission_20260930",
) -> None:
    """Revalidate the same-runtime external Mission + depth evidence."""
    entry = progress.get(entry_key)
    if not isinstance(entry, dict):
        raise ValueError("external multirotor shared-policy depth Mission reference is missing")
    evidence_path = _path(
        root,
        entry.get("evidence"),
        "external multirotor shared-policy depth Mission evidence",
    )
    if entry.get("evidence_bytes") != evidence_path.stat().st_size:
        raise ValueError("external multirotor shared-policy depth Mission byte count drift")
    evidence_sha = _sha256(evidence_path)
    if entry.get("evidence_sha256") != evidence_sha:
        raise ValueError("external multirotor shared-policy depth Mission SHA-256 drift")
    legacy_billboard_audit = entry.get("superseded") is True and (
        entry.get("status") == "superseded_legacy_billboard_model_audit"
        or evidence_path.name in {
            "external-fixed-wing-depth-shared-policy-20261006-current-parity-audit-r4.json",
            "external-multirotor-depth-shared-policy-20261006-current-parity-audit-r4.json",
            "external-fixed-wing-depth-shared-policy-20261006-geometry-label-r1.json",
            "external-multirotor-depth-shared-policy-20261006-geometry-label-r1.json",
        }
    )
    evidence = _json(evidence_path)
    evidence_vehicle_label = (
        "fixed-wing" if entry_key.startswith("external_fixed_wing") else "multirotor"
    )
    if (
        evidence.get("schema") != "multidetect.simulator-docker-acceptance"
        or evidence.get("schema_version") != 1
        or evidence.get("gate_passed") is not True
        or evidence.get("runtime_gate_passed") is not True
        or evidence.get("mission_transaction_gate_passed") is not True
        or evidence.get("sensor_transport_gate_passed") is not True
        or evidence.get("shared_policy_used") is not True
        or evidence.get("current_release_parity_bound") is not True
        or evidence.get("policy_artifact_sha256") != entry.get("policy_artifact_canonical_sha256")
        or evidence.get("ranker_sha256") != entry.get("ranker_sha256")
    ):
        raise ValueError("external multirotor shared-policy depth Mission gate binding drift")
    parity_binding = evidence.get("release_parity")
    if (
        not isinstance(parity_binding, dict)
        or parity_binding.get("path") != "configs/release-parity.json"
        or parity_binding.get("bytes") != parity_path.stat().st_size
        or parity_binding.get("file_sha256") != parity_sha
        or parity_binding.get("hashes") != parity.get("parity")
    ):
        raise ValueError("external multirotor shared-policy depth Mission parity drift")
    policy_path = _path(root, entry.get("policy_artifact"), "external multirotor policy artifact")
    ranker_path = _path(root, entry.get("ranker"), "external multirotor ranker")
    if entry.get("policy_artifact_file_sha256") != _sha256(policy_path) or entry.get(
        "ranker_file_sha256"
    ) != _sha256(ranker_path):
        raise ValueError("external multirotor policy input file identity drift")
    artifact_document = _json(policy_path)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from multidetect.autonomy.policy_artifact import PolicyArtifact
        from multidetect.autonomy.task_ranker import load_task_ranker

        artifact = PolicyArtifact.from_mapping(artifact_document)
        ranker = load_task_ranker(ranker_path, expected_sha256=entry.get("ranker_file_sha256", ""))
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("external multirotor policy/ranker parsing failed") from exc
    if (
        artifact.canonical_hash != entry.get("policy_artifact_canonical_sha256")
        or ranker.canonical_hash != entry.get("ranker_sha256")
        or artifact.ranker_sha256 != ranker.canonical_hash
        or artifact.airframe_profile_hash
        != evidence.get("mission_transaction", {}).get("airframe_profile_hash")
        or artifact.airframe_profile_hash
        != evidence.get("sensor_transport", {}).get("airframe_profile_sha256")
        or evidence.get("external_airframe_profile_sha256") != artifact.airframe_profile_hash
    ):
        raise ValueError("external multirotor policy artifact canonical binding drift")
    task_binding = evidence.get("task_binding")
    mission = evidence.get("mission_transaction")
    sensor = evidence.get("sensor_transport")
    cleanup = evidence.get("container_runtime", {}).get("cleanup")
    runtime_container_id = evidence.get("container_runtime", {}).get("container_id")
    plan = evidence.get("plan")
    plan_vehicle = plan.get("vehicle") if isinstance(plan, dict) else None
    mission_attestation = mission.get("runtime_attestation") if isinstance(mission, dict) else None
    sensor_runtime_binding = evidence.get("sensor_runtime_binding")
    expected_model_name = (
        "rc_cessna_mono_cam"
        if entry_key == "external_fixed_wing_shared_policy_depth_mission_20260930"
        else "x500_mono_cam"
    )
    expected_entry_values = {
        "release_label": "0.3.0-pre.8",
        "current_release_parity_bound": True,
        "gate_passed": True,
        "runtime_gate_passed": True,
        "mission_transaction_gate_passed": True,
        "sensor_transport_gate_passed": True,
        "shared_policy_used": True,
        "planner_revalidated": True,
        "flight_started": False,
        "physical_output": False,
        "policy_applied_to_actuator": False,
        "truth_free": True,
        "container_cleanup": True,
        "qgc_manual_visual_acceptance": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "hardware_acceptance": False,
        "final_release_allowed": False,
    }
    if any(entry.get(key) != value for key, value in expected_entry_values.items()):
        raise ValueError(f"external {evidence_vehicle_label} depth Mission index entry drift")
    if (
        not isinstance(plan, dict)
        or not isinstance(plan_vehicle, dict)
        or not isinstance(sensor, dict)
        or not isinstance(sensor.get("transport"), dict)
        or not isinstance(evidence.get("container_runtime"), dict)
    ):
        raise ValueError(
            f"external {evidence_vehicle_label} depth Mission provenance is incomplete"
        )
    try:
        from multidetect.simulator.stack import resolve_repository_model_asset_bundle

        model_root = root / "simulation" / "gazebo" / "models"
        asset_bundle = resolve_repository_model_asset_bundle(
            model_root,
            repository_root=root,
        )
        expected_model_assets = {
            "active_model_file_count": sum(
                1 for path in (model_root / expected_model_name).rglob("*") if path.is_file()
            ),
            "active_model_name": expected_model_name,
            **asset_bundle.portable_mapping(),
        }
        expected_sensor_binding_hash = _recompute_external_gazebo_sensor_binding_hash(
            root,
            entry,
            evidence,
            model_name=expected_model_name,
        )
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise ValueError(
            f"external {evidence_vehicle_label} depth Mission provenance revalidation failed"
        ) from exc
    inspect_checks = evidence["container_runtime"].get("inspect_checks")
    transport = sensor["transport"]
    repository_assets = sensor.get("repository_model_assets")
    legacy_model_binding_ok = (
        isinstance(repository_assets, dict)
        and repository_assets.get("active_model_name") == expected_model_name
        and repository_assets.get("assets_sha256") == sensor.get("model_assets_sha256")
        and (
            not isinstance(plan.get("model_assets_sha256"), str)
            or plan.get("model_assets_sha256") == sensor.get("model_assets_sha256")
        )
        and (
            not isinstance(transport.get("model_assets_sha256"), str)
            or transport.get("model_assets_sha256") == sensor.get("model_assets_sha256")
        )
    )
    current_model_binding_ok = (
        sensor.get("repository_model_assets") == expected_model_assets
        and sensor.get("model_assets_sha256") == expected_model_assets["assets_sha256"]
        and (
            not isinstance(plan.get("model_assets_sha256"), str)
            or plan.get("model_assets_sha256") == expected_model_assets["assets_sha256"]
        )
        and (
            not isinstance(transport.get("model_assets_sha256"), str)
            or transport.get("model_assets_sha256") == expected_model_assets["assets_sha256"]
        )
    )
    if (
        (
            (legacy_billboard_audit and legacy_model_binding_ok is not True)
            or (not legacy_billboard_audit and current_model_binding_ok is not True)
        )
        or (
            entry_key != "external_fixed_wing_shared_policy_depth_mission_20260930"
            and plan_vehicle.get("model_name") != expected_model_name
        )
        or sensor.get("sensor_binding_hash") != expected_sensor_binding_hash
        or transport.get("sensor_binding_sha256") != expected_sensor_binding_hash
        or sensor.get("sensor_binding_hash") != transport.get("sensor_binding_sha256")
        or not isinstance(inspect_checks, dict)
        or set(inspect_checks)
        != {
            "bridge_network",
            "control_port_environment",
            "exact_mount_count",
            "loopback_port_bindings",
            "mount_default_sdf_readonly",
            "mount_default_sdf_source",
            "mount_multidetect-gazebo-models_readonly",
            "mount_multidetect-gazebo-models_source",
            "mount_scripts_readonly",
            "mount_scripts_source",
            "name",
            "not_privileged",
            "pinned_image",
            "planned_environment_present",
            "protected_port_untouched",
            "purpose_label",
            "running",
            "telemetry_port_environment",
            "telemetry_port_not_published",
        }
        or any(value is not True for value in inspect_checks.values())
    ):
        raise ValueError(
            f"external {evidence_vehicle_label} depth Mission provenance binding drift"
        )
    if (
        not isinstance(task_binding, dict)
        or task_binding.get("decision", {}).get("action") != "select"
        or task_binding.get("decision", {}).get("planner_revalidated") is not True
        or task_binding.get("planner_revalidated") is not True
        or task_binding.get("mission_bound") is not True
        or task_binding.get("truth_free") is not True
        or task_binding.get("policy_applied_to_actuator") is not False
        or task_binding.get("decision", {}).get("policy_artifact_sha256")
        != entry.get("policy_artifact_canonical_sha256")
        or not isinstance(mission, dict)
        or mission.get("status") != "passed"
        or mission.get("external_runtime_attested") is not True
        or mission.get("readback", {}).get("hash_match") is not True
        or mission.get("checks", {}).get("commit_hash_match") is not True
        or mission.get("checks", {}).get("start_permit_bound") is not True
        or mission.get("flight_started") is not False
        or mission.get("physical_output_enabled") is not False
        or not isinstance(mission_attestation, dict)
        or mission_attestation.get("container_id") != runtime_container_id
        or mission.get("executable_mission_hash") != task_binding.get("executable_mission_sha256")
        or mission.get("protocol_hash") != task_binding.get("wire_hash")
        or mission.get("readback", {}).get("uploaded_protocol_sha256")
        != task_binding.get("wire_hash")
        or mission.get("readback", {}).get("readback_protocol_sha256")
        != task_binding.get("wire_hash")
        or not isinstance(sensor, dict)
        or sensor.get("status") != "passed"
        or sensor.get("requested") is not True
        or sensor.get("qualified") is not True
        or type(sensor.get("requested_frame_count")) is not int
        or sensor.get("requested_frame_count", 0) <= 0
        or sensor.get("transport", {}).get("depth_enabled") is not True
        or sensor.get("transport", {}).get("depth_companion_count")
        != entry.get("depth_companion_count")
        or sensor.get("transport", {}).get("depth_companion_count")
        != entry.get("received_sensor_frames")
        or (
            entry_key != "external_fixed_wing_shared_policy_depth_mission_20260930"
            and sensor.get("transport", {}).get("depth_topic") != entry.get("depth_topic")
        )
        or (
            entry_key != "external_fixed_wing_shared_policy_depth_mission_20260930"
            and sensor.get("transport", {}).get("depth_calibration_sha256")
            != entry.get("depth_calibration_sha256")
        )
        or sensor.get("transport", {}).get("received_frame_count")
        != entry.get("received_sensor_frames")
        or sensor.get("transport", {}).get("expected_frame_count")
        != sensor.get("requested_frame_count")
        or sensor.get("transport", {}).get("received_frame_count")
        != sensor.get("requested_frame_count")
        or sensor.get("transport", {}).get("complete") is not True
        or sensor.get("truth_free") is not True
        or not isinstance(cleanup, dict)
        or cleanup.get("removed") is not True
        or cleanup.get("errors") != []
        or evidence.get("claims", {}).get("hardware") != "not_claimed"
        or evidence.get("claims", {}).get("visual") != "not_claimed"
        or not isinstance(sensor_runtime_binding, dict)
        or sensor_runtime_binding.get("container_id") != runtime_container_id
        or sensor_runtime_binding.get("mission_container_id")
        != mission_attestation.get("container_id")
        or sensor_runtime_binding.get("same_container") is not True
        or sensor_runtime_binding.get("truth_free") is not True
        or evidence.get("release_status") != "v0.3.0-pre.8; final v0.3.0 remains blocked"
        or not isinstance(evidence.get("safety"), dict)
        or evidence.get("safety", {}).get("flight_control_commands_sent") != 0
        or evidence.get("safety", {}).get("flight_control_output_enabled") is not False
        or evidence.get("safety", {}).get("hardware_control_enabled") is not False
        or evidence.get("safety", {}).get("real_pixhawk_contacted") is not False
        or evidence.get("safety", {}).get("truth_free") is not True
    ):
        raise ValueError(
            f"external {evidence_vehicle_label} shared-policy depth Mission safety binding drift"
        )
    if entry_key == "external_fixed_wing_shared_policy_depth_mission_20260930":
        expected_scenario_path = "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json"
        if entry.get("scenario") != expected_scenario_path:
            raise ValueError("external fixed-wing scenario path drift")
        scenario_path = _path(root, entry.get("scenario"), "external fixed-wing scenario")
        scenario_document = _json(scenario_path)
        runtime_reference = scenario_document.get("channels", {}).get("runtime_input")
        if not isinstance(runtime_reference, dict):
            raise ValueError("external fixed-wing runtime channel reference is missing")
        if runtime_reference.get("path") != "coastal_patrol_fixed_wing_001.runtime.json":
            raise ValueError("external fixed-wing runtime channel path drift")
        runtime_path = _path(
            scenario_path.parent,
            runtime_reference.get("path"),
            "external fixed-wing runtime channel",
        )
        runtime_document = _json(runtime_path)
        depth_sensors = [
            sensor
            for sensor in runtime_document.get("sensors", [])
            if isinstance(sensor, dict) and sensor.get("kind") == "depth_camera"
        ]
        runtime_vehicle = runtime_document.get("vehicle")
        rgb_sensors = [
            sensor
            for sensor in runtime_document.get("sensors", [])
            if isinstance(sensor, dict) and sensor.get("kind") == "rgb_camera"
        ]
        if (
            len(rgb_sensors) != 1
            or len(depth_sensors) != 1
            or not isinstance(runtime_vehicle, dict)
        ):
            raise ValueError("external fixed-wing runtime sensor/vehicle contract is incomplete")
        depth_sensor = depth_sensors[0]
        calibration_path = _path(
            root,
            "simulation/gazebo/calibration/gazebo-fixed-wing-front-camera-v1.json",
            "external fixed-wing RGB calibration",
        )
        calibration = _json(calibration_path).get("calibration")
        calibration_evidence = sensor.get("camera_calibration")
        if (
            not isinstance(calibration, dict)
            or not isinstance(calibration_evidence, dict)
            or calibration_evidence.get("repository_path")
            != calibration_path.relative_to(root).as_posix()
            or calibration_evidence.get("file_sha256") != _sha256(calibration_path)
            or calibration_evidence.get("calibration") != calibration
            or calibration.get("calibration_id") != rgb_sensors[0].get("calibration_id")
        ):
            raise ValueError("external fixed-wing RGB calibration binding drift")
        expected_depth_topic = (
            "/world/default/model/rc_cessna_mono_cam_0/link/depth_camera_link/"
            "sensor/depth_camera/depth_image"
        )
        expected_manifest_file_sha = _sha256(scenario_path)
        expected_manifest_hash = _canonical_sha256(scenario_document)
        expected_airframe = _canonical_sha256(
            {
                "schema_version": 1,
                "airframe_profile_id": "px4_fixed_wing_gazebo_v1",
                "vehicle_model": "px4_fixed_wing_standard",
                "model_name": "rc_cessna_mono_cam",
            }
        )
        expected_depth_calibration = _canonical_sha256(
            {
                "schema_version": 1,
                "calibration_id": "gazebo-fixed-wing-depth-v1",
                "sensor_id": "forward-depth",
                "width_px": calibration["width_px"],
                "height_px": calibration["height_px"],
                "fx_px": calibration["fx_px"],
                "fy_px": calibration["fy_px"],
                "cx_px": calibration["cx_px"],
                "cy_px": calibration["cy_px"],
                "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
                "rectification": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                "extrinsics_rgb_to_depth": {
                    "translation_m": [0.0, 0.0, 0.0],
                    "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                },
                "registration_mode": "registered_to_rgb",
                "coordinate_space": "rgb_pixel_aligned",
                "encoding": "FLOAT32_M",
                "distance_semantics": "optical_axis_z_m",
                "near_clip_m": 0.1,
                "far_clip_m": 3000.0,
            }
        )
        scenario_evidence = evidence.get("scenario")
        plan_vehicle = evidence.get("plan", {}).get("vehicle")
        mission_attestation = evidence.get("mission_transaction", {}).get("runtime_attestation")
        if (
            depth_sensor.get("sensor_id") != "forward-depth"
            or entry.get("scenario_runtime_depth_sensor") != "forward-depth"
            or depth_sensor.get("stream_uri")
            != "gazebo://coastal_patrol_fixed_wing_001/forward-depth"
            or depth_sensor.get("calibration_id") != "gazebo-fixed-wing-depth-v1"
            or rgb_sensors[0].get("sensor_id") != "front-camera"
            or rgb_sensors[0].get("stream_uri")
            != "gazebo://coastal_patrol_fixed_wing_001/front-camera"
            or runtime_document.get("scenario_id") != "coastal_patrol_fixed_wing_001"
            or runtime_vehicle.get("model") != "px4_fixed_wing_standard"
            or runtime_vehicle.get("airframe_profile_id") != "px4_fixed_wing_gazebo_v1"
            or scenario_document.get("scenario_id") != "coastal_patrol_fixed_wing_001"
            or runtime_reference.get("sha256")
            != entry.get("scenario_runtime_channel_canonical_sha256")
            or _canonical_sha256(runtime_document) != runtime_reference.get("sha256")
            or evidence.get("scenario", {}).get("manifest") != str(scenario_path)
            or not isinstance(scenario_evidence, dict)
            or scenario_evidence.get("manifest_sha256") != expected_manifest_file_sha
            or scenario_evidence.get("scenario_manifest_hash") != expected_manifest_hash
            or scenario_evidence.get("scenario_id") != "coastal_patrol_fixed_wing_001"
            or scenario_evidence.get("vehicle_model") != "px4_fixed_wing_standard"
            or scenario_evidence.get("airframe_profile_id") != "px4_fixed_wing_gazebo_v1"
            or scenario_evidence.get("model_name") != "rc_cessna_mono_cam"
            or scenario_evidence.get("truth_loaded_by_runtime") is not False
            or scenario_evidence.get("expectations_loaded_by_runtime") is not False
            or not isinstance(plan_vehicle, dict)
            or plan_vehicle.get("model") != "px4_fixed_wing_standard"
            or plan_vehicle.get("model_name") != "rc_cessna_mono_cam"
            or plan_vehicle.get("airframe_profile_id") != "px4_fixed_wing_gazebo_v1"
            or evidence.get("plan", {}).get("scenario_id") != "coastal_patrol_fixed_wing_001"
            or evidence.get("plan", {}).get("scenario_manifest_hash") != expected_manifest_hash
            or not isinstance(mission_attestation, dict)
            or mission_attestation.get("scenario_id") != "coastal_patrol_fixed_wing_001"
            or mission_attestation.get("scenario_manifest_sha256") != expected_manifest_hash
            or mission_attestation.get("airframe_profile_id") != "px4_fixed_wing_gazebo_v1"
            or mission_attestation.get("vehicle_class") != "fixed-wing"
            or evidence.get("task_binding", {}).get("vehicle_class") != "fixed-wing"
            or evidence.get("mission_transaction", {}).get("vehicle_class") != "fixed-wing"
            or evidence.get("sensor_transport", {}).get("transport", {}).get("sensor_id")
            != "front-camera"
            or evidence.get("sensor_transport", {}).get("airframe_profile_sha256")
            != expected_airframe
            or evidence.get("external_airframe_profile_sha256") != expected_airframe
            or evidence.get("sensor_transport", {}).get("transport", {}).get("depth_topic")
            != expected_depth_topic
            or evidence.get("sensor_transport", {}).get("transport", {}).get("depth_calibration_id")
            != "gazebo-fixed-wing-depth-v1"
            or evidence.get("sensor_transport", {})
            .get("transport", {})
            .get("depth_calibration_sha256")
            != expected_depth_calibration
            or entry.get("depth_calibration_id") != "gazebo-fixed-wing-depth-v1"
            or entry.get("depth_calibration_sha256") != expected_depth_calibration
            or entry.get("depth_topic") != expected_depth_topic
        ):
            raise ValueError("external fixed-wing depth scenario binding drift")
    frames = sensor.get("frames")
    if (
        not isinstance(frames, list)
        or len(frames) != entry.get("received_sensor_frames")
        or not frames
        or len({frame.get("sequence") for frame in frames if isinstance(frame, dict)})
        != len(frames)
        or len({frame.get("frame_id") for frame in frames if isinstance(frame, dict)})
        != len(frames)
        or len({frame.get("depth_record_sha256") for frame in frames if isinstance(frame, dict)})
        != len(frames)
        or len({frame.get("depth_payload_sha256") for frame in frames if isinstance(frame, dict)})
        != len(frames)
    ):
        raise ValueError(f"external {evidence_vehicle_label} depth frame coverage drift")
    if [frame.get("sequence") for frame in frames] != list(range(len(frames))):
        raise ValueError(f"external {evidence_vehicle_label} depth companion binding drift")
    sensor_stamps = [frame.get("sensor_stamp_ns") for frame in frames]
    depth_stamps = [frame.get("depth_source_stamp_ns") for frame in frames]
    if (
        any(not isinstance(value, int) for value in (*sensor_stamps, *depth_stamps))
        or any(
            current <= previous
            for previous, current in zip(sensor_stamps, sensor_stamps[1:], strict=False)
        )
        or any(
            current <= previous
            for previous, current in zip(depth_stamps, depth_stamps[1:], strict=False)
        )
    ):
        raise ValueError(f"external {evidence_vehicle_label} depth companion binding drift")
    for frame in frames:
        if (
            not isinstance(frame, dict)
            or not isinstance(frame.get("depth_topic"), str)
            or frame.get("depth_topic") != entry.get("depth_topic")
            or frame.get("depth_topic") != sensor.get("transport", {}).get("depth_topic")
            or frame.get("depth_calibration_id")
            != sensor.get("transport", {}).get("depth_calibration_id")
            or frame.get("depth_calibration_sha256") != entry.get("depth_calibration_sha256")
            or frame.get("depth_calibration_sha256")
            != sensor.get("transport", {}).get("depth_calibration_sha256")
            or frame.get("depth_encoding") != "FLOAT32_M"
            or frame.get("depth_distance_semantics") != "optical_axis_z_m"
            or not isinstance(frame.get("depth_payload_sha256"), str)
            or len(frame["depth_payload_sha256"]) != 64
            or not isinstance(frame.get("depth_record_sha256"), str)
            or len(frame["depth_record_sha256"]) != 64
            or type(frame.get("depth_source_skew_ns")) is not int
            or not 0 <= frame["depth_source_skew_ns"] <= 100_000_000
            or type(frame.get("sensor_stamp_ns")) is not int
            or type(frame.get("depth_source_stamp_ns")) is not int
            or frame["depth_source_stamp_ns"] <= 0
            or frame["sensor_stamp_ns"] - frame["depth_source_stamp_ns"]
            != frame["depth_source_skew_ns"]
        ):
            raise ValueError(f"external {evidence_vehicle_label} depth companion binding drift")


def _verify_current_shared_policy_training_holdout(
    root: Path,
    progress: dict[str, Any],
) -> None:
    """Re-open the current training and independent-holdout evidence.

    The training report is software-only evidence.  This check deliberately
    verifies the parent/candidate identity and the frozen holdout metrics, but
    never treats equal rewards as a promotion signal.
    """

    entry = progress.get("shared_policy_training_holdout_20261001_r1")
    if not isinstance(entry, dict):
        raise ValueError("current shared-policy training holdout reference is missing")
    report_path = _path(root, entry.get("training_report"), "shared-policy training report")
    if entry.get("training_report_bytes") != report_path.stat().st_size or entry.get(
        "training_report_sha256"
    ) != _sha256(report_path):
        raise ValueError("shared-policy training report file identity drift")
    report = _json(report_path)
    if (
        report.get("schema_version") != 1
        or report.get("candidate_promoted") is not False
        or report.get("physical_performance_claimed") is not False
        or report.get("holdout_used_for_selection") is not False
    ):
        raise ValueError("shared-policy training safety/promotion boundary drift")
    dataset = report.get("dataset")
    if (
        not isinstance(dataset, dict)
        or str(dataset.get("path", "")).replace("\\", "/")
        != str(entry.get("training_dataset", "")).replace("\\", "/")
        or dataset.get("sha256") != entry.get("training_dataset_sha256")
    ):
        raise ValueError("shared-policy training dataset identity drift")
    if report.get("parent_file_sha256") != entry.get("parent_file_sha256"):
        raise ValueError("shared-policy parent file identity drift")
    candidate_path = _path(root, entry.get("candidate_file"), "shared-policy candidate ranker")
    if entry.get("candidate_file_sha256") != _sha256(candidate_path) or report.get(
        "candidate_file_sha256"
    ) != entry.get("candidate_file_sha256"):
        raise ValueError("shared-policy candidate ranker file identity drift")
    try:
        from multidetect.autonomy.task_ranker import load_task_ranker

        candidate = load_task_ranker(
            candidate_path,
            expected_sha256=entry["candidate_file_sha256"],
        )
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise ValueError("shared-policy candidate ranker parsing failed") from exc
    if candidate.canonical_hash != entry.get("candidate_ranker_sha256") or report.get(
        "candidate_file_sha256"
    ) != _sha256(candidate_path):
        raise ValueError("shared-policy candidate ranker semantic identity drift")
    evaluations = report.get("evaluations")
    rewards = entry.get("reward_summary")
    if not isinstance(evaluations, dict) or not isinstance(rewards, dict):
        raise ValueError("shared-policy training evaluation summary is missing")
    for split in ("train", "validation", "test"):
        rows = evaluations.get(split)
        reward_prefix = "training" if split == "train" else split
        if (
            not isinstance(rows, dict)
            or not isinstance(rows.get("parent"), dict)
            or not isinstance(rows.get("candidate"), dict)
            or rows["parent"].get("reward") != rewards.get(f"{reward_prefix}_parent")
            or rows["candidate"].get("reward") != rewards.get(f"{reward_prefix}_candidate")
        ):
            raise ValueError("shared-policy training reward binding drift")
    campaign_path = _path(
        root,
        entry.get("independent_campaign"),
        "shared-policy independent campaign",
    )
    if entry.get("independent_campaign_bytes") != campaign_path.stat().st_size or entry.get(
        "independent_campaign_sha256"
    ) != _sha256(campaign_path):
        raise ValueError("shared-policy independent campaign file identity drift")
    campaign = _json(campaign_path)
    if (
        campaign.get("method") != "hash_bound_shared_policy_independent_sim_campaign_v1"
        or campaign.get("input_hashes_disjoint") is not True
        or campaign.get("scenario_families_disjoint") is not True
        or campaign.get("planner_revalidated") is not True
        or campaign.get("candidate_promoted") is not False
        or campaign.get("learned_weights_loaded") is not False
        or campaign.get("physical_performance_claimed") is not False
        or campaign.get("hil_real_evidence") is not False
        or campaign.get("outdoor_gnss_evidence") is not False
        or campaign.get("final_release_allowed") is not False
        or campaign.get("training_input_count") != entry.get("training_input_count")
        or campaign.get("evaluation_input_count") != entry.get("evaluation_input_count")
        or campaign.get("parent_ranker_sha256") != entry.get("parent_ranker_sha256")
        or campaign.get("candidate_ranker_sha256") != entry.get("candidate_ranker_sha256")
    ):
        raise ValueError("shared-policy independent campaign safety/identity drift")
    independent = campaign.get("evaluations", {})
    if not isinstance(independent, dict):
        raise ValueError("shared-policy independent evaluations are missing")
    for role, expected in (
        ("independent_parent", rewards.get("independent_parent")),
        ("independent_candidate", rewards.get("independent_candidate")),
    ):
        rows = independent.get(role)
        if not isinstance(rows, dict):
            raise ValueError("shared-policy independent evaluation role is missing")
        total = sum(
            value.get("reward")
            for value in rows.values()
            if isinstance(value, dict) and isinstance(value.get("reward"), int)
        )
        if total != expected:
            raise ValueError("shared-policy independent reward binding drift")


def _verify_current_shared_policy_trained_external(
    root: Path,
    progress: dict[str, Any],
    parity_path: Path,
    parity_sha: str,
    parity: dict[str, Any],
) -> None:
    """Re-open the external SIM run that consumed the trained artifact."""

    entry = progress.get("shared_policy_trained_artifact_external_vtol_campaign_20261001_r1")
    if not isinstance(entry, dict):
        raise ValueError("current trained-artifact external campaign reference is missing")
    report_path = _path(root, entry.get("training_report"), "trained-artifact training report")
    if entry.get("training_report_bytes") != report_path.stat().st_size or entry.get(
        "training_report_sha256"
    ) != _sha256(report_path):
        raise ValueError("trained-artifact training report file identity drift")
    artifact_path = _path(root, entry.get("policy_artifact"), "trained policy artifact")
    if entry.get("policy_artifact_file_bytes") != artifact_path.stat().st_size or entry.get(
        "policy_artifact_file_sha256"
    ) != _sha256(artifact_path):
        raise ValueError("trained policy artifact file identity drift")
    try:
        from multidetect.autonomy.policy_artifact import PolicyArtifact

        artifact = PolicyArtifact.from_mapping(_json(artifact_path))
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("trained policy artifact parsing failed") from exc
    if artifact.canonical_hash != entry.get(
        "policy_artifact_canonical_sha256"
    ) or artifact.ranker_sha256 != entry.get("ranker_sha256"):
        raise ValueError("trained policy artifact canonical identity drift")
    runtime_path = _path(root, entry.get("runtime_evidence"), "trained-artifact runtime evidence")
    if entry.get("runtime_evidence_bytes") != runtime_path.stat().st_size or entry.get(
        "runtime_evidence_sha256"
    ) != _sha256(runtime_path):
        raise ValueError("trained-artifact runtime evidence file identity drift")
    runtime = _json(runtime_path)
    policy_input = runtime.get("policy_artifact_input")
    campaign = runtime.get("campaign")
    if (
        runtime.get("gate_passed") is not True
        or runtime.get("failure") is not None
        or runtime.get("execution", {}).get("status") != "passed"
        or not isinstance(policy_input, dict)
        or policy_input.get("file_sha256") != entry.get("policy_artifact_file_sha256")
        or policy_input.get("canonical_sha256") != entry.get("policy_artifact_canonical_sha256")
        or not isinstance(campaign, dict)
        or campaign.get("verified_sorties") is None
    ):
        raise ValueError("trained-artifact runtime safety/identity drift")
    verified_sorties = campaign["verified_sorties"]
    if [item.get("task_id") for item in verified_sorties if isinstance(item, dict)] != entry.get(
        "verified_task_order"
    ) or entry.get("verified_sortie_count") != 2:
        raise ValueError("trained-artifact sortie order/count drift")
    summary_path = _path(root, entry.get("campaign_summary"), "trained-artifact campaign summary")
    if entry.get("campaign_summary_bytes") != summary_path.stat().st_size or entry.get(
        "campaign_summary_sha256"
    ) != _sha256(summary_path):
        raise ValueError("trained-artifact campaign summary identity drift")
    summary = _json(summary_path)
    if (
        summary.get("status") != "all_tasks_completed"
        or summary.get("shared_policy_used") is not True
        or summary.get("planner_revalidated") is not True
        or summary.get("ranker_sha256") != entry.get("ranker_sha256")
        or summary.get("policy_artifact_sha256") != entry.get("policy_artifact_canonical_sha256")
        or [item.get("task_id") for item in summary.get("completed", [])]
        != entry.get("verified_task_order")
    ):
        raise ValueError("trained-artifact campaign summary binding drift")
    receipt_path = _path(root, entry.get("receipt"), "trained-artifact campaign receipt")
    if entry.get("receipt_bytes") != receipt_path.stat().st_size or entry.get(
        "receipt_file_sha256"
    ) != _sha256(receipt_path):
        raise ValueError("trained-artifact receipt file identity drift")
    receipt = _json(receipt_path)
    stored_receipt_sha = receipt.get("receipt_sha256")
    unsigned_receipt = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        not isinstance(stored_receipt_sha, str)
        or stored_receipt_sha != _canonical_sha256(unsigned_receipt)
        or stored_receipt_sha != entry.get("receipt_canonical_sha256")
        or receipt.get("evidence_scope") != "retained_files_audit"
        or receipt.get("runtime_source_parity_verified") is not False
        or receipt.get("external_sim_rerun_performed") is not False
        or receipt.get("shared_policy_used") is not True
        or receipt.get("planner_revalidated") is not True
        or receipt.get("policy_applied_to_actuator") is not False
        or receipt.get("final_release_allowed") is not False
        or receipt.get("hil_real_evidence") is not False
        or receipt.get("outdoor_gnss_evidence") is not False
    ):
        raise ValueError("trained-artifact receipt safety/canonical binding drift")
    receipt_parity = receipt.get("release_parity")
    if (
        not isinstance(receipt_parity, dict)
        or receipt_parity.get("file_sha256") != parity_sha
        or receipt_parity.get("bytes") != parity_path.stat().st_size
        or receipt_parity.get("hashes") != parity.get("parity")
    ):
        raise ValueError("trained-artifact receipt release parity drift")
    cleanup = receipt.get("cleanup")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("runtime_evidence_sha256") != _sha256(runtime_path)
        or cleanup.get("removed") is not True
        or cleanup.get("post_remove_absence_proven") is not True
    ):
        raise ValueError("trained-artifact receipt cleanup binding drift")


def verify(
    root: Path,
    progress_path: Path | None = None,
    *,
    check_source_parity: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    progress_path = (progress_path or root / "docs/current-progress.json").resolve()
    progress = _json(progress_path)
    version = _json(root / "configs/product-version.json")
    parity_path = root / "configs/release-parity.json"
    parity = _json(parity_path)
    source_parity = verify_release_parity_sources(root) if check_source_parity else None
    updated = progress.get("updated_at_utc")
    if not isinstance(updated, str):
        raise ValueError("updated_at_utc is required")
    try:
        datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("updated_at_utc must be ISO-8601") from exc
    expected_product = {
        "release_label": version.get("release_label"),
        "numeric": version.get("numeric"),
        "python": version.get("python"),
        "channel": version.get("channel"),
    }
    if progress.get("product_version") != expected_product:
        raise ValueError("progress product_version differs from configs/product-version.json")
    if progress.get("release_parity", {}).get("bytes") != parity_path.stat().st_size:
        raise ValueError("release parity byte count drift")
    parity_sha = _sha256(parity_path)
    if progress.get("release_parity", {}).get("file_sha256") != parity_sha:
        raise ValueError("release parity SHA-256 drift")
    if progress.get("release_parity", {}).get("hashes") != parity.get("parity"):
        raise ValueError("release parity mapping drift")
    _verify_v030_pre1_foundation(
        root,
        progress,
        parity_path=parity_path,
        parity_sha=parity_sha,
        release_label=str(version.get("release_label")),
    )
    _verify_current_combined_sim_qgc(root, progress)
    matrix_index = progress.get("external_mission_matrix_20260927_r6")
    if not isinstance(matrix_index, dict):
        raise ValueError("external Mission matrix reference is missing")
    matrix_path = _path(root, matrix_index.get("evidence"), "external Mission matrix")
    if matrix_index.get("evidence_bytes") != matrix_path.stat().st_size:
        raise ValueError("external Mission matrix byte count drift")
    if matrix_index.get("evidence_sha256") != _sha256(matrix_path):
        raise ValueError("external Mission matrix SHA-256 drift")
    matrix = _json(matrix_path)
    if matrix.get("schema") != "multidetect.external-mission-matrix-receipt":
        raise ValueError("external Mission matrix schema drift")
    if matrix_index.get("receipt_sha256") != matrix.get("receipt_sha256"):
        raise ValueError("external Mission matrix canonical identity drift")
    if matrix.get("same_external_mission_path") is not True:
        raise ValueError("external Mission matrix path binding is missing")
    if matrix.get("same_core_compiler_and_transaction") is not True:
        raise ValueError("external Mission matrix compiler binding is missing")
    if matrix.get("current_release_parity_bound") is not True:
        raise ValueError("external Mission matrix parity status drift")
    if matrix.get("source_release_parity_binding_required_for_promotion") is not True:
        raise ValueError("external Mission matrix promotion guard is missing")
    if (
        matrix.get("flight_started") is not False
        or matrix.get("physical_output_enabled") is not False
    ):
        raise ValueError("external Mission matrix safety binding drift")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from scripts.verify_external_mission_matrix_receipt import verify_receipt

        matrix_verification = verify_receipt(root=root, receipt_path=matrix_path)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"external Mission matrix source revalidation failed: {exc}") from exc
    if matrix_verification.get("status") != "passed" or matrix_verification.get(
        "receipt_sha256"
    ) != matrix.get("receipt_sha256"):
        raise ValueError("external Mission matrix source revalidation drift")
    observed_index = progress.get("shared_policy_observed_session_20260924")
    if not isinstance(observed_index, dict):
        raise ValueError("observed shared-policy session reference is missing")
    observed_path = _path(root, observed_index.get("evidence"), "observed session evidence")
    if observed_index.get("evidence_bytes") != observed_path.stat().st_size:
        raise ValueError("observed session evidence byte count drift")
    observed_sha = _sha256(observed_path)
    if observed_index.get("evidence_sha256") != observed_sha:
        raise ValueError("observed session evidence SHA-256 drift")
    observed = _json(observed_path)
    observed_parity = observed.get("release_parity")
    if not isinstance(observed_parity, dict):
        raise ValueError("observed session release parity is missing")
    if observed_parity.get("path") != "configs/release-parity.json":
        raise ValueError("observed session parity path drift")
    if observed_parity.get("bytes") != parity_path.stat().st_size:
        raise ValueError("observed session parity byte count drift")
    if observed_parity.get("file_sha256") != parity_sha:
        raise ValueError("observed session parity SHA-256 drift")
    if observed_parity.get("hashes") != parity.get("parity"):
        raise ValueError("observed session parity mapping drift")
    if observed_index.get("release_parity_file_sha256") != parity_sha:
        raise ValueError("progress observed session parity SHA-256 drift")
    if observed_index.get("release_parity_bytes") != parity_path.stat().st_size:
        raise ValueError("progress observed session parity byte count drift")
    if observed_index.get("release_parity_core_algorithm_hash") != parity.get("parity", {}).get(
        "core_algorithm_hash"
    ):
        raise ValueError("progress observed session core parity drift")
    observed_required = {
        "all_tasks_completed": True,
        "deterministic_repeat_passed": True,
        "planner_revalidated": True,
        "truth_free": True,
        "backend_started": False,
        "mavlink_sent": False,
        "actuator_capability_granted": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    if any(observed.get(key) is not value for key, value in observed_required.items()):
        raise ValueError("observed session safety/completion contract drift")
    if any(observed_index.get(key) is not value for key, value in observed_required.items()):
        raise ValueError("observed session safety/completion progress drift")
    for key in (
        "campaign_sha256",
        "policy_artifact_file_sha256",
        "policy_artifact_canonical_sha256",
        "ranker_sha256",
        "airframe_profile_sha256",
        "session_sha256",
        "repeat_session_sha256",
    ):
        value = observed.get(key)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"observed session {key} is missing or malformed")
    observed_session = observed.get("session")
    if not isinstance(observed_session, dict):
        raise ValueError("observed session details are missing")
    if observed.get("session_sha256") != _canonical_sha256(observed_session):
        raise ValueError("observed session canonical hash drift")
    if (
        observed_session.get("stop_reason") != "all_tasks_completed"
        or observed_session.get("remaining_task_ids") != []
        or observed_session.get("planner_revalidated") is not True
        or observed_session.get("step_count") != 2
        or not isinstance(observed_session.get("steps"), list)
        or len(observed_session["steps"]) != 2
    ):
        raise ValueError("observed session task completion binding drift")
    selected_ids = observed_session.get("selected_task_ids")
    if (
        selected_ids != observed_index.get("selected_task_ids")
        or selected_ids != ["synthetic-east", "synthetic-west"]
        or observed_index.get("remaining_task_ids") != []
        or observed_index.get("completed_task_count") != 2
    ):
        raise ValueError("observed session task sequence drift")
    for step in observed_session["steps"]:
        if (
            not isinstance(step, dict)
            or step.get("planner_revalidated") is not True
            or step.get("policy_artifact_sha256")
            != observed.get("policy_artifact_canonical_sha256")
            or step.get("ranker_sha256") != observed.get("ranker_sha256")
            or step.get("outcome", {}).get("status") != "completed"
        ):
            raise ValueError("observed session step policy/outcome binding drift")
    _verify_current_shared_policy_observed_session(
        root=root,
        progress=progress,
        parity_path=parity_path,
        parity_sha=parity_sha,
        parity=parity,
    )
    _verify_current_shared_policy_training_holdout(root, progress)
    _verify_current_shared_policy_trained_external(
        root=root,
        progress=progress,
        parity_path=parity_path,
        parity_sha=parity_sha,
        parity=parity,
    )
    receipt_index = progress.get("shared_policy_campaign_receipts_20260925")
    if not isinstance(receipt_index, dict):
        raise ValueError("shared-policy campaign receipt reference is missing")
    for role, path_key, sha_key, bytes_key in (
        ("parent", "parent_receipt", "parent_receipt_sha256", "parent_receipt_bytes"),
        (
            "candidate",
            "candidate_receipt",
            "candidate_receipt_sha256",
            "candidate_receipt_bytes",
        ),
    ):
        receipt_path = _path(root, receipt_index.get(path_key), f"{role} campaign receipt")
        if receipt_index.get(bytes_key) != receipt_path.stat().st_size:
            raise ValueError(f"{role} campaign receipt byte count drift")
        receipt_sha = _sha256(receipt_path)
        if receipt_index.get(sha_key) != receipt_sha:
            raise ValueError(f"{role} campaign receipt SHA-256 drift")
        receipt = _json(receipt_path)
        if (
            receipt.get("schema") != "multidetect.shared-policy-campaign-receipt"
            or receipt.get("schema_version") != 1
            or receipt.get("shared_policy_used") is not True
            or receipt.get("planner_revalidated") is not True
            or receipt.get("truth_free") is not True
            or receipt.get("policy_applied_to_actuator") is not False
            or receipt.get("release_parity", {}).get("verified") is not True
            or receipt.get("runtime_binding", {}).get("verified") is not True
            or receipt.get("runtime_binding", {}).get("runtime_mode")
            != receipt_index.get("runtime_mode")
            or receipt.get("runtime_binding", {}).get("scenario_id")
            != receipt_index.get("scenario_id")
            or receipt.get("runtime_binding", {}).get("scenario_manifest_sha256")
            != receipt_index.get("scenario_manifest_sha256")
            or receipt.get("runtime_binding", {}).get("airframe_profile_sha256")
            != receipt_index.get("airframe_profile_sha256")
            or not isinstance(receipt.get("cleanup"), dict)
            or receipt.get("cleanup", {}).get("verified")
            is not receipt_index.get("cleanup_verified")
            or receipt.get("hil_real_evidence") is not False
            or receipt.get("outdoor_gnss_evidence") is not False
            or receipt.get("final_release_allowed") is not False
        ):
            raise ValueError(f"{role} campaign receipt contract drift")
        cleanup = receipt["cleanup"]
        if (
            cleanup.get("verified") is True
            and cleanup.get("post_remove_absence_proven") is not True
        ):
            raise ValueError(f"{role} campaign receipt cleanup binding drift")
        receipt_parity = receipt["release_parity"]
        if (
            receipt_parity.get("bytes") != parity_path.stat().st_size
            or receipt_parity.get("file_sha256") != parity_sha
            or receipt_parity.get("hashes") != parity.get("parity")
        ):
            raise ValueError(f"{role} campaign receipt release parity drift")
        if (
            receipt_index.get("evidence_scope") != "retained_files_audit"
            or receipt_index.get("release_parity_bound") is not False
            or receipt_index.get("audit_release_parity_bound") is not True
            or receipt_index.get("current_runtime_parity_proven") is not False
            or receipt_index.get("external_sim_rerun_performed") is not False
            or receipt.get("evidence_scope") != "retained_files_audit"
            or receipt.get("runtime_source_parity_verified") is not False
            or receipt.get("external_sim_rerun_performed") is not False
            or receipt_parity.get("scope") != "receipt_generation_file_binding"
        ):
            raise ValueError("campaign receipt release parity/audit scope drift")
        stored_receipt_sha = receipt.pop("receipt_sha256", None)
        if stored_receipt_sha != _canonical_sha256(receipt):
            raise ValueError(f"{role} campaign receipt canonical hash drift")
        canonical_key = f"{role}_receipt_canonical_sha256"
        if receipt_index.get(canonical_key) != stored_receipt_sha:
            raise ValueError(f"{role} campaign receipt canonical identity drift")
        sorties = receipt.get("sorties")
        if receipt.get("sortie_count") != 2 or not isinstance(sorties, list) or len(sorties) != 2:
            raise ValueError(f"{role} campaign receipt sortie count drift")
        for ordinal, sortie in enumerate(sorties, 1):
            if not isinstance(sortie, dict) or sortie.get("ordinal") != ordinal:
                raise ValueError(f"{role} campaign receipt sortie order drift")
            mission = _path(root, sortie.get("mission_evidence_path"), f"{role} Mission evidence")
            journal = _path(
                root, sortie.get("transaction_journal_path"), f"{role} transaction journal"
            )
            if sortie.get("mission_evidence_sha256") != _sha256(mission):
                raise ValueError(f"{role} Mission evidence SHA-256 drift")
            if sortie.get("transaction_journal_sha256") != _sha256(journal):
                raise ValueError(f"{role} transaction journal SHA-256 drift")
            policy = _path(root, sortie.get("policy_evidence_path"), f"{role} policy evidence")
            if sortie.get("policy_evidence_sha256") != _sha256(policy):
                raise ValueError(f"{role} policy evidence SHA-256 drift")
            if sortie.get("planner_revalidated") is not True:
                raise ValueError(f"{role} campaign sortie planner binding drift")
    _verify_current_external_policy_campaign(root, progress, parity_path, parity_sha, parity)
    _verify_current_shared_policy_sensor_campaign(root, progress, parity_path, parity_sha, parity)
    _verify_current_external_multirotor_shared_policy_depth_mission(
        root, progress, parity_path, parity_sha, parity
    )
    _verify_current_external_multirotor_shared_policy_depth_mission(
        root,
        progress,
        parity_path,
        parity_sha,
        parity,
        entry_key="external_fixed_wing_shared_policy_depth_mission_20260930",
    )
    _verify_current_external_shared_policy_pair(root, progress, parity)
    _verify_current_shared_policy_vehicle_matrix(root, progress)
    _verify_current_shared_policy_vtol_campaign(root, progress, parity_path, parity)
    _verify_current_shared_policy_runtime_matrix(root, progress, parity)
    _verify_current_airframe_transaction_acceptance(root, progress, parity)
    external = progress.get("external_docker_sim_refresh_20260925")
    if not isinstance(external, dict):
        raise ValueError("current external Docker SIM reference is missing")
    external_path = _path(root, external.get("evidence"), "external Docker SIM evidence")
    if external.get("evidence_bytes") != external_path.stat().st_size:
        raise ValueError("external Docker SIM evidence byte count drift")
    if external.get("evidence_sha256") != _sha256(external_path):
        raise ValueError("external Docker SIM evidence SHA-256 drift")
    external_document = _json(external_path)
    if external_document.get("gate_passed") is not True:
        raise ValueError("external Docker SIM evidence must be passed")
    if external_document.get("telemetry", {}).get("qualified") is not True:
        raise ValueError("external Docker SIM telemetry is not qualified")
    if external_document.get("telemetry", {}).get("gps", {}).get("fix_type") != 3:
        raise ValueError("external Docker SIM GPS fix binding drift")
    if external_document.get("container_runtime", {}).get("cleanup", {}).get("removed") is not True:
        raise ValueError("external Docker SIM cleanup binding drift")
    if external_document.get("hardware", {}).get("status") != "not_claimed":
        raise ValueError("external Docker SIM hardware claim must remain not_claimed")
    _verify_gazebo_fault_runs(root, progress)
    parity_receipt_index = progress.get("external_docker_ai_flight_parity_receipt_20260927_r2")
    if not isinstance(parity_receipt_index, dict):
        raise ValueError("AI flight parity receipt reference is missing")
    parity_receipt_path = _path(
        root, parity_receipt_index.get("receipt"), "AI flight parity receipt"
    )
    if parity_receipt_index.get("receipt_bytes") != parity_receipt_path.stat().st_size:
        raise ValueError("AI flight parity receipt byte count drift")
    if parity_receipt_index.get("receipt_file_sha256") != _sha256(parity_receipt_path):
        raise ValueError("AI flight parity receipt SHA-256 drift")
    schema_path = _path(root, parity_receipt_index.get("schema"), "AI flight parity schema")
    if parity_receipt_index.get("schema_bytes") != schema_path.stat().st_size:
        raise ValueError("AI flight parity schema byte count drift")
    if parity_receipt_index.get("schema_sha256") != _sha256(schema_path):
        raise ValueError("AI flight parity schema SHA-256 drift")
    parity_receipt = _json(parity_receipt_path)
    declared_receipt_sha = parity_receipt.get("receipt_sha256")
    unsigned_receipt = {
        key: value for key, value in parity_receipt.items() if key != "receipt_sha256"
    }
    if declared_receipt_sha != _canonical_sha256(unsigned_receipt):
        raise ValueError("AI flight parity receipt canonical hash drift")
    if parity_receipt_index.get("receipt_sha256") != declared_receipt_sha:
        raise ValueError("AI flight parity receipt canonical identity drift")
    if parity_receipt.get("schema") != "multidetect.simulator-ai-flight-parity-receipt":
        raise ValueError("AI flight parity receipt schema drift")
    bindings = parity_receipt.get("bindings")
    if (
        not isinstance(bindings, dict)
        or parity_receipt.get("evidence_scope") != "retained_layers_file_audit"
        or not isinstance(bindings.get("same_release_parity"), bool)
        or bindings.get("same_audit_reference_parity") is not True
        or not isinstance(bindings.get("current_runtime_parity_proven"), bool)
        or bindings.get("external_sim_rerun_performed") is not False
        or parity_receipt_index.get("evidence_scope") != "retained_layers_file_audit"
        or not isinstance(parity_receipt_index.get("same_release_parity"), bool)
        or not isinstance(parity_receipt_index.get("current_runtime_parity_proven"), bool)
        or parity_receipt_index.get("external_sim_rerun_performed") is not False
        or parity_receipt_index.get("same_audit_reference_parity") is not True
        or bindings.get("same_scenario_manifest") is not True
        or bindings.get("same_airframe_profile_identity") is not True
        or bindings.get("policy_applied_to_actuator") is not False
        or bindings.get("truth_free") is not True
        or bindings.get("planner_revalidated") is not True
        or bindings.get("final_release_allowed") is not False
        or bindings.get("same_release_parity")
        != bindings.get("current_runtime_parity_proven")
        or parity_receipt_index.get("same_release_parity")
        != parity_receipt_index.get("current_runtime_parity_proven")
    ):
        raise ValueError("AI flight parity receipt safety binding drift")
    receipt_parity = parity_receipt.get("release_parity")
    if (
        not isinstance(receipt_parity, dict)
        or receipt_parity.get("file_sha256") != parity_sha
        or receipt_parity.get("bytes") != parity_path.stat().st_size
        or receipt_parity.get("hashes") != parity.get("parity")
        or receipt_parity.get("scope") != "receipt_generation_file_binding"
    ):
        raise ValueError("AI flight parity receipt release parity drift")
    policy_layer = parity_receipt.get("shared_policy_mission_layer")
    ai_layer = parity_receipt.get("simulator_ai_flight_layer")
    if not isinstance(policy_layer, dict) or not isinstance(ai_layer, dict):
        raise ValueError("AI flight parity receipt layers are missing")
    policy_path = _path(root, policy_layer.get("path"), "AI flight policy receipt")
    ai_path = _path(root, ai_layer.get("path"), "AI flight evidence")
    if policy_layer.get("file_sha256") != _sha256(policy_path):
        raise ValueError("AI flight policy receipt SHA-256 drift")
    if ai_layer.get("file_sha256") != _sha256(ai_path):
        raise ValueError("AI flight evidence SHA-256 drift")
    try:
        from scripts.create_simulator_ai_flight_parity_receipt import (
            _parity,
            _validate_ai_report,
            _validate_policy_receipt,
        )

        audited_policy = _validate_policy_receipt(policy_path, _parity(parity_path))
        audited_ai = _validate_ai_report(ai_path, audited_policy, _parity(parity_path))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"AI flight retained-layer source audit failed: {exc}") from exc
    if policy_layer != audited_policy or ai_layer != audited_ai:
        raise ValueError("AI flight retained-layer source binding drift")
    ai_index = progress.get("external_docker_ai_flight_acceptance_20260927_r1")
    if not isinstance(ai_index, dict) or ai_index.get("evidence") != str(
        ai_path.relative_to(root)
    ).replace("\\", "/"):
        raise ValueError("AI flight parity evidence path drift")
    if ai_index.get("evidence_sha256") != ai_layer.get("file_sha256"):
        raise ValueError("AI flight parity evidence identity drift")
    release = progress.get("release_status")
    if not isinstance(release, dict) or release.get("release_allowed") is not False:
        raise ValueError("current progress must remain release-blocked")
    release_ref = progress.get("evidence", {}).get("release_gate_report")
    if not isinstance(release_ref, dict):
        raise ValueError("release-gate report reference is missing")
    audit_path = _path(root, release_ref.get("audit_summary_path"), "audit_summary_path")
    audit = _json(audit_path)
    if audit.get("release_allowed") is not False or audit.get("gate_passed") is not False:
        raise ValueError("release-gate audit is not fail-closed")
    if (
        audit.get("runtime_reexecuted") is not False
        or audit.get("parity_rebind_only") is not True
        or audit.get("evidence_scope") != "retained_files_audit"
        or audit.get("source_runtime_evaluated_at_utc") != audit.get("evaluated_at_utc")
    ):
        raise ValueError("release-gate audit provenance scope drift")
    current_parity_file = audit.get("current_parity_file")
    if not isinstance(current_parity_file, dict):
        raise ValueError("release-gate audit current parity file binding is missing")
    if (
        current_parity_file.get("path") != str(parity_path.relative_to(root)).replace("\\", "/")
        or current_parity_file.get("bytes") != parity_path.stat().st_size
        or current_parity_file.get("sha256") != parity_sha
    ):
        raise ValueError("release-gate audit current parity file binding drift")
    if audit.get("current_parity") != parity.get("parity"):
        raise ValueError("release-gate audit parity drift")
    mismatches = audit.get("mode_parity_mismatches")
    if not isinstance(mismatches, dict) or set(mismatches) != {"replay", "sim", "hil", "real"}:
        raise ValueError("release-gate audit mode parity mismatches are missing")
    parity_keys = set(parity.get("parity", {}))
    if any(
        not isinstance(fields, list) or not set(fields) <= parity_keys
        for fields in mismatches.values()
    ):
        raise ValueError("release-gate audit mode parity mismatches are malformed")
    parity_bound = not any(mismatches.values())
    if release_ref.get("current_release_parity_bound") is not parity_bound:
        raise ValueError("release-gate progress parity binding drift")
    if audit.get("current_release_parity_bound") is not parity_bound:
        raise ValueError("release-gate audit parity binding drift")
    reasons = audit.get("release_blocked_reasons")
    if (
        not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(reason, str) and reason for reason in reasons)
    ):
        raise ValueError("release-gate audit reasons are missing")
    if audit.get("blocker_count") != len(reasons) or release.get("blocker_count") != len(reasons):
        raise ValueError("release-gate blocker count drift")
    if release_ref.get("blocker_count") != len(reasons):
        raise ValueError("release-gate progress blocker count drift")
    if (
        release_ref.get("gate_passed") is not False
        or release_ref.get("release_allowed") is not False
    ):
        raise ValueError("release-gate progress is not fail-closed")
    if release_ref.get("local_path") != audit.get("report_path"):
        raise ValueError("release-gate report path drift")
    if release_ref.get("manifest_local_path") != audit.get("manifest_path"):
        raise ValueError("release-gate manifest path drift")
    if release_ref.get("file_bytes") != audit.get("report_bytes"):
        raise ValueError("release-gate report byte count drift")
    if release_ref.get("file_sha256") != audit.get("report_sha256"):
        raise ValueError("release-gate report SHA-256 drift")
    report_artifact_path = _path(root, audit.get("report_path"), "release-gate report")
    manifest_artifact_path = _path(root, audit.get("manifest_path"), "release-gate manifest")
    for label, artifact_path, bytes_key, sha_key in (
        ("report", report_artifact_path, "report_bytes", "report_sha256"),
        ("manifest", manifest_artifact_path, "manifest_bytes", "manifest_sha256"),
    ):
        if audit.get(bytes_key) != artifact_path.stat().st_size:
            raise ValueError(f"release-gate {label} byte count drift")
        if audit.get(sha_key) != _sha256(artifact_path):
            raise ValueError(f"release-gate {label} SHA-256 drift")
    raw_report = _json(report_artifact_path)
    if raw_report.get("release_blocked_reasons") != reasons:
        raise ValueError("release-gate report reasons drift")
    if raw_report.get("gate_passed") is not False or raw_report.get("release_allowed") is not False:
        raise ValueError("release-gate report is not fail-closed")
    modes = raw_report.get("modes")
    if not isinstance(modes, dict) or set(modes) != {"replay", "sim", "hil", "real"}:
        raise ValueError("release-gate report modes are missing")
    for mode, record in modes.items():
        if not isinstance(record, dict) or not isinstance(record.get("parity"), dict):
            raise ValueError(f"release-gate report {mode} parity is missing")
        actual_mismatches = sorted(
            key for key, value in parity["parity"].items() if record["parity"].get(key) != value
        )
        if sorted(mismatches[mode]) != actual_mismatches:
            raise ValueError(f"release-gate report {mode} parity drift")
    replay_gate = raw_report.get("replay_set")
    if not isinstance(replay_gate, dict):
        raise ValueError("release-gate replay-set section is missing")
    positive_replay_ref = progress.get("evidence", {}).get("replay_set_positive_fixture_20260924")
    if not isinstance(positive_replay_ref, dict):
        raise ValueError("positive replay fixture reference is missing")
    replay_manifest = replay_gate.get("manifest")
    if not isinstance(replay_manifest, dict):
        raise ValueError("release-gate replay-set manifest binding is missing")
    if (
        Path(str(replay_manifest.get("path", ""))).resolve()
        != _path(
            root, positive_replay_ref.get("manifest_path"), "positive replay manifest"
        ).resolve()
    ):
        raise ValueError("release-gate replay-set manifest path drift")
    if replay_manifest.get("sha256") != positive_replay_ref.get("manifest_sha256"):
        raise ValueError("release-gate replay-set manifest SHA-256 drift")
    replay_accuracy = replay_gate.get("accuracy_evidence")
    if not isinstance(replay_accuracy, dict):
        raise ValueError("release-gate replay-set accuracy binding is missing")
    if (
        Path(str(replay_accuracy.get("path", ""))).resolve()
        != _path(
            root,
            positive_replay_ref.get("accuracy_report_path"),
            "positive replay accuracy report",
        ).resolve()
    ):
        raise ValueError("release-gate replay-set accuracy path drift")
    if replay_accuracy.get("sha256") != positive_replay_ref.get("accuracy_report_sha256"):
        raise ValueError("positive replay accuracy report SHA-256 drift")
    policy_update = progress.get("shared_policy_external_campaign_api_update")
    if not isinstance(policy_update, dict):
        raise ValueError("shared policy external campaign progress section is missing")
    canonical = policy_update.get("external_repeat_3x3_sim_energy_evaluation_evidence")
    canonical_sha = policy_update.get("external_repeat_3x3_sim_energy_evaluation_sha256")
    if canonical is not None:
        canonical_report_path = _path(root, canonical, "external repeat evaluation evidence")
        if canonical_sha != _sha256(canonical_report_path):
            raise ValueError("external repeat evaluation SHA-256 drift")
    field = progress.get("evidence", {}).get("field_link_read_only_current_probe")
    if not isinstance(field, dict):
        raise ValueError("field-link probe reference is missing")
    field_path = _path(root, field.get("local_path"), "field-link preflight")
    if field.get("file_bytes") != field_path.stat().st_size:
        raise ValueError("field-link preflight byte count drift")
    if field.get("file_sha256") != _sha256(field_path):
        raise ValueError("field-link preflight SHA-256 drift")
    interface_path = _path(
        root, field.get("interface_diagnostic_local_path"), "field-link interface diagnostic"
    )
    if field.get("interface_diagnostic_file_bytes") != interface_path.stat().st_size:
        raise ValueError("field-link interface diagnostic byte count drift")
    if field.get("interface_diagnostic_file_sha256") != _sha256(interface_path):
        raise ValueError("field-link interface diagnostic SHA-256 drift")
    if field.get("hardware_link_ready") is not False:
        raise ValueError("field-link readiness must remain fail-closed")
    audited_current_field = audit.get("current_field_link_probe")
    if audited_current_field is not None:
        if not isinstance(audited_current_field, dict):
            raise ValueError("release-gate current field-link probe is malformed")
        if (
            field.get("local_path") != audited_current_field.get("preflight_path")
            or field.get("file_bytes") != audited_current_field.get("preflight_bytes")
            or field.get("file_sha256") != audited_current_field.get("preflight_sha256")
        ):
            raise ValueError("release-gate field-link report path drift")
        if (
            field.get("interface_diagnostic_local_path")
            != audited_current_field.get("interface_diagnostic_path")
            or field.get("interface_diagnostic_file_bytes")
            != audited_current_field.get("interface_diagnostic_bytes")
            or field.get("interface_diagnostic_file_sha256")
            != audited_current_field.get("interface_diagnostic_sha256")
        ):
            raise ValueError("release-gate field-link diagnostic binding drift")
        if audited_current_field.get("hardware_link_ready") is not False:
            raise ValueError("release-gate current field-link readiness drift")
        report_field_binding = audit.get("field_link_probe")
        if not isinstance(report_field_binding, dict):
            raise ValueError("release-gate historical field-link binding is missing")
    else:
        # Older release-gate audits predate the separate current read-only
        # probe.  Their real-field-link report is still valid only against the
        # historical binding embedded in that audit; do not compare it with
        # the newer current probe reference above.
        report_field_binding = audit.get("field_link_probe")
        if not isinstance(report_field_binding, dict):
            raise ValueError("release-gate historical field-link binding is missing")
    field_report = raw_report.get("real_field_link", {}).get("report")
    if not isinstance(field_report, dict):
        raise ValueError("release-gate real field-link report is missing")
    bound_field_path = (
        _path(
            root,
            report_field_binding.get("preflight_path"),
            "release-gate historical field-link",
        )
        if report_field_binding is not None
        else field_path
    ).resolve()
    report_field_path = Path(str(field_report.get("path", ""))).resolve()
    if bound_field_path != report_field_path:
        raise ValueError("release-gate historical field-link report path drift")
    expected_report_sha = (
        report_field_binding.get("preflight_sha256")
        if report_field_binding is not None
        else field.get("file_sha256")
    )
    expected_report_bytes = (
        report_field_binding.get("preflight_bytes")
        if report_field_binding is not None
        else field.get("file_bytes")
    )
    if field_report.get("sha256") != expected_report_sha:
        raise ValueError("release-gate field-link report SHA-256 drift")
    if field_report.get("bytes") != expected_report_bytes:
        raise ValueError("release-gate field-link report byte count drift")
    diagnostic = field_report.get("checks", {}).get("interface_diagnostic")
    if not isinstance(diagnostic, dict):
        raise ValueError("release-gate field-link interface diagnostic is missing")
    bound_diagnostic_path = (
        _path(
            root,
            report_field_binding.get("interface_diagnostic_path"),
            "release-gate historical field-link diagnostic",
        )
        if report_field_binding is not None
        else interface_path
    ).resolve()
    report_diagnostic_path = Path(str(diagnostic.get("report_path", ""))).resolve()
    if bound_diagnostic_path != report_diagnostic_path:
        raise ValueError("release-gate historical field-link diagnostic path drift")
    expected_diagnostic_sha = (
        report_field_binding.get("interface_diagnostic_sha256")
        if report_field_binding is not None
        else field.get("interface_diagnostic_file_sha256")
    )
    expected_diagnostic_bytes = (
        report_field_binding.get("interface_diagnostic_bytes")
        if report_field_binding is not None
        else field.get("interface_diagnostic_file_bytes")
    )
    if diagnostic.get("report_sha256") != expected_diagnostic_sha:
        raise ValueError("release-gate field-link diagnostic SHA-256 drift")
    if diagnostic.get("report_bytes") != expected_diagnostic_bytes:
        raise ValueError("release-gate field-link diagnostic byte count drift")
    positive = positive_replay_ref
    if not isinstance(positive, dict):
        raise ValueError("positive replay fixture reference is missing")
    positive_paths = (
        ("manifest", "manifest_path", "manifest_bytes", "manifest_sha256"),
        (
            "accuracy report",
            "accuracy_report_path",
            "accuracy_report_bytes",
            "accuracy_report_sha256",
        ),
        (
            "readiness report",
            "readiness_report_path",
            "readiness_report_bytes",
            "readiness_report_sha256",
        ),
        (
            "calibration report",
            "calibration_report_path",
            "calibration_report_bytes",
            "calibration_report_sha256",
        ),
        (
            "calibration verification",
            "calibration_verification_path",
            "calibration_verification_bytes",
            "calibration_verification_sha256",
        ),
    )
    positive_documents: dict[str, dict[str, Any]] = {}
    for label, path_key, bytes_key, sha_key in positive_paths:
        artifact_path = _path(root, positive.get(path_key), f"positive replay {label}")
        if positive.get(bytes_key) != artifact_path.stat().st_size:
            raise ValueError(f"positive replay {label} byte count drift")
        if positive.get(sha_key) != _sha256(artifact_path):
            raise ValueError(f"positive replay {label} SHA-256 drift")
        positive_documents[label] = _json(artifact_path)
    positive_accuracy = positive_documents["accuracy report"]
    positive_readiness = positive_documents["readiness report"]
    positive_calibration = positive_documents["calibration report"]
    positive_calibration_verification = positive_documents["calibration verification"]
    if positive.get("source_kind") != "synthetic_demo":
        raise ValueError("positive replay fixture source kind must remain synthetic_demo")
    if positive.get("positive_truth_by_stratum") is not True:
        raise ValueError("positive replay truth strata binding is missing")
    if positive.get("positive_prediction_by_stratum") is not True:
        raise ValueError("positive replay prediction strata binding is missing")
    if positive.get("deployment_domain_evidence_complete") is not False:
        raise ValueError("positive replay deployment-domain claim must remain false")
    if positive.get("annotations_reviewed") is not False:
        raise ValueError("positive replay review claim must remain false")
    if positive.get("gate_passed") is not False:
        raise ValueError("positive replay fixture must remain release-blocked")
    if positive_accuracy.get("manifest_sha256") != positive.get("manifest_sha256"):
        raise ValueError("positive replay accuracy manifest binding drift")
    if positive_accuracy.get("positive_strata_coverage_passed") is not True:
        raise ValueError("positive replay accuracy strata binding is missing")
    if positive_accuracy.get("deployment_domain_evidence_complete") is not False:
        raise ValueError("positive replay accuracy deployment claim must remain false")
    if positive_readiness.get("manifest_sha256") != positive.get("manifest_sha256"):
        raise ValueError("positive replay readiness manifest binding drift")
    readiness_checks = positive_readiness.get("checks")
    if not isinstance(readiness_checks, dict):
        raise ValueError("positive replay readiness checks are missing")
    if readiness_checks.get("positive_truth_by_stratum") is not True:
        raise ValueError("positive replay readiness truth strata binding is missing")
    if readiness_checks.get("positive_prediction_by_stratum") is not True:
        raise ValueError("positive replay readiness prediction strata binding is missing")
    if positive_readiness.get("gate_passed") is not False:
        raise ValueError("positive replay readiness must remain release-blocked")
    if positive_calibration.get("manifest_sha256") != positive.get("manifest_sha256"):
        raise ValueError("positive replay calibration manifest binding drift")
    if positive_calibration.get("promotion_ready") is not False:
        raise ValueError("positive replay calibration must remain advisory")
    if positive_calibration_verification.get("manifest_path") != str(
        _path(root, positive.get("manifest_path"), "positive replay manifest").resolve()
    ):
        raise ValueError("positive replay calibration verification manifest path drift")
    if not positive_calibration_verification.get("checks", {}).get("manifest_sha256_matches"):
        raise ValueError("positive replay calibration verification manifest binding drift")
    if positive_calibration_verification.get("gate_passed") is not True:
        raise ValueError("positive replay calibration verification must be internally valid")
    replay = progress.get("evidence", {}).get("replay_set_readiness_e7")
    if not isinstance(replay, dict):
        raise ValueError("replay-set readiness reference is missing")
    latest = replay.get("latest_readiness_recomputation")
    if not isinstance(latest, dict):
        raise ValueError("latest replay-set readiness recomputation is missing")
    readiness_path = _path(root, latest.get("path"), "replay-set readiness report")
    if latest.get("bytes") != readiness_path.stat().st_size:
        raise ValueError("replay-set readiness byte count drift")
    if latest.get("sha256") != _sha256(readiness_path):
        raise ValueError("replay-set readiness SHA-256 drift")
    if latest.get("gate_passed") is not False:
        raise ValueError("replay-set readiness must remain fail-closed")
    return {
        "status": "passed",
        "updated_at_utc": updated,
        "release_label": version.get("release_label"),
        "release_allowed": release.get("release_allowed"),
        "audit_summary_path": str(audit_path.relative_to(root)),
        "release_gate_report_bound": True,
        "canonical_external_repeat_bound": canonical is not None,
        "field_link_bound": True,
        "observed_session_bound": True,
        "current_shared_policy_observed_session_bound": True,
        "shared_policy_training_holdout_bound": True,
        "shared_policy_trained_artifact_external_bound": True,
        "shared_policy_campaign_receipts_bound": True,
        "shared_policy_vehicle_matrix_bound": True,
        "current_shared_policy_vtol_campaign_bound": True,
        "shared_policy_runtime_matrix_bound": True,
        "external_gazebo_sensor_fault_runs_bound": True,
        "replay_set_readiness_bound": True,
        "external_mission_matrix_bound": True,
        "external_multirotor_shared_policy_depth_mission_bound": True,
        "external_fixed_wing_shared_policy_depth_mission_bound": True,
        "current_combined_sim_qgc_bound": True,
        "v030_pre1_foundation_bound": True,
        "current_source_recomputed": source_parity is not None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--progress", type=Path)
    parser.add_argument(
        "--check-sources",
        action="store_true",
        help="recompute release parity from its explicit source list before checking the index",
    )
    parser.add_argument(
        "--check-sources-only",
        action="store_true",
        help="only validate the canonical parity document against current source files",
    )
    args = parser.parse_args(argv)
    try:
        source_result = (
            verify_release_parity_sources(args.root) if args.check_sources_only else None
        )
        result = (
            source_result
            if args.check_sources_only
            else verify(args.root, args.progress, check_source_parity=args.check_sources)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
