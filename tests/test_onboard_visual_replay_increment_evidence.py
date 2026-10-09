from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "onboard_visual_replay_increment_20261007_r4"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bound(reference: dict, *, required: bool = False) -> Path | None:
    path = (ROOT / reference["path"]).resolve()
    path.relative_to(ROOT.resolve())
    if not path.is_file():
        assert not required, path
        return None
    assert path.stat().st_size == reference["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    return path


def _receipt() -> tuple[dict, dict]:
    entry = _json(ROOT / "docs/current-progress.json")[ENTRY]
    path = _bound(
        {
            "path": entry["evidence"],
            "bytes": entry["evidence_bytes"],
            "sha256": entry["evidence_sha256"],
        },
        required=True,
    )
    assert path is not None
    return entry, _json(path)


def test_replay_increment_is_preregistered_incremental_visual_training() -> None:
    entry, receipt = _receipt()
    plan_path = _bound(receipt["preregistered_plan"], required=True)
    assert plan_path is not None
    plan = _json(plan_path)
    assert receipt["training"]["parent_checkpoint"]["sha256"] == plan["parent"]["sha256"]
    assert receipt["training"]["configuration"] == plan["configuration"]
    assert receipt["development_quality_rules"] == plan["evaluation_rules_frozen_before_training"]
    assert receipt["dataset"]["split_frames"] == {"train": 832, "val": 256, "test": 256}
    assert receipt["dataset"]["original_train_clip_frames"] == 64
    assert receipt["dataset"]["original_val_clip_frames_used_for_checkpoint_selection"] == 64
    assert receipt["dataset"]["source_manifests"] == plan["source_manifests"]
    assert receipt["dataset"]["test_frames_moved_to_train"] == 0
    assert receipt["training"]["transferred_parameter_items"] == "708/708"
    assert receipt["training"]["epochs_completed"] == 12
    assert receipt["training"]["native_exit_code"] == 0
    assert entry["candidate_onnx_sha256"] == receipt["training"]["onnx"]["sha256"]
    assert receipt["training"]["class_names"] == {"0": "search_target"}


def test_available_replay_increment_training_and_source_bytes_are_bound() -> None:
    _, receipt = _receipt()
    for reference in receipt["sources"] + receipt["dataset"]["source_manifests"]:
        _bound(reference, required=reference["path"].startswith(("scripts/", "tests/")))
    _bound(receipt["dataset"]["manifest"])
    training = receipt["training"]
    for key in (
        "parent_checkpoint",
        "parent_report",
        "report",
        "checkpoint",
        "onnx",
        "export_receipt",
        "invocation_binding",
        "closed_input_inventory",
        "results_csv",
        "log",
    ):
        _bound(training[key])
    report_path = _bound(training["report"])
    if report_path is not None:
        report = _json(report_path)
        assert report["dataset"]["sha256"] == receipt["dataset"]["manifest"]["sha256"]
        assert report["parent"]["sha256"] == training["parent_checkpoint"]["sha256"]
        assert report["onnx"]["sha256"] == training["onnx"]["sha256"]
        assert report["names"] == training["class_names"]
        assert report["epochs_requested"] == 12
    csv_path = _bound(training["results_csv"])
    if csv_path is not None:
        with csv_path.open(encoding="utf-8", newline="") as stream:
            epochs = [int(row["epoch"]) for row in csv.DictReader(stream)]
        assert epochs == list(range(1, 13))


@pytest.mark.parametrize("group", ["far_static", "old_static"])
def test_static_comparisons_use_identical_bound_models_and_fixed_thresholds(group: str) -> None:
    _, receipt = _receipt()
    result = receipt["comparisons"][group]
    path = _bound(result["report"])
    if path is None:
        return
    report = _json(path)
    assert result["native_exit_code"] == 0
    assert report["evaluation_confidence"] == 0.25
    assert report["iou_threshold"] == 0.5
    for name in ("parent", "candidate"):
        actual = report["models"][name]
        assert actual["overall"]["overall"] == result[name]
    assert report["models"]["parent"]["sha256"] == receipt["training"]["parent_onnx"]["sha256"]
    assert report["models"]["candidate"]["sha256"] == receipt["training"]["onnx"]["sha256"]


@pytest.mark.parametrize("group", ["far_clips", "old_clips"])
def test_clip_comparisons_bind_native_predictions_and_identity_counters(group: str) -> None:
    _, receipt = _receipt()
    result = receipt["comparisons"][group]
    for name in ("parent", "candidate"):
        item = result[name]
        path = _bound(item["report"])
        assert item["native_exit_code"] == 0
        if path is None:
            continue
        report = _json(path)
        assert report["observed_counters"] == item["observed_counters"]
        assert report["model"]["configured_confidence_threshold"] == 0.25
        assert report["truth_into_runtime"] is False
        assert report["predictions_completed_before_truth_evaluation"] is True
        assert (
            report["model"]["sha256"]
            == receipt["training"]["parent_onnx" if name == "parent" else "onnx"]["sha256"]
        )


def test_observation_audit_does_not_silently_admit_wrong_seed_or_policy_training() -> None:
    _, receipt = _receipt()
    audit = receipt["observation_corpus_audit"]
    assert audit["native_exit_code"] == 2
    assert audit["payload_readable_frames"] == 403
    assert audit["source_verified_frames"] == 303
    assert audit["source_verified_recordings"] == 4
    assert audit["rejected_staging_seed"] == 42017
    assert audit["actual_raw_seed"] == 71041
    assert audit["observation_pretraining_eligible"] is False
    path = _bound(audit["report"])
    if path is not None:
        assert _json(path)["observation_pretraining_eligible"] is False


def test_development_gates_are_recomputed_not_inferred_from_ap() -> None:
    entry, receipt = _receipt()
    rules = receipt["development_quality_rules"]
    compared = receipt["comparisons"]
    passed = {}
    for name in ("far_static", "old_static", "old_partial_occlusion"):
        counts = compared[name]["candidate"]
        passed[name] = (
            counts["true_positives"] >= rules[name]["tp_min"]
            and counts["false_positives"] <= rules[name]["fp_max"]
        )
    for name in ("far_clips", "old_clips"):
        counts = compared[name]["candidate"]["observed_counters"]
        passed[name] = (
            counts["detection_tp"] >= rules[name]["tp_min"]
            and counts["detection_fp"] <= rules[name]["fp_max"]
            and counts["id_switches"] <= rules[name]["id_switches_max"]
            and counts["same_identity_recovered_events"]
            >= rules[name]["same_id_reacquisitions_min"]
        )
    assert passed == receipt["development_quality_results"]
    assert all(passed.values()) == receipt["all_development_quality_rules_passed"]
    assert entry["development_quality_results"] == passed
    for key in (
        "onboard_policy_reference_changed",
        "original_detector_changed",
        "training_ready",
        "gazebo_capture",
        "autonomous_flight_verified",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "temporal_model_training_performed",
        "prompt_understanding_training_performed",
        "flight_action_training_performed",
    ):
        assert receipt[key] is entry[key] is False


def test_pretrained_research_documents_are_durable_and_not_runtime_claims() -> None:
    progress = _json(ROOT / "docs/current-progress.json")
    entry = progress["onboard_pretrained_multimodal_research_20261007"]
    path = _bound(
        {
            "path": entry["evidence"],
            "bytes": entry["evidence_bytes"],
            "sha256": entry["evidence_sha256"],
        },
        required=True,
    )
    assert path is not None
    receipt = _json(path)
    assert receipt["parallel_agents"] == 3
    assert len(receipt["research_documents"]) == 3
    assert len(receipt["vlm_candidates"]) == 5
    for document in receipt["research_documents"]:
        _bound(document, required=True)
    for key in (
        "vlm_weights_downloaded",
        "vlm_local_inference_measured",
        "vlm_finetuning_performed",
        "world_model_local_inference_measured",
        "world_model_finetuning_performed",
        "onboard_policy_reference_changed",
        "original_detector_changed",
        "candidate_promoted",
        "autonomous_flight_verified",
        "physical_performance_claimed",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert receipt[key] is entry[key] is False


def test_old_detector_policy_and_parity_are_preserved_by_visual_increment() -> None:
    _, receipt = _receipt()
    for reference in receipt["original_model_and_parity_preservation"]:
        _bound(reference, required=reference["path"].startswith("configs/"))
