"""Injected CPU/stdlib regression tests; not actual model or optimizer evidence."""

from __future__ import annotations

import copy
import gc
import hashlib
import json
import subprocess
import sys
import weakref
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import train_onboard_navigation_streamed as runner


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


class EncodedObjectFixture:
    """Weak-referenceable stand-in, deliberately not a Torch tensor or GPU object."""

    def __init__(self, generation, field):
        self.generation, self.field = generation, field


def streamed_fixture(monkeypatch, *, sample_count=2):
    from scripts import onboard_navigation_balanced_encoding as encoding

    prepared, _ = prepared_fixture(monkeypatch)
    original_record = prepared["fixture"]
    samples = tuple(SimpleNamespace(sample_id=f"sample-{i}") for i in range(sample_count))
    records, references = {}, {}
    for sample in samples:
        value = copy.deepcopy(original_record)
        value["actor_identity_sha256"] = hashlib.sha256(sample.sample_id.encode()).hexdigest()
        records[sample.sample_id] = value
        references[sample.sample_id] = {
            "sample_id": sample.sample_id,
            "actor_identity_sha256": value["actor_identity_sha256"],
            "training_prefix": {
                "prefix_tokens": value["prefix_tokens"],
                "tensors": copy.deepcopy(value["prefix_tensors"]),
            },
        }
    calls, objects = [], []

    def encode(sample, root, **kwargs):
        calls.append({"sample_id": sample.sample_id, "root": root, **kwargs})
        value = copy.deepcopy(records[sample.sample_id])
        for field in ("prefix", "full", "labels"):
            value[field] = EncodedObjectFixture(len(calls), field)
            objects.append(weakref.ref(value[field]))
        return value

    monkeypatch.setattr(encoding, "encode_navigation_sample", encode)
    monkeypatch.setattr(runner, "read_bound", lambda _: {"samples": list(references.values())})
    processor, torch = object(), object()  # There is no dependency import or actual processing.
    budget = runner.BalancedTrainingBudget()
    result = runner.StreamedInputs(samples, records, references, processor, torch, budget)
    return SimpleNamespace(
        provider=result,
        samples=samples,
        records=records,
        references=references,
        calls=calls,
        objects=objects,
        encode=encode,
        processor=processor,
        torch=torch,
        budget=budget,
    )


def test_streamed_repeated_access_reencodes_fresh_objects_and_retains_no_values(monkeypatch):
    fixture = streamed_fixture(monkeypatch)
    first = fixture.provider["sample-0"]
    second = fixture.provider["sample-0"]
    assert first is not second
    assert runner.metadata(first) == runner.metadata(second) == fixture.records["sample-0"]
    for field in ("prefix", "full", "labels"):
        assert first[field] is not second[field]
        assert first[field].generation == 1 and second[field].generation == 2
    assert len(fixture.calls) == fixture.provider.accesses == 2
    assert fixture.provider.accessed_sample_ids == {"sample-0"}
    assert all(call["session"] is None for call in fixture.calls)
    assert all(call["processor"] is fixture.processor for call in fixture.calls)
    assert all(call["torch_module"] is fixture.torch for call in fixture.calls)
    assert all(call["budget"] is fixture.budget for call in fixture.calls)
    assert all(call["prefix_reference"] == fixture.references["sample-0"] for call in fixture.calls)
    del first, second
    gc.collect()
    assert all(reference() is None for reference in fixture.objects)


def test_streamed_records_are_deep_copied_and_items_do_not_reencode(monkeypatch):
    fixture = streamed_fixture(monkeypatch, sample_count=86)
    original = copy.deepcopy(fixture.provider.records)
    fixture.records["sample-0"]["prefix_tensors"]["input_ids"]["shape"][1] = 999
    fixture.records["sample-0"]["target_sha256"] = "0" * 64
    fixture.records["extra"] = {}
    assert fixture.provider.records == original
    assert dict(fixture.provider.items()) == original
    assert dict(fixture.provider.items()) == original
    assert len(original) == 86
    assert not fixture.calls and fixture.provider.accesses == 0
    assert fixture.provider.accessed_sample_ids == set()
    assert all(not {"prefix", "full", "labels"} & set(value) for value in original.values())
    json.dumps(original, allow_nan=False)


def test_streamed_unknown_identity_fails_before_any_encoder_call(monkeypatch):
    fixture = streamed_fixture(monkeypatch)
    with pytest.raises(KeyError):
        fixture.provider["unknown"]
    assert not fixture.calls and fixture.provider.accesses == 0
    assert not fixture.provider.accessed_sample_ids


@pytest.mark.parametrize("table", ["samples", "records", "references"])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_streamed_identity_tables_must_cover_exactly_the_same_samples(monkeypatch, table, change):
    fixture = streamed_fixture(monkeypatch)
    samples = list(fixture.samples)
    records, references = copy.deepcopy(fixture.records), copy.deepcopy(fixture.references)
    if table == "samples":
        samples = (
            samples[:-1] if change == "missing" else samples + [SimpleNamespace(sample_id="x")]
        )
    else:
        value = records if table == "records" else references
        if change == "missing":
            value.pop("sample-0")
        else:
            value["x"] = copy.deepcopy(value["sample-0"])
    with pytest.raises(ValueError, match="identities"):
        runner.StreamedInputs(
            samples, records, references, fixture.processor, fixture.torch, fixture.budget
        )
    assert not fixture.calls


@pytest.mark.parametrize(
    "field",
    [
        "actor_identity_sha256",
        "prefix_tensors",
        "input_tensors",
        "prefix_tokens",
        "total_tokens",
        "response_tokens",
        "supervised_tokens",
        "prefix_reference_verified",
        "prompt_label_mask_verified",
        "response_labels_sha256",
        "target_sha256",
        "extra",
        "missing",
        "bool_int_alias",
    ],
)
def test_streamed_reencoded_metadata_every_field_is_exact(monkeypatch, field):
    from scripts import onboard_navigation_balanced_encoding as encoding

    fixture = streamed_fixture(monkeypatch)

    def changed(*args, **kwargs):
        result = fixture.encode(*args, **kwargs)
        if field in {"prefix_tensors", "input_tensors"}:
            result[field]["input_ids"]["sha256"] = "0" * 64
        elif field == "extra":
            result["unadmitted"] = 1
        elif field == "missing":
            del result["target_sha256"]
        elif field == "bool_int_alias":
            result["prompt_label_mask_verified"] = 1
        elif field in {"prefix_reference_verified", "prompt_label_mask_verified"}:
            result[field] = False
        elif field.endswith("tokens"):
            result[field] += 1
        else:
            result[field] = "0" * 64
        return result

    monkeypatch.setattr(encoding, "encode_navigation_sample", changed)
    with pytest.raises(ValueError, match="streamed sample differs"):
        fixture.provider["sample-0"]
    assert len(fixture.calls) == 1
    assert fixture.provider.accesses == 0 and not fixture.provider.accessed_sample_ids


@pytest.mark.parametrize("unserializable", [EncodedObjectFixture(1, "full"), float("nan")])
def test_streamed_metadata_cannot_contain_tensor_standins_or_nonfinite_values(
    monkeypatch, unserializable
):
    fixture = streamed_fixture(monkeypatch)
    fixture.records["sample-0"]["hidden_cache"] = unserializable
    with pytest.raises((TypeError, ValueError)):
        runner.StreamedInputs(
            fixture.samples,
            fixture.records,
            fixture.references,
            fixture.processor,
            fixture.torch,
            fixture.budget,
        )


def injected_source_session(samples, *, verified=True):
    class Session:
        def __init__(self, root):
            self.root, self.verified = root, False

        def __enter__(self):
            return self

        def prime(self, actual):
            assert actual == samples

        def __exit__(self, kind, value, traceback):
            self.verified = kind is None and verified

    return Session


def test_encode_all_default_metadata_only_and_releases_previous_before_next(monkeypatch):
    from scripts import onboard_navigation_balanced_encoding as encoding

    fixture = streamed_fixture(monkeypatch, sample_count=86)

    def no_overlap(*args, **kwargs):
        assert all(ref() is None for ref in fixture.objects), (
            "previous sample objects still retained"
        )
        return fixture.encode(*args, **kwargs)

    monkeypatch.setattr(encoding, "encode_navigation_sample", no_overlap)
    progress = []
    monkeypatch.setattr(runner, "emit", lambda event, **values: progress.append((event, values)))
    pilot = SimpleNamespace(
        navigation=SimpleNamespace(
            NavigationSourceValidationSession=injected_source_session(fixture.samples)
        )
    )
    result = runner.encode_all(
        {"cpu_prefix_report": {}},
        fixture.budget,
        {},
        fixture.samples,
        pilot,
        fixture.processor,
        fixture.torch,
    )
    assert isinstance(result, runner.StreamedInputs)
    assert dict(result.items()) == fixture.records
    assert len(fixture.calls) == 86 and result.accesses == 0
    assert [call["sample_id"] for call in fixture.calls] == [s.sample_id for s in fixture.samples]
    assert all(call["session"].verified for call in fixture.calls)
    assert [values["completed"] for _, values in progress] == [10, 20, 30, 40, 50, 60, 70, 80, 86]
    assert all(ref() is None for ref in fixture.objects)
    runner.validate_prepared_inputs(
        dict(result.items()),
        {"cpu_prefix_report": {}, "diagnostics": {"sample_ids": list(fixture.records)}},
        fixture.budget,
    )


def test_encode_all_rejects_unverified_final_source_session(monkeypatch):
    fixture = streamed_fixture(monkeypatch)
    pilot = SimpleNamespace(
        navigation=SimpleNamespace(
            NavigationSourceValidationSession=injected_source_session(
                fixture.samples, verified=False
            )
        )
    )
    with pytest.raises(ValueError, match="Source validation did not finish"):
        runner.encode_all(
            {"cpu_prefix_report": {}},
            fixture.budget,
            {},
            fixture.samples,
            pilot,
            fixture.processor,
            fixture.torch,
        )


def test_evaluate_all_reencodes_once_per_sample_and_releases_all_returned_objects(monkeypatch):
    from scripts import onboard_navigation_balanced_encoding as encoding

    fixture = streamed_fixture(monkeypatch, sample_count=3)
    seen = []

    def no_overlap(*args, **kwargs):
        assert all(ref() is None for ref in fixture.objects), (
            "prior evaluated sample still retained"
        )
        return fixture.encode(*args, **kwargs)

    monkeypatch.setattr(encoding, "encode_navigation_sample", no_overlap)

    def generate(value, *, parent):
        seen.append((value["prefix"].generation, value["actor_identity_sha256"], parent))
        return {
            "raw_output": "{}",
            "output_token_ids": [41],
            "generated_tokens": 1,
            "token_cap_reached": False,
            "elapsed_s": 1.0,
        }

    backend = SimpleNamespace(generate=generate)
    pilot = SimpleNamespace(
        check_memory=lambda *_: {},
        action_class=lambda _: "forward",
        diagnose=lambda *_: {"strict_json_valid": False},
    )
    rows = runner.evaluate_all(
        backend,
        fixture.samples,
        fixture.provider,
        {s.sample_id: {} for s in fixture.samples},
        pilot,
        fixture.budget,
        parent=False,
    )
    assert len(fixture.calls) == len(rows) == 3
    assert fixture.provider.accesses == 3
    assert fixture.provider.accessed_sample_ids == set(fixture.records)
    assert [generation for generation, _, _ in seen] == [1, 2, 3]
    assert all(not parent for _, _, parent in seen)
    assert [row["actor_identity_sha256"] for row in rows] == [identity for _, identity, _ in seen]
    for row in rows:
        assert row["actual_prefix_tensors"] == fixture.records[row["sample_id"]]["prefix_tensors"]
    gc.collect()
    assert all(ref() is None for ref in fixture.objects)


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "oversize",
        "negative_id",
        "bool_id",
        "tuple_ids",
        "count_bool",
        "count_mismatch",
        "false_cap_at_192",
        "true_cap_before_192",
        "nonboolean_cap",
        "elapsed_missing",
        "elapsed_negative",
        "elapsed_nan",
        "elapsed_inf",
        "elapsed_bool",
        "elapsed_string",
    ],
)
def test_generated_token_count_cap_and_elapsed_malformed_output_is_rejected(monkeypatch, case):
    prepared, _ = prepared_fixture(monkeypatch)
    diagnostic = {
        "strict_json_valid": False,
        "action_class_correct": False,
        "metric_correct": False,
    }
    pilot = SimpleNamespace(action_class=lambda _: "left", diagnose=lambda *_: diagnostic)
    generation = {
        "raw_output": "{}",
        "output_token_ids": [1, 2],
        "generated_tokens": 2,
        "token_cap_reached": False,
        "elapsed_s": 1.0,
    }
    if case in {"empty", "oversize", "false_cap_at_192"}:
        generation["output_token_ids"] = [1] * {
            "empty": 0,
            "oversize": 193,
            "false_cap_at_192": 192,
        }[case]
        generation["generated_tokens"] = len(generation["output_token_ids"])
    elif case == "negative_id":
        generation["output_token_ids"][0] = -1
    elif case == "bool_id":
        generation["output_token_ids"][0] = True
    elif case == "tuple_ids":
        generation["output_token_ids"] = (1, 2)
    elif case == "count_bool":
        generation["generated_tokens"] = True
    elif case == "count_mismatch":
        generation["generated_tokens"] = 1
    elif case == "true_cap_before_192":
        generation["token_cap_reached"] = True
    elif case == "nonboolean_cap":
        generation["token_cap_reached"] = 0
    elif case == "elapsed_missing":
        del generation["elapsed_s"]
    else:
        generation["elapsed_s"] = {
            "elapsed_negative": -0.1,
            "elapsed_nan": float("nan"),
            "elapsed_inf": float("inf"),
            "elapsed_bool": True,
            "elapsed_string": "1.0",
        }[case]
    row = {
        "sample_id": "fixture",
        "generated": generation,
        "parent": False,
        "actor_identity_sha256": "e" * 64,
        "actual_prefix_tensors": prepared["fixture"]["prefix_tensors"],
        "gold_action_class": "left",
        "diagnostic": diagnostic,
    }
    with pytest.raises(ValueError, match="Actual generated tokens"):
        runner.diagnose_rows([row], ["fixture"], prepared, {"fixture": {}}, pilot, parent=False)


@pytest.mark.parametrize("elapsed", [0.0, 29.9, 30.0, 35.0])
def test_actual_generation_cap_and_elapsed_boundary_are_recorded_not_repaired(monkeypatch, elapsed):
    prepared, _ = prepared_fixture(monkeypatch)
    diagnostic = {
        "strict_json_valid": False,
        "action_class_correct": False,
        "metric_correct": False,
    }
    pilot = SimpleNamespace(action_class=lambda _: "left", diagnose=lambda *_: diagnostic)
    row = {
        "sample_id": "fixture",
        "generated": {
            "raw_output": "{}",
            "output_token_ids": [1] * 192,
            "generated_tokens": 192,
            "token_cap_reached": True,
            "elapsed_s": elapsed,
        },
        "parent": False,
        "actor_identity_sha256": "e" * 64,
        "actual_prefix_tensors": prepared["fixture"]["prefix_tensors"],
        "gold_action_class": "left",
        "diagnostic": diagnostic,
    }
    summary = runner.diagnose_rows(
        [row], ["fixture"], prepared, {"fixture": {}}, pilot, parent=False
    )
    assert summary["token_cap_reached"] == 1
    assert summary["elapsed_at_or_above_30s_generation_bound"] == int(elapsed >= 30)
    assert row["generated"]["elapsed_s"] == elapsed


def test_streamed_entry_reuses_the_six_frozen_r1_sources_without_modification():
    root = Path(runner.__file__).resolve().parents[1]
    path = root / "configs/training/onboard-navigation-balanced-continuation-20261007-r1.json"
    plan = json.loads(path.read_bytes())
    frozen = {
        "scripts/train_onboard_navigation_balanced.py",
        "tests/test_train_onboard_navigation_balanced.py",
        "scripts/onboard_navigation_balanced_encoding.py",
        "tests/test_onboard_navigation_balanced_encoding.py",
        "scripts/onboard_navigation_balanced_journal.py",
        "tests/test_onboard_navigation_balanced_journal.py",
    }
    bindings = {Path(row["path"]).resolve(): row for row in plan["source_files"]}
    for name in frozen:
        source = root / name
        binding = bindings[source.resolve()]
        raw = source.read_bytes()
        assert len(raw) == binding["bytes"]
        assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    assert frozen <= set(runner.NEW_FILES)
    assert {
        "scripts/train_onboard_navigation_streamed.py",
        "tests/test_train_onboard_navigation_streamed.py",
    } <= set(runner.NEW_FILES)
