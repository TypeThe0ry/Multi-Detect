"""Keep real 3-D source admission separate from trained-model/flight claims."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tests.onboard_navigation_evidence_support import requires_local_artifacts

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-openfly-navigation-dataset-20261007-r2.json"
ENTRY = "onboard_openfly_navigation_dataset_20261007_r2"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bound(record):
    path = ROOT / record["path"]
    if not path.is_file() and record["path"].startswith("artifacts/"):
        pytest.skip("native datasets are intentionally absent from a clean clone")
    assert path.resolve().is_relative_to(ROOT.resolve())
    assert path.stat().st_size == record["bytes"]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    assert digest.hexdigest() == record["sha256"]
    return path


@pytest.mark.parametrize("group", ["sources", "implementation_snapshots", "frozen_defaults"])
@requires_local_artifacts(ROOT)
def test_original_sources_snapshots_and_defaults_remain_byte_bound(group):
    rows = read(RECEIPT)[group]
    assert rows and len(rows) == len({row["path"] for row in rows})
    for row in rows:
        bound(row)


@requires_local_artifacts(ROOT)
def test_dataset_is_complete_original_train_steps_without_false_holdout():
    receipt = read(RECEIPT)
    source = read(bound(receipt["source_manifest"]))
    report = read(bound(receipt["dataset_report"]))
    samples = [
        json.loads(line)
        for line in bound(receipt["dataset_samples"]).read_text(encoding="utf-8").splitlines()
    ]
    assert (
        source["selection"]["annotation_record_count"]
        == receipt["original_training_records"]
        == 100226
    )
    assert [ep["annotation_index"] for ep in source["episodes"]] == [0, 7, 8]
    assert [len(ep["images"]) for ep in source["episodes"]] == [19, 30, 37]
    assert report["admitted_sample_count"] == len(samples) == receipt["sample_count"] == 86
    assert report["partitions"]["group_count"] == receipt["scene_group_count"] == 1
    assert report["partition_counts"] == receipt["partition_counts"] == {"train": 86}
    assert {sample["partition"] for sample in samples} == {"train"}
    assert {sample["scene_id"] for sample in samples} == {"env_airsim_18"}
    assert report["aggregate_annotation_actions_used_as_gold"] is False
    assert all(sample["alignment"]["dt_s"] is None for sample in samples)
    assert all(sample["frames"][-1]["step_index"] == sample["step_index"] for sample in samples)
    for sample in samples:
        assert sample["source"]["original_split"] == "train"
        assert sample["source"]["review_receipt"] is None
        assert sample["pose"]["origin"] == "planned_pose"
        assert all(frame["step_index"] <= sample["step_index"] for frame in sample["frames"])
        assert all(frame["source_stamp"] is None for frame in sample["frames"])


@requires_local_artifacts(ROOT)
def test_independent_review_reopened_actual_source_and_actor_pixels():
    receipt = read(RECEIPT)
    summary = receipt["independent_review"]
    outputs = {Path(row["path"]).name: row for row in summary["outputs"]}
    for row in summary["outputs"]:
        bound(row)
    review = read(bound(outputs["independent-review.json"]))
    assert review["result"] == "pass"
    assert review["samples"]["sha256"] == receipt["dataset_samples"]["sha256"]
    assert review["dataset_report"]["sha256"] == receipt["dataset_report"]["sha256"]
    assert review["actual_actor_rgb_inputs_verified"] == summary["actor_rgb_inputs_verified"] == 169
    assert review["original_source_bindings_reopened"] == summary["source_files_reopened"] == 110
    assert review["aggregate_annotation_selected_frame_mismatch_count"] == 14
    assert review["max_position_error_m"] <= 1e-4
    assert review["max_yaw_error_rad"] <= 1e-6
    assert review["actor_future_pose_truth_label_or_source_identifier_leakage_detected"] is False
    assert review["missing_raw_primitive_categories"] == ["go up", "go down"]
    assert summary["native_exit_code"] == 0


@requires_local_artifacts(ROOT)
def test_training_approval_is_only_a_bounded_resubstitution_pilot():
    receipt = read(RECEIPT)
    report_path = bound(receipt["dataset_report"])
    approval = read(report_path.parent / "training-review.json")
    assert approval["dataset_report_sha256"] == receipt["dataset_report"]["sha256"]
    assert approval["dataset_samples_sha256"] == receipt["dataset_samples"]["sha256"]
    assert approval["allowed_training_use"] == "planned_navigation_train_only_resubstitution_pilot"
    assert approval["optimizer_allowed"] is True
    assert approval["independent_quality_evidence"] is False
    for key in (
        "training_ready",
        "independent_holdout_available",
        "optimizer_performed_by_dataset_stage",
        "dt_available",
        "aggregate_annotation_actions_used_as_gold",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "external_image_color_orientation_live_camera_parity_verified",
    ):
        assert receipt[key] is False
    assert receipt["source_planned_not_executed"] is True


@requires_local_artifacts(ROOT)
def test_joint_validation_counts_are_actual_xml_counts():
    validation = read(RECEIPT)["validation"]
    suites = list(ElementTree.parse(bound(validation["report"])).getroot().iter("testsuite"))
    assert sum(int(suite.get("tests", 0)) for suite in suites) == validation["tests"] == 142
    for attribute, key in (("failures", "failures"), ("errors", "errors"), ("skipped", "skips")):
        assert sum(int(suite.get(attribute, 0)) for suite in suites) == validation[key] == 0
    assert validation["native_exit_code"] == 0


@requires_local_artifacts(ROOT)
def test_previous_stage_receipt_and_document_bytes_are_preserved():
    receipt = read(RECEIPT)
    bound(receipt["previous_receipt"])
    snapshots = receipt["previous_stage_documentation_snapshots"]
    assert len(snapshots) == 7
    postseal = read(
        ROOT
        / "artifacts/evaluation/onboard-live-vlm-retained-replay-native-20261007-r1"
        / "postseal-validation.json"
    )
    original = {row["path"]: row for row in postseal["documentation_sources"]}
    for row in snapshots:
        bound(row)
        if row["original_path"] in original:
            old = original[row["original_path"]]
            assert (old["bytes"], old["sha256"]) == (row["bytes"], row["sha256"])


def test_repository_progress_points_to_exact_source_evidence():
    entry = read(ROOT / "docs/current-progress.json")[ENTRY]
    assert entry["evidence"] == RECEIPT.relative_to(ROOT).as_posix()
    assert entry["evidence_bytes"] == RECEIPT.stat().st_size
    assert entry["evidence_sha256"] == hashlib.sha256(RECEIPT.read_bytes()).hexdigest()
    assert entry["software_tests"] == 142
    assert entry["samples"] == 86 and entry["scenes"] == 1
    assert entry["independent_holdout_available"] is False
    assert entry["final_release_allowed"] is False
    assert (ROOT / "README.md").stat().st_size < 50000
    # README is the stable entry point; historical stage details live behind
    # current-progress (development-update-policy.md), not on every front page.
    assert "](docs/current-progress.md)" in (ROOT / "README.md").read_text(
        encoding="utf-8-sig"
    )
    assert (ROOT / "docs/onboard-openfly-navigation-pilot.md").is_file()
    for name in (
        "docs/current-progress.md",
        "docs/implementation-roadmap.md",
        "docs/product-goals-and-roadmap.md",
        "CHANGELOG.md",
    ):
        assert "onboard-openfly-navigation-pilot.md" in (ROOT / name).read_text(
            encoding="utf-8-sig"
        )
