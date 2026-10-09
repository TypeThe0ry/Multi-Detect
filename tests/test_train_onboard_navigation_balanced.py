"""Injected CPU/stdlib regression tests; not actual model or optimizer evidence."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import train_onboard_navigation_balanced as runner


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("max_optimizer_steps", 100),
        ("max_optimizer_steps", 200),
        ("max_optimizer_steps", True),
        ("seed", 1),
        ("rank", 8),
        ("alpha", 16),
        ("learning_rate", 0.0),
        ("gradient_checkpointing", 1),
        ("max_input_tokens", 5000),
        ("max_new_tokens", 1000),
        ("gradient_clip_norm", 0.0),
        ("max_cuda_reserved_bytes", 9999999999),
    ],
)
def test_budget_is_new_explicit_280_not_old_validator(field, bad):
    runner.BalancedTrainingBudget().validate()
    with pytest.raises(ValueError):
        replace(runner.BalancedTrainingBudget(), **{field: bad}).validate()


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', "NaN", "Infinity", "-Infinity", '{"a":[1e999]}'])
def test_strict_json_rejects_ambiguous_or_nonfinite(raw):
    with pytest.raises(ValueError):
        runner.strict_json(raw)


def test_help_imports_no_gpu_or_repository_modules():
    command = [sys.executable, "-I", "-S", "-B", str(Path(runner.__file__)), "--help"]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert "--plan-sha256" in result.stdout
    assert "--worker" not in result.stdout


def test_create_only_and_source_tamper(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    path = tmp_path / "source.json"
    runner.create_json(path, {"a": 1})
    binding = runner.record(path)
    assert runner.read_bound(binding) == {"a": 1}
    with pytest.raises(FileExistsError):
        runner.create_json(path, {"a": 2})
    path.write_bytes(b'{"a":2}\n')
    with pytest.raises(ValueError, match="changed"):
        runner.read_bound(binding)
    with pytest.raises(ValueError):
        runner.binding_path({**binding, "bytes": True})


def test_output_direct_fresh_child_only(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    models = tmp_path / "artifacts/models"
    models.mkdir(parents=True)
    fresh = models / "onboard-navigation-balanced-fixture"
    assert runner.fresh_run_directory(fresh) == fresh
    for path in (
        models / "wrong",
        models / "old/onboard-navigation-balanced-fixture",
        tmp_path / "onboard-navigation-balanced-fixture",
    ):
        with pytest.raises(ValueError):
            runner.fresh_run_directory(path)
    fresh.mkdir()
    with pytest.raises(FileExistsError):
        runner.fresh_run_directory(fresh)


def process_fixture(tmp_path):
    exe = tmp_path / "python.exe"
    exe.touch()
    argv = [str(exe), "-I", "-B", "script.py", "--worker", "--phase", "train"]
    row = {
        "pid": 10,
        "parent_pid": 1,
        "executable": str(exe),
        "command_line": json.dumps(argv),
        "created_at": "2026-10-07T12:00:00+00:00",
    }
    snapshot = {"observed_at": "2026-10-07T12:00:02+00:00", "processes": [row]}
    return exe, argv, snapshot


def test_live_same_pid_or_venv_launcher_chain(tmp_path):
    exe, argv, snapshot = process_fixture(tmp_path)
    kwargs = {"supervisor_pid": 1, "parse_argv": json.loads}
    actual = runner.verify_process_observation(snapshot, 10, argv, [exe], **kwargs)
    assert actual["actual_worker_pid"] == 10
    child = {
        **snapshot["processes"][0],
        "pid": 11,
        "parent_pid": 10,
        "created_at": "2026-10-07T12:00:01+00:00",
    }
    snapshot["processes"].append(child)
    actual = runner.verify_process_observation(snapshot, 10, argv, [exe], **kwargs)
    assert actual["actual_worker_pid"] == 11


@pytest.mark.parametrize(
    "case",
    [
        "pid_bool",
        "parent",
        "argv",
        "exe",
        "future",
        "timezone",
        "duplicate",
        "extra",
        "child_parent",
    ],
)
def test_live_process_identity_rejects_unbound_data(tmp_path, case):
    exe, argv, snapshot = process_fixture(tmp_path)
    row = snapshot["processes"][0]
    if case == "pid_bool":
        row["pid"] = True
    elif case == "parent":
        row["parent_pid"] = 2
    elif case == "argv":
        row["command_line"] = json.dumps(argv + ["--unbound"])
    elif case == "exe":
        row["executable"] = str(tmp_path / "other.exe")
    elif case == "future":
        row["created_at"] = "2026-10-07T12:00:03+00:00"
    elif case == "timezone":
        snapshot["observed_at"] = "2026-10-07T12:00:02"
    elif case == "duplicate":
        snapshot["processes"].append(copy.deepcopy(row))
    elif case == "extra":
        row["extra"] = "unbound"
    else:
        snapshot["processes"].append({**row, "pid": 11, "parent_pid": 12})
    with pytest.raises(ValueError):
        runner.verify_process_observation(
            snapshot, 10, argv, [exe], supervisor_pid=1, parse_argv=json.loads
        )


def prepared_fixture(monkeypatch):
    tensors = {
        "input_ids": {"shape": [1, 3], "dtype": "torch.int64", "sha256": "a" * 64},
        "attention_mask": {"shape": [1, 3], "dtype": "torch.int64", "sha256": "b" * 64},
        "pixel_values": {"shape": [64, 12], "dtype": "torch.float32", "sha256": "c" * 64},
        "image_grid_thw": {"shape": [1, 3], "dtype": "torch.int64", "sha256": "d" * 64},
    }
    row = {
        "sample_id": "fixture",
        "actor_identity_sha256": "e" * 64,
        "training_prefix": {"prefix_tokens": 3, "tensors": tensors},
    }
    monkeypatch.setattr(runner, "read_bound", lambda _: {"samples": [row]})
    full = copy.deepcopy(tensors)
    full["input_ids"]["shape"] = full["attention_mask"]["shape"] = [1, 5]
    value = {
        "actor_identity_sha256": "e" * 64,
        "prefix_tensors": tensors,
        "prefix_tokens": 3,
        "total_tokens": 5,
        "response_tokens": 2,
        "supervised_tokens": 2,
        "prefix_reference_verified": True,
        "prompt_label_mask_verified": True,
        "input_tensors": full,
        "response_labels_sha256": "f" * 64,
        "target_sha256": "1" * 64,
    }
    plan = {"cpu_prefix_report": {}, "diagnostics": {"sample_ids": ["fixture"]}}
    return {"fixture": value}, plan


def test_prepared_metadata_matches_retained_prefix(monkeypatch):
    prepared, plan = prepared_fixture(monkeypatch)
    assert (
        runner.validate_prepared_inputs(prepared, plan, runner.BalancedTrainingBudget()) == prepared
    )


@pytest.mark.parametrize(
    "case",
    [
        "actor",
        "prefix",
        "labels",
        "tokens",
        "bool_count",
        "pixels",
        "missing",
        "extra_tensor",
        "target_sha",
    ],
)
def test_prepared_metadata_rejects_tamper(monkeypatch, case):
    prepared, plan = prepared_fixture(monkeypatch)
    value = prepared["fixture"]
    if case == "actor":
        value["actor_identity_sha256"] = "0" * 64
    elif case == "prefix":
        value["prefix_tensors"] = {}
    elif case == "labels":
        value["prompt_label_mask_verified"] = False
    elif case == "tokens":
        value["supervised_tokens"] = 1
    elif case == "bool_count":
        value["response_tokens"] = True
    elif case == "pixels":
        value["input_tensors"]["pixel_values"]["sha256"] = "0" * 64
    elif case == "missing":
        prepared.clear()
    elif case == "extra_tensor":
        value["input_tensors"]["extra"] = {}
    else:
        value["target_sha256"] = "bad"
    with pytest.raises(ValueError):
        runner.validate_prepared_inputs(prepared, plan, runner.BalancedTrainingBudget())


def test_parent_recomputes_diagnostic_not_self_report(monkeypatch):
    prepared, _ = prepared_fixture(monkeypatch)
    diagnostic = {
        "strict_json_valid": False,
        "action_class_correct": False,
        "metric_correct": False,
    }
    pilot = SimpleNamespace(action_class=lambda _: "left", diagnose=lambda raw, target: diagnostic)
    row = {
        "sample_id": "fixture",
        "generated": {
            "raw_output": '{"kind":"translate"}',
            "output_token_ids": [1, 2],
            "generated_tokens": 2,
            "token_cap_reached": False,
            "elapsed_s": 1.0,
        },
        "parent": False,
        "actor_identity_sha256": "e" * 64,
        "actual_prefix_tensors": prepared["fixture"]["prefix_tensors"],
        "gold_action_class": "left",
        "diagnostic": diagnostic.copy(),
    }
    summary = runner.diagnose_rows(
        [row], ["fixture"], prepared, {"fixture": {}}, pilot, parent=False
    )
    assert summary["json_syntax_valid"] == 1
    assert summary["strict_contract_valid"] == 0
    assert summary["confusion"]["left"]["invalid"] == 1
    row["diagnostic"]["strict_json_valid"] = True
    with pytest.raises(ValueError, match="independently"):
        runner.diagnose_rows([row], ["fixture"], prepared, {"fixture": {}}, pilot, parent=False)


@pytest.mark.parametrize(
    "field",
    [
        *runner.FALSE_CLAIMS,
        "optimizer_state_restored",
        "scheduler_state_restored",
        "rng_state_restored",
        "actual_live_camera_parity_verified",
        "optimizer_performed",
        "model_loaded",
        "inference_performed",
    ],
)
def test_cpu_phase_acceptance_rejects_model_or_flight_claim(tmp_path, field):
    report = {
        "contract": runner.CONTRACT,
        "phase": "cpu-encode",
        "native_exit_code": 0,
        "source_bindings_unchanged": True,
        "all86_full_chat_encoding_verified": True,
        **dict.fromkeys(runner.FALSE_CLAIMS, False),
        "optimizer_state_restored": False,
        "scheduler_state_restored": False,
        "rng_state_restored": False,
        "actual_live_camera_parity_verified": False,
        "optimizer_performed": False,
        "model_loaded": False,
        "inference_performed": False,
    }
    report[field] = True
    with pytest.raises(ValueError, match="scope"):
        runner.validate_phase_outputs(
            tmp_path, "cpu-encode", {}, runner.BalancedTrainingBudget(), report
        )


def test_metadata_never_persists_tensor_objects():
    assert runner.metadata(
        {"prefix": object(), "full": object(), "labels": object(), "prefix_tokens": 3}
    ) == {"prefix_tokens": 3}


def test_repository_imports_restores_sys_path_on_error():
    before = sys.path[:]
    with pytest.raises(RuntimeError), runner.repository_imports():
        assert str(runner.ROOT / "src") in sys.path
        raise RuntimeError("injected")
    assert sys.path == before


def test_optimizer_declaration_is_fresh():
    assert asdict(runner.BalancedTrainingBudget())["max_optimizer_steps"] == 280
    for key in (
        "optimizer_state_restored",
        "scheduler_state_restored",
        "rng_state_restored",
        "fused",
        "foreach",
        "amsgrad",
    ):
        assert runner.OPTIMIZER[key] is False
    assert runner.OPTIMIZER["weight_decay"] == 0.0


@pytest.mark.parametrize("case", ["missing", "changed", "duplicate"])
def test_mandatory_runtime_inventory_cannot_be_dropped(tmp_path, monkeypatch, case):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    source = tmp_path / "runtime.py"
    source.write_bytes(b"original")
    row = runner.record(source)
    declared = [row.copy()]
    required = {str(source): row}
    runner.require_source_inventory(declared, required)
    if case == "missing":
        declared.clear()
    elif case == "changed":
        declared[0]["sha256"] = "0" * 64
    else:
        declared.append(row.copy())
    with pytest.raises(ValueError):
        runner.require_source_inventory(declared, required)


def test_outer_cleanup_survives_hook_install_exception(tmp_path, monkeypatch):
    from scripts import run_onboard_navigation_pilot_streaming as streaming
    from scripts import run_onboard_navigation_pilot_streaming_no_warmup as body
    from scripts import train_onboard_vlm_navigation_pilot as pilot

    alias = object()
    safe = SimpleNamespace(safe_open=alias)
    original_warmup = object()
    mu = SimpleNamespace(caching_allocator_warmup=original_warmup)
    transformers = SimpleNamespace(modeling_utils=mu)
    monkeypatch.setitem(sys.modules, "safetensors", safe)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "transformers.modeling_utils", mu)
    monkeypatch.setattr(runner, "read_bound", lambda _: {})
    monkeypatch.setattr(runner, "binding_path", lambda row: tmp_path / row["path"])
    monkeypatch.setattr(streaming, "header_binding", lambda _: {})
    for name in ("base", "warm"):
        (tmp_path / name).write_bytes(b"fixture")

    def install(allowed, openings):
        safe.safe_open = object()
        return {}

    def fail_after_install(runtime):
        mu.caching_allocator_warmup = object()
        raise BrokenPipeError("after installing hook, before callback returned")

    monkeypatch.setattr(streaming, "install_streaming_loader", install)
    monkeypatch.setattr(body, "install_warmup_skip", fail_after_install)
    old_declarations = body.DECLARATIONS
    ctor = pilot.NavigationBackend.__init__
    report = {"phase": "baseline"}
    plan = {
        "runtime_reference": {},
        "source_files": [],
        "warm_start": {"weights": {"path": "warm"}},
    }
    old = {"base": {"safetensors": {"path": "base"}}}
    with pytest.raises(BrokenPipeError), runner.loading_scope(plan, old, tmp_path, report):
        pytest.fail("broken install must not enter body")
    assert safe.safe_open is alias
    assert mu.caching_allocator_warmup is original_warmup
    assert pilot.NavigationBackend.__init__ is ctor
    assert body.DECLARATIONS is old_declarations
    assert report["outer_hook_fallbacks_restored"] is True


@pytest.mark.parametrize(
    ("attempted", "returned", "completed", "performed", "uncertain"),
    [
        (0, 0, 0, False, False),
        (1, 0, 0, None, True),
        (1, 1, 0, True, True),
        (280, 280, 280, True, False),
    ],
)
def test_partial_step_does_not_invent_absence_of_update(
    attempted, returned, completed, performed, uncertain
):
    report = {"optimizer_performed": False}
    journal = {
        "optimizer_steps_attempted": attempted,
        "optimizer_steps_returned": returned,
        "optimizer_steps_synchronized": completed,
        "optimizer_steps_completed": completed,
        "optimizer_update_uncertain": uncertain,
        "complete": completed == 280,
    }
    runner.record_optimizer_observation(report, journal)
    assert report["optimizer_performed"] is performed
    assert report["optimizer_update_uncertain"] is uncertain
    assert report["training_completed"] is (completed == 280)


@pytest.mark.parametrize(
    "case", ["valid", "missing", "wrong_pid", "wrong_hash", "duplicate", "reversed", "truncated"]
)
def test_native_zero_still_requires_actual_finished_event(tmp_path, case):
    binding = {"path": "report.json", "bytes": 10, "sha256": "a" * 64}
    rows = [
        {"event": "balanced_worker_admitted", "phase": "train", "pid": 123},
        {
            "event": "balanced_worker_finished",
            "phase": "train",
            "native_exit_code": 0,
            "report": binding.copy(),
        },
    ]
    if case == "missing":
        rows.pop()
    elif case == "wrong_pid":
        rows[0]["pid"] = 124
    elif case == "wrong_hash":
        rows[1]["report"]["sha256"] = "b" * 64
    elif case == "duplicate":
        rows.append(rows[1])
    elif case == "reversed":
        rows.reverse()
    path = tmp_path / "stdout.bin"
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    path.write_bytes(raw[:-1] if case == "truncated" else raw)
    if case == "valid":
        assert runner.verify_finished_events(path, "train", 123, binding)["finished"] is True
    else:
        with pytest.raises(ValueError):
            runner.verify_finished_events(path, "train", 123, binding)


@pytest.mark.parametrize(
    "case", ["valid", "cpu_base", "repeat_name", "shape", "span", "dtype", "unbound_path"]
)
def test_load_audit_binds_actual_device_header_names_and_spans(tmp_path, monkeypatch, case):
    from scripts import run_onboard_navigation_pilot_streaming as streaming

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    base_path, adapter_path = tmp_path / "base", tmp_path / "adapter"
    base_path.write_bytes(b"fixture")
    adapter_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        runner, "read_bound", lambda _: {"base": {"safetensors": runner.record(base_path)}}
    )
    header = {"file_bytes": 7, "header_declared_bytes": 1, "header_sha256": "a" * 64}
    entries = {
        name: {"dtype": "BF16", "shape": [1], "data_offsets": [0, 2]}
        for name in (f"base{i}" for i in range(625))
    }
    adapters = {
        f"adapter{i}": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]} for i in range(112)
    }
    monkeypatch.setattr(
        streaming,
        "header_binding",
        lambda path, with_entries=False: (
            (header, entries if path == str(base_path) else adapters) if with_entries else header
        ),
    )

    def reader(path, kind, count, size, source, device):
        reads = [
            {
                "name": name,
                "dtype": entry["dtype"],
                "shape": entry["shape"],
                "completed": True,
                "declared_bytes": entry["data_offsets"][1],
                "actual_bytes_read": entry["data_offsets"][1],
            }
            for name, entry in source.items()
        ]
        return {
            "path": str(path),
            "kind": kind,
            "header": header,
            "final_diagnostics": {
                "filename": str(path),
                **header,
                "closed": True,
                "tensor_calls_attempted": count,
                "tensor_calls_completed": count,
                "tensor_payload_bytes_read": size,
                "normalized_device": device,
                "tensor_reads": reads,
            },
        }

    report = {
        "phase": "baseline",
        "pid": 123,
        "loader_aliases_restored": True,
        "outer_hook_fallbacks_restored": True,
        "warmup": {
            "pid": 123,
            "warmup_override_restored": True,
            "training_budget_changed": True,
            "warmup_calls_attempted": 1,
            "warmup_calls_skipped": 1,
        },
        "constructor": {"restored": True, "calls_attempted": 1, "exceptions_observed": 0},
        "reader_openings": [
            reader(base_path, "base", 625, 4255064064, entries, "cuda:0"),
            reader(adapter_path, "adapter", 112, 3211264, adapters, "cpu"),
        ],
    }
    diag = report["reader_openings"][0]["final_diagnostics"]
    if case == "cpu_base":
        diag["normalized_device"] = "cpu"
    elif case == "repeat_name":
        diag["tensor_reads"][1]["name"] = "base0"
    elif case == "shape":
        diag["tensor_reads"][0]["shape"] = [2]
    elif case == "span":
        diag["tensor_reads"][0]["declared_bytes"] = 4
    elif case == "dtype":
        diag["tensor_reads"][0]["dtype"] = "F32"
    elif case == "unbound_path":
        report["reader_openings"][0]["path"] = str(tmp_path / "other")
    plan = {"original_plan": {}, "warm_start": {"weights": runner.record(adapter_path)}}
    if case == "valid":
        runner.verify_load_observations(report, plan, tmp_path)
    else:
        with pytest.raises(ValueError):
            runner.verify_load_observations(report, plan, tmp_path)
