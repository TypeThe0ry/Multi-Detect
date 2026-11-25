"""Injected bookkeeping fixtures only: these are not actual training evidence."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_onboard_navigation_balanced_schedule as builder
from scripts import onboard_navigation_balanced_journal as journal
from scripts.train_onboard_navigation_balanced import BalancedTrainingBudget


@pytest.fixture
def draws():
    samples, targets = [], {}
    for name, count in builder.SOURCE_COUNTS.items():
        for index in range(count):
            sample_id = f"injected-{name}-{index}"
            samples.append(
                SimpleNamespace(
                    sample_id=sample_id,
                    partition="train",
                    scene_id="injected-scene",
                    source=SimpleNamespace(original_split="train"),
                )
            )
            targets[sample_id] = name
    rows, _ = builder.build_draws(
        samples,
        targets,
        [sample.sample_id for sample in samples],
        seed=20261007,
        classify=lambda value: value,
    )
    return rows


@pytest.fixture
def schedule(draws):
    exposures = {}
    for row in draws:
        exposures[row["sample_id"]] = row["per_sample_exposure"]
    plan = {"path": "configs/injected-plan.json", "bytes": 1, "sha256": "a" * 64}
    dataset = {"path": "artifacts/injected-samples.jsonl", "bytes": 1, "sha256": "b" * 64}
    return {
        "schema_version": 1,
        "contract": journal.SCHEDULE_CONTRACT,
        "status": "balanced_draw_schedule_prepared_not_optimizer_authorized_or_executed",
        "source_plan": plan,
        "dataset_samples": dataset,
        "source_bindings": [plan, dataset],
        "seed": 20261007,
        "sampling_algorithm": "seeded_class_cycles_then_shuffled_class_slots_v1",
        "source_class_counts": dict(builder.SOURCE_COUNTS),
        "draw_class_counts": dict.fromkeys(builder.CLASSES, 70),
        "source_unique_samples": 86,
        "draw_count": 280,
        "unique_draw_samples": 86,
        "per_sample_exposure": exposures,
        "scene_count": 1,
        "scene_ids": ["injected-scene"],
        "episode_count": 3,
        "partition_counts": {"train": 86},
        "original_optimizer_budget": {
            **asdict(BalancedTrainingBudget()),
            "max_optimizer_steps": 100,
            "gradient_clip_norm": 1,
        },
        "actor_input": "unchanged_original_prompt_current_and_at_most_latest_past_rgb",
        "draws": draws,
        **dict.fromkeys(journal.FALSE_FLAGS, False),
        **dict.fromkeys(journal.TRUE_FLAGS, True),
    }


def event(draw, stage):
    measurements = {
        "attempted": {"loss": 0.25, "gradient_norm": 0.5},
        "returned": {},
        "synchronized": {},
        "completed": {"elapsed_s": 1.5, "peak_allocated_bytes": 1024, "peak_reserved_bytes": 2048},
    }
    return {
        "schema_version": 1,
        "contract": journal.JOURNAL_CONTRACT,
        **draw,
        "stage": stage,
        **measurements[stage],
    }


def events(draws):
    return [event(draw, stage) for draw in draws for stage in journal.STAGES]


def write(path, rows):
    path.write_bytes(
        "".join(json.dumps(row, allow_nan=False) + "\n" for row in rows).encode("utf-8")
    )


def test_schedule_and_new_budget_admit_injected_balanced_draws(schedule, draws):
    assert journal.validate_balanced_schedule(schedule) is schedule
    budget = asdict(BalancedTrainingBudget())
    assert journal.validate_balanced_budget(budget, draws) is budget
    assert budget == journal.BALANCED_OPTIMIZATION
    assert budget["max_optimizer_steps"] == 280
    assert schedule["original_optimizer_budget"]["max_optimizer_steps"] == 100


def test_actual_frozen_schedule_static_admission_is_not_training_evidence():
    path = Path(journal.__file__).resolve().parents[1] / (
        "artifacts/training/onboard-navigation-balanced-schedule-20261007-r2/schedule.json"
    )
    document = json.loads(path.read_bytes())
    assert type(document["original_optimizer_budget"]["gradient_clip_norm"]) is int
    journal.validate_balanced_schedule(document)
    journal.validate_balanced_budget(asdict(BalancedTrainingBudget()), document["draws"])


@pytest.mark.parametrize("key", tuple(journal.BALANCED_OPTIMIZATION))
def test_every_budget_value_is_exact_not_a_relaxed_ceiling(key, draws):
    budget = asdict(BalancedTrainingBudget())
    value = budget[key]
    budget[key] = not value if type(value) is bool else value * 2
    with pytest.raises(ValueError, match="exact declared"):
        journal.validate_balanced_budget(budget, draws)


@pytest.mark.parametrize("mutation", ["old100", "bool", "int_float", "extra", "missing"])
def test_new_budget_does_not_accept_old_budget_or_json_type_coercion(mutation, draws):
    budget = asdict(BalancedTrainingBudget())
    if mutation == "old100":
        budget["max_optimizer_steps"] = 100
    elif mutation == "bool":
        budget["epochs"] = True
    elif mutation == "int_float":
        budget["gradient_clip_norm"] = 1
    elif mutation == "extra":
        budget["optimizer_state_restored"] = False
    else:
        del budget["seed"]
    with pytest.raises(ValueError):
        journal.validate_balanced_budget(budget, draws)


@pytest.mark.parametrize("key", tuple(journal.DRAW_FIELDS))
def test_every_bound_draw_field_is_checked(key, schedule):
    row = schedule["draws"][0]
    row[key] = "unknown" if type(row[key]) is str else row[key] + 1
    with pytest.raises(ValueError):
        journal.validate_balanced_schedule(schedule)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_in_cycle",
        "class_migration",
        "bool_index",
        "extra_draw_field",
        "draw_279",
        "draw_281",
        "count",
        "source_count",
        "exposure",
        "exposure_bool",
        "flag",
        "flag_int",
        "partition",
        "scene",
        "binding_missing",
        "binding_duplicate",
        "binding_extra",
        "binding_escape",
        "binding_sha",
        "binding_bool_bytes",
        "extra",
        "missing",
        "seed",
    ],
)
def test_schedule_rejects_identity_counts_structure_and_authority_tampering(mutation, schedule):
    if mutation == "duplicate_in_cycle":
        schedule["draws"][1]["sample_id"] = schedule["draws"][0]["sample_id"]
        schedule["draws"][1]["per_sample_exposure"] = 2
    elif mutation == "class_migration":
        schedule["draws"][2]["sample_id"] = schedule["draws"][0]["sample_id"]
        schedule["draws"][2]["per_sample_exposure"] = 2
    elif mutation == "bool_index":
        schedule["draws"][0]["draw_index"] = True
    elif mutation == "extra_draw_field":
        schedule["draws"][0]["truth_for_actor"] = True
    elif mutation == "draw_279":
        schedule["draws"].pop()
    elif mutation == "draw_281":
        schedule["draws"].append(copy.deepcopy(schedule["draws"][-1]))
    elif mutation == "count":
        schedule["draw_class_counts"]["left"] = 69
    elif mutation == "source_count":
        schedule["source_class_counts"]["left"] = 7
    elif mutation in ("exposure", "exposure_bool"):
        sample_id = schedule["draws"][0]["sample_id"]
        schedule["per_sample_exposure"][sample_id] = 0 if mutation == "exposure" else True
    elif mutation in ("flag", "flag_int"):
        schedule["optimizer_authorized"] = True if mutation == "flag" else 0
    elif mutation == "partition":
        schedule["partition_counts"] = {"test": 86}
    elif mutation == "scene":
        schedule["scene_ids"].append("other")
    elif mutation == "binding_missing":
        schedule["source_bindings"].pop()
    elif mutation == "binding_duplicate":
        schedule["source_bindings"].append(copy.deepcopy(schedule["source_bindings"][0]))
    elif mutation == "binding_extra":
        schedule["source_bindings"][0]["extra"] = 1
    elif mutation == "binding_escape":
        schedule["source_bindings"][0]["path"] = "../outside"
    elif mutation == "binding_sha":
        schedule["source_bindings"][0]["sha256"] = "A" * 64
    elif mutation == "binding_bool_bytes":
        schedule["source_bindings"][0]["bytes"] = True
    elif mutation == "extra":
        schedule["resume_optimizer"] = True
    elif mutation == "missing":
        del schedule["draw_count"]
    else:
        schedule["seed"] = True
    with pytest.raises(ValueError):
        journal.validate_balanced_schedule(schedule)


@pytest.mark.parametrize("prefix", [0, 1, 2, 3, 4, 5, 15, 1119, 1120])
def test_partial_counts_are_truthful_and_uncertainty_survives_synchronization(
    tmp_path,
    draws,
    prefix,
):
    path = tmp_path / "injected-events.jsonl"
    write(path, events(draws)[:prefix])
    result = journal.verify_journal(path, draws)
    completed, remainder = divmod(prefix, 4)
    for index, stage in enumerate(journal.STAGES):
        assert result[f"optimizer_steps_{stage}"] == completed + (remainder > index)
    assert result["event_count"] == prefix
    assert result["last_phase"] == (journal.STAGES[(prefix - 1) % 4] if prefix else None)
    assert result["optimizer_update_uncertain"] == bool(remainder)
    assert result["complete"] == (prefix == 1120)
    assert not {"loss", "gradient_norm", "elapsed_s"} & result.keys()
    assert journal.read_journal is journal.verify_journal
    if prefix < 1120:
        with pytest.raises(ValueError, match="280 completed"):
            journal.verify_journal(path, draws, require_complete=True)
    else:
        assert journal.verify_journal(path, draws, require_complete=True)["complete"]


def test_missing_journal_is_not_invented_zero_success(tmp_path, draws):
    with pytest.raises(FileNotFoundError):
        journal.verify_journal(tmp_path / "absent.jsonl", draws)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "skip",
        "reorder",
        "next_draw",
        "wrong_sample",
        "wrong_exposure",
        "wrong_class_cycle",
        "extra",
        "missing_measurement",
        "stage_measurement",
        "schema_bool",
        "contract",
        "draw_bool",
        "unknown_stage",
        "overflow_events",
    ],
)
def test_journal_rejects_order_and_exact_bound_draw_tampering(tmp_path, draws, mutation):
    rows = events(draws)[:8]
    if mutation == "duplicate":
        rows.insert(1, copy.deepcopy(rows[0]))
    elif mutation == "skip":
        rows.pop(1)
    elif mutation == "reorder":
        rows[1], rows[2] = rows[2], rows[1]
    elif mutation == "next_draw":
        rows[1] = event(draws[1], "attempted")
    elif mutation == "wrong_sample":
        rows[0]["sample_id"] = draws[1]["sample_id"]
    elif mutation == "wrong_exposure":
        rows[0]["per_sample_exposure"] += 1
    elif mutation == "wrong_class_cycle":
        rows[0]["class_cycle"] += 1
    elif mutation == "extra":
        rows[0]["extra"] = 1
    elif mutation == "missing_measurement":
        del rows[0]["loss"]
    elif mutation == "stage_measurement":
        rows[1]["loss"] = 0.25
    elif mutation == "schema_bool":
        rows[0]["schema_version"] = True
    elif mutation == "contract":
        rows[0]["contract"] = "old-journal"
    elif mutation == "draw_bool":
        rows[0]["draw_index"] = True
    elif mutation == "unknown_stage":
        rows[0]["stage"] = "step_succeeded"
    else:
        rows = events(draws) + [event(draws[-1], "attempted")]
    path = tmp_path / "tampered.jsonl"
    write(path, rows)
    with pytest.raises(ValueError):
        journal.verify_journal(path, draws)


@pytest.mark.parametrize(
    "field,value,stage",
    [
        ("loss", -1, "attempted"),
        ("loss", True, "attempted"),
        ("loss", 1e7, "attempted"),
        ("loss", float("nan"), "attempted"),
        ("gradient_norm", float("inf"), "attempted"),
        ("gradient_norm", "0.5", "attempted"),
        ("gradient_norm", 10**400, "attempted"),
        ("elapsed_s", -1, "completed"),
        ("elapsed_s", float("inf"), "completed"),
        ("elapsed_s", 86401, "completed"),
        ("peak_allocated_bytes", 1.0, "completed"),
        ("peak_reserved_bytes", True, "completed"),
        ("peak_reserved_bytes", -1, "completed"),
        ("peak_reserved_bytes", 8053063681, "completed"),
        ("peak_allocated_bytes", 2049, "completed"),
    ],
)
def test_append_rejects_unbounded_measurements_before_any_write(
    tmp_path, draws, field, value, stage
):
    path = tmp_path / "injected-events.jsonl"
    row = event(draws[0], stage)
    measurements = {key: row[key] for key in journal.MEASUREMENT_FIELDS[stage]}
    measurements[field] = value
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        with pytest.raises(ValueError):
            journal.append_journal_event(stream, draws[0], stage, **measurements)
    assert path.read_bytes() == b""


@pytest.mark.parametrize(
    "bad",
    [
        b"\n",
        b"{}\n",
        b"[]\n",
        b"null\n",
        b"{} {}\n",
        b"\xff\n",
        b'{"stage":"attempted","stage":"returned"}\n',
        b"{}\r\n",
        b'{"loss":NaN}\n',
        b'{"loss":Infinity}\n',
        b'{"loss":-Infinity}\n',
        b" " * 2049 + b"\n",
    ],
)
def test_strict_json_rejects_invalid_truncated_duplicate_and_nonfinite_lines(tmp_path, draws, bad):
    path = tmp_path / "injected-events.jsonl"
    first = json.dumps(event(draws[0], "attempted")).encode("utf-8")
    path.write_bytes(first + b"\n" + bad)
    with pytest.raises(ValueError):
        journal.verify_journal(path, draws)


def test_valid_json_without_final_newline_is_truncated_not_a_completed_event(tmp_path, draws):
    path = tmp_path / "injected-events.jsonl"
    path.write_bytes(json.dumps(event(draws[0], "attempted")).encode("utf-8"))
    with pytest.raises(ValueError, match="truncated"):
        journal.verify_journal(path, draws)


@pytest.mark.parametrize("mode", ["x", "xb"])
def test_each_of_four_stages_flushes_then_fsyncs_a_complete_line(
    tmp_path, draws, monkeypatch, mode
):
    path = tmp_path / "injected-events.jsonl"
    sizes = []
    native_fsync = journal.os.fsync

    def observed_fsync(fd):
        native_fsync(fd)
        payload = path.read_bytes()
        assert payload.endswith(b"\n")
        sizes.append(len(payload.splitlines()))

    monkeypatch.setattr(journal.os, "fsync", observed_fsync)
    options = {"encoding": "utf-8", "newline": "\n"} if mode == "x" else {}
    with path.open(mode, **options) as stream:
        for stage in journal.STAGES:
            row = event(draws[0], stage)
            journal.append_journal_event(
                stream,
                draws[0],
                stage,
                **{key: row[key] for key in journal.MEASUREMENT_FIELDS[stage]},
            )
    assert sizes == [1, 2, 3, 4]
    assert journal.verify_journal(path, draws)["optimizer_steps_completed"] == 1


def test_measurement_keywords_cannot_override_bound_draw_metadata(tmp_path, draws):
    with (tmp_path / "injected.jsonl").open("x", encoding="utf-8") as stream:
        with pytest.raises(ValueError, match="measurement fields"):
            journal.append_journal_event(
                stream, draws[0], "attempted", loss=0.1, gradient_norm=0.2, sample_id="overridden"
            )


def test_fsync_failure_is_not_silently_reported_as_durability(tmp_path, draws, monkeypatch):
    def failure(_fd):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(journal.os, "fsync", failure)
    with (tmp_path / "injected.jsonl").open("x", encoding="utf-8") as stream:
        with pytest.raises(OSError, match="injected fsync"):
            journal.append_journal_event(stream, draws[0], "attempted", loss=0.1, gradient_norm=0.2)


def test_isolated_import_is_stdlib_only_without_torch_or_source_side_effects(tmp_path):
    module = Path(journal.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('isolated_journal',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not {'torch','transformers','peft','PIL'} & sys.modules.keys(); "
        "assert module.DRAW_COUNT == 280"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(module)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert list(tmp_path.iterdir()) == []
