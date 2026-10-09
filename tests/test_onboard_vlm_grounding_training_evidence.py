"""Durable native LoRA evidence, not schema compliance or autonomous flight proof."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts import build_onboard_vlm_grounding_dataset as builder
from scripts import train_onboard_vlm_grounding as trainer

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-vlm-grounding-training-20261007-r1.json"
ENTRY = "onboard_vlm_grounding_training_20261007"
CONTRACT = "multidetect.onboard-vlm-grounding-training-evidence-v1"
FALSE_FLAGS = (
    "candidate_promoted",
    "final_release_allowed",
    "flight_action_training_performed",
    "physical_performance_claimed",
    "autonomous_flight_verified",
    "hil_real_evidence",
    "jetson_evidence",
    "outdoor_gnss_evidence",
    "onboard_policy_reference_changed",
    "original_detector_changed",
    "mission_compiled",
    "transport_created",
    "world_model_local_inference_measured",
    "world_model_finetuning_performed",
)
BASELINES = {
    "configs/training/onboard-policy-current-reference-v1.json": (
        "dc6bf892db1af3e6694bed5bf7e684e341141bd41627eaccd530bdb70e6808ed"
    ),
    "artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx": (
        "727d0e71834311d8aa51dfe072e08ec249062b1945d9501394f5d89ad7a16ee6"
    ),
    "artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.pt": (
        "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"
    ),
}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    value = trainer.strict_factual_json(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _bound(reference: dict, *, root: Path = ROOT) -> Path:
    assert {"path", "bytes", "sha256"} <= set(reference)
    assert isinstance(reference["path"], str) and reference["path"]
    assert type(reference["bytes"]) is int and reference["bytes"] >= 0
    trainer.probe.require_sha256(reference["sha256"])
    path = (root / reference["path"]).resolve()
    relative = path.relative_to(root.resolve())
    if not path.is_file() and relative.parts[0] == "artifacts":
        pytest.skip(f"Ignored development artifact absent: {relative.as_posix()}")
    assert path.is_file(), f"Required repository evidence/source/config missing: {relative}"
    assert path.stat().st_size == reference["bytes"]
    assert _sha(path) == reference["sha256"]
    return path


def _finite(value, *, nonnegative=True):
    assert type(value) in {int, float} and math.isfinite(value)
    if nonnegative:
        assert value >= 0


def _raw_visibility_diagnostic(rows: list[dict]) -> dict:
    """Offline field-only diagnosis; never substitute for strict intent acceptance."""
    counts = {key: 0 for key in ("tp", "tn", "fp", "fn", "invalid")}
    correctness = {}
    parsed_by_id = {}
    correct_reference_ids = missing_unknown_fields = nonunknown_navigation = 0
    for row in rows:
        try:
            parsed = trainer.strict_factual_json(row["raw_output"].encode("utf-8"))
        except (ValueError, TypeError, KeyError):
            parsed = None
        parsed = parsed if isinstance(parsed, dict) else {}
        parsed_by_id[row["sample_id"]] = parsed
        prediction = parsed.get("current_target_visibility")
        expected = row["expected_target"]["current_target_visibility"]
        legal = isinstance(prediction, str) and prediction in {"visible", "not_confirmed"}
        correctness[row["sample_id"]] = legal and prediction == expected
        if not legal:
            counts["invalid"] += 1
            counts["fn" if expected == "visible" else "fp"] += 1
        elif prediction == "visible":
            counts["tp" if expected == "visible" else "fp"] += 1
        else:
            counts["fn" if expected == "visible" else "tn"] += 1
        correct_reference_ids += (
            parsed.get("target_reference_id") == row["expected_target"]["target_reference_id"]
        )
        missing_unknown_fields += any(
            key not in parsed for key in ("gps", "ned", "aircraft_motion")
        )
        nonunknown_navigation += any(
            key in parsed and parsed[key] != "unknown" for key in ("gps", "ned", "aircraft_motion")
        )
    sensitivity = counts["tp"] / (counts["tp"] + counts["fn"])
    specificity = counts["tn"] / (counts["tn"] + counts["fp"])
    pairs = consistent = 0
    for positive in rows:
        if positive["expected_target"]["current_target_visibility"] != "visible":
            continue
        for negative in rows:
            if negative["expected_target"]["current_target_visibility"] != "not_confirmed":
                continue
            if any(
                positive[key] != negative[key]
                for key in ("split", "source_clip_id", "reference_rgb_sha256", "prompt_sha256")
            ) or (
                positive["expected_target"]["target_reference_id"]
                != negative["expected_target"]["target_reference_id"]
            ):
                continue
            pairs += 1
            consistent += (
                parsed_by_id[positive["sample_id"]].get("current_target_visibility") == "visible"
                and parsed_by_id[negative["sample_id"]].get("current_target_visibility")
                == "not_confirmed"
            )
    return {
        "confusion": {
            **counts,
            "sensitivity": sensitivity,
            "specificity": specificity,
            "balanced_accuracy": (sensitivity + specificity) / 2,
        },
        "correctness": correctness,
        "correct_reference_ids": correct_reference_ids,
        "missing_unknown_fields": missing_unknown_fields,
        "nonunknown_navigation": nonunknown_navigation,
        "pair_count": pairs,
        "consistent_pair_count": consistent,
    }


@pytest.fixture(scope="module")
def evidence():
    # Missing durable receipt is a failure, never a success-producing fallback.
    assert RECEIPT.is_file(), "Final native training receipt has not been published"
    document = _json(RECEIPT)
    assert document["contract"] == CONTRACT
    return document


@pytest.fixture(scope="module")
def dataset(evidence):
    manifest = _bound(evidence["dataset_manifest"])
    review = _bound(evidence["review_receipt"])
    document = _json(manifest)
    source = _bound(document["source_manifest"])
    original = _json(source)
    for frame in original["frames"]:
        for name, relative in frame.items():
            if name.endswith("_path") and name != "world_path":
                path = (source.parent / relative).resolve()
                path.relative_to(source.parent.resolve())
                if not path.is_file():
                    pytest.skip(f"Ignored original rendered asset absent: {path.name}")
    for sample in document["samples"]:
        for image in sample["model_input"]["images"]:
            _bound({**image, "path": str(manifest.parent / image["path"])})
    return builder.validate_grounding_dataset(
        manifest,
        evidence["dataset_manifest"]["sha256"],
        review_receipt=review,
        require_review=True,
    )


@pytest.fixture(scope="module")
def native(evidence):
    return _json(_bound(evidence["native_report"]))


def test_durable_progress_byte_identity_and_truthful_nonflight_boundary(evidence):
    entry = _json(ROOT / "docs/current-progress.json")[ENTRY]
    assert (
        _bound(
            {
                "path": entry["evidence"],
                "bytes": entry["evidence_bytes"],
                "sha256": entry["evidence_sha256"],
            }
        )
        == RECEIPT.resolve()
    )
    assert entry["native_exit_code"] == evidence["native_exit_code"] == 1
    assert entry["optimizer_steps_completed"] == evidence["optimizer_steps_completed"] == 26
    assert entry["vlm_finetuning_performed"] is evidence["vlm_finetuning_performed"] is True
    for field in FALSE_FLAGS:
        assert evidence[field] is entry[field] is False
    assert evidence["quality"]["quality_passed"] is False


def test_required_source_config_baseline_and_report_bindings(evidence):
    paths = set()
    for reference in evidence["source_files"]:
        paths.add(_bound(reference).relative_to(ROOT).as_posix())
    assert {
        "scripts/build_onboard_vlm_grounding_dataset.py",
        "scripts/train_onboard_vlm_grounding.py",
        "scripts/probe_onboard_vlm.py",
        "tests/test_onboard_vlm_grounding_training_evidence.py",
    } <= paths
    preservation = {reference["path"]: reference for reference in evidence["baseline_preservation"]}
    assert set(BASELINES) <= set(preservation)
    # Check required source/config bytes before optionally absent ignored models.
    _bound(evidence["training_plan"])
    for reference in preservation.values():
        if not reference["path"].startswith("artifacts/"):
            _bound(reference)
    for path, digest in BASELINES.items():
        assert preservation[path]["sha256"] == digest
    for reference in preservation.values():
        _bound(reference)
    for key in (
        "dataset_manifest",
        "review_receipt",
        "preflight_report",
        "native_report",
        "training_plan",
    ):
        _bound(evidence[key])


def test_actual_source_facts_original_splits_crops_and_excluded_ambiguity(dataset):
    assert dataset["sample_count"] == 110
    assert dataset["split_samples"] == {"train": 26, "val": 41, "test": 43}
    assert dataset["split_clip_counts"] == {"train": 2, "val": 3, "test": 3}
    assert dataset["split_labels"] == {
        "train": {"visible": 18, "not_confirmed": 8},
        "val": {"visible": 28, "not_confirmed": 13},
        "test": {"visible": 30, "not_confirmed": 13},
    }
    assert dataset["ambiguous_excluded_count"] == 10 and dataset["skipped_clip_count"] == 1
    assert dataset["training_ready"] is False
    assert dataset["label_policy"] == builder.POLICY
    manifest = ROOT / dataset["output_directory"] / "manifest.pending-review.json"
    owners = {}
    for sample in dataset["samples"]:
        provenance = sample["provenance"]
        clip = provenance["source_clip_id"]
        assert owners.setdefault(clip, sample["split"]) == sample["split"]
        assert provenance["original_split"] == sample["split"]
        assert provenance["reference"]["clip_id"] == provenance["current"]["clip_id"] == clip
        assert provenance["reference"]["captured_at_s"] < provenance["current"]["captured_at_s"]
        assert provenance["semantic_identity_confirmed"] is False
        assert provenance["amodal_truth_model_visible"] is False
        assert sample["model_input"]["context"] == builder.UNKNOWN_CONTEXT
        visible = provenance["current"]["visible_target_pixels"]
        fraction = provenance["current"]["visibility_measurement"]["visible_fraction"]
        label = sample["assistant_target"]["current_target_visibility"]
        assert (visible >= 128 and fraction >= 0.10) if label == "visible" else visible == 0
        for field in ("gps", "ned", "aircraft_motion"):
            assert sample["assistant_target"][field] == "unknown"
        supplied = builder.model_sample_input(sample, manifest.parent)
        assert len(supplied["images"]) == 2
        assert all(
            set(image) == {"image_id", "role", "image_bytes"} for image in supplied["images"]
        )


def test_fixed_plan_preflight_did_backward_not_optimizer(evidence, native):
    plan_path = _bound(evidence["training_plan"])
    plan = trainer.load_training_plan(plan_path, evidence["training_plan"]["sha256"])
    assert native["budget"] == asdict(trainer.budget_from_plan(plan))
    assert native["budget"]["epochs"] == 1 and native["budget"]["max_optimizer_steps"] == 200
    assert native["training_plan_sha256"] == evidence["training_plan"]["sha256"]
    preflight = _json(_bound(evidence["preflight_report"]))
    assert preflight["mode"] == "preflight" and preflight["native_exit_code"] == 0
    assert (
        preflight["optimizer_steps_completed"] == 0 and preflight["vlm_training_performed"] is False
    )
    assert preflight["base_parameter_sha256_before"] == preflight["base_parameter_sha256_after"]
    assert (
        preflight["adapter_parameter_sha256_before"] == preflight["adapter_parameter_sha256_after"]
    )
    assert preflight["base_vision_frozen"] is True
    _finite(preflight["backward_preflight"]["loss"])
    assert preflight["backward_preflight"]["gradient_absolute_sum"] > 0


def test_native_optimizer_used_exact_train_rows_and_finite_gradients(native, dataset, evidence):
    assert native["contract"] == trainer.CONTRACT and native["mode"] == "train"
    assert native["native_exit_code"] == 1 and native["vlm_training_performed"] is True
    assert native["failure_type"] == "ValueError"
    assert native["failure"] == (
        "Actual offline loaded adapter tensor bytes differ from trained adapter"
    )
    assert native["dataset_sha256"] == evidence["dataset_manifest"]["sha256"]
    assert native["review_receipt_sha256"] == evidence["review_receipt"]["sha256"]
    assert native["development_training_authorized"] is True
    assert native["base_vision_frozen"] is native["response_only_loss"] is True
    assert native["checkpoint_selection_performed"] is False
    assert native.get("source_bytes_reverified") is not True
    assert native["parent_evaluation_before_optimizer"] is True
    for flag in (
        "flight_action_training_performed",
        "transport_created",
        "execution_authority",
        "candidate_promoted",
        "final_release_allowed",
        "hard_deadline_enforced",
    ):
        assert native[flag] is False
    train_ids = {row["sample_id"] for row in dataset["samples"] if row["split"] == "train"}
    rows = native["optimization_rows"]
    assert native["optimizer_steps_completed"] == len(rows) == len(train_ids) == 26
    assert {row["sample_id"] for row in rows} == train_ids
    assert len({row["sample_id"] for row in rows}) == len(rows)
    assert [row["step"] for row in rows] == list(range(1, 27))
    assert all(row["split"] == "train" and row["epoch"] == 1 for row in rows)
    for row in rows:
        _finite(row["loss"])
        _finite(row["gradient_norm_before_clip"])
        _finite(row["elapsed_s"])
    assert any(row["gradient_norm_before_clip"] > 0 for row in rows)
    assert native["optimizer_steps_completed"] <= native["budget"]["max_optimizer_steps"]
    log = _bound(
        next(
            record
            for record in native["output_artifact_files"]
            if Path(record["path"]).name == "training-log.jsonl"
        )
    )
    assert [trainer.strict_factual_json(line) for line in log.read_bytes().splitlines()] == rows


def test_real_optimizer_change_is_not_saved_reload_verification_success(native):
    assert native["base_parameter_bytes_unchanged"] is True
    assert native["base_parameter_sha256_before"] == native["base_parameter_sha256_after"]
    assert native["adapter_parameter_sha256_before"] != native["adapter_parameter_sha256_after"]
    for record in native["adapter_files"]:
        _bound(record)
    # The reload raised before these assignments. Do not convert optimizer success
    # or a later standalone diagnosis into success for this immutable native run.
    assert "saved_adapter_reload" not in native
    assert "base_parameter_sha256_after_reload" not in native


def test_all_native_output_artifact_bytes_and_individual_raw_rows_reopen(native):
    references = native["output_artifact_files"]
    assert len(references) == 175
    paths = [_bound(reference) for reference in references]
    assert len(set(paths)) == len(paths)
    expected = {
        f"{kind}-rows": {row["sample_id"]: row for row in native[f"{kind}_evaluation"]["samples"]}
        for kind in ("parent", "candidate")
    }
    seen = {kind: set() for kind in expected}
    seen_evaluations = set()
    for path in paths:
        if path.parent.name in expected:
            row = _json(path)
            assert path.stem == row["sample_id"]
            assert row == expected[path.parent.name][row["sample_id"]]
            assert row["sample_id"] not in seen[path.parent.name]
            seen[path.parent.name].add(row["sample_id"])
        elif path.name in {"parent-evaluation.json", "candidate-evaluation.json"}:
            kind = path.name.removesuffix("-evaluation.json")
            assert _json(path) == native[f"{kind}_evaluation"]
            seen_evaluations.add(kind)
    assert seen_evaluations == {"parent", "candidate"}
    assert all(len(identifiers) == 84 for identifiers in seen.values())
    assert seen["parent-rows"] == seen["candidate-rows"]


def test_same_holdout_input_ids_encoded_rgb_and_strict_raw_replay(native, dataset, evidence):
    by_id = {sample["sample_id"]: sample for sample in dataset["samples"]}
    assert set(native["encoded_samples"]) == set(by_id)
    holdout_ids = {identifier for identifier, sample in by_id.items() if sample["split"] != "train"}
    parents = native["parent_evaluation"]
    candidates = native["candidate_evaluation"]
    assert parents["adapter_disabled"] is True and candidates["adapter_disabled"] is False
    assert parents["count"] == candidates["count"] == len(holdout_ids) == 84
    assert [row["sample_id"] for row in parents["samples"]] == [
        row["sample_id"] for row in candidates["samples"]
    ]
    manifest_root = ROOT / dataset["output_directory"]
    budget = trainer.TrainingBudget(**native["budget"])
    for group in (parents, candidates):
        assert {row["sample_id"] for row in group["samples"]} == holdout_ids
        assert group["checkpoint_selection_performed"] is False
        for row in group["samples"]:
            sample = by_id[row["sample_id"]]
            assert row["split"] == sample["split"] and row["split"] in {"val", "test"}
            assert row["model_input_sha256"] == sample["model_input_sha256"]
            assert row["expected_target"] == sample["assistant_target"]
            assert row["native_generation_completed"] is True
            assert row["generated_tokens"] == len(row["output_token_ids"])
            assert all(type(token) is int and token >= 0 for token in row["output_token_ids"])
            parsed, accepted = None, False
            try:
                if row["token_cap_reached"] or row["elapsed_s"] > 30:
                    raise ValueError("Outside frozen completion budget")
                parsed = trainer.strict_factual_json(row["raw_output"].encode("utf-8"))
                trainer.validate_target(parsed, sample["model_input"]["target_reference_id"])
                accepted = True
            except (ValueError, TypeError, KeyError):
                pass
            assert row["parsed_output"] == parsed
            assert row["strict_fact_reply_accepted"] is accepted
            assert row["exact_factual_match"] is (accepted and parsed == sample["assistant_target"])
        assert group["metrics"] == trainer.grounding_metrics(group["samples"])
        assert group["strict_reply_count"] == sum(
            row["strict_fact_reply_accepted"] for row in group["samples"]
        )
        assert group["exact_match_count"] == sum(
            row["exact_factual_match"] for row in group["samples"]
        )
    for identifier, encoded in native["encoded_samples"].items():
        sample = by_id[identifier]
        _messages, preprocessing = trainer.image_messages(sample, manifest_root, budget)
        assert encoded["preprocessing"] == preprocessing
        assert 0 < encoded["supervised_tokens"] <= encoded["response_tokens"]
        assert encoded["prefix_tokens"] + encoded["response_tokens"] == encoded["total_tokens"]
        assert encoded["total_tokens"] <= budget.max_input_tokens
        assert len(encoded["actual_image_grid_thw"]) == len(encoded["actual_image_pixels"]) == 2
        assert all(
            0 < pixels <= budget.max_pixels_per_image for pixels in encoded["actual_image_pixels"]
        )
    assert {
        key: evidence["quality"][key]
        for key in ("parent", "candidate", "paired_sample_count", "quality_passed")
    } == {
        "parent": parents["metrics"],
        "candidate": candidates["metrics"],
        "paired_sample_count": 84,
        "quality_passed": False,
    }
    for prefix, group in (("parent", parents), ("candidate", candidates)):
        assert evidence["quality"][f"{prefix}_strict_reply_count"] == group["strict_reply_count"]
        assert evidence["quality"][f"{prefix}_exact_match_count"] == group["exact_match_count"]
    assert candidates["metrics"]["unknown_facts_included_in_visibility_accuracy"] is False


def test_original_official_tokenizer_reproduces_saved_raw_without_repair(native):
    tokenizers = pytest.importorskip("tokenizers", reason="CPU raw-token decoder is optional")
    tokenizer_record = next(
        record
        for record in native["model_provenance"]["files"]
        if Path(record["path"]).name == "tokenizer.json"
    )
    tokenizer_path = Path(tokenizer_record["path"])
    if not tokenizer_path.is_file():
        pytest.skip("Ignored pinned official tokenizer cache absent")
    assert tokenizer_path.stat().st_size == tokenizer_record["bytes"]
    assert _sha(tokenizer_path) == tokenizer_record["sha256"]
    decoder = tokenizers.Tokenizer.from_file(str(tokenizer_path))
    for group in ("parent_evaluation", "candidate_evaluation"):
        for row in native[group]["samples"]:
            assert (
                decoder.decode(row["output_token_ids"], skip_special_tokens=True)
                == row["raw_output"]
            )


def test_raw_field_diagnostics_are_recomputed_separately_not_promoted(native, evidence):
    parent = _raw_visibility_diagnostic(native["parent_evaluation"]["samples"])
    candidate = _raw_visibility_diagnostic(native["candidate_evaluation"]["samples"])
    diagnostic = evidence["offline_semantic_diagnostics"]
    assert diagnostic["acceptance_or_promotion_evidence"] is False
    assert diagnostic["diagnostic_excludes_full_contract_ID_and_unknown_field_validation"] is True
    assert diagnostic["parent_visibility_field_only"] == parent["confusion"]
    assert diagnostic["candidate_visibility_field_only"] == candidate["confusion"]
    assert set(parent["correctness"]) == set(candidate["correctness"])
    changes = {
        key: 0
        for key in (
            "both_correct",
            "both_wrong",
            "corrected_by_candidate",
            "regressed_by_candidate",
        )
    }
    for identifier, was_correct in parent["correctness"].items():
        is_correct = candidate["correctness"][identifier]
        key = (
            "both_correct"
            if was_correct and is_correct
            else "both_wrong"
            if not was_correct and not is_correct
            else "corrected_by_candidate"
            if is_correct
            else "regressed_by_candidate"
        )
        changes[key] += 1
    assert diagnostic["paired_change_counts"] == changes
    assert sum(changes.values()) == 84
    assert diagnostic["parent_correct_reference_id_count"] == parent["correct_reference_ids"]
    assert diagnostic["candidate_correct_reference_id_count"] == candidate["correct_reference_ids"]
    assert (
        diagnostic["candidate_wrong_reference_id_count"] == 84 - candidate["correct_reference_ids"]
    )
    assert diagnostic["parent_missing_unknown_fields_count"] == parent["missing_unknown_fields"]
    assert diagnostic["parent_navigation_hallucination_count"] == parent["nonunknown_navigation"]
    assert (
        diagnostic["candidate_explicit_nonunknown_navigation_fact_count"]
        == candidate["nonunknown_navigation"]
    )
    assert parent["pair_count"] == candidate["pair_count"] == 250
    assert diagnostic["raw_image_pair_consistent_count"] == {
        "parent": parent["consistent_pair_count"],
        "candidate": candidate["consistent_pair_count"],
        "total": parent["pair_count"],
        "independent_observations": False,
    }
    # A field-only diagnostic gain does not override ID errors, strict failures,
    # negative-frame regressions, failed native reload, or promotion/readiness.
    gain_pp = (
        candidate["confusion"]["balanced_accuracy"] - parent["confusion"]["balanced_accuracy"]
    ) * 100
    assert round(gain_pp, 3) == 6.565
    assert candidate["confusion"]["specificity"] < parent["confusion"]["specificity"]
    assert candidate["correct_reference_ids"] < parent["correct_reference_ids"]
    assert native["native_exit_code"] == 1 and evidence["candidate_promoted"] is False


def test_raw_diagnostic_does_not_repair_json_or_authorize_wrong_reference_ids():
    common = {
        "split": "val",
        "source_clip_id": "clip",
        "reference_rgb_sha256": "reference",
        "prompt_sha256": "prompt",
    }
    rows = [
        {
            **common,
            "sample_id": "positive",
            "expected_target": {
                "current_target_visibility": "visible",
                "target_reference_id": "real",
            },
            "raw_output": '{"current_target_visibility":"visible","target_reference_id":"wrong"}',
        },
        {
            **common,
            "sample_id": "negative",
            "expected_target": {
                "current_target_visibility": "not_confirmed",
                "target_reference_id": "real",
            },
            "raw_output": (
                '{"current_target_visibility":"not_confirmed",'
                '"current_target_visibility":"visible"}'
            ),
        },
    ]
    result = _raw_visibility_diagnostic(rows)
    assert result["confusion"]["tp"] == 1 and result["confusion"]["invalid"] == 1
    assert result["confusion"]["fp"] == 1
    assert result["correct_reference_ids"] == 0
    assert result["missing_unknown_fields"] == 2
    assert result["consistent_pair_count"] == 0
    parsed = trainer.strict_factual_json(rows[0]["raw_output"].encode())
    with pytest.raises(ValueError):
        trainer.validate_target(parsed, "real")


def test_byte_binding_rejects_required_absence_same_size_mutation_and_escape(tmp_path):
    file = tmp_path / "source.py"
    file.write_bytes(b"initial")
    reference = {"path": file.name, "bytes": 7, "sha256": _sha(file)}
    assert _bound(reference, root=tmp_path) == file
    file.write_bytes(b"changed")
    with pytest.raises(AssertionError):
        _bound(reference, root=tmp_path)
    with pytest.raises(AssertionError, match="Required"):
        _bound({**reference, "path": "missing.py"}, root=tmp_path)
    with pytest.raises(ValueError):
        _bound({**reference, "path": "../escape.py"}, root=tmp_path)
    with pytest.raises(pytest.skip.Exception):
        _bound({**reference, "path": "artifacts/missing-model.onnx"}, root=tmp_path)
