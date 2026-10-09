"""Increment provenance, no holdout optimization and class-specific gates."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import train_onboard_vlm_grounding_increment as training


def valid_plan():
    budget = training.core.TrainingBudget(learning_rate=0.00002, seed=20261019)
    return {
        "contract": training.PLAN_CONTRACT,
        "model_id": training.core.MODEL_ID,
        "revision": training.core.REVISION,
        "parent_report_sha256": training.PARENT_REPORT_SHA256,
        "parent_adapter_sha256": training.PARENT_ADAPTER_SHA256,
        "recovery_report_sha256": training.RECOVERY_REPORT_SHA256,
        "optimizer_reads_only_train_split": True,
        "holdout_inputs_unchanged": True,
        "fixed_final_checkpoint": True,
        "budget": asdict(budget),
        **{name: False for name in training.FALSE_FLAGS},
    }


def write_plan(tmp_path, plan):
    payload = json.dumps(plan).encode()
    path = tmp_path / "plan.json"
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def test_bound_single_epoch_increment_plan(tmp_path):
    path, digest = write_plan(tmp_path, valid_plan())
    plan, budget = training.load_plan(path, digest)
    assert budget.epochs == 1
    assert budget.learning_rate == 0.00002
    assert plan["candidate_promoted"] is False


@pytest.mark.parametrize("flag", training.FALSE_FLAGS)
def test_plan_does_not_gain_authority(tmp_path, flag):
    plan = valid_plan()
    plan[flag] = True
    path, digest = write_plan(tmp_path, plan)
    with pytest.raises(ValueError, match="authority"):
        training.load_plan(path, digest)


@pytest.mark.parametrize(
    "key", ["parent_report_sha256", "parent_adapter_sha256", "recovery_report_sha256"]
)
def test_parent_identity_cannot_drift(tmp_path, key):
    plan = valid_plan()
    plan[key] = "0" * 64
    path, digest = write_plan(tmp_path, plan)
    with pytest.raises(ValueError, match="provenance"):
        training.load_plan(path, digest)


@pytest.mark.parametrize(
    "key,value", [("epochs", 2), ("learning_rate", 0.0001), ("seed", 1), ("rank", 8)]
)
def test_no_post_hoc_parameter_retries(tmp_path, key, value):
    plan = valid_plan()
    plan["budget"][key] = value
    path, digest = write_plan(tmp_path, plan)
    with pytest.raises(ValueError):
        training.load_plan(path, digest)


def test_changed_plan_bytes_rejected(tmp_path):
    path, digest = write_plan(tmp_path, valid_plan())
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA"):
        training.load_plan(path, digest)


@pytest.mark.parametrize("split", ["val", "test"])
def test_holdout_rejected_before_optimizer_or_log(tmp_path, split):
    backend = SimpleNamespace()
    with pytest.raises(ValueError, match="holdout"):
        training.train_epoch(backend, [{"split": split}], {}, tmp_path)
    assert not (tmp_path / "training-log.jsonl").exists()


@pytest.mark.parametrize("count", [0, 201])
def test_no_empty_or_truncated_train_epoch(tmp_path, count):
    backend = SimpleNamespace(budget=training.core.TrainingBudget())
    with pytest.raises(ValueError, match="whole train epoch"):
        training.train_epoch(backend, [{"split": "train"}] * count, {}, tmp_path)
    assert not (tmp_path / "training-log.jsonl").exists()


def evaluation():
    return {
        "samples": [{"sample_id": "a", "model_input_sha256": "b"}],
        "strict_reply_count": 84,
        "exact_match_count": 60,
        "count": 84,
        "metrics": {
            "per_split_visibility": {split: {"tp": 24, "tn": 8} for split in ("val", "test")},
            "visibility_confusion": {"tp": 48, "tn": 16},
            "unknown_navigation_fact_count": 84,
        },
    }


def test_class_specific_improvement_never_promotes():
    before = evaluation()
    after = copy.deepcopy(before)
    after["metrics"]["per_split_visibility"]["val"]["tn"] = 9
    after["metrics"]["visibility_confusion"]["tn"] = 17
    result = training.quality_comparison(before, after)
    assert result["quality_passed"] is True
    assert result["candidate_promoted"] is False
    assert result["action_learning_claimed"] is False


def test_no_gain_does_not_pass_quality():
    assert training.quality_comparison(evaluation(), evaluation())["quality_passed"] is False


@pytest.mark.parametrize(
    "split,field", [("val", "tp"), ("test", "tp"), ("val", "tn"), ("test", "tn")]
)
def test_overall_gain_cannot_hide_class_or_split_regression(split, field):
    before = evaluation()
    after = copy.deepcopy(before)
    after["metrics"]["visibility_confusion"]["tn"] = 20
    after["metrics"]["per_split_visibility"][split][field] -= 1
    assert training.quality_comparison(before, after)["quality_passed"] is False


@pytest.mark.parametrize("key", ["sample_id", "model_input_sha256"])
def test_parent_candidate_inputs_must_match(key):
    before = evaluation()
    after = copy.deepcopy(before)
    after["samples"][0][key] = "changed"
    with pytest.raises(ValueError, match="identical ordered holdout"):
        training.quality_comparison(before, after)


def test_import_does_not_load_a_model():
    assert isinstance(training.CONTRACT, str)
    assert not hasattr(training, "torch")
    assert isinstance(Path(training.__file__), Path)
