from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-vlm-execution-admission-20261007-r1.json"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_execution_development_receipt_binds_actual_sources_and_unchanged_baselines():
    receipt = _json(RECEIPT)
    for record in receipt["sources"] + receipt["baselines"]:
        path = ROOT / record["path"]
        assert path.stat().st_size == record["bytes"]
        assert _sha(path) == record["sha256"]
    old = _json(ROOT / receipt["prior_runtime_receipt"]["path"])
    for record in old["sources"] + old["baselines"]:
        path = ROOT / record["path"]
        assert path.stat().st_size == record["bytes"]
        assert _sha(path) == record["sha256"]
    for record in (receipt["prior_runtime_receipt"], receipt["cluster_diagnostic_receipt"]):
        path = ROOT / record["path"]
        assert path.stat().st_size == record["bytes"]
        assert _sha(path) == record["sha256"]


def test_execution_software_gates_do_not_claim_training_flight_or_live_coverage():
    receipt = _json(RECEIPT)
    assert receipt["release_label"] == "0.3.0-pre.8"
    assert receipt["software_validation"]["source_vs_current_async_admission_verified"]
    assert receipt["software_validation"]["receive_only_evidence_collector_implemented"]
    assert receipt["software_validation"]["full_mission_wire_clearance_required"]
    assert receipt["software_validation"]["consume_time_unavailability_does_not_clear_mission"]
    assert receipt["software_validation"]["source_expiry_preserves_fresh_queue"]
    assert receipt["software_validation"]["source_and_processing_clock_domains_separated"]
    assert receipt["software_validation"]["native_exit_code"] == 0
    for name in (
        "candidate_promoted",
        "final_release_allowed",
        "physical_performance_claimed",
        "autonomous_flight_verified",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "vlm_finetuning_performed",
        "native_vlm_generation_performed",
        "flight_action_training_performed",
        "world_model_local_inference_measured",
        "execution_authority",
        "live_route_clearance_producer_implemented",
        "live_collector_receiver_join_implemented",
        "collector_scheduler_revision_join_implemented",
        "inflight_continuation_mission_implemented",
        "arbitrary_prompt_task_resolution_verified",
        "onboard_policy_reference_changed",
        "original_detector_changed",
    ):
        assert receipt[name] is False


def test_progress_points_to_new_exact_receipt_without_rewriting_old_native_proof():
    entry = _json(ROOT / "docs/current-progress.json")["onboard_vlm_execution_development_20261007"]
    assert entry["evidence"] == str(RECEIPT.relative_to(ROOT)).replace("\\", "/")
    assert entry["evidence_bytes"] == RECEIPT.stat().st_size
    assert entry["evidence_sha256"] == _sha(RECEIPT)
    assert entry["source_vs_current_async_admission_software_verified"] is True
    assert entry["receive_only_evidence_collector_implemented"] is True
    assert entry["live_collector_receiver_join_implemented"] is False
    assert entry["actual_flight_verified"] is False
    assert entry["final_release_allowed"] is False


def test_recovered_cluster_collector_success_is_not_gazebo_runtime_success():
    receipt = _json(RECEIPT)
    cluster = _json(ROOT / receipt["cluster_diagnostic_receipt"]["path"])
    for name in ("artifact", "stdout"):
        record = cluster[name]
        path = ROOT / record["local_path"]
        assert path.stat().st_size == record["bytes"]
        assert _sha(path) == record["sha256"]
        assert record["controller_sha256_match"] is True
    document = _json(ROOT / cluster["artifact"]["local_path"])
    assert document == _json(ROOT / cluster["stdout"]["local_path"])
    assert cluster["native_exit_semantics"]["collector_job_native_exit_code"] == 0
    assert cluster["native_exit_semantics"]["gazebo_runtime_probe_native_exit_code"] == 2
    assert cluster["native_exit_semantics"]["docker_info_native_exit_code"] == 1
    assert cluster["verified_probe_outcome"]["container_capture_prerequisites_ready"] is False
    assert cluster["verified_probe_outcome"]["rendering_verified"] is False
