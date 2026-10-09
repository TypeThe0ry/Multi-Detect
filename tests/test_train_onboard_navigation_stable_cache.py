"""Small CPU/stand-in fixtures, not actual features, model runs or budget evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import train_onboard_navigation_stable_cache as runner

torch = pytest.importorskip("torch")
pytest.importorskip("safetensors")
inference = importlib.import_module("multidetect.autonomy.onboard_navigation_inference")
scoring = importlib.import_module("scripts.evaluate_onboard_navigation_original82")
loss = importlib.import_module("scripts.onboard_navigation_action_readout")
stable = importlib.import_module("scripts.onboard_navigation_stable_readout")


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    assert not torch.cuda.is_initialized()

    def forbidden(*args, **kwargs):
        raise AssertionError("No CUDA in fixture verification")

    monkeypatch.setattr(torch.cuda, "_lazy_init", forbidden)
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)
    assert not torch.cuda.is_initialized()


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def rows208():
    rows = []
    for episode, count in enumerate((19, 30, 37, 15, 29, 17, 25, 15, 21)):
        for offset in range(count):
            stop = offset == count - 1
            rows.append(
                {
                    "sample_id": sha(f"toy-{episode}-{offset}"),
                    "episode_id": sha(f"episode-{episode}"),
                    "partition": "train",
                    "scene_id": "env_airsim_18" if episode < 3 else "env_airsim_26",
                    "source": {"original_split": "train"},
                    "step_index": offset,
                    "planned_action": {
                        "kind": "stop" if stop else "translate",
                        "translation_m": None if stop else [3.0, 0.0, 0.0],
                        "yaw_delta_rad": None,
                    },
                }
            )
    return rows


def write(path, value, *, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if jsonl:
        path.write_bytes(b"".join(runner.canonical(row) + b"\n" for row in value))
    else:
        path.write_bytes(runner.canonical(value) + b"\n")
    return stable.file_binding(path)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    """A fake closed report wraps actual tiny CPU tensor bytes, never model evidence."""
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    rows = rows208()
    output = tmp_path / "artifacts/diagnostics/fixture-candidate208"
    output.mkdir(parents=True)
    source = write(tmp_path / "fixture-source.json", {"scope": "standin_only"})
    datasets = {
        "old86": write(tmp_path / "old.jsonl", rows[:86], jsonl=True),
        "new122": write(tmp_path / "new.jsonl", rows[86:], jsonl=True),
    }
    implementation = [
        write(tmp_path / name, {"scope": "standin"})
        for name in (
            "extract_onboard_navigation_feature_cache.py",
            "test_extract_onboard_navigation_feature_cache.py",
        )
    ]
    identity = {"base": sha("fixture-base"), "adapter": sha("fixture-adapter")}
    ids = [r["sample_id"] for r in rows]
    plan = {
        "contract": runner.EXTRACT_CONTRACT + "-plan",
        "dataset_bindings": datasets,
        "sample_ids": ids,
        "expected_model_identity": identity,
        "source_files": implementation,
        "model_file_bindings": {
            "base": {"sha256": sha("base_file")},
            "adapter": {"sha256": sha("adapter_file")},
        },
    }
    plan_binding = write(tmp_path / "extract-plan.json", plan)
    features = torch.zeros(208, 2048, dtype=torch.float32)
    observations = []
    for i, sid in enumerate(ids):
        observations.append(
            {
                "sample_id": sid,
                "actor_identity_sha256": sha(sid),
                "prefix_tensors": {
                    name: {
                        "dtype": "torch.float32" if name == "pixel_values" else "torch.int64",
                        "shape": [1, 3],
                        "sha256": sha(name),
                    }
                    for name in stable.FOUR_PREFIX
                },
                "feature": stable.tensor_record(features[i : i + 1]),
                "source_session_closed_verified": True,
                "gold_in_model_inputs": False,
            }
        )
    cache = stable.save_feature_cache(
        output / "cache",
        features,
        observations=observations,
        model_identity=identity,
        partitions=["train"] * 208,
        episode_ids=[r["episode_id"] for r in rows],
        extraction_metadata={"scope": "CPU_fixture_not_model"},
        sources=[source],
    )
    record = stable.tensor_record(features)
    manifest = write(
        output / "manifest.json",
        {
            "contract": runner.EXTRACT_CONTRACT + "-manifest",
            "plan": plan_binding,
            "cache_receipt": cache,
            "sample_ids": ids,
            "model_identity": identity,
            "feature_matrix": record,
            "episode_ids": [r["episode_id"] for r in rows],
            "partitions": ["train"] * 208,
        },
    )
    report = {
        "contract": runner.EXTRACT_CONTRACT,
        "success": True,
        "native_exit_code": 0,
        "feature_cache_admitted": True,
        "feature_count": 208,
        "fused_prefill_attempts": 208,
        "source_sessions_verified": True,
        "all208_prefix_metadata_equal": True,
        "cache_saved_and_exactly_reloaded": True,
        "source_validation_sessions_verified": 208,
        "model_loaded": True,
        "claims": {"quality_claim": False},
        "optimizer_steps": dict.fromkeys(runner.STAGES, 0),
        "partial_failures": [],
        "source_bindings_before": {"repository": [source], "runtime": []},
        "source_bindings_after": {"repository": [source], "runtime": []},
        "source_bindings_unchanged": True,
        "model_parameter_identity_before": identity,
        "model_parameter_identity_after": identity,
        "plan": plan_binding,
        "sample_ids": ids,
        "cache_receipt": cache,
        "manifest": manifest,
        "feature_matrix": record,
        "cache_tensor_bytes": 208 * 2048 * 4,
    }
    report_binding = write(output / "report.json", report)
    receipt = json.loads(Path(cache["path"]).read_bytes())
    terminal = {
        "contract": runner.TERMINAL_CONTRACT,
        "actual_execution": {
            "actual_native_exit_code": 0,
            "worker_pid_absent": True,
            "relevant_model_processes_at_exit_check": 0,
            "terminal_tool_chunk": "fixture-only",
            "process_exit_check_tool_chunk": "fixture-only",
        },
        "observations": {
            "success": True,
            "feature_cache_admitted": True,
            "feature_count": 208,
            "cache_exact_reload": True,
        },
        "bindings": [
            report_binding,
            plan_binding,
            cache,
            receipt["metadata"],
            receipt["weights"],
            manifest,
            *implementation,
        ],
        "claims": {"quality_claim": False},
    }
    terminal_binding = write(tmp_path / runner.TERMINAL_PATH, terminal)
    # These are fixture stand-ins for the separately tested frozen-code/history gates.
    monkeypatch.setattr(runner, "code_bindings", lambda: [source])
    monkeypatch.setattr(runner, "history", lambda: [source])
    return SimpleNamespace(
        root=tmp_path,
        rows=rows,
        report=report,
        report_binding=report_binding,
        terminal=terminal,
        terminal_binding=terminal_binding,
        plan=plan,
        plan_binding=plan_binding,
        cache=cache,
        features=features,
        observations=observations,
        source=source,
    )


def admit(f):
    return runner.admit_extractor(
        f.report_binding["path"],
        f.report_binding["sha256"],
        f.terminal_binding["path"],
        f.terminal_binding["sha256"],
    )


def make_plan(f):
    return runner.build_plan(
        f.report_binding["path"],
        f.report_binding["sha256"],
        f.terminal_binding["path"],
        f.terminal_binding["sha256"],
    )


def reseal_report(f):
    f.report_binding = write(Path(f.report_binding["path"]), f.report)
    f.terminal["bindings"][0] = f.report_binding
    f.terminal_binding = write(Path(f.terminal_binding["path"]), f.terminal)


def test_ordered208_and_fixed_budget_plan(fixture):
    f = fixture
    report, plan, rows, metadata, consumed = admit(f)
    assert len(rows) == 208 and metadata["sample_ids"] == report["sample_ids"]
    assert (
        len(consumed) >= 8
        and plan["dataset_bindings"]["old86"] == f.plan["dataset_bindings"]["old86"]
    )
    p = make_plan(f)
    assert p["rule"] == runner.RULE
    assert p["new_family_attempt_cap"] == 2200
    assert p["cumulative_head_attempt_cap"] == 3000 and p["inherited_head_attempts"] == 800
    assert p["inherited_base_lora_attempts"] == 280 and p["base_lora_new_updates"] == 0
    assert len(p["heads"]) == 11 and sum(len(s["eval_ids"]) for s in p["heads"]) == 502
    assert all(v is False for v in p["claims"].values())
    for spec in p["heads"][:9]:
        assert not set(spec["train_ids"]) & set(spec["eval_ids"])
        assert len(spec["train_ids"]) + len(spec["eval_ids"]) == 208
    assert p["heads"][-2]["train_ids"] == p["sample_ids"][:86]
    assert p["heads"][-1]["train_ids"] == p["sample_ids"]


@pytest.mark.parametrize(
    "mutation",
    [
        "native",
        "success",
        "admitted",
        "count",
        "sessions",
        "zero_bool",
        "partial",
        "source",
        "identity",
        "gold_claim",
    ],
)
def test_report_false_or_partial_gate(fixture, mutation):
    f = fixture
    if mutation == "native":
        f.report["native_exit_code"] = True
    if mutation == "success":
        f.report["success"] = False
    if mutation == "admitted":
        f.report["feature_cache_admitted"] = False
    if mutation == "count":
        f.report["feature_count"] = 207
    if mutation == "sessions":
        f.report["source_validation_sessions_verified"] = 207
    if mutation == "zero_bool":
        f.report["optimizer_steps"]["attempted"] = False
    if mutation == "partial":
        f.report["partial_failures"] = [{"completed": 207}]
    if mutation == "source":
        f.report["source_bindings_after"] = {}
    if mutation == "identity":
        f.report["model_parameter_identity_after"] = {"base": sha("drift")}
    if mutation == "gold_claim":
        f.report["claims"]["quality_claim"] = True
    reseal_report(f)
    with pytest.raises(ValueError):
        admit(f)


@pytest.mark.parametrize(
    "mutation",
    ["exit", "pid", "processes", "chunk", "count", "cache", "binding", "missing_binding"],
)
def test_terminal_must_certify_real_exit_and_exact_files(fixture, mutation):
    f = fixture
    if mutation == "exit":
        f.terminal["actual_execution"]["actual_native_exit_code"] = 1
    if mutation == "pid":
        f.terminal["actual_execution"]["worker_pid_absent"] = False
    if mutation == "processes":
        f.terminal["actual_execution"]["relevant_model_processes_at_exit_check"] = 1
    if mutation == "chunk":
        f.terminal["actual_execution"]["terminal_tool_chunk"] = ""
    if mutation == "count":
        f.terminal["observations"]["feature_count"] = 207
    if mutation == "cache":
        f.terminal["observations"]["cache_exact_reload"] = False
    if mutation == "binding":
        f.terminal["bindings"][0]["sha256"] = sha("wrong-report")
    if mutation == "missing_binding":
        f.terminal["bindings"].pop()
    f.terminal_binding = write(Path(f.terminal_binding["path"]), f.terminal)
    with pytest.raises(ValueError):
        admit(f)


def test_missing_real_report_refuses_early_without_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        runner.build_plan("missing-report.json", sha("absent"), runner.TERMINAL_PATH, sha("absent"))
    assert not (tmp_path / runner.OUTPUT).exists()


@pytest.mark.parametrize("mutation", ["role", "id", "scene", "episode", "missing"])
def test_nine_episode_roles_and_order_not_random_split(mutation):
    rows = rows208()
    if mutation == "role":
        rows[2]["partition"] = "dev-test"
    if mutation == "id":
        rows[1]["sample_id"] = rows[0]["sample_id"]
    if mutation == "scene":
        rows[0]["scene_id"] = "env_airsim_26"
    if mutation == "episode":
        rows[0]["episode_id"] = rows[100]["episode_id"]
    if mutation == "missing":
        rows.pop()
    with pytest.raises(ValueError):
        runner.schedule(rows)


def test_source_drift_and_explicit_shas(fixture):
    f = fixture
    with pytest.raises(ValueError, match="External extractor"):
        runner.admit_extractor(
            f.report_binding["path"],
            sha("wrong"),
            f.terminal_binding["path"],
            f.terminal_binding["sha256"],
        )
    with pytest.raises(ValueError, match="External terminal"):
        runner.admit_extractor(
            f.report_binding["path"],
            f.report_binding["sha256"],
            f.terminal_binding["path"],
            sha("wrong"),
        )
    Path(f.source["path"]).write_bytes(b"drift")
    with pytest.raises(ValueError):
        admit(f)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_strict_json_rejects_bad_values(raw):
    with pytest.raises(ValueError):
        runner.strict_json(raw)


def test_emit_create_only_and_plan_mutation(fixture):
    f = fixture
    path = f.root / "configs/training/onboard-navigation-stable-cache-20261008-r1.json"
    path.parent.mkdir(parents=True)
    args = [
        "--emit-plan",
        "--extractor-report",
        f.report_binding["path"],
        "--extractor-report-sha256",
        f.report_binding["sha256"],
        "--terminal-receipt",
        f.terminal_binding["path"],
        "--terminal-receipt-sha256",
        f.terminal_binding["sha256"],
        "--plan-output",
        str(path),
    ]
    assert runner.main(args) == 0
    b = runner.binding(path)
    assert runner.admit_plan(path, b["sha256"])[0]["rule"] == runner.RULE
    assert runner.main(args) == 1
    p = runner.read_bound(b)
    p["rule"]["updates"] = 201
    b = write(path, p)
    with pytest.raises(ValueError, match="Precommitted"):
        runner.admit_plan(path, b["sha256"])
    with pytest.raises(ValueError):
        make_plan_with_other_output(f)


def make_plan_with_other_output(f):
    return runner.build_plan(
        f.report_binding["path"],
        f.report_binding["sha256"],
        f.terminal_binding["path"],
        f.terminal_binding["sha256"],
        "artifacts/retry",
    )


def events(heads, count):
    counts = dict.fromkeys(runner.STAGES, 0)
    for n in range(count):
        head, offset = divmod(n, 800)
        update, stage = divmod(offset, 4)
        name = runner.STAGES[stage]
        counts[name] += 1
        yield {
            "fit_id": heads[head]["head_id"],
            "update": update + 1,
            "stage": name,
            "global_count": counts[name],
            "pre_step_loss": 1.25,
        }


def test_journal_truthful_partial_and_complete(tmp_path):
    heads = runner.schedule(rows208())
    path = tmp_path / "wal.jsonl"
    write(path, list(events(heads, 5)), jsonl=True)
    result = runner.verify_journal(path, heads)
    assert result["counts"] == {"attempted": 2, "returned": 1, "synchronized": 1, "completed": 1}
    assert result["optimizer_update_uncertain"] is True
    with pytest.raises(ValueError):
        runner.verify_journal(path, heads, require_complete=True)
    write(path, list(events(heads, 8800)), jsonl=True)
    assert runner.verify_journal(path, heads, require_complete=True)["complete"]


@pytest.mark.parametrize("mutation", ["order", "extra", "loss", "bool", "truncated"])
def test_journal_rejects_tamper(tmp_path, mutation):
    heads = runner.schedule(rows208())
    records = list(events(heads, 4))
    if mutation == "order":
        records[1], records[2] = records[2], records[1]
    if mutation == "extra":
        records[0]["extra"] = 1
    if mutation == "loss":
        records[2]["pre_step_loss"] = 2
    if mutation == "bool":
        records[0]["update"] = True
    path = tmp_path / "wal.jsonl"
    write(path, records, jsonl=True)
    if mutation == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError):
        runner.verify_journal(path, heads)


def test_durable_writer_write_flush_fsync(monkeypatch):
    calls = []

    class Stream:
        def write(self, raw):
            calls.append(("write", raw))
            return len(raw)

        def flush(self):
            calls.append(("flush",))

        def fileno(self):
            return 99

    monkeypatch.setattr(runner.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    runner.append_row(Stream(), {"attempted": 1})
    assert [row[0] for row in calls] == ["write", "flush", "fsync"]
    monkeypatch.setattr(
        runner.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failed"))
    )
    with pytest.raises(OSError):
        runner.append_row(Stream(), {"attempted": 1})


def fake_navigation():
    def sample(row):
        action = row["planned_action"]
        return SimpleNamespace(sample_id=row["sample_id"], planned_action=SimpleNamespace(**action))

    return SimpleNamespace(
        NavigationSupervisionSample=SimpleNamespace(from_mapping=sample),
        validate_navigation_episode_samples=lambda samples: None,
    )


def fake_fit(features, labels, *, sample_ids, partitions, rule, ledger, event_sink, fit_id):
    """Stand-in orchestration only: simulated update events, not navigation training."""
    norm = stable.fit_normalization(
        features, sample_ids=sample_ids, partitions=partitions, epsilon=rule.epsilon
    )
    head = stable.make_head(features.shape[1])
    with torch.no_grad():
        head.weight.zero_()
        head.bias.copy_(torch.tensor([1.0, 0.0, 0.0, 0.0, 3.0, 0.5]))
    for update in range(1, rule.updates + 1):
        for stage in runner.STAGES:
            ledger.publish(stage, event_sink, fit_id=fit_id, update=update, pre_step_loss=1.25)
    head.zero_grad(set_to_none=True)
    head.eval()
    report = {
        "success": True,
        "training_loss_improved": True,
        "final_loss_recomputed_after_updates": True,
        "optimizer_update_uncertain": False,
        "training_sample_ids": sample_ids,
        "claims": dict(stable.FALSE_CLAIMS),
        "initial_train_loss": {"total": 2.0},
        "final_train_loss": {"total": 1.0},
        "final_tensor_records": {
            "weight": stable.tensor_record(head.weight),
            "bias": stable.tensor_record(head.bias),
            "center": stable.tensor_record(norm.center),
            "scale": stable.tensor_record(norm.scale),
        },
    }
    return stable.FitResult(head, norm, report)


def setup_run(f, monkeypatch):
    plan = make_plan(f)
    plan_binding = write(f.root / "train-plan.json", plan)
    apis = (stable, loss, scoring, inference, fake_navigation(), [])
    monkeypatch.setattr(runner, "load_cpu_apis", lambda plan: apis)
    monkeypatch.setattr(stable, "fit_readout", fake_fit)
    monkeypatch.setattr(runner.os, "fsync", lambda fd: None)  # Durability is covered separately.
    return SimpleNamespace(plan=plan_binding["path"], plan_sha256=plan_binding["sha256"])


def test_11_head_orchestration_and_raw_before_gold(fixture, monkeypatch):
    f = fixture
    args = setup_run(f, monkeypatch)
    original_score = scoring.score_prediction
    score_calls = []

    def observed_score(*a):
        raw_path = f.root / runner.OUTPUT / "predictions.jsonl"
        assert len(raw_path.read_bytes().splitlines()) == 502
        score_calls.append(True)
        return original_score(*a)

    monkeypatch.setattr(scoring, "score_prediction", observed_score)
    report = runner.run(args)
    assert report["success"] is True, report.get("error")
    assert report["optimizer_steps"] == dict.fromkeys(runner.STAGES, 2200)
    assert report["combined_head_optimizer_steps"] == dict.fromkeys(runner.STAGES, 3000)
    assert (
        report["remaining_new_family_attempts"] == 0
        and report["inherited_base_lora_optimizer_attempts"] == 280
    )
    assert len(report["heads"]) == 11 and len(score_calls) == 502
    assert report["raw_durable_before_gold_scoring"] is True
    assert all(v is False for v in report["claims"].values())
    assert all(head["normalization_train_ids"] == head["train_ids"] for head in report["heads"])
    with pytest.raises(FileExistsError):
        runner.run(args)


def test_uncertain_failure_keeps_spend_and_cannot_restart(fixture, monkeypatch):
    f = fixture
    args = setup_run(f, monkeypatch)

    def fail(*a, ledger, event_sink, fit_id, **kw):
        ledger.publish("attempted", event_sink, fit_id=fit_id, update=1, pre_step_loss=1.0)
        raise stable.FitRejected(
            "simulated failure", {"success": False, "optimizer_update_uncertain": True}
        )

    monkeypatch.setattr(stable, "fit_readout", fail)
    report = runner.run(args)
    assert report["native_exit_code"] == 1 and report["optimizer_steps"]["attempted"] == 1
    assert report["combined_head_optimizer_steps"]["attempted"] == 801
    assert (
        report["optimizer_update_uncertain"] is True
        and report["remaining_new_family_attempts"] == 2199
    )
    assert (f.root / runner.OUTPUT / "report.json").is_file()
    with pytest.raises(FileExistsError):
        runner.run(args)


def test_saved_reload_output_mismatch_is_failure(fixture, monkeypatch):
    f = fixture
    args = setup_run(f, monkeypatch)
    real = stable.load_readout

    def changed(*a, **kw):
        head, norm, meta = real(*a, **kw)
        with torch.no_grad():
            head.bias[0] += 1
        return head, norm, meta

    monkeypatch.setattr(stable, "load_readout", changed)
    report = runner.run(args)
    assert report["native_exit_code"] == 1 and report["optimizer_steps"]["completed"] == 200
    assert (
        report["raw_prediction_count"] == 0 and "raw outputs differ" in report["error"]["message"]
    )


def test_actual_tiny_cpu_fit_uses_fixed_loss_not_backbone():
    features = torch.eye(4, dtype=torch.float32)
    labels = [
        stable.ReadoutLabel("forward", 3),
        stable.ReadoutLabel("turn_yaw_left", angle_magnitude_rad=0.5),
        stable.ReadoutLabel("turn_yaw_right", angle_magnitude_rad=0.5),
        stable.ReadoutLabel("sequence_stop"),
    ]
    events_out = []
    result = stable.fit_readout(
        features,
        labels,
        sample_ids=["a", "b", "c", "d"],
        partitions=["train"] * 4,
        rule=stable.TrainingRule(200, 2200),
        ledger=stable.UpdateLedger(2200),
        event_sink=events_out.append,
        fit_id="tiny_CPU",
    )
    assert result.report["training_loss_improved"] and len(events_out) == 800
    assert result.report["final_loss_recomputed_after_updates"]


def test_guarded_import_stdlib_only():
    path = Path(runner.__file__).resolve()
    code = (
        f"import runpy,sys;runpy.run_path({str(path)!r},run_name='not_main');"
        "assert not any(n.split('.')[0] in {'torch','PIL','transformers','peft','numpy'} "
        "for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_actual_CPU_runtime_origin_gate_and_shadow_rejection(monkeypatch, tmp_path):
    plan = {"runtime": runner.runtime_reference()}
    assert runner.verify_runtime_entrypoints(plan) == plan["runtime"]["entrypoints"]
    module = importlib.import_module("torch.optim.adamw")
    monkeypatch.setattr(module, "__file__", str(tmp_path / "shadow.py"))
    with pytest.raises(ValueError, match="entrypoint differs"):
        runner.verify_runtime_entrypoints(plan)


def test_AdamW_export_alias_cannot_bypass_gate(monkeypatch):
    plan = {"runtime": runner.runtime_reference()}
    monkeypatch.setattr(torch.optim, "AdamW", object())
    with pytest.raises(ValueError, match="AdamW class"):
        runner.verify_runtime_entrypoints(plan)


def test_attempted_fsync_failure_reservation_not_refunded(fixture, monkeypatch):
    f = fixture
    args = setup_run(f, monkeypatch)
    original = runner.append_row

    def fail(stream, row):
        if row.get("stage") == "attempted":
            raise OSError("simulated attempted-event fsync failed")
        return original(stream, row)

    monkeypatch.setattr(runner, "append_row", fail)
    report = runner.run(args)
    assert report["optimizer_steps"] == {
        "attempted": 1,
        "returned": 0,
        "synchronized": 0,
        "completed": 0,
    }
    assert report["optimizer_API_calls"] == 0 and report["optimizer_update_uncertain"]
    assert report["combined_head_optimizer_steps"]["attempted"] == 801
    with pytest.raises(FileExistsError):
        runner.run(args)


def test_late_consumed_source_drift_cannot_publish_success(fixture, monkeypatch):
    f = fixture
    args = setup_run(f, monkeypatch)
    original = runner.capture_sources
    calls = []

    def capture(*a):
        calls.append(True)
        if len(calls) == 2:
            Path(f.source["path"]).write_bytes(b"drift-after-all-heads")
        return original(*a)

    monkeypatch.setattr(runner, "capture_sources", capture)
    report = runner.run(args)
    assert report["native_exit_code"] == 1 and report["success"] is False
    assert report["optimizer_steps"]["attempted"] == 2200
    assert report["source_bindings_unchanged"] is False and report["source_bindings_after"] is None
    assert all(v is False for v in report["claims"].values())
