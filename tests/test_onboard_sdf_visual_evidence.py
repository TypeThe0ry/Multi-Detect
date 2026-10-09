from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VISUAL_ENTRY = "onboard_sdf_blender_visual_training_20261007"
RUNTIME_ENTRY = "gazebo_3d_capture_runtime_blockers_20261007"
CURRICULUM_ENTRY = "onboard_sdf_blender_curriculum_training_20261007_r2"
FAR_PREPARATION_ENTRY = "onboard_far_visual_preparation_20261007"
SEQUENCE_ENTRY = "onboard_sequence_input_development_20261007"
FAR_INCREMENT_ENTRY = "onboard_far_visual_increment_20261007_r3"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bound_artifact(reference: dict) -> Path | None:
    path = (ROOT / reference["path"]).resolve()
    path.relative_to(ROOT.resolve())
    # Large development outputs are deliberately not required in a clean clone.
    if not path.is_file():
        return None
    assert path.stat().st_size == reference["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    return path


@pytest.mark.parametrize("entry_name", [VISUAL_ENTRY, RUNTIME_ENTRY])
def test_sdf_visual_progress_binds_durable_evidence(entry_name: str) -> None:
    entry = _json(ROOT / "docs/current-progress.json")[entry_name]
    path = ROOT / entry["evidence"]
    assert path.is_file()
    assert path.stat().st_size == entry["evidence_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["evidence_sha256"]


def test_sdf_visual_development_does_not_promote_or_replace_models() -> None:
    progress = _json(ROOT / "docs/current-progress.json")
    entry = progress[VISUAL_ENTRY]
    evidence = _json(ROOT / entry["evidence"])
    for key in (
        "onboard_policy_reference_changed",
        "original_detector_changed",
        "training_ready",
        "gazebo_capture",
        "autonomous_flight_verified",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert entry[key] is False
        assert evidence[key] is False
    assert evidence["training"]["names"] == entry["class_names"] == {"0": "search_target"}
    assert evidence["training"]["onnx"]["sha256"] == entry["candidate_onnx_sha256"]
    assert evidence["training"]["onnx"]["path"] == entry["candidate_onnx"]
    assert evidence["dataset"]["billboards_or_pasted_target_images"] is False
    assert entry["data_provenance"] == (
        "offline_Blender_render_of_repository_SDF_geometry_not_Gazebo_capture"
    )
    splits = evidence["dataset"]["splits"]
    assert len({item["world"] for item in splits.values()}) == len(splits)
    assert {name: item["frames"] for name, item in splits.items()} == entry["split_frames"]
    assert all(item["positive"] + item["negative"] == item["frames"] for item in splits.values())


def test_available_visual_training_and_runtime_artifacts_match_evidence() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[VISUAL_ENTRY]
    evidence = _json(ROOT / entry["evidence"])
    _bound_artifact(evidence["dataset"])
    _bound_artifact(evidence["training"]["checkpoint"])
    _bound_artifact(evidence["training"]["onnx"])
    report_path = _bound_artifact(evidence["training"]["report"])
    if report_path is not None:
        report = _json(report_path)
        assert report["onnx"]["sha256"] == evidence["training"]["onnx"]["sha256"]
        assert report["dataset"]["sha256"] == evidence["dataset"]["sha256"]
        assert report["parent"]["sha256"] == evidence["training"]["parent_sha256"]
        assert report["names"] == evidence["training"]["names"]
        assert report["epochs_requested"] == evidence["training"]["epochs_completed"]
        assert report["original_detector_changed"] is False
        assert report["onboard_policy_reference_changed"] is False
    runtime = evidence["shared_runtime_test"]
    runtime_path = _bound_artifact(runtime)
    if runtime_path is not None:
        raw = _json(runtime_path)
        full_frame = raw["all_classes"]["full_frame"]
        assert raw["model_sha256"] == evidence["training"]["onnx"]["sha256"]
        assert raw["model_class_names"] == ["search_target"]
        assert full_frame["frame_count"] == runtime["frames"]
        assert full_frame["confidence_threshold"] == runtime["confidence_threshold"]
        assert full_frame["iou_threshold"] == runtime["iou_threshold"]
        for key in ("true_positives", "false_positives", "false_negatives"):
            assert full_frame["overall"][key] == runtime[key] == entry["shared_onnx_test"][key]
        assert full_frame["inference_latency_p95_ms"] == runtime["inference_p95_ms"]


def test_curriculum_receipt_is_bound_and_does_not_claim_flight() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[CURRICULUM_ENTRY]
    path = ROOT / entry["evidence"]
    assert path.stat().st_size == entry["evidence_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["evidence_sha256"]
    receipt = _json(path)
    for name in (
        "training_ready",
        "gazebo_capture",
        "autonomous_flight_verified",
        "candidate_promoted",
        "onboard_policy_reference_changed",
        "original_detector_changed",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert receipt[name] is entry[name] is False
    assert receipt["data_provenance"] == (
        "offline_Blender_render_of_repository_SDF_geometry_not_Gazebo_capture"
    )
    assert receipt["geometry_holdout"]["verified_from_sdf_primitives"] is True
    assert receipt["geometry_holdout"]["test_geometry_disjoint"] is True
    counts = receipt["dataset"]["split_frames"]
    assert counts == entry["split_frames"]
    for split, frames in counts.items():
        assert sum(receipt["dataset"]["measured_conditions"][split].values()) == frames
    assert receipt["clips"]["flight_action_labels_created"] is False
    assert receipt["clip_evaluation"]["truth_into_runtime"] is False
    assert receipt["clip_evaluation"]["model_and_tracker_input_fields"] == ["image_bgr", "dt"]


def test_available_curriculum_outputs_match_training_and_paired_results() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[CURRICULUM_ENTRY]
    receipt = _json(ROOT / entry["evidence"])
    _bound_artifact(receipt["dataset"])
    _bound_artifact(receipt["training"]["checkpoint"])
    _bound_artifact(receipt["training"]["onnx"])
    report_path = _bound_artifact(receipt["training"]["report"])
    if report_path is not None:
        report = _json(report_path)
        assert report["parent"]["sha256"] == receipt["training"]["parent_checkpoint_sha256"]
        assert report["dataset"]["sha256"] == receipt["dataset"]["sha256"]
        assert report["onnx"]["sha256"] == receipt["training"]["onnx"]["sha256"]
        assert (
            report["parent_training_report"]["sha256"]
            == receipt["training"]["parent_report_sha256"]
        )
        assert report["epochs_requested"] == receipt["training"]["epochs_completed"]
        assert report["names"] == {"0": "search_target"}
        _bound_artifact(receipt["training"]["invocation_binding"])
        _bound_artifact(receipt["training"]["export_receipt"])
    paired_path = _bound_artifact(receipt["paired_evaluation"])
    if paired_path is not None:
        paired = _json(paired_path)
        assert paired["dataset"]["sha256"] == receipt["dataset"]["sha256"]
        assert paired["test_geometry_disjoint"] is True
        assert paired["condition_counts"] == receipt["dataset"]["measured_conditions"]["test"]
        assert paired["models"]["candidate"]["sha256"] == receipt["training"]["onnx"]["sha256"]
        for name in ("parent", "candidate"):
            assert (
                paired["models"][name]["overall"]["overall"]
                == receipt["paired_evaluation"]["overall"][name]
            )
    _bound_artifact(receipt["clips"])
    for name in ("parent", "candidate"):
        replay_path = _bound_artifact(receipt["clip_evaluation"][name])
        if replay_path is not None:
            replay = _json(replay_path)
            assert replay["manifest"]["sha256"] == receipt["clips"]["sha256"]
            assert (
                replay["observed_counters"] == receipt["clip_evaluation"][name]["observed_counters"]
            )
            assert replay["truth_into_runtime"] is False
            assert replay["model"]["label_aliases"] == {}
            assert replay["model"]["class_names"] == ["search_target"]


def test_far_preparation_is_a_dated_pretraining_receipt_not_a_training_result() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[FAR_PREPARATION_ENTRY]
    path = ROOT / entry["evidence"]
    assert path.stat().st_size == entry["evidence_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["evidence_sha256"]
    receipt = _json(path)
    assert receipt["observed_at_utc"] == entry["observed_at_utc"]
    assert receipt["planned_increment"]["training_started"] is False
    assert receipt["planned_increment"]["trained_model_created"] is False
    assert entry["training_started_at_observation"] is False
    assert entry["trained_model_created_at_observation"] is False
    assert receipt["full_training_dataset"]["status"] == (
        "render_in_progress_no_sealed_manifest_claimed"
    )
    for key in (
        "training_ready", "gazebo_capture", "autonomous_flight_verified",
        "candidate_promoted", "onboard_policy_reference_changed", "original_detector_changed",
        "physical_performance_claimed", "hil_real_evidence", "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert receipt[key] is entry[key] is False


def test_available_far_preparation_artifacts_and_failed_parent_baseline_are_bound() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[FAR_PREPARATION_ENTRY]
    receipt = _json(ROOT / entry["evidence"])
    for reference in receipt["sources"]:
        assert _bound_artifact(reference) is not None
    _bound_artifact(receipt["suite"])
    _bound_artifact(receipt["smoke"]["manifest"])
    _bound_artifact(receipt["smoke"]["audit"])
    _bound_artifact(receipt["previous_dataset_audit"])
    report_path = _bound_artifact(receipt["parent_baseline"])
    if report_path is not None:
        report = _json(report_path)
        assert report["manifest"]["sha256"] == receipt["suite"]["sha256"]
        assert report["observed_counters"] == receipt["parent_baseline"]["counters"]
        assert report["truth_into_runtime"] is False
    counts = receipt["parent_baseline"]["counters"]
    assert counts["detection_tp"] + counts["detection_fn"] == counts["visible_gt_frames"]
    assert counts["occlusion_events_with_preexisting_track"] == 0
    assert counts["same_identity_recovered_events"] == 0
    assert receipt["clips"]["flight_action_labels_created"] is False
    assert receipt["clips"]["training_medium_clip_fully_occluded_frames"] == 16


def test_sequence_input_helper_is_not_temporal_training_or_flight_evidence() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[SEQUENCE_ENTRY]
    path = ROOT / entry["evidence"]
    assert path.stat().st_size == entry["evidence_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["evidence_sha256"]
    receipt = _json(path)
    assert receipt["admission_parser"] == "scripts.train_onboard_model._trajectory_samples"
    assert receipt["sample_type"] == "scripts.train_onboard_model.BootstrapSample"
    assert receipt["properties"]["padding_labels"] is None
    assert receipt["properties"]["missing_actions_generated"] is False
    assert receipt["properties"]["unreviewed_opt_in_training_ready"] is False
    for key in (
        "current_single_step_mlp_changed", "temporal_model_training_performed",
        "flight_action_labels_created", "training_ready", "gazebo_capture",
        "autonomous_flight_verified", "candidate_promoted", "final_release_allowed",
    ):
        assert receipt[key] is entry[key] is False
    for reference in receipt["sources"]:
        assert _bound_artifact(reference) is not None


def test_far_increment_is_bound_without_policy_or_flight_promotion() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[FAR_INCREMENT_ENTRY]
    path = ROOT / entry["evidence"]
    assert path.stat().st_size == entry["evidence_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["evidence_sha256"]
    receipt = _json(path)
    for key in (
        "temporal_model_training_performed", "onboard_policy_reference_changed",
        "original_detector_changed", "training_ready", "gazebo_capture",
        "autonomous_flight_verified", "candidate_promoted", "physical_performance_claimed",
        "hil_real_evidence", "outdoor_gnss_evidence", "final_release_allowed",
    ):
        assert receipt[key] is entry[key] is False
    assert receipt["training"]["names"] == entry["class_names"] == {"0": "search_target"}
    assert receipt["training"]["epochs_completed"] == entry["epochs_completed"] == 30
    assert receipt["training"]["onnx"]["sha256"] == entry["candidate_onnx_sha256"]
    assert receipt["dataset"]["split_frames"] == entry["split_frames"]
    assert receipt["dataset"]["development_holdout_not_untouched_blind_benchmark"] is True
    assert receipt["clip_suite"]["flight_action_labels_created"] is False
    assert receipt["clip_suite"]["truth_into_runtime"] is False
    assert receipt["clip_suite"]["model_and_tracker_input_fields"] == ["image_bgr", "dt"]
    assert receipt["clip_suite"]["label_aliases"] == {}
    for source in receipt["sources"]:
        assert _bound_artifact(source) is not None
    for original in receipt["original_model_preservation"]:
        _bound_artifact(original)


def test_available_far_increment_training_and_regressions_match_raw_reports() -> None:
    entry = _json(ROOT / "docs/current-progress.json")[FAR_INCREMENT_ENTRY]
    receipt = _json(ROOT / entry["evidence"])
    _bound_artifact(receipt["dataset"])
    audit_path = _bound_artifact(receipt["audit"])
    if audit_path is not None:
        audit = _json(audit_path)
        assert audit["audit_passed"] is True
        assert audit["dataset"]["sha256"] == receipt["dataset"]["sha256"]
        assert audit["splits"]["train"]["visible_low_view_and_small_bbox_count"] == 73
    training = receipt["training"]
    for key in (
        "checkpoint", "onnx", "export_receipt", "invocation_binding",
        "closed_input_inventory", "results_csv", "log",
    ):
        _bound_artifact(training[key])
    report_path = _bound_artifact(training["report"])
    if report_path is not None:
        report = _json(report_path)
        assert report["onnx"]["sha256"] == training["onnx"]["sha256"]
        assert report["checkpoint"]["sha256"] == training["checkpoint"]["sha256"]
        assert report["dataset"]["sha256"] == receipt["dataset"]["sha256"]
        assert report["parent"]["sha256"] == training["parent_checkpoint_sha256"]
        assert report["parent_training_report"]["sha256"] == training["parent_report_sha256"]
    for key in ("paired_far_static", "prior_static_regression"):
        path = _bound_artifact(receipt[key])
        if path is not None:
            raw = _json(path)
            assert raw["models"]["candidate"]["sha256"] == training["onnx"]["sha256"]
            for name in ("parent", "candidate"):
                assert raw["models"][name]["overall"]["overall"] == receipt[key]["overall"][name]
                if key == "prior_static_regression":
                    partial = raw["models"][name]["by_measured_condition"]["partially_occluded"]
                    expected_counts = receipt[key]["partially_occluded_regression"][name]
                    for metric, expected in expected_counts.items():
                        assert partial["overall"][metric] == expected
    _bound_artifact(receipt["clip_suite"]["manifest"])
    for key in ("clip_suite", "prior_clip_regression"):
        reference = receipt[key]["candidate"]
        path = _bound_artifact(reference)
        if path is not None:
            raw = _json(path)
            assert raw["observed_counters"] == reference["observed_counters"]
            assert raw["model"]["sha256"] == training["onnx"]["sha256"]
            assert raw["truth_into_runtime"] is False
        counters = reference["observed_counters"]
        assert counters["same_identity_recovered_events"] == 0
        assert counters["id_switches"] == 2
