"""Bind the native CPU86 report without upgrading it to live or model evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-navigation-shared-actor-inputs-20261007-r1.json"
FIELDS = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
FALSE_CLAIMS = (
    "cuda_initialized",
    "model_loaded",
    "model_inference_performed",
    "optimizer_performed",
    "forward_backward_performed",
    "training_completed",
    "independent_holdout_available",
    "quality_claim",
    "candidate_promoted",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "execution_authority",
    "transport_created",
    "mission_created",
    "actual_live_camera_parity_verified",
    "independently_captured_live_observations",
    "old_training_prefix_identity_claimed",
    "sim_real_parity_verified",
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def bound(row):
    path = ROOT / row["path"]
    assert not Path(row["path"]).is_absolute()
    assert path.resolve().is_relative_to(ROOT.resolve())
    if not path.is_file() and row["path"].startswith("artifacts/"):
        pytest.skip("native artifacts are intentionally absent from clean clones")
    assert path.stat().st_size == row["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    return path


def binding(value, suffix):
    rows = [row for row in value["bindings"] if row["path"].endswith(suffix)]
    assert len(rows) == 1
    return bound(rows[0])


def native_report(value):
    return read(binding(value, "/onboard-navigation-actor-inputs-20261007-r1/report.json"))


def test_official_cpu86_native_receipt_is_not_a_model_or_live_acceptance():
    value = read(RECEIPT)
    assert value["contract"] == "multidetect.onboard-navigation-shared-actor-inputs-evidence-v1"
    assert len(value["bindings"]) == len({row["path"] for row in value["bindings"]})
    for row in value["bindings"]:
        bound(row)
    report = native_report(value)
    assert report["contract"] == "multidetect.onboard-navigation-actor-prefix-verification-v1"
    assert value["native_execution"] == {
        "session_id": 27927,
        "tool_chunk": "c51040",
        "native_exit_code": 0,
        "mode": "official_local_processor_cpu86_no_model",
    }
    assert report["native_exit_code"] == 0
    assert report["official_processor_used"] is value["official_processor_used"] is True
    assert report["cpu_only"] is value["cpu_only"] is True
    assert report["retained_source_projection"] is value["retained_source_projection"] is True
    assert all(report[key] is value[key] is False for key in FALSE_CLAIMS)
    processor = report["processor"]
    assert processor["class"] == "Qwen3VLProcessor"
    assert processor["loading_scope"] == "official_pinned_local_only"
    assert processor["local_files_only"] is True and processor["trust_remote_code"] is False
    assert processor["transformers_version"] == "4.57.6"
    assert processor["torch_version"] == "2.6.0+cu124"
    assert (processor["min_pixels"], processor["max_pixels"], processor["max_tokens"]) == (
        1024,
        50176,
        4096,
    )
    before, after = report["source_bindings_before"], report["source_bindings_after"]
    assert before == after and report["source_bindings_unchanged"]
    assert (
        len(before) == len({row["path"] for row in before}) == value["source_binding_count"] == 129
    )
    assert report["source_validation_session_verified"] is True
    assert (ROOT / "artifacts/evaluation/onboard-navigation-actor-inputs-20261007-r1").parent == (
        ROOT / "artifacts/evaluation"
    )
    suites = ElementTree.parse(
        binding(value, "/onboard-navigation-actor-inputs-tests-20261007-r2.xml")
    )
    cases = list(suites.iter("testcase"))
    assert len(cases) == 97
    assert not list(suites.iter("failure")) and not list(suites.iter("error"))
    assert len(list(suites.iter("skipped"))) == value["unit_tests"]["skipped"] == 1
    assert value["unit_tests"]["passed"] == 96
    assert value["unit_tests"]["symlink_runtime_validation_passed"] is False
    progress = read(ROOT / "docs/current-progress.json")[
        "onboard_navigation_shared_actor_inputs_20261007_r1"
    ]
    assert progress["evidence_sha256"] == hashlib.sha256(RECEIPT.read_bytes()).hexdigest()
    assert progress["evidence_bytes"] == RECEIPT.stat().st_size
    assert progress["sample_count"] == 86 and progress["native_exit_code"] == 0
    assert progress["candidate_promoted"] is progress["final_release_allowed"] is False


def test_all_86_source_order_and_four_cpu_tensor_records_remain_equal():
    from multidetect.autonomy.onboard_navigation_actor import ACTOR_CONTRACT, ACTOR_SYSTEM_PROMPT

    value = read(RECEIPT)
    report = native_report(value)
    plan = read(binding(value, "/onboard-vlm-navigation-pilot-plan-20261007-r1.json"))
    samples = [
        json.loads(line) for line in bound(plan["dataset_samples"]).read_bytes().splitlines()
    ]
    rows = report["samples"]
    ids = [row["sample_id"] for row in rows]
    assert ids == plan["train_sample_ids"] == [row["sample_id"] for row in samples]
    assert len(ids) == len(set(ids)) == report["sample_count"] == value["sample_count"] == 86
    assert report["source_plan_sha256"] == hashlib.sha256(
        binding(value, "/onboard-vlm-navigation-pilot-plan-20261007-r1.json").read_bytes()
    ).hexdigest()
    assert report["actor_contract"] == value["actor_contract"] == ACTOR_CONTRACT
    assert report["system_prompt_sha256"] == hashlib.sha256(
        ACTOR_SYSTEM_PROMPT.encode("utf-8")
    ).hexdigest()
    for row, sample in zip(rows, samples, strict=True):
        assert sample["partition"] == sample["source"]["original_split"] == "train"
        assert row["source_prompt_sha256"] == sample["prompt"]["utf8_sha256"]
        assert re.fullmatch("[0-9a-f]{64}", row["actor_identity_sha256"])
        assert row["retained_history_valid"] and row["actor_identities_equal"]
        assert row["all_four_tensor_bytes_equal"]
        assert row["labels_pose_future_ids_entered_model_messages"] is False
        frames = row["source_frames"]
        assert len(frames) in (1, 2)
        assert frames[-1]["sequence"] == sample["step_index"]
        if len(frames) == 2:
            assert frames[0]["sequence"] == sample["step_index"] - 1
        assert len(frames) == len(sample["frames"])
        for frame, source_frame in zip(frames, sample["frames"], strict=True):
            assert frame["sequence"] == source_frame["step_index"]
            for field in ("path", "bytes", "sha256", "width", "height", "encoding"):
                assert frame[field] == source_frame[field]
            bound(frame)
        prefix = row["training_prefix"]
        assert prefix == row["observable_prefix"]
        assert set(prefix["tensors"]) == FIELDS
        tokens = prefix["prefix_tokens"]
        assert 1 <= tokens <= 4096
        for field, tensor in prefix["tensors"].items():
            assert re.fullmatch("[0-9a-f]{64}", tensor["sha256"])
            assert tensor["dtype"] == (
                "torch.float32" if field == "pixel_values" else "torch.int64"
            )
        assert prefix["tensors"]["input_ids"]["shape"] == [1, tokens]
        assert prefix["tensors"]["attention_mask"]["shape"] == [1, tokens]
        assert prefix["tensors"]["image_grid_thw"]["shape"] == [len(frames), 3]
        patch, temporal = prefix["patch_size"], prefix["temporal_patch_size"]
        grids = prefix["actual_image_grid_thw"]
        assert len(grids) == len(frames)
        assert all(t == 1 and h > 0 and w > 0 for t, h, w in grids)
        assert prefix["actual_image_pixels"] == [h * w * patch**2 for _, h, w in grids]
        assert all(1024 <= pixels <= 50176 for pixels in prefix["actual_image_pixels"])
        assert prefix["tensors"]["pixel_values"]["shape"] == [
            sum(t * h * w for t, h, w in grids),
            3 * temporal * patch**2,
        ]
    assert report["all_86_prefix_pairs_equal"] is True
    assert max(row["training_prefix"]["prefix_tokens"] for row in rows) == 453
    assert report["max_actual_prefix_tokens"] == value["max_actual_prefix_tokens"] == 453
    assert report["max_actual_image_pixels"] == value["max_actual_image_pixels"] == 46080
