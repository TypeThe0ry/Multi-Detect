"""CPU-only cross-run failure accounting and last pre-optimizer admission."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_onboard_vlm_grounding_increment_dataset as builder
from scripts import train_onboard_vlm_grounding_increment as training


class _Scalar:
    def item(self):
        return 1.0

    def detach(self):
        return self

    def backward(self):
        pass


class _FailingLog:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def write(self, value):
        raise OSError("CPU fixture: training log write failed after synchronized update")

    def flush(self):
        return self.stream.flush()


def _fixture(monkeypatch, tmp_path, failure, *, fail_at=1, train_count=1):
    events = []
    state = {
        "optimizer_creations": 0,
        "step_calls": 0,
        "sync_calls": 0,
        "validations": 0,
        "plan_loads": 0,
    }
    budget = training.core.TrainingBudget(learning_rate=0.00002, seed=20261019)
    rows = [{"sample_id": f"train-{index}", "split": "train"} for index in range(train_count)] + [
        {"sample_id": split, "split": split} for split in ("val", "test")
    ]
    document = {"samples": rows}
    plan = {
        "dataset_sha256": "a" * 64,
        "review_receipt_sha256": "b" * 64,
        "split_counts": {"train": train_count, "val": 1, "test": 1},
    }

    def validate(*args, **kwargs):
        assert kwargs["require_review"] is True
        state["validations"] += 1
        events.append("dataset_validation")
        if failure == "revoked_review" and state["validations"] == 2:
            raise ValueError("CPU fixture: independent review revoked before optimizer")
        return document

    def record(path):
        if failure == "review_identity_drift" and state["validations"] == 2:
            return {"path": str(path), "bytes": 0, "sha256": "0" * 64}
        return {"path": str(path), "bytes": 0, "sha256": "b" * 64}

    def load_plan(*args):
        state["plan_loads"] += 1
        if failure == "plan_drift" and state["plan_loads"] == 2:
            raise ValueError("CPU fixture: expected plan bytes changed before optimizer")
        return plan, budget

    def verify(records):
        events.append("file_verification")
        if failure == "source_drift" and state["validations"] == 2:
            raise ValueError("CPU fixture: bound source changed before optimizer")

    class Optimizer:
        def __init__(self, *args, **kwargs):
            state["optimizer_creations"] += 1
            events.append("optimizer_created")

        def zero_grad(self, **kwargs):
            pass

        def step(self):
            state["step_calls"] += 1
            events.append("optimizer_step_entered")
            if failure == "step" and state["step_calls"] == fail_at:
                raise RuntimeError("CPU fixture: optimizer native call failed")

    def sync():
        state["sync_calls"] += 1
        if failure == "sync" and state["sync_calls"] == fail_at:
            raise RuntimeError("CPU fixture: CUDA completion failed after native return")

    torch = SimpleNamespace(
        optim=SimpleNamespace(AdamW=Optimizer),
        cuda=SimpleNamespace(
            get_device_name=lambda index: "CPU fixture; not CUDA evidence",
            reset_peak_memory_stats=lambda: None,
            synchronize=sync,
            max_memory_allocated=lambda: 0,
            max_memory_reserved=lambda: 0,
        ),
        isfinite=lambda value: SimpleNamespace(item=lambda: True),
        nn=SimpleNamespace(utils=SimpleNamespace(clip_grad_norm_=lambda *a, **k: _Scalar())),
    )

    class Backend:
        base_digest_initial = "c" * 64
        trainable_audit = {}

        def __init__(self, *args):
            self.torch = torch
            self.budget = budget
            self.model = SimpleNamespace(
                parameters=lambda: [SimpleNamespace(requires_grad=True)],
                zero_grad=lambda **kwargs: None,
            )

        def runtime_records(self):
            return []

        def encode(self, sample, directory):
            return {"total_tokens": 1}

        def backward_preflight(self, encoded):
            events.append("backward_preflight")
            return {"optimizer_step_performed": False}

        def loss(self, encoded):
            return _Scalar()

    def parent_evaluation(*args, **kwargs):
        events.append("parent_evaluation")
        return {}

    monkeypatch.setattr(training, "load_plan", load_plan)
    monkeypatch.setattr(
        training,
        "bound_json",
        lambda path, digest: {
            "native_exit_code": 0,
            "base_parameter_bytes_unchanged": True,
            "optimizer_steps_performed_by_this_check": 0,
        },
    )
    monkeypatch.setattr(training.recovery, "validate_original", lambda value: None)
    monkeypatch.setattr(training.recovery, "original_records", lambda value: [])
    monkeypatch.setattr(builder, "validate_grounding_increment_dataset", validate)
    monkeypatch.setattr(
        training.core,
        "admit_samples",
        lambda doc, b: {
            split: [row for row in doc["samples"] if row["split"] == split]
            for split in ("train", "val", "test")
        },
    )
    monkeypatch.setattr(training.core.probe, "file_record", record)
    monkeypatch.setattr(training.core.probe, "verify_records", verify)
    monkeypatch.setattr(training.core.probe, "require_d_storage", lambda path: path)
    monkeypatch.setattr(training.core.probe, "configure_d_cache", lambda path: None)
    monkeypatch.setattr(
        training.core.probe, "prepare_model_snapshot", lambda *args: (tmp_path, {"files": []})
    )
    monkeypatch.setattr(training.core, "GroundingBackend", Backend)
    monkeypatch.setattr(training, "warm_start", lambda *args: {})
    monkeypatch.setattr(training.core, "parameter_digest", lambda *args, **kwargs: "c" * 64)
    monkeypatch.setattr(training, "input_inventory", lambda encoded: {})
    monkeypatch.setattr(training.core, "evaluate_rows", parent_evaluation)
    if failure == "log":
        original_open = Path.open

        def open_with_failing_log(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path.name == "training-log.jsonl" and args and args[0] == "x":
                return _FailingLog(stream)
            return stream

        monkeypatch.setattr(Path, "open", open_with_failing_log)
    args = SimpleNamespace(
        plan=tmp_path / "plan.json",
        plan_sha256="d" * 64,
        parent_report=tmp_path / "parent.json",
        recovery_report=tmp_path / "recovery.json",
        dataset_manifest=tmp_path / "manifest.json",
        dataset_sha256="a" * 64,
        review_receipt=tmp_path / "review.json",
        parent_adapter=tmp_path / "parent-adapter",
        cache_directory=tmp_path / "cache",
        output_directory=tmp_path / "result",
    )
    return args, events, state


@pytest.mark.parametrize("failure,returned", [("step", 0), ("sync", 1)])
def test_native_or_sync_failure_never_claims_zero_training(
    monkeypatch, tmp_path, failure, returned
):
    args, _, state = _fixture(monkeypatch, tmp_path, failure)
    assert training.run(args) == 1
    report = json.loads((args.output_directory / "report.json").read_text())
    assert state["step_calls"] == 1
    assert report["optimizer_steps_attempted"] == 1
    assert report["optimizer_native_calls_returned"] == returned
    assert report["optimizer_steps_completed"] == 0
    assert report["optimizer_update_uncertain"] is True
    assert report["vlm_training_performed"] is None
    assert report["optimizer_log_rows_completed"] == 0
    assert report["native_exit_code"] == 1
    assert report["candidate_promoted"] is False


def test_log_failure_does_not_erase_completed_update(monkeypatch, tmp_path):
    args, _, _ = _fixture(monkeypatch, tmp_path, "log")
    assert training.run(args) == 1
    report = json.loads((args.output_directory / "report.json").read_text())
    assert report["optimizer_steps_attempted"] == report["optimizer_native_calls_returned"] == 1
    assert report["optimizer_steps_completed"] == report["optimizer_cuda_sync_completed_count"] == 1
    assert report["optimizer_update_uncertain"] is False
    assert report["vlm_training_performed"] is True
    assert report["optimizer_log_rows_completed"] == 0
    assert report["native_exit_code"] == 1


@pytest.mark.parametrize("failure", ["step", "sync"])
def test_later_uncertain_update_preserves_first_completed_step(monkeypatch, tmp_path, failure):
    args, _, _ = _fixture(monkeypatch, tmp_path, failure, fail_at=2, train_count=2)
    assert training.run(args) == 1
    report = json.loads((args.output_directory / "report.json").read_text())
    assert report["optimizer_steps_attempted"] == 2
    assert report["optimizer_steps_completed"] == 1
    assert report["optimizer_cuda_sync_completed_count"] == 1
    assert report["optimizer_update_uncertain"] is True
    assert report["vlm_training_performed"] is True
    assert report["optimizer_log_rows_completed"] == 1


@pytest.mark.parametrize(
    "failure", ["revoked_review", "source_drift", "plan_drift", "review_identity_drift"]
)
def test_late_admission_failure_precedes_adamw_creation(monkeypatch, tmp_path, failure):
    args, events, state = _fixture(monkeypatch, tmp_path, failure)
    assert training.run(args) == 1
    report = json.loads((args.output_directory / "report.json").read_text())
    assert state["validations"] == 2
    assert events.index("parent_evaluation") < events.index("backward_preflight")
    assert events.index("backward_preflight") < len(events) - 1
    assert events[events.index("backward_preflight") + 1] == "dataset_validation"
    assert state["optimizer_creations"] == state["step_calls"] == 0
    assert report["optimizer_steps_completed"] == 0
    assert report["vlm_training_performed"] is False
    assert report["native_exit_code"] == 1
    assert not (args.output_directory / "training-log.jsonl").exists()
