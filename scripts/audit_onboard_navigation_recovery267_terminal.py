"""Read-only filesystem/FP32 consistency audit of the sealed recovery267 r2 run.

This stdlib-only entry is not a training runner or a process supervisor. It
refuses incomplete phase reports before opening any training journal. Reported
phase exit codes are checked, not promoted to independently observed tool exits
or process termination. Large original sources are stat-only in this audit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXED_PLAN = "configs/training/onboard-navigation-recovery-20261008-r2.json"
FIXED_PLAN_SHA = "0c50e812781e4cbbf0f4935714486ae78e65e252a7fd0e33d2c321bb10602979"
FIXED_RUN = "artifacts/models/onboard-navigation-recovery-20261008-r2"
CONTRACT = "multidetect.onboard-navigation-recovery267-terminal-filesystem-audit-v1"
TRAINER_CONTRACT = "multidetect.onboard-navigation-recovery267-v2"
TRAINER = "scripts/train_onboard_navigation_recovery_v2.py"
TRAINER_SHA = "03360b614a1199ca4c4de25f66a7defde16e319a5a3e9bf5b2fb2658cb2f5a65"
BUDGET = "scripts/onboard_navigation_recovery_budget.py"
BUDGET_SHA = "56580ea056279027867c82ff70e2e2da0162424fd225fc4f58e1e1b22d9bac71"
SMALL_LIMIT = 2 * 1024 * 1024
ADAPTER_LIMIT = 4 * 1024 * 1024
STAGES = ("attempted", "returned", "synchronized", "completed")
CLASSES = ("forward", "left", "right", "stop")
HISTORY = (
    "failure_evidence",
    "failure_report",
    "failure_journal",
    "completion",
    "prepared_inputs",
    "baseline_evidence",
)
EXTRA_SOURCES = (
    "baseline_accepted",
    "baseline_report",
    "base_round",
    "warm_round",
    "cpu_report",
    "cpu_plan",
    "closed_prepared_inputs",
    "prior_failed_zero_plan",
    "prior_failed_zero_report",
    "prior_failed_zero_evidence",
)
FALSE_CLAIMS = (
    "quality_claim",
    "candidate_promoted",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "execution_authority",
    "transport_created",
    "mission_created",
    "independent_holdout_available",
    "sim_real_parity_verified",
    "exact_resume",
    "optimizer_state_restored",
    "scheduler_state_restored",
    "rng_state_restored",
    "prior_journal_copied",
    "budget_reset",
    "default_model_changed",
)
PARAMETER = re.compile(
    r"base_model\.model\.model\.language_model\.layers\.\d+\.self_attn\."
    r"(q_proj|v_proj)\.lora_(A|B)\.default\.weight"
)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def reject(value):
        raise ValueError("Nonfinite JSON constant: " + value)

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    canonical(value)
    return value


def check(condition, message):
    if not condition:
        raise ValueError(message)


def exact_int(value, expected, name):
    check(type(value) is int and value == expected, name + " differs")


def sha(value):
    check(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "Canonical SHA-256 is required",
    )
    return value


def safe_path(path, *, exists=True, allow_external=False):
    path = Path(path)
    originally_absolute = path.is_absolute()
    if not originally_absolute:
        path = ROOT / path
    resolved = path.resolve(strict=exists)
    check(
        resolved.is_relative_to(ROOT.resolve()) or (allow_external and originally_absolute),
        "Path escapes the authoritative repository",
    )
    # Resolve is necessary but insufficient: an in-repository link is not a
    # create-only evidence target or an original bound payload either.
    cursor = path.absolute()
    while cursor != ROOT.absolute() and cursor.is_relative_to(ROOT.absolute()):
        check(not cursor.is_symlink(), "Symlink evidence paths are not admitted")
        cursor = cursor.parent
    return resolved


def binding(path, *, limit=SMALL_LIMIT, allow_external=False):
    path = safe_path(path, allow_external=allow_external)
    size = path.stat().st_size
    check(path.is_file() and 0 < size <= limit, "File is not within the explicit byte budget")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(65536):
            digest.update(data)
    check(path.stat().st_size == size, "File size changed during hashing")
    return {"path": str(path), "bytes": size, "sha256": digest.hexdigest()}


def normalize(row, *, allow_external=False):
    check(
        type(row) is dict and set(row) == {"path", "bytes", "sha256"},
        "Exact path/bytes/SHA binding required",
    )
    check(
        type(row["path"]) is str and type(row["bytes"]) is int and row["bytes"] > 0,
        "Invalid file binding types",
    )
    return {
        "path": str(safe_path(row["path"], allow_external=allow_external)),
        "bytes": row["bytes"],
        "sha256": sha(row["sha256"]),
    }


def read_json(path):
    row = binding(path)
    value = strict_json(Path(row["path"]).read_bytes())
    check(binding(path) == row, "JSON file changed during reading")
    return value, row


def read_bound(row):
    declared = normalize(row)
    value, current = read_json(declared["path"])
    check(current == declared, "Bound JSON bytes changed")
    return value


def source_inventory(plan, plan_binding):
    rows = [
        plan["legacy_plan"],
        *(plan[name] for name in HISTORY),
        *(plan[name] for name in EXTRA_SOURCES),
        *plan["warm_start"].values(),
        plan_binding,
    ]
    result = {}
    # Only source_files in the exact sealed plan may authorize external,
    # read-only runtime inputs. Artifact bindings and output paths stay local.
    for row, external in [
        *((row, True) for row in plan["source_files"]),
        *((row, False) for row in rows),
    ]:
        normalized = normalize(row, allow_external=external)
        key = normalized["path"]
        check(key not in result or result[key] == normalized, "Conflicting source declarations")
        result[key] = normalized
    return [result[key] for key in sorted(result)]


def verify_current_sources(inventory):
    small, stat_only = [], []
    for row in inventory:
        path = Path(row["path"])
        check(path.is_file() and path.stat().st_size == row["bytes"], "Source size changed")
        if row["bytes"] <= SMALL_LIMIT:
            check(binding(path, allow_external=True) == row, "Current small source bytes changed")
            small.append(row)
        else:
            stat_only.append(row)
    return {
        "byte_hashed_small_sources": small,
        "stat_only_large_sources": stat_only,
        "all_original_source_bytes_independently_rehashed": not stat_only,
    }


def timestamp(value):
    check(type(value) is str, "Phase timestamp must be an explicit string")
    parsed = datetime.fromisoformat(value)
    check(
        parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0,
        "Phase timestamp must be UTC",
    )
    return parsed


def phase_report(value, phase, plan, plan_sha):
    check(
        type(value) is dict
        and value.get("contract") == TRAINER_CONTRACT
        and value.get("phase") == phase,
        "Phase contract/identity differs",
    )
    exact_int(value.get("native_exit_code"), 0, phase + " reported native exit")
    check(
        value.get("plan_sha256") == plan_sha and value.get("source_bindings_unchanged") is True,
        "Phase plan/source identity differs",
    )
    check(type(value.get("pid")) is int and value["pid"] > 0, "Reported worker PID is invalid")
    check(
        not any(key in value for key in ("failure", "failure_type", "journal_validation_error")),
        "A success phase still contains failure evidence",
    )
    check(all(value.get(name) is False for name in FALSE_CLAIMS), "Phase claims exceed scope")
    check(
        value.get("optimizer_update_uncertain") is False, "Uncertain optimizer reservation remains"
    )
    exact_int(value.get("old_spent_optimizer_attempts"), 13, "Preserved old spent ledger")
    exact_int(value.get("prior_failed_zero_optimizer_attempts"), 0, "Preserved zero-update attempt")
    for name in (
        "prior_failed_zero_plan",
        "prior_failed_zero_report",
        "prior_failed_zero_evidence",
    ):
        check(
            canonical(value.get(name)) == canonical(plan[name]), "Prior zero-update binding differs"
        )
    start, end = timestamp(value.get("started_at_utc")), timestamp(value.get("ended_at_utc"))
    check(start <= end <= datetime.now(UTC), "Phase chronology is incomplete or in the future")
    expected = 267 if phase == "train" else 0
    for stage in STAGES:
        exact_int(value.get("optimizer_steps_" + stage), expected, phase + " " + stage)
    if phase == "train":
        check(
            value.get("training_completed") is True
            and value.get("optimizer_performed") is True
            and value.get("model_loaded") is True,
            "Actual completed training flags are required",
        )
        exact_int(value.get("optimizer_api_calls_started"), 267, "Actual optimizer API calls")
        exact_int(value.get("cumulative_campaign_attempts"), 280, "Cumulative spent budget")
        exact_int(value.get("closed_sample_accesses"), 357, "Train closed-input access count")
        check(
            value.get("fresh_optimizer_initial_state_empty") is True
            and value.get("optimizer_attempt_count_scope")
            == "conservative_reservations_before_durable_step_intent_not_only_returned_calls",
            "Fresh optimizer/conservative reservation evidence differs",
        )
    elif phase == "reload":
        check(
            value.get("model_loaded") is True
            and value.get("optimizer_performed") is False
            and value.get("training_completed") is False
            and value.get("fresh_saved_adapter_reload_verified") is True,
            "Fresh reload phase flags differ",
        )
        exact_int(value.get("closed_sample_accesses"), 86, "Reload closed-input access count")
    else:
        check(
            value.get("model_loaded") is False
            and value.get("optimizer_performed") is False
            and value.get("training_completed") is False
            and value.get("closed_CPU86_accepted") is True
            and canonical(value.get("CPU_acceptance_report")) == canonical(plan["cpu_report"]),
            "CPU admission phase differs",
        )
    return start, end


def frozen_journal_verifier(plan):
    """Import only the authenticated stdlib trainer/budget, never its history()."""
    declared = {row["path"]: row for row in source_inventory(plan, binding(ROOT / FIXED_PLAN))}
    for name, expected in ((TRAINER, TRAINER_SHA), (BUDGET, BUDGET_SHA)):
        current = binding(ROOT / name)
        check(
            current["sha256"] == expected and declared.get(current["path"]) == current,
            "Frozen journal gate source changed before import",
        )
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "_recovery267_terminal_frozen_gate", ROOT / TRAINER
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    budget_module = module.budget_api()
    check(
        Path(budget_module.__file__).resolve() == (ROOT / BUDGET).resolve(),
        "Cached budget module is not the bound source",
    )
    return module.verify_journal


def fp32_parameter_digest(path, audit):
    """Same original names/order/canonical bytes as saved_parameter_digest;
    inspect little-endian saved FP32 bytes directly instead of importing Torch.
    """
    path = safe_path(path)
    check(
        path.is_file() and 8 < path.stat().st_size <= ADAPTER_LIMIT,
        "Adapter size is outside the explicit byte budget",
    )
    raw = path.read_bytes()
    check(8 < len(raw) <= ADAPTER_LIMIT, "Adapter changed outside the explicit byte budget")
    header_size = struct.unpack_from("<Q", raw)[0]
    check(1 <= header_size <= 65536 and 8 + header_size < len(raw), "Invalid safetensors header")
    header = strict_json(raw[8 : 8 + header_size])
    check(type(header) is dict, "Safetensors header must be an object")
    entries = {key: value for key, value in header.items() if key != "__metadata__"}
    parameters = audit.get("trainable_parameters")
    check(
        type(parameters) is list and len(parameters) == 112 and len(entries) == 112,
        "Exactly112 audited FP32 tensors are required",
    )
    payload = memoryview(raw)[8 + header_size :]
    spans, keys, names, tensor_checks, total = [], set(), set(), [], 0
    digest = hashlib.sha256()
    for item in parameters:
        name = item.get("name")
        match = PARAMETER.fullmatch(name) if type(name) is str else None
        check(
            match is not None and name not in names and item.get("dtype") == "torch.float32",
            "Original parameter audit is not unique FP32 language q/v LoRA",
        )
        names.add(name)
        key = name.replace(".default.", ".")
        keys.add(key)
        entry = entries.get(key)
        check(
            type(entry) is dict and set(entry) == {"dtype", "shape", "data_offsets"},
            "Saved adapter tensor fields differ",
        )
        shape, offsets = entry["shape"], entry["data_offsets"]
        check(
            entry["dtype"] == "F32"
            and type(shape) is list
            and len(shape) == 2
            and all(type(n) is int and n > 0 for n in shape)
            and shape[0 if match[2] == "A" else 1] == 4,
            "FP32 LoRA dtype/rank/shape differs",
        )
        count = math.prod(shape)
        exact_int(item.get("numel"), count, "Audited tensor size")
        check(
            type(offsets) is list
            and len(offsets) == 2
            and all(type(n) is int for n in offsets)
            and 0 <= offsets[0] < offsets[1] <= len(payload)
            and offsets[1] - offsets[0] == 4 * count,
            "Invalid saved tensor span",
        )
        segment = payload[offsets[0] : offsets[1]]
        check(
            all(math.isfinite(value[0]) for value in struct.iter_unpack("<f", segment)),
            "Saved FP32 parameters contain nonfinite values",
        )
        digest.update(canonical([name, shape, "torch.float32"]))
        digest.update(segment)
        spans.append(tuple(offsets))
        total += count
        tensor_checks.append(
            {
                "original_parameter_name": name,
                "saved_key": key,
                "shape": shape,
                "dtype": "torch.float32",
                "numel": count,
                "loaded_bytes_equal_saved": True,
            }
        )
    exact_int(audit.get("trainable_parameter_count"), total, "Original total parameter count")
    check(keys == set(entries), "Unexpected saved adapter tensors")
    cursor = 0
    for start, end in sorted(spans):
        check(start == cursor, "Saved tensor spans overlap or omit payload bytes")
        cursor = end
    check(cursor == len(payload), "Unclaimed saved tensor payload bytes")
    return digest.hexdigest(), tensor_checks


def check_exact_load(report, expected_digest, checks):
    value = report.get("exact_adapter_load")
    check(
        type(value) is dict
        and value.get("saved_parameter_sha256") == expected_digest
        and value.get("loaded_parameter_sha256") == expected_digest
        and value.get("exact_tensor_equality") is True
        and value.get("dtype_conversion_performed") is False
        and value.get("requires_grad_used_for_identity") is False
        and value.get("lora_missing_keys") == []
        and value.get("unexpected_keys") == [],
        "Exact adapter load evidence differs",
    )
    exact_int(value.get("tensor_count"), 112, "Loaded tensor count")
    exact_int(
        value.get("expected_frozen_base_missing_key_count"), 626, "Frozen base missing-key count"
    )
    check(
        canonical(value.get("tensor_checks")) == canonical(checks), "Loaded tensor mapping differs"
    )


def check_draw_events(path, schedule):
    row = binding(path)
    raw = Path(row["path"]).read_bytes()
    check(raw.endswith(b"\n"), "Draw events are truncated")
    observed = [strict_json(line) for line in raw.splitlines()]
    expected = []
    for index, draw in enumerate(schedule["draws"], 1):
        expected.extend(
            (
                {"event": "draw_start", **draw},
                {"event": "draw_end", **draw, "completed_new_updates": index},
            )
        )
    check(
        len(observed) == 534 and canonical(observed) == canonical(expected),
        "All267 exact draw start/end pairs are required",
    )
    return row


def check_snapshots(run, train, wal, audit, plan, bindings):
    expected_steps = list(range(8, 265, 8))
    expected_names = {f"checkpoint-{step:03d}" for step in expected_steps}
    actual_names = {
        path.name for path in (run / "train").iterdir() if path.name.startswith("checkpoint-")
    }
    check(actual_names == expected_names, "Exactly33 periodic checkpoint directories are required")
    check(
        type(train.get("checkpoints")) is list and len(train["checkpoints"]) == 33,
        "Exactly33 periodic checkpoint receipts are required",
    )
    config_reference = read_bound(plan["warm_start"]["config"])
    results = []
    wal_lines = wal.splitlines(keepends=True)
    for index, step in enumerate([*expected_steps, 267]):
        directory = run / "train" / (f"checkpoint-{step:03d}" if step != 267 else "adapter")
        check(
            directory.is_dir()
            and {path.name for path in directory.iterdir()}
            == {
                "checkpoint.json",
                "adapter_model.safetensors",
                "adapter_config.json",
                "training-events-prefix.jsonl",
            },
            "Snapshot files are incomplete or unexpected",
        )
        receipt, receipt_binding = read_json(directory / "checkpoint.json")
        bindings.append(receipt_binding)
        exact_int(receipt.get("completed_new_updates"), step, "Snapshot completed updates")
        exact_int(
            receipt.get("cumulative_campaign_attempts"), 13 + step, "Snapshot cumulative budget"
        )
        check(
            all(
                receipt.get(key) is False
                for key in (
                    "optimizer_state_saved",
                    "scheduler_rng_state_saved",
                    "exact_resume",
                    "candidate_promoted",
                )
            ),
            "Snapshot overstates resume or promotion",
        )
        for key, name in (
            ("weights", "adapter_model.safetensors"),
            ("config", "adapter_config.json"),
            ("journal", "training-events-prefix.jsonl"),
        ):
            actual = binding(
                directory / name, limit=ADAPTER_LIMIT if key == "weights" else SMALL_LIMIT
            )
            check(normalize(receipt.get(key)) == actual, "Snapshot " + key + " bytes/path changed")
            bindings.append(actual)
        check(
            canonical(read_bound(receipt["config"])) == canonical(config_reference),
            "Snapshot adapter configuration differs from the original warm adapter",
        )
        prefix = Path(receipt["journal"]["path"]).read_bytes()
        check(
            prefix == b"".join(wal_lines[: 4 * step]) and len(prefix.splitlines()) == 4 * step,
            "Snapshot journal is not the exact complete WAL prefix",
        )
        digest, checks = fp32_parameter_digest(directory / "adapter_model.safetensors", audit)
        check(
            digest == sha(receipt.get("adapter_parameter_sha256")),
            "Snapshot FP32 raw digest differs",
        )
        if step != 267:
            check(
                canonical(receipt) == canonical(train["checkpoints"][index]),
                "Train periodic receipt differs from actual checkpoint receipt",
            )
        results.append(
            {
                "completed_new_updates": step,
                "adapter_parameter_sha256": digest,
                "receipt": receipt_binding,
                "journal_rows": 4 * step,
            }
        )
    final = read_bound(results[-1]["receipt"])
    check(
        normalize(train.get("saved_adapter_weights")) == normalize(final["weights"])
        and normalize(train.get("saved_adapter_config")) == normalize(final["config"]),
        "Final saved adapter differs from the train report",
    )
    return results, digest, checks


def validate_rows(rows, ids, prepared, classes, *, parent, resubstitution):
    check(
        type(rows) is list and len(rows) == 86 and [row.get("sample_id") for row in rows] == ids,
        "All86 diagnostic identities/order are required",
    )
    for row in rows:
        sample_id = row["sample_id"]
        check(
            row.get("parent") is parent and row.get("gold_action_class") == classes[sample_id],
            "Row parent/gold class differs",
        )
        check(
            row.get("actor_identity_sha256") == prepared[sample_id]["actor_identity_sha256"]
            and canonical(row.get("actual_prefix_tensors"))
            == canonical(prepared[sample_id]["prefix_tensors"]),
            "Actual actor/prefix identity differs from the accepted CPU input",
        )
        if resubstitution:
            check(
                row.get("resubstitution_only") is True,
                "Candidate/reload must be labeled resubstitution",
            )
        generated = row.get("generated")
        check(type(generated) is dict, "Generated output metadata is required")
        raw, tokens, elapsed = (
            generated.get("raw_output"),
            generated.get("output_token_ids"),
            generated.get("elapsed_s"),
        )
        check(
            type(raw) is str
            and len(raw.encode("utf-8")) <= 65536
            and type(tokens) is list
            and 1 <= len(tokens) <= 192
            and all(type(token) is int and token >= 0 for token in tokens)
            and type(elapsed) in (int, float)
            and math.isfinite(elapsed)
            and elapsed >= 0,
            "Generated raw/token/elapsed bounds differ",
        )
        exact_int(generated.get("generated_tokens"), len(tokens), "Generated token count")
        check(generated.get("token_cap_reached") is (len(tokens) >= 192), "Token cap flag differs")
        diagnostic = row.get("diagnostic")
        check(
            type(diagnostic) is dict
            and diagnostic.get("quality_claim") is False
            and diagnostic.get("resubstitution_only") is True
            and all(
                type(diagnostic.get(key)) is bool
                for key in ("strict_json_valid", "action_class_correct", "metric_correct")
            ),
            "Stored diagnostic types/scope differ",
        )


def rederive_diagnostics(plan, legacy, original, prepared, rounds, inventory):
    """Recompute original strict raw diagnostics without loading model or RGB.

    Gold targets are read solely for offline scoring, never supplied to an
    actor/processor/model by this entry. Imports of the frozen parser chain
    remain stdlib-only and are authenticated before use.
    """
    code_names = (
        TRAINER,
        "scripts/train_onboard_navigation_streamed.py",
        "scripts/train_onboard_vlm_navigation_pilot.py",
        "scripts/train_onboard_vlm_grounding.py",
        "scripts/verify_onboard_vlm_grounding_adapter.py",
        "scripts/probe_onboard_vlm.py",
        "src/multidetect/autonomy/onboard_navigation_supervision.py",
        "src/multidetect/autonomy/serialization.py",
    )
    declarations = {row["path"]: row for row in inventory}
    for name in code_names:
        actual = binding(ROOT / name)
        check(
            declarations.get(actual["path"]) == actual,
            "Raw-diagnostic parser source is not the sealed execution source",
        )
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import train_onboard_navigation_recovery_v2 as gate
    from scripts import train_onboard_navigation_streamed as old_api

    check(
        Path(gate.__file__).resolve() == ROOT / TRAINER
        and Path(old_api.__file__).resolve() == ROOT / code_names[1],
        "Cached raw-diagnostic module source differs",
    )
    blocked = {"torch", "PIL", "numpy", "requests", "transformers", "peft", "accelerate", "pyarrow"}
    check(
        not blocked.intersection(sys.modules),
        "Heavy dependencies are not admitted in terminal audit",
    )
    source = normalize(original["dataset_samples"])
    check(binding(source["path"]) == source, "Original86 gold sample bytes changed")
    rows = [
        strict_json(line) for line in Path(source["path"]).read_bytes().splitlines() if line.strip()
    ]
    targets, pools = gate.targets_from_rows(rows, original["train_sample_ids"], prepared)
    check(
        canonical(pools) == canonical(plan["schedule"]["sources"]),
        "Original86 gold classes differ from the sealed schedule",
    )
    with old_api.repository_imports():
        from scripts import train_onboard_vlm_navigation_pilot as pilot

        check(
            Path(pilot.__file__).resolve() == ROOT / code_names[2],
            "Cached pilot parser source differs",
        )
        results = {
            name: old_api.diagnose_rows(
                values,
                original["train_sample_ids"],
                prepared,
                targets,
                pilot,
                parent=(name == "old_base"),
            )
            for name, values in rounds.items()
        }
    check(not blocked.intersection(sys.modules), "Raw scoring imported a heavy dependency")
    check(binding(source["path"]) == source, "Gold sample bytes changed during scoring")
    return results


def audit_terminal(plan_path, plan_sha):
    """No journal is opened until both complete native0 phase reports pass."""
    check(
        safe_path(plan_path) == (ROOT / FIXED_PLAN).resolve(), "Only the fixed r2 plan is admitted"
    )
    check(sha(plan_sha) == FIXED_PLAN_SHA, "Only the sealed r2 plan SHA is admitted")
    plan, plan_binding = read_json(plan_path)
    check(
        plan_binding["sha256"] == plan_sha and plan.get("contract") == TRAINER_CONTRACT + "-plan",
        "Fixed r2 plan bytes/contract changed",
    )
    run = safe_path(plan["output_dir"])
    check(
        run == (ROOT / FIXED_RUN).resolve() and safe_path(plan["artifact_root"]) == ROOT.resolve(),
        "Fixed r2 output/repository identity differs",
    )
    check(not (run / "active-phase.lock").exists(), "A recovery phase is still active")
    check(
        all(plan.get("claims", {}).get(name) is False for name in FALSE_CLAIMS),
        "Plan claims exceed scope",
    )
    train, train_binding = read_json(run / "train/report.json")
    reload, reload_binding = read_json(run / "reload/report.json")
    train_times = phase_report(train, "train", plan, plan_sha)
    reload_times = phase_report(reload, "reload", plan, plan_sha)
    check(train_times[1] <= reload_times[0], "Reload began before train finished")
    accepted, accepted_binding = read_json(run / "admit-cpu/report.json")
    admitted_times = phase_report(accepted, "admit-cpu", plan, plan_sha)
    check(admitted_times[1] <= train_times[0], "Training began before CPU admission finished")
    inventory = source_inventory(plan, plan_binding)
    for value in (accepted, train, reload):
        check(
            canonical(value.get("sources_before")) == canonical(inventory)
            and canonical(value.get("sources_after")) == canonical(inventory),
            "Actual phase before/after sources differ from the complete plan inventory",
        )
    source_scope = verify_current_sources(inventory)
    legacy = read_bound(plan["legacy_plan"])
    original = read_bound(legacy["original_plan"])
    original_training = read_bound(legacy["original_training_report"])
    audit = original_training["trainable_audit"]
    ids = original["train_sample_ids"]
    check(
        type(ids) is list and len(ids) == 86 and len(set(ids)) == 86,
        "Original86 sample IDs must be unique",
    )
    prepared = read_bound(plan["closed_prepared_inputs"])
    check(
        set(prepared) == set(ids) and legacy["diagnostics"]["sample_ids"] == ids,
        "Complete original/CPU86 identities differ",
    )
    classes = {
        sample_id: name
        for name, values in plan["schedule"]["sources"].items()
        for sample_id in values
    }
    check(len(classes) == 86 and set(classes) == set(ids), "Schedule does not cover the original86")
    verifier = frozen_journal_verifier(plan)
    wal_path = run / "train/training-events.jsonl"
    wal_binding = binding(wal_path)
    journal = verifier(wal_path, plan["schedule"], require_complete=True)
    check(
        canonical(journal) == canonical(train.get("journal")),
        "Train journal summary differs from actual WAL",
    )
    wal = wal_path.read_bytes()
    check(wal.endswith(b"\n") and len(wal.splitlines()) == 1068, "Complete1068-row WAL required")
    bindings = [
        plan_binding,
        accepted_binding,
        train_binding,
        reload_binding,
        wal_binding,
        check_draw_events(run / "train/draw-events.jsonl", plan["schedule"]),
    ]
    snapshots, digest, checks = check_snapshots(run, train, wal, audit, plan, bindings)
    check(
        train.get("adapter_parameter_sha256_before") == plan["expected_warm_start_parameter_sha256"]
        and digest != plan["expected_warm_start_parameter_sha256"],
        "Original warm/candidate parameter identity differs",
    )
    for value in (train, reload):
        check(
            value.get("base_parameter_sha256_before") == plan["expected_base_parameter_sha256"]
            and value.get("base_parameter_sha256_after") == plan["expected_base_parameter_sha256"],
            "Reported frozen base identity differs",
        )
    check(
        train.get("adapter_parameter_sha256_after") == digest
        and reload.get("adapter_parameter_sha256_before") == digest
        and reload.get("adapter_parameter_sha256_after") == digest,
        "Final saved/train-after/reload-before/reload-after raw parameter identity differs",
    )
    check_exact_load(reload, digest, checks)
    warm_digest, warm_checks = fp32_parameter_digest(plan["warm_start"]["weights"]["path"], audit)
    check(
        warm_digest == plan["expected_warm_start_parameter_sha256"],
        "Warm-start saved FP32 identity changed",
    )
    current_warm_binding = binding(plan["warm_start"]["weights"]["path"], limit=ADAPTER_LIMIT)
    check(
        current_warm_binding == normalize(plan["warm_start"]["weights"]),
        "Original warm-start full checkpoint bytes changed",
    )
    bindings.append(current_warm_binding)
    check_exact_load(train, warm_digest, warm_checks)
    probes = legacy["diagnostics"]["smoke_probes"]
    check(
        type(train.get("backward_preflights")) is list
        and len(train["backward_preflights"]) == 4
        and [row.get("sample_id") for row in train["backward_preflights"]]
        == [row["sample_id"] for row in probes],
        "Four original backward-only probes differ",
    )
    candidate = read_bound(train["candidate_all86"])
    check(
        normalize(train["candidate_all86"])["path"]
        == str((run / "train/candidate-all86.json").resolve()),
        "Candidate file is not the fixed train output",
    )
    reloaded, reloaded_binding = read_json(run / "reload/reloaded-all86.json")
    bindings.extend([normalize(train["candidate_all86"]), reloaded_binding])
    base, warm = read_bound(plan["base_round"]), read_bound(plan["warm_round"])
    for rows, parent, resubstitution in (
        (base, True, False),
        (warm, False, False),
        (candidate, False, True),
        (reloaded, False, True),
    ):
        validate_rows(rows, ids, prepared, classes, parent=parent, resubstitution=resubstitution)
    for prior, actual in zip(candidate, reloaded, strict=True):
        for key in (
            "sample_id",
            "gold_action_class",
            "parent",
            "actor_identity_sha256",
            "actual_prefix_tensors",
            "diagnostic",
            "resubstitution_only",
        ):
            check(
                canonical(actual[key]) == canonical(prior[key]),
                "Candidate/reload row identity changed: " + key,
            )
        for key in ("raw_output", "output_token_ids", "generated_tokens", "token_cap_reached"):
            check(
                canonical(actual["generated"][key]) == canonical(prior["generated"][key]),
                "Candidate/reload generation changed: " + key,
            )
    # Generation timing is recomputed from each phase's own raw rows; elapsed
    # values need not match across phases. None of these resubstitution metrics
    # are accepted as improvement, independent quality or real-time evidence.
    metrics = train.get("train_resubstitution_metrics_not_holdout")
    check(
        type(metrics) is dict and set(metrics) == {"old_base", "old_warm", "new_candidate"},
        "Full86 baseline/candidate diagnostic summaries are required",
    )
    for summary in (*metrics.values(), reload.get("reload_resubstitution_metrics_not_holdout")):
        check(
            type(summary) is dict
            and summary.get("resubstitution_only") is True
            and summary.get("quality_claim") is False,
            "Metrics overstate training-set scope",
        )
        exact_int(summary.get("sample_count"), 86, "Summary sample count")
    recomputed = rederive_diagnostics(
        plan,
        legacy,
        original,
        prepared,
        {"old_base": base, "old_warm": warm, "new_candidate": candidate, "reloaded": reloaded},
        inventory,
    )
    for name in metrics:
        check(
            canonical(metrics[name]) == canonical(recomputed[name]),
            "Stored train summary differs from independently diagnosed raw output: " + name,
        )
    check(
        canonical(reload["reload_resubstitution_metrics_not_holdout"])
        == canonical(recomputed["reloaded"]),
        "Stored reload summary differs from independently diagnosed raw output",
    )
    # Re-read every admitted small/explicit adapter/checkpoint output after the
    # computation; reject mutation rather than publishing a stale success.
    check(verify_current_sources(inventory) == source_scope, "Source verification scope changed")
    for row in bindings:
        check(
            binding(row["path"], limit=ADAPTER_LIMIT) == row,
            "Audited output changed during verification",
        )
    check(not (run / "active-phase.lock").exists(), "A phase became active during terminal audit")
    # The generic inventory pass only stat-checks large sources. The original
    # warm adapter is an explicit exception: its full file was separately
    # hashed and its original ordered FP32 payload was independently audited.
    remaining_stat_only = [
        row
        for row in source_scope["stat_only_large_sources"]
        if row["path"] != current_warm_binding["path"]
    ]
    reported_source_scope = {
        **source_scope,
        "stat_only_large_sources": remaining_stat_only,
        "explicit_adapter_source_byte_hashed": [current_warm_binding],
        "all_original_source_bytes_independently_rehashed": not remaining_stat_only,
    }
    return {
        "contract": CONTRACT,
        "schema_version": 1,
        "success": True,
        "filesystem_and_static_parameter_consistency_verified": True,
        "reported_train_and_reload_native_exit_codes": {"train": 0, "reload": 0},
        "actual_tool_exit_status_independently_verified": False,
        "actual_process_exit_independently_observed": False,
        "phase_report_chronology_verified": True,
        "active_phase_lock_absent": True,
        "bindings": bindings,
        "source_scope": reported_source_scope,
        "phase_source_arrays_equal_complete_plan_inventory": True,
        "snapshots": snapshots,
        "snapshot_count": 34,
        "periodic_snapshot_count": 33,
        "final_adapter_parameter_sha256": digest,
        "raw_FP32_tensor_count": 112,
        "raw_parameter_order_from_original_training_report": normalize(
            legacy["original_training_report"]
        ),
        "original_5GB_base_parameters_independently_rehashed": False,
        "journal": journal,
        "draw_event_count": 534,
        "closed_sample_accesses": {"train": 357, "reload": 86},
        "candidate_reload_all86_raw_tokens_actor_prefix_exact": True,
        "baseline_all86_same_actor_inputs_verified": True,
        "raw_diagnostics_independently_rederived": True,
        "diagnostic_gold_target_projection_not_actor_input": True,
        "recomputed_all86_resubstitution_metrics": recomputed,
        "retained_resubstitution_metrics_not_independent_quality": metrics,
        "retained_reload_resubstitution_metrics": reload[
            "reload_resubstitution_metrics_not_holdout"
        ],
        "generation_timing_is_not_end_to_end_realtime_evidence": True,
        "model_loaded_by_audit": False,
        "optimizer_performed_by_audit": False,
        "processor_called_by_audit": False,
        "GPU_used_by_audit": False,
        **dict.fromkeys(FALSE_CLAIMS, False),
    }


def output_target(path):
    path = safe_path(path, exists=False)
    parent = safe_path(ROOT / "artifacts/diagnostics")
    check(
        path.parent == parent and path.name.startswith("onboard-navigation-recovery267-terminal-"),
        "Fresh top-level terminal diagnostics prefix is required",
    )
    check(not path.exists(), "Audit output directory is create-only")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        output = output_target(args.output_dir)
        report = audit_terminal(args.plan, args.plan_sha256)
        report["audit_sources"] = [
            binding(Path(__file__)),
            binding(ROOT / "tests/test_audit_onboard_navigation_recovery267_terminal.py"),
        ]
        report["created_at_utc"] = datetime.now(UTC).isoformat()
        # Deliberately defer all writes until terminal gates pass. mkdir/open
        # remain exclusive; an existing target is never reused or overwritten.
        output.mkdir(exist_ok=False)
        path = output / "report.json"
        with path.open("xb") as stream:
            stream.write(canonical(report) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(
            json.dumps(
                {
                    "event": "recovery267_terminal_filesystem_audit_finished",
                    "success": True,
                    "report": binding(path),
                }
            ),
            flush=True,
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "recovery267_terminal_filesystem_audit_rejected",
                    "success": False,
                    "failure_type": type(error).__name__,
                    "failure": str(error)[:4096],
                    "actual_tool_exit_status_independently_verified": False,
                    "actual_process_exit_independently_observed": False,
                    **dict.fromkeys(FALSE_CLAIMS, False),
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
