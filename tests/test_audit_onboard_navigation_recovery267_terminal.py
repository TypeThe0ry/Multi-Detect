"""Synthetic CPU fixtures exercise terminal gates, not an actual training run."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import audit_onboard_navigation_recovery267_terminal as audit
from scripts import onboard_navigation_recovery_budget as budget
from scripts import train_onboard_navigation_recovery_v2 as frozen


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audit.canonical(value) + b"\n")
    return audit.binding(path)


def adapter_file(path, parameters, value):
    header, payload = {}, bytearray()
    for item in reversed(parameters):  # Header order must not drive parameter identity.
        start = len(payload)
        payload.extend(struct.pack("<16f", *([value] * 16)))
        header[item["name"].replace(".default.", ".")] = {
            "dtype": "F32",
            "shape": [4, 4],
            "data_offsets": [start, len(payload)],
        }
    encoded = audit.canonical(header)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + payload)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "frozen_journal_verifier", lambda plan: frozen.verify_journal)
    # This fixture is synthetic; the separate isolated parser test below uses
    # real frozen functions without any model/processor execution.
    monkeypatch.setattr(
        audit,
        "rederive_diagnostics",
        lambda *args: {
            name: {"sample_count": 86, "resubstitution_only": True, "quality_claim": False}
            for name in ("old_base", "old_warm", "new_candidate", "reloaded")
        },
    )
    run = tmp_path / audit.FIXED_RUN
    for name in ("admit-cpu", "train", "reload"):
        (run / name).mkdir(parents=True)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    parameters = [
        {
            "name": f"base_model.model.model.language_model.layers.{layer}.self_attn."
            f"{projection}.lora_{part}.default.weight",
            "dtype": "torch.float32",
            "numel": 16,
        }
        for layer in range(28)
        for projection in ("q_proj", "v_proj")
        for part in ("A", "B")
    ]
    original_audit = {"trainable_parameters": parameters, "trainable_parameter_count": 1792}
    pools, classes, ids = {}, {}, []
    for name, count in budget.SOURCE_COUNTS.items():
        pools[name] = []
        for index in range(count):
            sample_id = hashlib.sha256(f"{name}{index}".encode()).hexdigest()
            pools[name].append(sample_id)
            classes[sample_id] = name
            ids.append(sample_id)
    schedule = budget.build_recovery_draws(pools, targets=classes, classify=lambda value: value)
    sha = hashlib.sha256(b"fixture").hexdigest()
    tensor = {"dtype": "torch.int64", "shape": [1, 3], "sha256": sha}
    prefix = {
        name: tensor for name in ("input_ids", "attention_mask", "image_grid_thw", "pixel_values")
    }
    prepared = {
        sample_id: {"actor_identity_sha256": sha, "prefix_tensors": prefix} for sample_id in ids
    }
    prepared_binding = write_json(tmp_path / "inputs/prepared.json", prepared)
    warm_path = tmp_path / "inputs/warm/adapter_model.safetensors"
    adapter_file(warm_path, parameters, 0.25)
    warm_digest, warm_checks = audit.fp32_parameter_digest(warm_path, original_audit)
    warm_binding = audit.binding(warm_path)
    config_binding = write_json(
        tmp_path / "inputs/warm/adapter_config.json", {"r": 4, "lora_alpha": 8}
    )
    original_training = write_json(
        tmp_path / "inputs/original-training.json", {"trainable_audit": original_audit}
    )
    original = write_json(tmp_path / "inputs/original.json", {"train_sample_ids": ids})
    probes = [{"sample_id": pools[name][0], "action_class": name} for name in budget.CLASSES]
    legacy_binding = write_json(
        tmp_path / "inputs/legacy.json",
        {
            "original_plan": original,
            "original_training_report": original_training,
            "diagnostics": {"sample_ids": ids, "smoke_probes": probes},
        },
    )
    rows = [
        {
            "sample_id": sample_id,
            "gold_action_class": classes[sample_id],
            "parent": False,
            "actor_identity_sha256": sha,
            "actual_prefix_tensors": prefix,
            "resubstitution_only": True,
            "generated": {
                "raw_output": "{}",
                "output_token_ids": [1],
                "generated_tokens": 1,
                "token_cap_reached": False,
                "elapsed_s": 1.0,
            },
            "diagnostic": {
                "strict_json_valid": False,
                "action_class_correct": False,
                "metric_correct": False,
                "quality_claim": False,
                "resubstitution_only": True,
            },
        }
        for sample_id in ids
    ]
    base_rows = copy.deepcopy(rows)
    for row in base_rows:
        row["parent"] = True
    base_binding = write_json(tmp_path / "inputs/base86.json", base_rows)
    warm_rows_binding = write_json(tmp_path / "inputs/warm86.json", rows)
    misc_binding = write_json(tmp_path / "inputs/misc.json", {"fixture": True})
    plan = {
        "contract": audit.TRAINER_CONTRACT + "-plan",
        "artifact_root": str(tmp_path),
        "output_dir": str(run),
        "claims": dict.fromkeys(audit.FALSE_CLAIMS, False),
        "source_files": [original, original_training, misc_binding],
        "legacy_plan": legacy_binding,
        **dict.fromkeys(audit.HISTORY, misc_binding),
        **dict.fromkeys(audit.EXTRA_SOURCES, misc_binding),
        "closed_prepared_inputs": prepared_binding,
        "base_round": base_binding,
        "warm_round": warm_rows_binding,
        "warm_start": {"weights": warm_binding, "config": config_binding},
        "expected_warm_start_parameter_sha256": warm_digest,
        "expected_base_parameter_sha256": sha,
        "schedule": schedule,
    }
    plan_path = tmp_path / audit.FIXED_PLAN
    plan_binding = write_json(plan_path, plan)
    monkeypatch.setattr(audit, "FIXED_PLAN_SHA", plan_binding["sha256"])
    inventory = audit.source_inventory(plan, plan_binding)
    wal = bytearray()
    draw_events = []
    for index, draw in enumerate(schedule["draws"], 1):
        for stage in frozen.STAGES:
            measurements = (
                {"loss": 1.0, "gradient_norm": 1.0}
                if stage == "attempted"
                else {"elapsed_s": 2.0, "peak_allocated_bytes": 1, "peak_reserved_bytes": 2}
                if stage == "completed"
                else {}
            )
            wal.extend(
                audit.canonical(
                    {"contract": frozen.JOURNAL_CONTRACT, **draw, "stage": stage, **measurements}
                )
                + b"\n"
            )
        draw_events.extend(
            (
                {"event": "draw_start", **draw},
                {"event": "draw_end", **draw, "completed_new_updates": index},
            )
        )
    wal_path = run / "train/training-events.jsonl"
    wal_path.write_bytes(wal)
    (run / "train/draw-events.jsonl").write_bytes(
        b"".join(audit.canonical(row) + b"\n" for row in draw_events)
    )
    periodic = []
    for step in (*range(8, 265, 8), 267):
        directory = run / "train" / (f"checkpoint-{step:03d}" if step != 267 else "adapter")
        adapter_file(directory / "adapter_model.safetensors", parameters, 0.5)
        (directory / "adapter_config.json").write_bytes(Path(config_binding["path"]).read_bytes())
        (directory / "training-events-prefix.jsonl").write_bytes(
            b"".join(wal.splitlines(keepends=True)[: 4 * step])
        )
        digest, checks = audit.fp32_parameter_digest(
            directory / "adapter_model.safetensors", original_audit
        )
        receipt = {
            "completed_new_updates": step,
            "cumulative_campaign_attempts": 13 + step,
            "adapter_parameter_sha256": digest,
            "weights": audit.binding(directory / "adapter_model.safetensors"),
            "config": audit.binding(directory / "adapter_config.json"),
            "journal": audit.binding(directory / "training-events-prefix.jsonl"),
            "optimizer_state_saved": False,
            "scheduler_rng_state_saved": False,
            "exact_resume": False,
            "candidate_promoted": False,
        }
        write_json(directory / "checkpoint.json", receipt)
        if step != 267:
            periodic.append(receipt)
    candidate_binding = write_json(run / "train/candidate-all86.json", rows)
    write_json(run / "reload/reloaded-all86.json", rows)
    metrics = {"sample_count": 86, "resubstitution_only": True, "quality_claim": False}

    def exact_load(parameter_digest, tensor_checks):
        return {
            "saved_parameter_sha256": parameter_digest,
            "loaded_parameter_sha256": parameter_digest,
            "exact_tensor_equality": True,
            "dtype_conversion_performed": False,
            "requires_grad_used_for_identity": False,
            "lora_missing_keys": [],
            "unexpected_keys": [],
            "tensor_count": 112,
            "expected_frozen_base_missing_key_count": 626,
            "tensor_checks": tensor_checks,
        }

    reports = {}
    for phase, started, ended in (
        ("admit-cpu", "00:00:00", "00:01:00"),
        ("train", "00:02:00", "00:03:00"),
        ("reload", "00:04:00", "00:05:00"),
    ):
        value = {
            "contract": audit.TRAINER_CONTRACT,
            "phase": phase,
            "native_exit_code": 0,
            "pid": 123,
            "plan_sha256": plan_binding["sha256"],
            "started_at_utc": "2026-01-01T" + started + "+00:00",
            "ended_at_utc": "2026-01-01T" + ended + "+00:00",
            "source_bindings_unchanged": True,
            "sources_before": inventory,
            "sources_after": inventory,
            "model_loaded": phase != "admit-cpu",
            "optimizer_performed": phase == "train",
            "training_completed": phase == "train",
            "optimizer_update_uncertain": False,
            "old_spent_optimizer_attempts": 13,
            "prior_failed_zero_optimizer_attempts": 0,
            **{
                name: plan[name]
                for name in (
                    "prior_failed_zero_plan",
                    "prior_failed_zero_report",
                    "prior_failed_zero_evidence",
                )
            },
            **dict.fromkeys(audit.FALSE_CLAIMS, False),
            **dict.fromkeys(
                ("optimizer_steps_" + name for name in audit.STAGES), 267 if phase == "train" else 0
            ),
        }
        if phase == "train":
            value.update(
                optimizer_api_calls_started=267,
                cumulative_campaign_attempts=280,
                closed_sample_accesses=357,
                fresh_optimizer_initial_state_empty=True,
                optimizer_attempt_count_scope="conservative_reservations_before_durable_step_intent_not_only_returned_calls",
                journal=frozen.verify_journal(wal_path, schedule, require_complete=True),
                checkpoints=periodic,
                saved_adapter_weights=receipt["weights"],
                saved_adapter_config=receipt["config"],
                candidate_all86=candidate_binding,
                adapter_parameter_sha256_before=warm_digest,
                adapter_parameter_sha256_after=digest,
                base_parameter_sha256_before=sha,
                base_parameter_sha256_after=sha,
                exact_adapter_load=exact_load(warm_digest, warm_checks),
                backward_preflights=probes,
                train_resubstitution_metrics_not_holdout=dict.fromkeys(
                    ("old_base", "old_warm", "new_candidate"), metrics
                ),
            )
        elif phase == "reload":
            value.update(
                fresh_saved_adapter_reload_verified=True,
                closed_sample_accesses=86,
                adapter_parameter_sha256_before=digest,
                adapter_parameter_sha256_after=digest,
                base_parameter_sha256_before=sha,
                base_parameter_sha256_after=sha,
                exact_adapter_load=exact_load(digest, checks),
                reload_resubstitution_metrics_not_holdout=metrics,
            )
        else:
            value.update(closed_CPU86_accepted=True, CPU_acceptance_report=plan["cpu_report"])
        reports[phase] = value
        write_json(run / phase / "report.json", value)
    return {
        "root": tmp_path,
        "run": run,
        "plan": plan,
        "plan_path": plan_path,
        "sha": plan_binding["sha256"],
        "reports": reports,
        "audit": original_audit,
        "rows": rows,
        "digest": digest,
    }


def run_audit(case):
    return audit.audit_terminal(case["plan_path"], case["sha"])


def modify_report(case, phase, key, value):
    case["reports"][phase][key] = value
    write_json(case["run"] / phase / "report.json", case["reports"][phase])


def test_complete_synthetic267_gate(case):
    report = run_audit(case)
    assert report["snapshot_count"] == 34
    assert report["journal"]["event_count"] == 1068
    assert report["draw_event_count"] == 534
    assert report["final_adapter_parameter_sha256"] == case["digest"]
    assert report["candidate_reload_all86_raw_tokens_actor_prefix_exact"] is True
    assert report["raw_diagnostics_independently_rederived"] is True
    assert report["actual_process_exit_independently_observed"] is False
    assert report["actual_tool_exit_status_independently_verified"] is False
    assert all(report[key] is False for key in audit.FALSE_CLAIMS)


@pytest.mark.parametrize("mode", ("missing-train", "missing-reload", "active", "unfinished"))
def test_incomplete_reports_do_not_open_journals_or_create_output(case, monkeypatch, mode):
    if mode.startswith("missing-"):
        (case["run"] / mode.removeprefix("missing-") / "report.json").unlink()
    elif mode == "active":
        (case["run"] / "active-phase.lock").touch()
    else:
        modify_report(case, "train", "native_exit_code", 1)
    original = Path.read_bytes

    def guarded(path):
        assert path.name not in {
            "training-events.jsonl",
            "draw-events.jsonl",
            "training-events-prefix.jsonl",
        }
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    output = case["root"] / "artifacts/diagnostics/onboard-navigation-recovery267-terminal-fixture"
    assert (
        audit.main(
            [
                "--plan",
                str(case["plan_path"]),
                "--plan-sha256",
                case["sha"],
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


@pytest.mark.parametrize(
    "phase,key,value",
    (
        ("train", "native_exit_code", False),
        ("train", "optimizer_steps_attempted", 266),
        ("train", "optimizer_update_uncertain", True),
        ("train", "optimizer_api_calls_started", 266),
        ("train", "closed_sample_accesses", 356),
        ("reload", "closed_sample_accesses", 85),
        ("reload", "optimizer_performed", True),
        ("train", "cumulative_campaign_attempts", 267),
        ("train", "fresh_optimizer_initial_state_empty", False),
        ("train", "quality_claim", True),
        ("reload", "started_at_utc", "2026-01-01T00:02:30+00:00"),
        ("reload", "adapter_parameter_sha256_before", "0" * 64),
        ("train", "adapter_parameter_sha256_after", "0" * 64),
    ),
)
def test_phase_and_parameter_identity_failures(case, phase, key, value):
    modify_report(case, phase, key, value)
    with pytest.raises(ValueError):
        run_audit(case)


@pytest.mark.parametrize(
    "mode", ("missing", "extra", "wrong-prefix", "bad-weight", "wrong-receipt")
)
def test_snapshot_actual_byte_gates(case, mode):
    directory = case["run"] / "train/checkpoint-008"
    if mode == "missing":
        (directory / "checkpoint.json").unlink()
    elif mode == "extra":
        (case["run"] / "train/checkpoint-272").mkdir()
    elif mode == "wrong-prefix":
        path = directory / "training-events-prefix.jsonl"
        path.write_bytes(path.read_bytes().replace(b'"loss":1.0', b'"loss":2.0', 1))
        receipt = audit.strict_json((directory / "checkpoint.json").read_bytes())
        receipt["journal"] = audit.binding(path)
        write_json(directory / "checkpoint.json", receipt)
    elif mode == "bad-weight":
        path = directory / "adapter_model.safetensors"
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
    else:
        receipt = audit.strict_json((directory / "checkpoint.json").read_bytes())
        receipt["completed_new_updates"] = True
        write_json(directory / "checkpoint.json", receipt)
    with pytest.raises(ValueError):
        run_audit(case)


@pytest.mark.parametrize("mode", ("wal-order", "wal-truncated", "draw-error", "draw-type"))
def test_complete_wal_and_draw_events(case, mode):
    name = "training-events.jsonl" if mode.startswith("wal") else "draw-events.jsonl"
    path = case["run"] / "train" / name
    data = path.read_bytes()
    if mode == "wal-truncated":
        path.write_bytes(data[:-1])
    elif mode == "wal-order":
        path.write_bytes(data.replace(b'"stage":"attempted"', b'"stage":"returned"', 1))
    elif mode == "draw-error":
        path.write_bytes(data.replace(b'"event":"draw_end"', b'"event":"draw_error"', 1))
    else:
        path.write_bytes(
            data.replace(b'"completed_new_updates":1,', b'"completed_new_updates":true,', 1)
        )
    with pytest.raises(ValueError):
        run_audit(case)


@pytest.mark.parametrize(
    "mode", ("token", "raw", "prefix", "actor", "id", "diagnostic", "cap", "bool-token")
)
def test_all86_reload_exactness(case, mode):
    rows = copy.deepcopy(case["rows"])
    row = rows[0]
    if mode == "token":
        row["generated"]["output_token_ids"] = [2]
    elif mode == "raw":
        row["generated"]["raw_output"] = "[]"
    elif mode == "prefix":
        row["actual_prefix_tensors"] = {}
    elif mode == "actor":
        row["actor_identity_sha256"] = "0" * 64
    elif mode == "id":
        row["sample_id"] = rows[1]["sample_id"]
    elif mode == "diagnostic":
        row["diagnostic"]["action_class_correct"] = True
    elif mode == "cap":
        row["generated"]["token_cap_reached"] = True
    else:
        row["generated"]["output_token_ids"] = [True]
    write_json(case["run"] / "reload/reloaded-all86.json", rows)
    with pytest.raises(ValueError):
        run_audit(case)


def test_source_arrays_and_current_small_drift(case):
    path = Path(case["plan"]["source_files"][-1]["path"])
    path.write_bytes(path.read_bytes().replace(b"true", b"null"))
    with pytest.raises(ValueError, match="small source"):
        run_audit(case)


def test_source_declaration_conflict_rejected(case):
    plan = copy.deepcopy(case["plan"])
    altered = {**plan["source_files"][0], "sha256": "0" * 64}
    plan["source_files"].append(altered)
    with pytest.raises(ValueError, match="Conflicting"):
        audit.source_inventory(plan, audit.binding(case["plan_path"]))


def test_large_original_source_is_stat_only(case, monkeypatch):
    path = case["root"] / "inputs/large.bin"
    with path.open("wb") as stream:
        stream.truncate(audit.SMALL_LIMIT + 1)
    declaration = {"path": str(path), "bytes": path.stat().st_size, "sha256": "0" * 64}
    original = Path.open

    def guarded(current, *args, **kwargs):
        assert current != path
        return original(current, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    scope = audit.verify_current_sources([declaration])
    assert scope["stat_only_large_sources"] == [declaration]
    assert scope["all_original_source_bytes_independently_rehashed"] is False


def test_sealed_external_runtime_source_is_readonly_not_an_output_allowlist(case, monkeypatch):
    external = case["root"].parent / ("runtime-" + case["root"].name) / "stdlib-runtime.py"
    external.parent.mkdir()
    external.write_bytes(b"# synthetic external runtime\n")
    row = audit.binding(external, allow_external=True)
    plan = copy.deepcopy(case["plan"])
    plan["source_files"].append(row)
    plan_binding = write_json(case["plan_path"], plan)
    case["plan"], case["sha"] = plan, plan_binding["sha256"]
    monkeypatch.setattr(audit, "FIXED_PLAN_SHA", case["sha"])
    inventory = audit.source_inventory(plan, plan_binding)
    for phase, report in case["reports"].items():
        report.update(plan_sha256=case["sha"], sources_before=inventory, sources_after=inventory)
        write_json(case["run"] / phase / "report.json", report)
    result = run_audit(case)
    assert row in result["source_scope"]["byte_hashed_small_sources"]
    with pytest.raises(ValueError, match="escapes"):
        audit.output_target(external.parent / "onboard-navigation-recovery267-terminal-invalid")
    with pytest.raises(ValueError, match="escapes"):
        audit.read_bound(row)
    with pytest.raises(ValueError, match="escapes"):
        audit.safe_path("../" + external.parent.name + "/" + external.name, allow_external=True)
    external.write_bytes(b"# synthetic external DRIFT!!\n")
    with pytest.raises(ValueError):
        run_audit(case)


def test_raw_summary_mismatch_does_not_accept_saved_metrics(case):
    report = case["reports"]["train"]
    report["train_resubstitution_metrics_not_holdout"]["new_candidate"] = {
        "sample_count": 86,
        "resubstitution_only": True,
        "quality_claim": False,
        "strict_contract_valid": 86,
    }
    write_json(case["run"] / "train/report.json", report)
    with pytest.raises(ValueError, match="independently diagnosed"):
        run_audit(case)


@pytest.mark.parametrize("mode", ("dtype", "overlap", "extra-payload", "missing-key"))
def test_fp32_header_payload_gates(case, mode):
    path = case["run"] / "train/adapter/adapter_model.safetensors"
    raw = path.read_bytes()
    size = struct.unpack_from("<Q", raw)[0]
    header = audit.strict_json(raw[8 : 8 + size])
    keys = list(header)
    if mode == "dtype":
        header[keys[0]]["dtype"] = "BF16"
    elif mode == "overlap":
        header[keys[1]]["data_offsets"] = header[keys[0]]["data_offsets"]
    elif mode == "missing-key":
        del header[keys[0]]
    payload = raw[8 + size :] + (b"\0\0\0\0" if mode == "extra-payload" else b"")
    encoded = audit.canonical(header)
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + payload)
    with pytest.raises(ValueError):
        audit.fp32_parameter_digest(path, case["audit"])


def test_fp32_digest_preserves_original_order_and_rejects_nonfinite(case):
    path = case["run"] / "train/adapter/adapter_model.safetensors"
    reversed_audit = copy.deepcopy(case["audit"])
    reversed_audit["trainable_parameters"].reverse()
    assert audit.fp32_parameter_digest(path, reversed_audit)[0] != case["digest"]
    raw = bytearray(path.read_bytes())
    size = struct.unpack_from("<Q", raw)[0]
    struct.pack_into("<f", raw, 8 + size, float("nan"))
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="nonfinite"):
        audit.fp32_parameter_digest(path, case["audit"])


def test_create_only_cli_and_output_is_not_quality_acceptance(case, monkeypatch):
    monkeypatch.setattr(
        audit,
        "__file__",
        str(case["root"] / "scripts/audit_onboard_navigation_recovery267_terminal.py"),
    )
    for name in (
        "scripts/audit_onboard_navigation_recovery267_terminal.py",
        "tests/test_audit_onboard_navigation_recovery267_terminal.py",
    ):
        path = case["root"] / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"# synthetic fixture\n")
    output = case["root"] / "artifacts/diagnostics/onboard-navigation-recovery267-terminal-fixture"
    args = [
        "--plan",
        str(case["plan_path"]),
        "--plan-sha256",
        case["sha"],
        "--output-dir",
        str(output),
    ]
    assert audit.main(args) == 0
    before = (output / "report.json").read_bytes()
    assert audit.main(args) == 1
    assert (output / "report.json").read_bytes() == before
    with pytest.raises(ValueError):
        audit.output_target(case["root"] / "artifacts/diagnostics/not-terminal")


def test_stdlib_only_import_in_isolated_no_site_interpreter():
    path = Path(audit.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"s=importlib.util.spec_from_file_location('terminal_audit',{str(path)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "blocked={'torch','transformers','peft','accelerate','numpy','PIL','requests','pyarrow'}; "
        "assert not blocked.intersection(sys.modules); print('stdlib-only import verified')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "stdlib-only import verified" in result.stdout


def test_real_raw_diagnostic_parser_chain_is_stdlib_only():
    root = Path(audit.__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path[:0]=[{str(root)!r},{str(root / 'src')!r}]; "
        "from scripts import train_onboard_navigation_streamed as old; "
        "from scripts import train_onboard_vlm_navigation_pilot as pilot; "
        "target={'schema_version':1,'contract':'multidetect.onboard-planned-navigation-target-v1',"
        "'kind':'translate','translation_frame':'body_frd','yaw_convention':'clockwise_right_positive',"
        "'translation_m':[1.0,0.0,0.0],'yaw_delta_rad':None}; "
        "import json; raw=json.dumps(target,separators=(',',':')); "
        "diag=pilot.diagnose(raw,target); "
        "assert diag['strict_json_valid'] and diag['metric_correct']; "
        "sid='a'*64; prepared={sid:{'actor_identity_sha256':'b'*64,'prefix_tensors':{}}}; "
        "row={'sample_id':sid,'gold_action_class':'forward','parent':False,"
        "'actor_identity_sha256':'b'*64,'actual_prefix_tensors':{},'diagnostic':diag,"
        "'generated':{'raw_output':raw,'output_token_ids':[1],'generated_tokens':1,"
        "'token_cap_reached':False,'elapsed_s':0.5}}; "
        "summary=old.diagnose_rows([row],[sid],prepared,{sid:target},pilot,parent=False); "
        "assert summary['metric_correct']==1 and summary['quality_claim'] is False; "
        "blocked={'torch','PIL','numpy','requests','transformers','peft','accelerate','pyarrow'}; "
        "assert not blocked.intersection(sys.modules); "
        "print('strict raw diagnostic stdlib-only verified')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "strict raw diagnostic stdlib-only verified" in result.stdout


def test_strict_json_rejects_duplicate_and_overflow():
    for value in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'):
        with pytest.raises(ValueError):
            audit.strict_json(value)


def test_frozen_journal_lazy_import_does_not_load_ml():
    # Import the real frozen gate without any model/source-admission execution.
    spec = importlib.util.spec_from_file_location(
        "_terminal_audit_lazy_test", Path(frozen.__file__)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.budget_api().__file__ == budget.__file__
    assert callable(module.verify_journal)
    assert not {"torch", "transformers", "peft", "accelerate", "PIL"}.intersection(sys.modules)
