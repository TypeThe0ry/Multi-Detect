"""Source receipts keep software integration separate from flight evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-gazebo-live-shadow-development-20261007-r1.json"
ENTRY = "onboard_gazebo_live_shadow_development_20261007_r1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bound(row):
    path = ROOT / row["path"]
    if not path.is_file() and row["path"].startswith("artifacts/"):
        pytest.skip("native artifact is intentionally not committed in a clean clone")
    assert path.stat().st_size == row["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    return path


@pytest.mark.parametrize("group", ["sources", "frozen_dependencies_and_defaults"])
def test_current_new_sources_and_frozen_dependencies_match_receipt(group):
    records = read(RECEIPT)[group]
    assert records and len({row["path"] for row in records}) == len(records)
    for record in records:
        bound(record)


def test_recorded_cpu_regression_and_preflight_are_not_runtime_flight():
    receipt = read(RECEIPT)
    joint = receipt["validation"]["joint"]
    suite = ElementTree.parse(bound(joint["report"])).getroot().find("testsuite")
    assert suite is not None
    assert joint["native_exit_code"] == 0
    for key in ("tests", "failures", "errors"):
        assert int(suite.attrib[key]) == joint[key]
    assert int(suite.attrib["skipped"]) == joint["skipped"] == 1
    assert joint["tests"] == joint["passed"] + joint["skipped"] == 444
    preflight = receipt["validation"]["real_entry_preflight"]
    report = read(bound(preflight["report"]))
    assert report["status"] == preflight["status"] == "planned_not_executed"
    assert report["native_exit_code"] == preflight["native_exit_code"] == 0
    assert report["source_records_unchanged"] is True
    assert report["execution_requested"] is report["model_actuated_flight"] is False
    assert report["shadow"] is None
    capture = read(bound(preflight["capture_plan"]))
    assert capture["runtime_gate_passed"] is None


def test_latest_placement_no_submission_is_retained_not_new_capture():
    runtime = read(RECEIPT)["runtime"]
    assert runtime["latest_placement"] == "no_eligible_node"
    assert runtime["new_capture_submitted"] is False
    assert runtime["new_capture_native_exit_code"] is None
    assert runtime["new_rgb_depth_frames_verified"] == 0
    for key in (
        "diagnostic_attempt",
        "fleet_plan_result",
        "fleet_info_result",
        "previous_runtime_receipt",
        "previous_increment_receipt",
    ):
        bound(runtime[key])


def test_input_recording_and_budget_claims_keep_missing_facts_and_native_ipc_gap():
    receipt = read(RECEIPT)
    implementation = receipt["implementation"]
    assert implementation["actual_camera_callback_before_legacy_detection_filter"] is True
    assert implementation["source_wire_and_retained_recording_exactly_joined"] is True
    assert implementation["truth_or_training_labels_in_model_input"] is False
    assert implementation["model_owns_transport"] is False
    assert implementation["baseline_not_vlm_prompt_execution"] is True
    assert implementation["baseline_raw_payload_limit_bytes"] == 1073741824
    assert "error_type" in implementation["persistent_worker_error_detail_limitation"]
    budget = receipt["processor_budget"]
    assert (
        budget["review_reproductions"]["four_frames_without_new_guard"]
        > budget["serializer_total_pixels"]
    )
    assert budget["actual_image_grid_total_validated_before_generation"] is True
    assert budget["gpu_generation_performed"] is False
    assert all(value is False for value in receipt["claims"].values())
    assert receipt["model"]["model_weights_loaded_this_stage"] is False
    assert receipt["model"]["adapter_loaded"] is False


def test_durable_progress_and_user_docs_match_current_software_receipt():
    progress = read(ROOT / "docs/current-progress.json")
    entry = progress[ENTRY]
    bound(
        {
            "path": entry["evidence"],
            "bytes": entry["evidence_bytes"],
            "sha256": entry["evidence_sha256"],
        }
    )
    assert entry["joint_tests"] == 444
    assert entry["joint_native_exit_code"] == entry["preflight_native_exit_code"] == 0
    assert entry["live_capture_verified"] is entry["autonomous_flight_verified"] is False
    assert entry["candidate_promoted"] is entry["final_release_allowed"] is False
    assert progress["product_version"]["release_label"] == "0.3.0-pre.8"
    readme = (ROOT / "README.md").read_text(encoding="utf-8-sig")
    assert "docs/onboard-gazebo-live-shadow.md" in readme
    assert (ROOT / "README.md").stat().st_size < 50_000
    assert "444" in (ROOT / "docs/onboard-gazebo-live-shadow.md").read_text(encoding="utf-8")
