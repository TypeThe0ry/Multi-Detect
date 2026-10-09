"""CPU fixtures for evidence auditing; these are not model/flight results."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from scripts import audit_onboard_vlm_grounding_increment_result as audit


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, sort_keys=True, allow_nan=False).encode())


@pytest.fixture
def result(tmp_path, monkeypatch):
    """Only the already-independent data reviewer is stubbed.

    All report/log/raw JSON/source/image/SHA and tiny saved FP32 tensor checks
    use the production auditor. No VLM, CUDA or optimizer is invoked.
    """
    torch = pytest.importorskip("torch")
    safetensors = pytest.importorskip("safetensors.torch")
    budget = audit.factual.TrainingBudget(
        epochs=1, max_optimizer_steps=154, learning_rate=0.00002, seed=20261019
    )
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(audit.dataset, "ROOT", root)
    data = root / "data"
    data.mkdir()
    samples = []
    for split, count in audit.EXPECTED_SPLITS.items():
        for index in range(count):
            identifier = audit._sha(f"{split}/{index}".encode())[:32]
            ref = audit._sha(f"ref/{split}/{index // 8}".encode())[:32]
            images = []
            for role in ("reference", "current"):
                image = data / f"{identifier}-{role}.png"
                image.write_bytes(f"fixture source RGB bytes {identifier} {role}".encode())
                record = audit.factual.probe.file_record(image)
                images.append({**record, "path": image.name, "role": role})
            model_input = {
                "prompt": "Only state current low-level visibility; other facts unknown.",
                "target_reference_id": ref,
                "images": images,
                "context": {name: "unknown" for name in audit.NAVIGATION},
            }
            positive = index < {"train": 61, "val": 28, "test": 30}[split]
            target = {
                "target_reference_id": ref,
                "current_target_visibility": "visible" if positive else "not_confirmed",
                **{name: "unknown" for name in audit.NAVIGATION},
            }
            samples.append(
                {
                    "sample_id": identifier,
                    "split": split,
                    "model_input": model_input,
                    "assistant_target": target,
                    "model_input_sha256": audit._canonical_sha(model_input),
                    "assistant_target_sha256": audit._canonical_sha(target),
                    "provenance": {"source_clip_id": f"{split}-{index // 8}"},
                }
            )
    original = root / "original.json"
    _write_json(original, {"samples": [s for s in samples if s["split"] != "train"] + samples[:26]})
    document = {
        "samples": samples,
        "base_manifest": {
            "path": original.name,
            "sha256": audit.factual.probe.sha256_file(original),
        },
    }
    manifest = data / "manifest.pending-review.json"
    _write_json(manifest, document)
    review = data / "review-receipt.json"
    _write_json(review, {"test_independent_reviewer_stub": True})
    monkeypatch.setattr(
        audit.dataset,
        "validate_grounding_increment_dataset",
        lambda path, sha, **kw: audit._json(path, sha)[0],
    )
    model = root / "model"
    model.mkdir()
    train = [s for s in samples if s["split"] == "train"]
    holdout = [s for s in samples if s["split"] != "train"]
    train_log = [
        {
            "step": index,
            "epoch": 1,
            "sample_id": sample["sample_id"],
            "split": "train",
            "loss": 0.4,
            "gradient_norm_before_clip": 0.2,
            "elapsed_s": 0.1,
        }
        for index, sample in enumerate(train, 1)
    ]
    (model / "training-log.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in train_log),
        encoding="utf-8",
    )
    evaluations = {}
    for role in ("parent", "candidate"):
        row_dir = model / f"{role}-rows"
        row_dir.mkdir()
        rows = []
        diagnostics = []
        for sample in holdout:
            target = deepcopy(sample["assistant_target"])
            if (
                role == "parent"
                and sample["split"] == "val"
                and (
                    sample
                    == next(
                        s
                        for s in holdout
                        if s["split"] == "val"
                        and s["assistant_target"]["current_target_visibility"] == "not_confirmed"
                    )
                )
            ):
                target["current_target_visibility"] = "visible"
            row = {
                "sample_id": sample["sample_id"],
                "split": sample["split"],
                "model_input_sha256": sample["model_input_sha256"],
                "expected_target": sample["assistant_target"],
                "source_clip_id": sample["provenance"]["source_clip_id"],
                "reference_rgb_sha256": sample["model_input"]["images"][0]["sha256"],
                "current_rgb_sha256": sample["model_input"]["images"][1]["sha256"],
                "prompt_sha256": audit._sha(sample["model_input"]["prompt"].encode()),
                "native_generation_completed": True,
                "raw_output": json.dumps(target),
                "output_token_ids": [1, 2, 3],
                "generated_tokens": 3,
                "elapsed_s": 0.2,
                "token_cap_reached": False,
                "parsed_output": target,
                "strict_fact_reply_accepted": True,
                "exact_factual_match": target == sample["assistant_target"],
                "failure": None,
            }
            rebuilt, diagnostic = audit._parse_reply(
                row, sample, max_new_tokens=budget.max_new_tokens
            )
            rows.append(rebuilt)
            diagnostics.append(diagnostic)
            _write_json(row_dir / f"{sample['sample_id']}.json", rebuilt)
        summary = audit._summarize(rows, diagnostics)
        evaluations[role] = {
            "samples": rows,
            **{k: summary[k] for k in ("count", "strict_reply_count", "exact_match_count")},
            "metrics": audit.factual.grounding_metrics(rows),
        }
        _write_json(model / f"{role}-evaluation.json", evaluations[role])
    prefix = "base_model.model.model.language_model.layers.0.self_attn.q_proj"
    state = {
        f"{prefix}.lora_A.weight": torch.full((4, 3), 0.5, dtype=torch.float32),
        f"{prefix}.lora_B.weight": torch.full((3, 4), 0.25, dtype=torch.float32),
    }
    trainable = {
        "trainable_parameter_count": 24,
        "trainable_parameters": [
            {
                "name": key.replace(".weight", ".default.weight"),
                "numel": tensor.numel(),
                "dtype": str(tensor.dtype),
            }
            for key, tensor in state.items()
        ],
    }
    adapter = model / "adapter"
    adapter.mkdir()
    safetensors.save_file(state, str(adapter / "adapter_model.safetensors"))
    digest = audit.recovery.saved_parameter_digest(state, trainable, rank=4)
    row = evaluations["candidate"]["samples"][0]
    parent_state = {name: torch.full_like(tensor, 0.125) for name, tensor in state.items()}
    parent_weights = root / "parent/adapter_model.safetensors"
    parent_weights.parent.mkdir()
    safetensors.save_file(parent_state, str(parent_weights))
    parent_digest = audit.recovery.saved_parameter_digest(parent_state, trainable, rank=4)
    parent_weights_sha = audit.factual.probe.sha256_file(parent_weights)
    monkeypatch.setattr(audit.increment, "PARENT_ADAPTER_SHA256", parent_weights_sha)
    parent_report = root / "parent/report.json"
    _write_json(
        parent_report,
        {
            "model_id": audit.factual.MODEL_ID,
            "revision": audit.factual.REVISION,
            "native_exit_code": 1,
            "trainable_audit": trainable,
            "adapter_parameter_sha256_after": parent_digest,
            "base_parameter_sha256_after": "2" * 64,
        },
    )
    parent_report_sha = audit.factual.probe.sha256_file(parent_report)
    monkeypatch.setattr(audit.increment, "PARENT_REPORT_SHA256", parent_report_sha)
    document["parent_adapter"] = {
        **audit.factual.probe.file_record(parent_weights),
        "path": parent_weights.relative_to(root).as_posix(),
    }
    _write_json(manifest, document)
    plan = root / "plan.json"
    _write_json(
        plan,
        {
            "contract": audit.increment.PLAN_CONTRACT,
            "model_id": audit.factual.MODEL_ID,
            "revision": audit.factual.REVISION,
            "parent_report_sha256": parent_report_sha,
            "parent_adapter_sha256": parent_weights_sha,
            "recovery_report_sha256": audit.increment.RECOVERY_REPORT_SHA256,
            "optimizer_reads_only_train_split": True,
            "holdout_inputs_unchanged": True,
            "fixed_final_checkpoint": True,
            "budget": asdict(budget),
            "dataset_sha256": audit.factual.probe.sha256_file(manifest),
            "review_receipt_sha256": audit.factual.probe.sha256_file(review),
            "split_counts": audit.EXPECTED_SPLITS,
            **{name: False for name in audit.increment.FALSE_FLAGS},
        },
    )
    official_directory = root / "official-cache"
    official_directory.mkdir()
    config = official_directory / "config.json"
    config.write_bytes(b'{"test_fixture":"not an actual pretrained model"}')
    official_weights = official_directory / "model.safetensors"
    official_weights.write_bytes((adapter / "adapter_model.safetensors").read_bytes())
    config_record = audit.factual.probe.file_record(config)
    git_blob = hashlib.sha1(usedforsecurity=False)
    git_blob.update(f"blob {config_record['bytes']}\0".encode("ascii"))
    git_blob.update(config.read_bytes())
    weight_record = audit.factual.probe.file_record(official_weights)
    model_provenance = {
        "model_id": audit.factual.MODEL_ID,
        "revision": audit.factual.REVISION,
        "license": "apache-2.0",
        "gated": False,
        "expected_download_bytes": config_record["bytes"] + weight_record["bytes"],
        "files": [
            {
                **config_record,
                "repository_path": "config.json",
                "official_git_blob_sha1": git_blob.hexdigest(),
            },
            {
                **weight_record,
                "repository_path": "model.safetensors",
                "official_lfs_sha256": weight_record["sha256"],
            },
        ],
    }
    report = {
        "contract": audit.increment.CONTRACT,
        "model_id": audit.factual.MODEL_ID,
        "revision": audit.factual.REVISION,
        "native_exit_code": 0,
        "dataset_sha256": audit.factual.probe.sha256_file(manifest),
        "plan_sha256": audit.factual.probe.sha256_file(plan),
        "parent_adapter_sha256": parent_weights_sha,
        "source_files": [
            audit.factual.probe.file_record(p) for p in (manifest, review, plan, parent_report)
        ],
        "runtime_files": [],
        "model_provenance": model_provenance,
        "budget": asdict(budget),
        "optimizer_steps_attempted": 154,
        "optimizer_native_calls_returned": 154,
        "optimizer_cuda_sync_completed_count": 154,
        "optimizer_steps_completed": 154,
        "optimizer_update_uncertain": False,
        "optimization_rows": train_log,
        "vlm_training_performed": True,
        "pre_optimizer_source_revalidation_passed": True,
        "parent_evaluation": evaluations["parent"],
        "candidate_evaluation": evaluations["candidate"],
        "quality_comparison": audit.increment.quality_comparison(
            evaluations["parent"], evaluations["candidate"]
        ),
        "adapter_files": [audit.factual.probe.file_record(adapter / "adapter_model.safetensors")],
        "trainable_audit": trainable,
        "base_parameter_sha256_before": "2" * 64,
        "base_parameter_sha256_after": "2" * 64,
        "base_parameter_bytes_unchanged": True,
        "adapter_parameter_sha256_before": parent_digest,
        "adapter_parameter_sha256_after": digest,
        "warm_start": {
            "saved_parameter_sha256": parent_digest,
            "loaded_parameter_sha256": parent_digest,
            "exact_tensor_equality": True,
            "dtype_conversion_performed": False,
        },
        "saved_adapter_reload": {
            "saved_parameter_sha256": digest,
            "loaded_parameter_sha256": digest,
            "exact_tensor_equality": True,
            "fresh_base": True,
            "generation_token_ids_equal": True,
            "raw_output_equal": True,
            "sample_id": row["sample_id"],
            "generation": {
                "raw_output": row["raw_output"],
                "output_token_ids": row["output_token_ids"],
            },
            "dtype_conversion_performed": False,
        },
        **{name: False for name in audit.increment.FALSE_FLAGS},
    }
    report_path = model / "report.json"

    def seal():
        report["output_artifact_files"] = [
            audit.factual.probe.file_record(p)
            for p in sorted(model.rglob("*"))
            if p.is_file() and p != report_path
        ]
        _write_json(report_path, report)
        return {
            "report_path": report_path,
            "expected_report_sha256": audit.factual.probe.sha256_file(report_path),
            "dataset_manifest": manifest,
            "expected_dataset_sha256": audit.factual.probe.sha256_file(manifest),
            "review_receipt": review,
        }

    return {
        "root": root,
        "model": model,
        "report": report,
        "seal": seal,
        "document": document,
        "samples": samples,
        "state": state,
        "plan": plan,
        "parent_weights": parent_weights,
    }


def test_complete_reparse_log_and_actual_saved_fp32_tensors(result):
    outcome = audit.audit_increment_result(**result["seal"]())
    assert outcome["successfully_learned_gate"] is True
    assert outcome["paired_holdout_count"] == 84
    assert outcome["optimizer"]["logged_updates"] == 154
    assert outcome["candidate"]["per_split_visibility"]["val"] == {
        "tp": 28,
        "tn": 13,
        "fp": 0,
        "fn": 0,
        "invalid": 0,
    }
    assert outcome["candidate"]["per_split_visibility"]["test"]["tp"] == 30
    assert outcome["candidate"]["per_split_unknown_field_counts"] == {
        "val": dict.fromkeys(audit.NAVIGATION, 41),
        "test": dict.fromkeys(audit.NAVIGATION, 43),
    }
    assert outcome["saved_adapter"]["saved_fp32_digest_verified"] is True
    assert outcome["saved_adapter"]["independent_model_generation_replayed"] is False
    assert outcome["candidate_promoted"] is False
    assert outcome["flight_action_training_performed"] is False


@pytest.mark.parametrize("field", ["strict_reply_count", "exact_match_count", "metrics"])
def test_reported_metrics_cannot_replace_raw_recomputation(result, field):
    before = result["report"]["candidate_evaluation"]
    if field == "metrics":
        before[field]["visibility_confusion"]["tn"] += 1
    else:
        before[field] -= 1
    _write_json(result["model"] / "candidate-evaluation.json", before)
    with pytest.raises(ValueError, match="reported metrics"):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize(
    "field",
    [
        "model_input_sha256",
        "expected_target",
        "reference_rgb_sha256",
        "current_rgb_sha256",
        "prompt_sha256",
        "source_clip_id",
        "split",
    ],
)
def test_exact_source_holdout_row_binding(result, field):
    row = result["report"]["candidate_evaluation"]["samples"][0]
    row[field] = {"false": "target"} if field == "expected_target" else "foreign"
    _write_json(result["model"] / "candidate-rows" / f"{row['sample_id']}.json", row)
    with pytest.raises(ValueError, match="exact heldout"):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize("mutation", ["duplicate", "holdout", "epoch", "step", "negative_loss"])
def test_optimizer_integrity(result, mutation):
    rows = result["report"]["optimization_rows"]
    if mutation == "duplicate":
        rows[1]["sample_id"] = rows[0]["sample_id"]
    elif mutation == "holdout":
        rows[0]["sample_id"] = next(
            s["sample_id"] for s in result["samples"] if s["split"] == "val"
        )
    elif mutation == "epoch":
        rows[0]["epoch"] = 2
    elif mutation == "step":
        rows[0]["step"] = True
    else:
        rows[0]["loss"] = -1.0
    (result["model"] / "training-log.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize(
    "badraw",
    [
        '{"target_reference_id":"x","target_reference_id":"y"}',
        '{"gps":NaN}',
        "```json\n{}\n```",
        "not JSON",
    ],
    ids=["duplicate", "nan", "markdown", "plain"],
)
def test_raw_not_repaired_to_reported_acceptance(result, badraw):
    row = result["report"]["candidate_evaluation"]["samples"][0]
    row["raw_output"] = badraw
    _write_json(result["model"] / "candidate-rows" / f"{row['sample_id']}.json", row)
    with pytest.raises(ValueError, match="original raw reply"):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_cap_reached", True),
        ("elapsed_s", 30.01),
        ("generated_tokens", 4),
        ("output_token_ids", [True]),
        ("native_generation_completed", False),
    ],
)
def test_generation_facts_not_ignored(result, field, value):
    row = result["report"]["candidate_evaluation"]["samples"][0]
    row[field] = value
    _write_json(result["model"] / "candidate-rows" / f"{row['sample_id']}.json", row)
    with pytest.raises(ValueError):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize("pathkind", ["image", "raw", "tensor", "source"])
def test_changed_actual_bound_bytes_rejected(result, pathkind):
    arguments = result["seal"]()
    if pathkind == "image":
        image = result["samples"][0]["model_input"]["images"][0]
        path = arguments["dataset_manifest"].parent / image["path"]
    elif pathkind == "raw":
        path = next((result["model"] / "candidate-rows").glob("*.json"))
    elif pathkind == "tensor":
        path = result["model"] / "adapter/adapter_model.safetensors"
    else:
        path = arguments["review_receipt"]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="bound file"):
        audit.audit_increment_result(**arguments)


def test_native_failed_full_run_keeps_quality_but_never_success(result):
    result["report"]["native_exit_code"] = 1
    result["report"]["failure"] = "actual reload verification failed"
    result["report"].pop("saved_adapter_reload")
    outcome = audit.audit_increment_result(**result["seal"]())
    assert outcome["training_native_exit_code"] == 1
    assert outcome["quality_passed"] is True
    assert outcome["successfully_learned_gate"] is False
    assert outcome["native_verification_completed"] is False


def test_partial_run_retains_only_real_logged_updates_and_no_success(result):
    report = result["report"]
    report["native_exit_code"] = 1
    report["failure"] = "actual CUDA completion uncertain"
    report["optimizer_update_uncertain"] = True
    for key in ("optimizer_steps_attempted", "optimizer_native_calls_returned"):
        report[key] = 11
    for key in ("optimizer_steps_completed", "optimizer_cuda_sync_completed_count"):
        report[key] = 10
    report["optimizer_log_rows_completed"] = 10
    report.pop("optimization_rows")
    report.pop("candidate_evaluation")
    report.pop("quality_comparison")
    log = result["model"] / "training-log.jsonl"
    log.write_bytes(b"\n".join(log.read_bytes().splitlines()[:10]) + b"\n")
    for path in (result["model"] / "candidate-rows").glob("*.json"):
        path.unlink()
    (result["model"] / "candidate-evaluation.json").unlink()
    outcome = audit.audit_increment_result(**result["seal"]())
    assert outcome["optimizer"]["logged_updates"] == 10
    assert outcome["partial_run"] is True
    assert outcome["candidate"]["count"] == 0
    assert outcome["quality_passed"] is False
    assert outcome["successfully_learned_gate"] is False


@pytest.mark.parametrize("what", ["saved_digest", "base", "unchanged_adapter", "reload_tokens"])
def test_claimed_native_success_needs_actual_verification(result, what):
    report = result["report"]
    if what == "saved_digest":
        report["adapter_parameter_sha256_after"] = "4" * 64
    elif what == "base":
        report["base_parameter_sha256_after"] = "4" * 64
    elif what == "unchanged_adapter":
        report["adapter_parameter_sha256_before"] = report["adapter_parameter_sha256_after"]
    else:
        report["saved_adapter_reload"]["generation"]["output_token_ids"] = [9]
    with pytest.raises(ValueError, match="native success|warm-start"):
        audit.audit_increment_result(**result["seal"]())


def test_no_extra_output_or_authority(result):
    arguments = result["seal"]()
    (result["model"] / "unsealed.txt").write_text("not sealed")
    with pytest.raises(ValueError, match="inventory"):
        audit.audit_increment_result(**arguments)
    result["report"]["candidate_promoted"] = True
    with pytest.raises(ValueError, match="authority"):
        audit.audit_increment_result(**result["seal"]())


def test_quality_all_class_specific_gates_and_extra_tn(result):
    good = audit.audit_increment_result(**result["seal"]())
    parent = good["parent"]
    candidate = good["candidate"]
    assert all(audit.quality_gates(parent, candidate).values())
    for split in ("val", "test"):
        for kind in ("tp", "tn"):
            regressed = deepcopy(candidate)
            regressed["per_split_visibility"][split][kind] = (
                parent["per_split_visibility"][split][kind] - 1
            )
            assert not all(audit.quality_gates(parent, regressed).values())
    assert not audit.quality_gates(candidate, candidate)[
        "at_least_one_extra_correct_zero_pixel_holdout"
    ]
    candidate["unknown_navigation_fact_count"] = 83
    assert not audit.quality_gates(parent, candidate)["all_navigation_fields_remain_unknown"]


def test_create_only_audit_and_cli_preserve_failed_native_status(result):
    result["report"]["native_exit_code"] = 1
    result["report"]["failure"] = "preserved failed native run"
    arguments = result["seal"]()
    output = result["root"] / "new-audit.json"
    assert (
        audit.main(
            [
                "--report",
                str(arguments["report_path"]),
                "--report-sha256",
                arguments["expected_report_sha256"],
                "--dataset-manifest",
                str(arguments["dataset_manifest"]),
                "--dataset-sha256",
                arguments["expected_dataset_sha256"],
                "--review-receipt",
                str(arguments["review_receipt"]),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    written = json.loads(output.read_bytes())
    assert written["training_native_exit_code"] == 1
    assert written["successfully_learned_gate"] is False
    with pytest.raises(FileExistsError):
        audit.write_increment_audit(output, **arguments)


def test_corrupted_report_sha_rejected_before_writing(result):
    arguments = result["seal"]()
    arguments["expected_report_sha256"] = "1" * 64
    output = result["root"] / "not-created.json"
    with pytest.raises(ValueError, match="SHA256"):
        audit.write_increment_audit(output, **arguments)
    assert not output.exists()


def test_honest_wrong_reference_id_is_a_strict_failure_not_repaired(result):
    declared = result["report"]["candidate_evaluation"]
    row = declared["samples"][1]
    parsed = deepcopy(row["parsed_output"])
    parsed["target_reference_id"] = "9" * 32
    row.update(
        raw_output=json.dumps(parsed),
        parsed_output=parsed,
        strict_fact_reply_accepted=False,
        exact_factual_match=False,
        failure="Factual response names another target reference",
    )
    declared["strict_reply_count"] -= 1
    declared["exact_match_count"] -= 1
    declared["metrics"] = audit.factual.grounding_metrics(declared["samples"])
    _write_json(result["model"] / "candidate-rows" / f"{row['sample_id']}.json", row)
    _write_json(result["model"] / "candidate-evaluation.json", declared)
    result["report"]["quality_comparison"] = audit.increment.quality_comparison(
        result["report"]["parent_evaluation"],
        declared,
    )
    outcome = audit.audit_increment_result(**result["seal"]())
    assert outcome["candidate"]["raw_target_reference_id_correct_count"] == 83
    assert outcome["candidate"]["visibility_confusion"]["invalid"] == 1
    assert outcome["quality_passed"] is False
    assert outcome["native_verification_completed"] is True
    assert outcome["successfully_learned_gate"] is False


@pytest.mark.parametrize("mutation", ["bf16", "nan", "extra"])
def test_saved_checkpoint_is_not_repaired_or_trusted_by_hash_alone(result, mutation):
    torch = pytest.importorskip("torch")
    safetensors = pytest.importorskip("safetensors.torch")
    state = result["state"]
    first = next(iter(state))
    if mutation == "bf16":
        state[first] = state[first].to(torch.bfloat16)
    elif mutation == "nan":
        state[first][0, 0] = float("nan")
    else:
        state["foreign.weight"] = torch.ones(1)
    safetensors.save_file(state, str(result["model"] / "adapter/adapter_model.safetensors"))
    result["report"]["native_exit_code"] = 1
    result["report"]["failure"] = "saved adapter verification failed"
    result["report"]["adapter_files"] = [
        audit.factual.probe.file_record(
            result["model"] / "adapter/adapter_model.safetensors",
        )
    ]
    outcome = audit.audit_increment_result(**result["seal"]())
    assert outcome["saved_adapter"]["saved_fp32_digest_verified"] is False
    assert "saved_tensor_verification_failure" in outcome["saved_adapter"]
    assert outcome["successfully_learned_gate"] is False


def test_nonfinite_log_json_rejected_without_report_repair(result):
    log = result["model"] / "training-log.jsonl"
    payload = log.read_bytes()
    log.write_bytes(payload.replace(b'"loss": 0.4', b'"loss": NaN', 1))
    with pytest.raises(ValueError, match="Non-finite"):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize(
    "field", ["dataset_sha256", "review_receipt_sha256", "split_counts", "budget"]
)
def test_bound_plan_cannot_drift_from_actual_run(result, field):
    plan = json.loads(result["plan"].read_bytes())
    if field == "split_counts":
        plan[field] = {"train": 154, "val": 43, "test": 41}
    elif field == "budget":
        plan[field]["max_optimizer_steps"] = 155
    else:
        plan[field] = "9" * 64
    _write_json(result["plan"], plan)
    result["report"]["plan_sha256"] = audit.factual.probe.sha256_file(result["plan"])
    result["report"]["source_files"] = [
        audit.factual.probe.file_record(p)
        for p in [
            result["root"] / "data/manifest.pending-review.json",
            result["root"] / "data/review-receipt.json",
            result["plan"],
            result["root"] / "parent/report.json",
        ]
    ]
    with pytest.raises(ValueError, match="bound plan"):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize(
    "field",
    ["parent_adapter_sha256", "adapter_parameter_sha256_before", "base_parameter_sha256_before"],
)
def test_parent_identity_cannot_be_redeclared(result, field):
    result["report"][field] = "9" * 64
    with pytest.raises(ValueError, match="parent|warm-start"):
        audit.audit_increment_result(**result["seal"]())


def test_missing_plan_original_parent_source_and_parent_tensor_drift(result):
    initial = list(result["report"]["source_files"])
    result["report"]["source_files"] = [r for r in initial if r["path"] != str(result["plan"])]
    with pytest.raises(ValueError, match="absent"):
        audit.audit_increment_result(**result["seal"]())
    result["report"]["source_files"] = initial
    result["parent_weights"].write_bytes(result["parent_weights"].read_bytes() + b"drift")
    with pytest.raises(ValueError, match="bound file"):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize(
    "length,cap_flag,valid",
    [
        (193, False, False),
        (192, False, False),
        (192, True, True),
        (191, False, True),
        (191, True, False),
    ],
    ids=["over-cap", "hidden-full-cap", "honest-full-cap", "under-cap", "false-truncation"],
)
def test_exact_bound_generator_cap_boundaries(result, length, cap_flag, valid):
    sample = next(s for s in result["samples"] if s["split"] == "val")
    row = deepcopy(result["report"]["candidate_evaluation"]["samples"][0])
    row.update(output_token_ids=[1] * length, generated_tokens=length, token_cap_reached=cap_flag)
    if length == 192 and cap_flag:
        row.update(parsed_output=None, strict_fact_reply_accepted=False, exact_factual_match=False)
    if not valid:
        with pytest.raises(ValueError, match="token evidence|token-cap flag"):
            audit._parse_reply(row, sample, max_new_tokens=192)
        return
    rebuilt, _ = audit._parse_reply(row, sample, max_new_tokens=192)
    assert rebuilt["strict_fact_reply_accepted"] is (length < 192)


def test_public_audit_uses_verified_plan_cap_not_legacy_512(result):
    row = result["report"]["candidate_evaluation"]["samples"][0]
    row.update(output_token_ids=[1] * 193, generated_tokens=193, token_cap_reached=False)
    _write_json(result["model"] / "candidate-rows" / f"{row['sample_id']}.json", row)
    with pytest.raises(ValueError, match="token evidence"):
        audit.audit_increment_result(**result["seal"]())


@pytest.mark.parametrize(
    "attempted,returned,sync,completed",
    [
        (0, 0, 0, 1),
        (1, 1, 1, 0),
        (0, 1, 0, 0),
        (1, 0, 1, 1),
    ],
    ids=[
        "completed-without-sync",
        "sync-without-completed",
        "returned-without-attempt",
        "sync-without-return",
    ],
)
def test_partial_native_failure_counter_causality(tmp_path, attempted, returned, sync, completed):
    report = {
        "optimizer_steps_attempted": attempted,
        "optimizer_native_calls_returned": returned,
        "optimizer_cuda_sync_completed_count": sync,
        "optimizer_steps_completed": completed,
        "vlm_training_performed": True,
        "optimizer_update_uncertain": True,
        "native_exit_code": 1,
    }
    with pytest.raises(ValueError, match="causality"):
        audit._optimizer(report, tmp_path, [{"sample_id": "fixture-train"}], set())


@pytest.mark.parametrize("training_performed", [False, None])
def test_completed_updates_cannot_omit_training_fact(tmp_path, training_performed):
    report = {
        "optimizer_steps_attempted": 1,
        "optimizer_native_calls_returned": 1,
        "optimizer_cuda_sync_completed_count": 1,
        "optimizer_steps_completed": 1,
        "vlm_training_performed": training_performed,
        "native_exit_code": 1,
    }
    with pytest.raises(ValueError, match="training-performed"):
        audit._optimizer(report, tmp_path, [{"sample_id": "fixture-train"}], set())


def test_first_uncertain_update_preserves_unknown_not_completed(tmp_path):
    report = {
        "optimizer_steps_attempted": 1,
        "optimizer_native_calls_returned": 0,
        "optimizer_cuda_sync_completed_count": 0,
        "optimizer_steps_completed": 0,
        "vlm_training_performed": None,
        "optimizer_update_uncertain": True,
        "native_exit_code": 1,
    }
    outcome = audit._optimizer(report, tmp_path, [{"sample_id": "fixture-train"}], set())
    assert outcome["logged_updates"] == 0
    assert outcome["complete"] is False
    assert outcome["optimizer_update_uncertain"] is True


def test_official_provenance_real_five_field_shapes_and_generic_contract_stays_strict(result):
    outcome = audit.audit_increment_result(**result["seal"]())
    assert outcome["official_file_hashes_reverified"] is True
    assert outcome["official_model_provenance"] == result["report"]["model_provenance"]
    assert outcome["official_metadata_source_authentication_granted"] is False
    assert all(len(record) == 5 for record in outcome["official_model_provenance"]["files"])
    with pytest.raises(ValueError, match="unknown bound-file"):
        audit._records(result["report"]["model_provenance"]["files"], {})


@pytest.mark.parametrize(
    "mutation",
    [
        "model_id",
        "revision",
        "license",
        "gated",
        "download_sum",
        "git_digest",
        "lfs_digest",
        "extra",
        "both_hashes",
        "missing_hash",
        "absolute",
        "traversal",
        "windows_drive",
        "duplicate",
    ],
)
def test_official_provenance_metadata_tampering_rejected(result, mutation):
    provenance = result["report"]["model_provenance"]
    first = provenance["files"][0]
    second = provenance["files"][1]
    if mutation in {"model_id", "revision", "license"}:
        provenance[mutation] = "foreign"
    elif mutation == "gated":
        provenance[mutation] = True
    elif mutation == "download_sum":
        provenance["expected_download_bytes"] += 1
    elif mutation == "git_digest":
        first["official_git_blob_sha1"] = "9" * 40
    elif mutation == "lfs_digest":
        second["official_lfs_sha256"] = "9" * 64
    elif mutation == "extra":
        first["caller_authenticated"] = True
    elif mutation == "both_hashes":
        first["official_lfs_sha256"] = first["sha256"]
    elif mutation == "missing_hash":
        first.pop("official_git_blob_sha1")
    elif mutation == "duplicate":
        second["repository_path"] = first["repository_path"]
    else:
        first["repository_path"] = {
            "absolute": "/config.json",
            "traversal": "../config.json",
            "windows_drive": "C:/config.json",
        }[mutation]
    with pytest.raises(ValueError):
        audit._official_model_provenance(provenance, {})


def test_official_provenance_changed_local_bytes_rejected(result):
    provenance = result["report"]["model_provenance"]
    path = audit.Path(provenance["files"][0]["path"])
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(ValueError, match="bound file"):
        audit._official_model_provenance(provenance, {})


def test_native_success_requires_official_provenance_while_early_partial_may_omit_it(result):
    result["report"].pop("model_provenance")
    with pytest.raises(ValueError, match="native success"):
        audit.audit_increment_result(**result["seal"]())
    assert audit._official_model_provenance(None, {}) is None
