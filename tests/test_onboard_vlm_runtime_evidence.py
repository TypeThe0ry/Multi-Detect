from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    parse_onboard_vlm_runtime_response,
    serialize_onboard_vlm_messages,
)
from scripts.onboard_vlm_shadow_input import prepare_shadow_case

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-vlm-runtime-20261007-r1.json"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _report():
    receipt = _json(RECEIPT)
    return receipt, _json(ROOT / receipt["native_report"]["path"])


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def test_runtime_sources_baselines_and_native_report_are_actual_immutable_files():
    receipt, report = _report()
    for record in receipt["sources"] + receipt["baselines"] + [receipt["native_report"]]:
        path = ROOT / record["path"]
        assert path.stat().st_size == record["bytes"]
        assert _sha(path) == record["sha256"]
    for record in report["source_records"]:
        path = Path(record["path"])
        assert path.stat().st_size == record["bytes"]
        assert _sha(path) == record["sha256"]
    assert report["source_records_unchanged"] and report["worker_closed"]


def test_native_wiring_is_not_training_or_flight_acceptance():
    receipt, report = _report()
    for key in (
        "candidate_promoted",
        "final_release_allowed",
        "physical_performance_claimed",
        "autonomous_flight_verified",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "vlm_finetuning_performed",
        "flight_action_training_performed",
        "world_model_local_inference_measured",
        "world_model_finetuning_performed",
        "execution_authority",
    ):
        assert receipt[key] is False
    assert receipt["release_label"] == "0.3.0-pre.8"
    assert report["native_exit_code"] == 0
    assert report["native_generations_observed"] == report["strict_intents_accepted"] == 2
    assert receipt["software_validation"]["live_receipt_collector_implemented"] is False
    assert receipt["software_validation"]["source_vs_current_async_admission"] is False
    assert receipt["native_inference"]["production_latency_qualified"] is False
    assert receipt["runtime_readiness"]["action_training_ready_episodes"] == 0
    for case in report["cases"]:
        output = case["result"]["response"]["output"]
        assert output["response"] == "hold"
        assert output["task_action"] is None and output["flight_plan"] is None
        assert case["model_input"]["ledger"]["receipts"] == []
        assert all(frame["context"] is None for frame in case["model_input"]["frames"])


def test_current_progress_points_to_exact_runtime_receipt_without_broadening_claims():
    receipt, _report_value = _report()
    entry = _json(ROOT / "docs/current-progress.json")["onboard_vlm_runtime_development_20261007"]
    assert entry["evidence"] == str(RECEIPT.relative_to(ROOT)).replace("\\", "/")
    assert entry["evidence_bytes"] == RECEIPT.stat().st_size
    assert entry["evidence_sha256"] == _sha(RECEIPT)
    assert (
        entry["native_generations_completed"] == receipt["native_inference"]["native_generations"]
    )
    assert entry["source_vs_current_async_admission"] is False
    assert entry["live_receipt_collector_implemented"] is False
    assert entry["arbitrary_prompt_task_resolution_verified"] is False
    for name in (
        "candidate_promoted",
        "final_release_allowed",
        "physical_performance_claimed",
        "autonomous_flight_verified",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "vlm_finetuning_performed",
        "flight_action_training_performed",
        "execution_authority",
    ):
        assert entry[name] is False


@pytest.mark.parametrize("index", [0, 1])
def test_native_raw_binding_processor_tensors_and_generated_tokens_reproduce_on_cpu(index):
    import torch
    from transformers import AutoProcessor

    receipt, report = _report()
    row = report["cases"][index]
    manifest = _json(
        ROOT / "artifacts/evaluation/onboard-vlm-shadow-cases-20261007-r1/manifest.json"
    )
    source = next(case for case in manifest["cases"] if case["case_id"] == row["case_id"])
    request = OnboardVlmRuntimeRequest(
        prepare_shadow_case(source, repository_root=ROOT, deadline_s=30), max_total_pixels=401408
    )
    assert request.canonical_hash == row["request_sha256"]
    assert request.model_input.to_mapping() == row["model_input"]
    native = row["native_attempt"]
    parsed = parse_onboard_vlm_runtime_response(
        native["raw_output"],
        request,
        current_scope=request.model_input.scope,
        completed_at_s=native["completed_at_s"],
        inference_latency_ms=native["inference_latency_ms"],
    )
    assert parsed.to_mapping() == row["result"]["response"]
    snapshot = ROOT / (
        "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/"
        + receipt["native_inference"]["model_revision"]
    )
    processor = AutoProcessor.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False
    )
    metrics = native["backend_metrics"]
    assert (
        processor.tokenizer.decode(
            metrics["output_token_ids"],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        == native["raw_output"]
    )
    inputs = processor.apply_chat_template(
        serialize_onboard_vlm_messages(request),
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )
    assert int(inputs["input_ids"].shape[-1]) == metrics["input_tokens"]
    for name, tensor in inputs.items():
        if not isinstance(tensor, torch.Tensor):
            continue
        actual = {
            "shape": list(tensor.shape),
            "dtype": str(tensor.dtype),
            "sha256": hashlib.sha256(
                tensor.detach().contiguous().cpu().view(torch.uint8).numpy().tobytes()
            ).hexdigest(),
        }
        assert actual == metrics["input_tensors"][name]
