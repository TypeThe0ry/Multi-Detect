from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_multimodal import parse_onboard_multimodal_intent
from scripts.onboard_vlm_shadow_input import prepare_shadow_case

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "onboard_vlm_native_shadow_20261007"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bound(reference: dict, *, required: bool = False) -> Path | None:
    path = ROOT / reference["path"]
    if not path.exists() and not required:
        return None
    assert path.is_file(), reference["path"]
    payload = path.read_bytes()
    assert len(payload) == reference["bytes"], reference["path"]
    assert hashlib.sha256(payload).hexdigest() == reference["sha256"], reference["path"]
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
    return entry, _json(path)


def test_shadow_receipt_is_bound_and_distinct_from_historical_research() -> None:
    entry, receipt = _receipt()
    assert receipt["contract"] == "multidetect.onboard-vlm-native-shadow-evidence-v1"
    assert receipt["parallel_agents"] == 3
    assert receipt["native_exit_code"] == 0
    assert entry["vlm_weights_downloaded"] is receipt["vlm_weights_downloaded"] is True
    assert entry["vlm_local_inference_measured"] is receipt["vlm_local_inference_measured"] is True
    research = _json(ROOT / "docs/current-progress.json")[
        "onboard_pretrained_multimodal_research_20261007"
    ]
    assert research["vlm_weights_downloaded"] is False  # Retained earlier phase, not rewritten.
    for reference in receipt["source_files"]:
        _bound(reference, required=True)


def test_public_model_revision_and_original_models_are_preserved() -> None:
    _, receipt = _receipt()
    model = receipt["model"]
    assert model["model_id"] == "Qwen/Qwen3-VL-2B-Instruct"
    assert model["revision"] == "89644892e4d85e24eaac8bacfd4f463576704203"
    assert model["license"] == "apache-2.0"
    assert model["gated"] is False
    assert model["weights"]["sha256"] == model["weights"]["official_lfs_sha256"]
    for reference in receipt["original_model_and_parity_preservation"]:
        _bound(reference, required=reference["path"].startswith("configs/"))


def test_real_rgb_cases_have_no_action_or_metric_gold() -> None:
    _, receipt = _receipt()
    path = _bound(receipt["cases_manifest"])
    if path is None:
        pytest.skip("Local ignored RGB cases are unavailable in this checkout")
    cases = _json(path)
    assert cases["case_count"] == 8
    assert cases["unique_rgb_count"] == 7
    assert cases["source_split"] == "train"
    assert cases["evaluation_metadata_model_visible"] is False
    for case in cases["cases"]:
        assert all(frame["split"] == "train" for frame in case["frames"])
        assert case["evaluation_metadata"]["gold_flight_action"] is None
        assert case["evaluation_metadata"]["gold_metric_position"] is None
        prepared = prepare_shadow_case(case, repository_root=ROOT)
        assert all(frame.context is None for frame in prepared.frames)
        assert prepared.scope.home_sha256 is None
        assert prepared.ledger.receipts == ()


def test_native_raw_outputs_reparse_without_repair_and_counts_recompute() -> None:
    entry, receipt = _receipt()
    report_path = _bound(receipt["native_report"])
    manifest_path = _bound(receipt["cases_manifest"])
    if report_path is None or manifest_path is None:
        pytest.skip("Local ignored probe artifacts are unavailable in this checkout")
    report = _json(report_path)
    assert report["native_exit_code"] == 0
    assert report["raw_output_repaired"] is False
    assert report["budget"]["max_new_tokens"] == 512
    assert report["budget"]["deadline_s"] == 30.0
    cases = {case["case_id"]: case for case in _json(manifest_path)["cases"]}
    accepted = 0
    completed = 0
    for result in report["case_results"]:
        case = cases[result["case_id"]]
        prepared = prepare_shadow_case(
            case, repository_root=ROOT, deadline_s=report["budget"]["deadline_s"]
        )
        assert result["input_sha256"] == prepared.canonical_hash
        assert result["evaluation_metadata_sent_to_model"] is False
        raw_path = report_path.parent / result["case_id"] / "raw-output.txt"
        if not raw_path.exists():
            assert result["strict_intent_accepted"] is False
            continue
        completed += 1
        payload = raw_path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == result["raw_output_sha256"]
        assert len(result["output_token_ids"]) == result["generated_tokens"]
        assert result["generated_tokens"] <= report["budget"]["max_new_tokens"]
        assert result["completed_at_s"] == pytest.approx(
            prepared.requested_at_s + result["inference_latency_ms"] / 1000
        )
        try:
            parsed = parse_onboard_multimodal_intent(
                payload,
                prepared,
                completed_at_s=result["completed_at_s"],
                inference_latency_ms=result["inference_latency_ms"],
                expected_scope=prepared.scope,
            )
        except (ValueError, TypeError):
            assert result["strict_intent_accepted"] is False
        else:
            accepted += 1
            assert result["strict_intent_accepted"] is True
            assert parsed.to_mapping() == result["parsed_intent"]
            assert parsed.to_mapping()["execution_authority"] is False
    assert completed == receipt["native_generations_completed"]
    assert accepted == receipt["strict_intents_accepted"] == entry["strict_intents_accepted"]
    assert report["strict_intents_accepted"] == accepted


def test_model_visible_text_cannot_reveal_case_or_source_names() -> None:
    _, receipt = _receipt()
    report_path = _bound(receipt["native_report"])
    manifest_path = _bound(receipt["cases_manifest"])
    if report_path is None or manifest_path is None:
        pytest.skip("Local ignored probe artifacts are unavailable in this checkout")
    for case in _json(manifest_path)["cases"]:
        text = (report_path.parent / case["case_id"] / "model-chat-template.txt").read_text(
            encoding="utf-8"
        )
        assert case["case_id"] not in text
        for frame in case["frames"]:
            assert frame["frame_id"] not in text
            assert frame["source_clip_id"] not in text
        for key in ("evaluation_metadata", "formal_input_metadata", "source_provenance"):
            assert key not in text


def test_shadow_success_does_not_promote_or_claim_training_flight_or_hardware() -> None:
    entry, receipt = _receipt()
    for key in (
        "vlm_finetuning_performed",
        "world_model_local_inference_measured",
        "world_model_finetuning_performed",
        "onboard_policy_reference_changed",
        "original_detector_changed",
        "mission_compiled",
        "transport_created",
        "hard_deadline_enforced",
        "cancellable_worker",
        "candidate_promoted",
        "autonomous_flight_verified",
        "physical_performance_claimed",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert receipt[key] is entry[key] is False
