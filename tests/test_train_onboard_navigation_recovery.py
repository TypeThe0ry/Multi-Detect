"""Synthetic software stand-ins only: no CPU processor/model/GPU acceptance."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import train_onboard_navigation_recovery as trainer
from scripts.onboard_navigation_recovery_budget import build_recovery_draws


def schedule():
    pools = {
        name: [hashlib.sha256(f"fixture:{name}:{i}".encode()).hexdigest() for i in range(count)]
        for name, count in zip(trainer.CLASSES, (70, 6, 7, 3), strict=True)
    }
    targets = {sample_id: name for name, values in pools.items() for sample_id in values}
    return build_recovery_draws(pools, targets=targets, classify=lambda value: value)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_optimizer_steps": 280},
        {"max_optimizer_steps": 267.0},
        {"seed": 20261007},
        {"gradient_clip_norm": 1},
        {"rank": True},
    ],
)
def test_new_backend_budget_never_masquerades_as_old280(changes):
    trainer.RecoveryModelBudget().validate()
    with pytest.raises(ValueError):
        replace(trainer.RecoveryModelBudget(), **changes).validate()


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}'])
def test_strict_json_rejects_duplicates_and_nonfinite(raw):
    with pytest.raises(ValueError):
        trainer.strict_json(raw)


def write_prefix(path, draws, events):
    with path.open("x", encoding="utf-8") as stream:
        for index in range(events):
            stage = trainer.STAGES[index % 4]
            measurements = {name: 1 for name in trainer.STAGE_MEASUREMENTS[stage]}
            trainer.append_optimizer_event(stream, draws[index // 4], stage, **measurements)


@pytest.mark.parametrize(
    "events,counts,uncertain",
    [
        (0, (0, 0, 0, 0), False),
        (1, (1, 0, 0, 0), True),
        (2, (1, 1, 0, 0), True),
        (3, (1, 1, 1, 0), True),
        (4, (1, 1, 1, 1), False),
        (52, (13, 13, 13, 13), False),
        (1068, (267, 267, 267, 267), False),
    ],
)
def test_four_stage_partial_attempts_keep_separate_old13_ledger(
    tmp_path, events, counts, uncertain
):
    bound = schedule()
    path = tmp_path / "journal.jsonl"
    write_prefix(path, bound["draws"], events)
    actual = trainer.verify_journal(path, bound)
    assert [actual[f"optimizer_steps_{name}"] for name in trainer.STAGES] == list(counts)
    assert actual["optimizer_update_uncertain"] is uncertain
    assert actual["old_spent_attempts"] == 13
    assert actual["cumulative_campaign_attempts"] == 13 + counts[0]
    assert actual["complete"] is (events == 1068)
    if events != 1068:
        with pytest.raises(ValueError):
            trainer.verify_journal(path, bound, require_complete=True)


@pytest.mark.parametrize(
    "change",
    [
        "wrong_draw",
        "old_suffix",
        "stage",
        "extra",
        "bool_index",
        "nonfinite",
        "duplicate",
        "truncated",
        "extra_attempt",
    ],
)
def test_journal_strict_order_types_bytes_and_no_suffix(tmp_path, change):
    bound = schedule()
    row = {
        "contract": trainer.JOURNAL_CONTRACT,
        **bound["draws"][0],
        "stage": "attempted",
        "loss": 1.0,
        "gradient_norm": 1.0,
    }
    if change == "wrong_draw":
        row["sample_id"] = bound["draws"][1]["sample_id"]
    elif change == "old_suffix":
        row["source_draw_index"] = 14
    elif change == "stage":
        row["stage"] = "returned"
    elif change == "extra":
        row["arbitrary"] = 0
    elif change == "bool_index":
        row["local_draw_index"] = True
    elif change == "nonfinite":
        row["loss"] = float("nan")
    raw = json.dumps(row).encode() + b"\n"
    if change == "duplicate":
        raw = raw.replace(b'"stage": "attempted"', b'"stage":"attempted","stage":"attempted"')
    elif change == "truncated":
        raw = raw[:-1]
    elif change == "extra_attempt":
        path = tmp_path / "journal.jsonl"
        write_prefix(path, bound["draws"], 1068)
        raw = path.read_bytes() + raw
    path = tmp_path / "tampered.jsonl"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        trainer.verify_journal(path, bound)


@pytest.mark.parametrize(
    "changes",
    [
        {"loss": True},
        {"loss": float("inf")},
        {"loss": -1},
        {"elapsed_s": "1"},
        {"peak_reserved_bytes": 1.0},
        {"unbounded": 1},
    ],
)
def test_append_rejects_invalid_measurements_before_write(tmp_path, changes):
    path = tmp_path / "journal.jsonl"
    with path.open("x", encoding="utf-8") as stream, pytest.raises(ValueError):
        trainer.append_optimizer_event(stream, schedule()["draws"][0], "attempted", **changes)
    assert path.read_bytes() == b""


class Scalar:
    def __init__(self, value=1.0, events=None):
        self.value, self.events = value, events

    def item(self):
        return self.value

    def detach(self):
        return self

    def backward(self):
        self.events.append("backward")


def fixtures(failure=None):
    events, steps = [], []

    class Optimizer:
        state = {}

        def zero_grad(self, **kwargs):
            events.append("zero_grad")

        def step(self):
            events.append("step")
            steps.append(1)
            if failure == "step":
                raise RuntimeError("fixture step failed")

    def adamw(parameters, **kwargs):
        assert kwargs == {
            "lr": 2e-5,
            "weight_decay": 0.0,
            "betas": (0.9, 0.999),
            "eps": 1e-8,
            "amsgrad": False,
            "foreach": False,
            "fused": False,
        }
        events.append("fresh_optimizer")
        return Optimizer()

    def synchronize():
        events.append("synchronize")
        if failure == "synchronize" and steps:
            raise RuntimeError("fixture synchronize failed")

    torch = SimpleNamespace(
        optim=SimpleNamespace(AdamW=adamw),
        nn=SimpleNamespace(utils=SimpleNamespace(clip_grad_norm_=lambda *a: Scalar(2.0))),
        cuda=SimpleNamespace(synchronize=synchronize, reset_peak_memory_stats=lambda: None),
        isfinite=lambda value: Scalar(True),
    )
    model = SimpleNamespace(parameters=lambda: [SimpleNamespace(requires_grad=True)])

    def loss(encoded):
        assert encoded["verified"] is True
        events.append("loss")
        if failure == "loss":
            raise RuntimeError("fixture loss failed")
        return Scalar(1.0, events)

    backend = SimpleNamespace(torch=torch, model=model, loss=loss)
    audit = {
        "trainable_dtypes": ["torch.float32"],
        "frozen_base_dtypes": ["torch.bfloat16"],
        "all_base_and_vision_parameters_frozen": True,
        "trainable_parameters": [{} for _ in range(112)],
    }
    backend.trainable_audit = audit

    class Inputs:
        count = 0

        def get(self, sample_id):
            self.count += 1
            events.append("source_exit_verified_metadata_equal")
            if failure == "encode" or failure == "draw14" and self.count == 14:
                raise MemoryError("fixture closed admission failed")
            return {"verified": True}

    pilot = SimpleNamespace(
        core=SimpleNamespace(audit_trainable_parameters=lambda model: audit),
        finite_gradients=lambda backend: events.append("finite_gradients"),
        check_memory=lambda *a: {"peak_allocated_bytes": 1024, "peak_reserved_bytes": 2048},
    )
    return backend, Inputs(), pilot, events, steps


def test_all267_order_no_old13_journal_and_checkpoint_each8(tmp_path):
    backend, inputs, pilot, events, steps = fixtures()
    saved = []

    def saver(backend, pilot, budget, directory, completed, path, bound):
        actual = trainer.verify_journal(path, bound)
        assert actual["optimizer_steps_completed"] == completed
        assert actual["optimizer_update_uncertain"] is False
        saved.append(completed)
        return {"fixture_completed": completed}

    report = {}
    trainer.train_draws(
        backend,
        inputs,
        schedule(),
        trainer.RecoveryModelBudget(),
        tmp_path,
        report,
        pilot,
        saver=saver,
    )
    assert len(steps) == 267
    assert saved == list(range(8, 268, 8))
    assert report["cumulative_campaign_attempts"] == 280
    assert report["fresh_optimizer_initial_state_empty"] is True
    assert report["journal"]["complete"] is True
    assert report["optimizer_api_calls_started"] == 267
    assert report["optimizer_update_uncertain"] is False
    assert events[:4] == [
        "fresh_optimizer",
        "source_exit_verified_metadata_equal",
        "zero_grad",
        "loss",
    ]
    draws = [
        trainer.strict_json(line)
        for line in (tmp_path / "draw-events.jsonl").read_bytes().splitlines()
    ]
    assert [row["event"] for row in draws] == [
        value for _ in range(267) for value in ("draw_start", "draw_end")
    ]
    assert draws[0]["local_draw_index"] == 1 and draws[-1]["local_draw_index"] == 267


@pytest.mark.parametrize(
    "failure,counts,steps",
    [
        ("encode", (0, 0, 0, 0), 0),
        ("loss", (0, 0, 0, 0), 0),
        ("step", (1, 0, 0, 0), 1),
        ("synchronize", (1, 1, 0, 0), 1),
        ("draw14", (13, 13, 13, 13), 13),
    ],
)
def test_fail_before_loss_and_step_uncertainty_no_reset(tmp_path, failure, counts, steps):
    backend, inputs, pilot, events, calls = fixtures(failure)
    report = {}
    with pytest.raises((RuntimeError, MemoryError)):
        trainer.train_draws(
            backend,
            inputs,
            schedule(),
            trainer.RecoveryModelBudget(),
            tmp_path,
            report,
            pilot,
            saver=lambda *a: {"fixture": True},
        )
    assert len(calls) == steps
    assert [report[f"optimizer_steps_{name}"] for name in trainer.STAGES] == list(counts)
    assert report["cumulative_campaign_attempts"] == 13 + counts[0]
    assert report["optimizer_update_uncertain"] is (counts[0] > counts[3])
    if failure == "encode":
        assert "loss" not in events
    actual = trainer.verify_journal(tmp_path / "training-events.jsonl", schedule())
    assert actual["optimizer_steps_attempted"] == counts[0]
    assert actual["optimizer_update_uncertain"] is report["optimizer_update_uncertain"]
    logs = [
        trainer.strict_json(line)
        for line in (tmp_path / "draw-events.jsonl").read_bytes().splitlines()
    ]
    assert logs[-1]["event"] == "draw_error"


def test_checkpoint_failure_does_not_invent_uncertain_optimizer_update(tmp_path):
    backend, inputs, pilot, _, steps = fixtures()

    def fail(*args):
        raise OSError("fixture checkpoint failed")

    report = {}
    with pytest.raises(OSError):
        trainer.train_draws(
            backend,
            inputs,
            schedule(),
            trainer.RecoveryModelBudget(),
            tmp_path,
            report,
            pilot,
            saver=fail,
        )
    assert len(steps) == 8 and report["optimizer_steps_completed"] == 8
    assert report["optimizer_update_uncertain"] is False
    assert report["checkpoints"] == []


def test_closed_inputs_supplies_full_expected_metadata_and_no_cache(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "scripts.onboard_navigation_closed_sample_encoding",
        SimpleNamespace(encode_closed_navigation_sample=lambda *a, **k: None),
    )
    ids = ["a" * 64]
    prepared, refs = {ids[0]: {"full": "fixture"}}, {ids[0]: {"prefix": "fixture"}}
    calls = []

    def encoder(sample, root, **kwargs):
        calls.append(kwargs)
        return {"fresh": len(calls)}

    backend = SimpleNamespace(processor=object(), torch=object())
    inputs = trainer.ClosedInputs(
        [SimpleNamespace(sample_id=ids[0])],
        prepared,
        refs,
        trainer.RecoveryModelBudget(),
        backend,
        encoder=encoder,
    )
    assert inputs.get(ids[0]) != inputs.get(ids[0])
    assert inputs.accesses == 2 and calls[0]["expected_metadata"] is prepared[ids[0]]
    assert calls[0]["prefix_reference"] is refs[ids[0]]


def test_cli_import_is_stdlib_only_no_gpu_or_optimizer(tmp_path):
    source = Path(trainer.__file__).resolve()
    script = (
        f"import runpy,sys;sys.path.insert(0,{str(source.parents[1])!r});"
        f"runpy.run_path({str(source)!r},run_name='fixture_no_main');"
        "assert not (set(sys.modules)&{'torch','transformers','PIL','peft','numpy','pyarrow'})"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage", trainer.STAGES)
def test_stage_specific_metrics_cannot_be_omitted_or_moved(tmp_path, stage):
    path = tmp_path / "stage.jsonl"
    draws = schedule()["draws"]
    preceding = trainer.STAGES.index(stage)
    write_prefix(path, draws, preceding)
    row = {
        "contract": trainer.JOURNAL_CONTRACT,
        **draws[0],
        "stage": stage,
        **{name: 1 for name in trainer.STAGE_MEASUREMENTS[stage]},
    }
    if trainer.STAGE_MEASUREMENTS[stage]:
        del row[next(iter(trainer.STAGE_MEASUREMENTS[stage]))]
    else:
        row["loss"] = 1
    with path.open("ab") as stream:
        stream.write(trainer.canonical(row) + b"\n")
    with pytest.raises(ValueError):
        trainer.verify_journal(path, schedule())


def test_parameter_audit_fail_precedes_optimizer_creation(tmp_path):
    backend, inputs, pilot, events, _ = fixtures()
    backend.trainable_audit["trainable_dtypes"] = ["torch.bfloat16"]
    with pytest.raises(ValueError):
        trainer.train_draws(
            backend, inputs, schedule(), trainer.RecoveryModelBudget(), tmp_path, {}, pilot
        )
    assert events == [] and list(tmp_path.iterdir()) == []


def cpu_prerequisite(tmp_path):
    report = {
        "contract": trainer.CONTRACT,
        "phase": "admit-cpu",
        "native_exit_code": 0,
        "plan_sha256": "a" * 64,
        "source_bindings_unchanged": True,
        "sources_before": [{"fixture": "source"}],
        "sources_after": [{"fixture": "source"}],
        **dict.fromkeys(trainer.FALSE_CLAIMS, False),
        "closed_CPU86_accepted": True,
        "CPU_acceptance_report": {"fixture": "CPU report SHA binding"},
        "model_loaded": False,
        "optimizer_performed": False,
        **dict.fromkeys((f"optimizer_steps_{stage}" for stage in trainer.STAGES), 0),
    }
    directory = tmp_path / "admit-cpu"
    directory.mkdir()
    trainer.create_json(directory / "report.json", report)
    return {
        "cpu_report": report["CPU_acceptance_report"],
        "schedule": schedule(),
        "expected_base_parameter_sha256": "b" * 64,
        "expected_warm_start_parameter_sha256": "c" * 64,
    }, report


@pytest.mark.parametrize(
    "change", ["native", "boolnative", "source", "CUDAmodel", "step", "CPUbind", "claim"]
)
def test_completed_cpu_phase_gate_before_gpu_load(tmp_path, change):
    plan, report = cpu_prerequisite(tmp_path)
    if change == "native":
        report["native_exit_code"] = 1
    elif change == "boolnative":
        report["native_exit_code"] = False
    elif change == "source":
        report["sources_after"] = None
    elif change == "CUDAmodel":
        report["model_loaded"] = True
    elif change == "step":
        report["optimizer_steps_attempted"] = 1
    elif change == "CPUbind":
        report["CPU_acceptance_report"] = {"fixture": "substituted"}
    else:
        report["candidate_promoted"] = True
    (tmp_path / "admit-cpu/report.json").write_bytes(trainer.canonical(report))
    with pytest.raises(ValueError):
        trainer.require_phase_prerequisites(
            tmp_path, "train", plan, "a" * 64, expected_sources=[{"fixture": "source"}]
        )


def test_reload_refuses_native0_train_with_partial267_journal(tmp_path):
    plan, _ = cpu_prerequisite(tmp_path)
    train = tmp_path / "train"
    train.mkdir()
    write_prefix(train / "training-events.jsonl", plan["schedule"]["draws"], 52)
    report = {
        "contract": trainer.CONTRACT,
        "phase": "train",
        "native_exit_code": 0,
        "plan_sha256": "a" * 64,
        "source_bindings_unchanged": True,
        "sources_before": [{"fixture": "source"}],
        "sources_after": [{"fixture": "source"}],
        **dict.fromkeys(trainer.FALSE_CLAIMS, False),
        "training_completed": True,
    }
    trainer.create_json(train / "report.json", report)
    with pytest.raises(ValueError):
        trainer.require_phase_prerequisites(
            tmp_path, "reload", plan, "a" * 64, expected_sources=[{"fixture": "source"}]
        )


def test_saved_checkpoint_digest_and_immutable_journal_snapshot(tmp_path, monkeypatch):
    bound = schedule()
    journal = tmp_path / "live.jsonl"
    write_prefix(journal, bound["draws"], 32)

    def save(path, **kwargs):
        assert kwargs == {"safe_serialization": True}
        directory = Path(path)
        (directory / "adapter_model.safetensors").write_bytes(b"fixture weights")
        trainer.create_json(directory / "adapter_config.json", {"fixture": "config"})

    model = SimpleNamespace(save_pretrained=save)
    backend = SimpleNamespace(model=model, trainable_audit={"fixture": "audit"})
    pilot = SimpleNamespace(
        core=SimpleNamespace(parameter_digest=lambda *a, **k: "d" * 64),
        recovery=SimpleNamespace(
            verify_config=lambda *a: None, saved_parameter_digest=lambda *a, **k: "d" * 64
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "safetensors.torch",
        SimpleNamespace(load_file=lambda *a, **k: {"fixture": "state"}),
    )
    directory = tmp_path / "checkpoint-008"
    receipt = trainer.save_checkpoint(
        backend, pilot, trainer.RecoveryModelBudget(), directory, 8, journal, bound
    )
    assert receipt["completed_new_updates"] == 8 and receipt["cumulative_campaign_attempts"] == 21
    assert receipt["optimizer_state_saved"] is False and receipt["exact_resume"] is False
    copy_binding = receipt["journal"]
    with journal.open("a", encoding="utf-8") as stream:
        trainer.append_optimizer_event(
            stream, bound["draws"][8], "attempted", loss=1.0, gradient_norm=1.0
        )
    assert trainer.record(copy_binding["path"]) == copy_binding
    assert trainer.record(journal)["sha256"] != copy_binding["sha256"]
    with pytest.raises(FileExistsError):
        trainer.save_checkpoint(
            backend, pilot, trainer.RecoveryModelBudget(), directory, 8, journal, bound
        )


def test_actual_retained_semantic_metadata_binds86_without_reencoding():
    original_path = (
        trainer.ROOT / "configs/training/onboard-vlm-navigation-pilot-plan-20261007-r1.json"
    )
    prepared_path = trainer.ROOT / trainer.HISTORY["prepared_inputs"][0]
    if not original_path.is_file() or not prepared_path.is_file():
        pytest.skip("Runtime metadata artifacts are absent in a clean clone")
    original = trainer.strict_json(original_path.read_bytes())
    prepared = trainer.read_bound(trainer.record(prepared_path))
    assert trainer.record(prepared_path)["sha256"] == trainer.HISTORY["prepared_inputs"][1]
    data = trainer.bound_path(original["dataset_samples"])
    assert trainer.record(data) == {**original["dataset_samples"], "path": str(data)}
    rows = [trainer.strict_json(line) for line in data.read_bytes().splitlines()]
    targets, pools = trainer.targets_from_rows(rows, original["train_sample_ids"], prepared)
    drawn = build_recovery_draws(pools, targets=targets, classify=trainer.classify)
    assert drawn["unique_draw_samples"] == 86 and drawn["draw_count"] == 267
    assert drawn["draw_class_counts"] == dict(zip(trainer.CLASSES, (70, 66, 66, 65), strict=True))


def test_prior_equal_source_arrays_cannot_replace_current_inventory(tmp_path):
    plan, report = cpu_prerequisite(tmp_path)
    with pytest.raises(ValueError):
        trainer.require_phase_prerequisites(
            tmp_path,
            "train",
            plan,
            "a" * 64,
            expected_sources=[{"fixture": "different current source"}],
        )
    trainer.require_phase_prerequisites(
        tmp_path, "train", plan, "a" * 64, expected_sources=report["sources_before"]
    )


def test_failed_durable_intent_never_calls_optimizer_and_counts_conservatively(
    tmp_path, monkeypatch
):
    backend, inputs, pilot, _, steps = fixtures()

    def fail(*args, **kwargs):
        raise OSError("fixture fsync failed before step")

    monkeypatch.setattr(trainer, "append_optimizer_event", fail)
    report = {}
    with pytest.raises(OSError):
        trainer.train_draws(
            backend, inputs, schedule(), trainer.RecoveryModelBudget(), tmp_path, report, pilot
        )
    assert steps == [] and report["optimizer_api_calls_started"] == 0
    assert report["optimizer_steps_attempted"] == 1
    assert (
        report["cumulative_campaign_attempts"] == 14
        and report["optimizer_update_uncertain"] is True
    )


def test_malformed_schedule_fails_before_optimizer_or_loss(tmp_path):
    backend, inputs, pilot, events, _ = fixtures()
    bound = schedule()
    bound["draws"][0]["local_draw_index"] = 14
    with pytest.raises(ValueError):
        trainer.train_draws(
            backend, inputs, bound, trainer.RecoveryModelBudget(), tmp_path, {}, pilot
        )
    assert events == [] and list(tmp_path.iterdir()) == []
