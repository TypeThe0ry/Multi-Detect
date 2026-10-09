"""Keep native retained replay integrity separate from semantic/flight claims."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-live-vlm-retained-replay-20261007-r1.json"
ENTRY = "onboard_live_vlm_retained_replay_20261007_r1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bound(record):
    path = ROOT / record["path"]
    if not path.is_file() and record["path"].startswith("artifacts/"):
        pytest.skip("native artifacts are intentionally absent from a clean clone")
    assert path.stat().st_size == record["bytes"]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    assert digest.hexdigest() == record["sha256"]
    return path


@pytest.mark.parametrize("group", ["sources", "source_inputs", "native_outputs", "frozen_defaults"])
def test_current_sources_and_native_outputs_match_frozen_binding(group):
    records = read(RECEIPT)[group]
    assert len(records) == len({row["path"] for row in records}) == 4
    for record in records:
        bound(record)


def test_previous_receipts_and_exact_document_snapshots_are_not_rewritten():
    receipt = read(RECEIPT)
    previous = receipt["previous_receipts"]
    assert len(previous) == 2
    for record in previous:
        bound(record)
    postseal = read(ROOT / previous[1]["path"])
    snapshots = receipt["stage1_documentation_snapshots"]
    assert len(snapshots) == 6
    original = {row["path"]: row for row in postseal["documentation_sources"]}
    for snapshot in snapshots:
        bound(snapshot)
        old = original[snapshot["original_path"]]
        assert (snapshot["bytes"], snapshot["sha256"]) == (old["bytes"], old["sha256"])


def test_actual_native_attempt_is_one_generation_without_actuation():
    receipt = read(RECEIPT)
    summary = receipt["native"]
    native = read(bound(summary["report"]))
    assert native["native_exit_code"] == summary["native_exit_code"] == 0
    assert native["errors"] == summary["errors"] == []
    for key in ("worker_closed", "native_generation_observed", "strict_intent_accepted"):
        assert native[key] is summary[key] is True
    assert native["model_sha256"] == receipt["model"]["base_weights"]["sha256"]
    attempt = native["native_attempt"]
    metrics = attempt["backend_metrics"]
    assert attempt["inference_latency_ms"] == summary["inference_latency_ms"]
    assert native["startup_elapsed_s"] == summary["complete_worker_prewarm_s"]
    assert summary["inference_latency_ms"] == pytest.approx(9015)
    assert summary["complete_worker_prewarm_s"] == pytest.approx(32.032)
    assert summary["single_case_not_latency_distribution_or_real_time_control"] is True
    assert metrics["input_tokens"] == summary["input_tokens"] == 2646
    assert metrics["generated_tokens"] == summary["generated_tokens"] == 156
    assert metrics["actual_processor_total_pixels"] == 180224 <= 200704
    assert metrics["actual_image_grid_thw"] == [[1, 16, 22], [1, 16, 22]]
    output = native["result"]["response"]["output"]
    assert output["response"] == summary["response"] == "hold"
    assert output["task_action"] is output["flight_plan"] is None
    for key in ("mission_compiled", "transport_created", "execution_authority", "adapter_loaded"):
        assert native[key] is False


def test_independent_cpu_report_has_exact_tensor_and_raw_token_binding():
    receipt = read(RECEIPT)
    summary = receipt["independent_cpu_audit"]
    audit = read(bound(summary["report"]))
    native = read(bound(receipt["native"]["report"]))
    metrics = native["native_attempt"]["backend_metrics"]
    assert audit["native_exit_code"] == summary["native_exit_code"] == 0
    assert audit["audit_verified"] is summary["audit_verified"] is True
    assert len(audit["verified_records"]) == summary["verified_file_bindings"] == 31
    rebuilt = audit["processor_audit"]
    assert rebuilt["input_tensors"] == summary["input_tensors"] == metrics["input_tensors"]
    assert set(rebuilt["input_tensors"]) == {
        "input_ids",
        "attention_mask",
        "pixel_values",
        "image_grid_thw",
    }
    assert rebuilt["token_decode_equals_exact_raw"] is True
    assert rebuilt["processor_messages_sha256"] == metrics["actual_processor_messages_sha256"]
    assert rebuilt["processor_images_kwargs_reconstructed_before_call"] == {
        "min_pixels": 1024,
        "max_pixels": 100352,
    }
    assert rebuilt["processor_images_kwargs_observed_after_call"] == {}
    assert metrics["processor_images_kwargs"] == {}
    assert summary["reconstruction_is_not_a_native_pre_call_receipt"] is True
    for key in ("model_weights_loaded", "gpu_used", "generation_repeated"):
        assert audit[key] is summary[key] is False


def test_preserved_time_and_failed_sortie_do_not_become_current_flight():
    receipt = read(RECEIPT)
    scope = receipt["scope"]
    assert scope["original_capture_timestamps_s"] == [351854.265, 351854.484]
    assert scope["source_sensor_timestamps_ns"] == [96492000000, 96528000000]
    assert scope["clock_epoch"].startswith("retained-host-monotonic-replay:")
    assert scope["sensor_to_host_clock_conversion_claimed"] is scope["live_clock_verified"] is False
    assert scope["original_source_run"]["status"] == "failed"
    assert scope["original_source_run"]["complete_autonomous_task_success_claimed"] is False
    assert scope["truth_or_gold_in_model_input"] is False


def test_structure_pass_does_not_endorse_unsupported_semantics_or_self_confidence():
    receipt = read(RECEIPT)
    review = receipt["semantic_review"]
    assert review["semantic_quality_passed"] is review["semantic_accuracy_measured"] is False
    assert review["task_target_definition_or_gold_available"] is False
    assert review["reason_not_repaired"] is True
    assert review["confidence_is_model_self_report_not_calibrated_accuracy"] == 0.95
    native = read(bound(receipt["native"]["report"]))
    for unsupported in review["unsupported_reason_claims"]:
        assert unsupported in native["result"]["response"]["output"]["reason"]


def test_public_navigation_research_is_not_an_admitted_or_executed_action_dataset():
    research = read(RECEIPT)["research"]
    assert len(research["documents"]) == 2
    for record in research["documents"]:
        bound(record)
    for key in (
        "large_models_or_datasets_downloaded_this_research",
        "public_foreign_lora_merged_into_qwen",
        "action_dataset_imported_or_approved",
    ):
        assert research[key] is False
    assert research["external_planned_not_px4_executed"] is True
    assert research["unknown_dt_not_defaulted"] is True
    assert research["stop_not_land_or_task_completed"] is True


def test_software_regressions_are_not_counted_as_task_accuracy():
    joint = read(RECEIPT)["validation"]["joint"]
    suite = ElementTree.parse(bound(joint["report"])).getroot().find("testsuite")
    assert suite is not None
    assert joint["native_exit_code"] == 0
    for key in ("tests", "failures", "errors", "skipped"):
        assert int(suite.attrib[key]) == joint[key]
    assert joint["tests"] == joint["passed"] == 51


def test_progress_index_keeps_both_stage_receipts_and_false_release_claims():
    receipt = read(RECEIPT)
    progress = read(ROOT / "docs/current-progress.json")
    entry = progress[ENTRY]
    assert (
        bound(
            {
                "path": entry["evidence"],
                "bytes": entry["evidence_bytes"],
                "sha256": entry["evidence_sha256"],
            }
        )
        == RECEIPT
    )
    assert entry["native_exit_code"] == entry["cpu_audit_native_exit_code"] == 0
    assert entry["semantic_quality_passed"] is entry["candidate_promoted"] is False
    assert entry["final_release_allowed"] is False
    assert (
        progress["onboard_gazebo_live_shadow_development_20261007_r1"][
            "gpu_generation_performed_this_stage"
        ]
        is False
    )
    assert receipt["claims"]["gpu_generation_performed"] is True
    assert receipt["claims"]["cpu_integrity_verified"] is True
    for key, value in receipt["claims"].items():
        if key not in {"gpu_generation_performed", "cpu_integrity_verified"}:
            assert value is False, key
