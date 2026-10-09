"""One create-only CPU head family, never a navigation backbone or flight loader.

The caller supplies the SHA of an actually closed candidate208 extractor report.
Its loader/prefix provenance is inherited, not re-observed here. Nine episode-out
heads are head diagnostics (the backbone saw old86), not scene/official holdout.
Two final heads score their own TRAIN rows only; neither selects a winner.
Fixed last200 AdamW updates per head; no early stop, best checkpoint, or resume.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-stable-cache-training-v1"
EXTRACT_CONTRACT = "multidetect.onboard-navigation-candidate208-feature-cache-v1"
TERMINAL_CONTRACT = "multidetect.onboard-navigation-candidate208-feature-cache-actual-evidence-v1"
TERMINAL_PATH = (
    "docs/evidence/onboard-navigation-candidate208-feature-cache-actual-20261008-r1.json"
)
OUTPUT = "artifacts/diagnostics/onboard-navigation-stable-cache-20261008-r1"
STAGES = ("attempted", "returned", "synchronized", "completed")
NEW_CAP, OLD_HEAD_SPENT, HEAD_TOTAL_CAP, LORA_SPENT = 2200, 800, 3000, 280
RULE = {
    "updates": 200,
    "maximum_total_attempts": NEW_CAP,
    "learning_rate": 0.0001,
    "weight_decay": 0.01,
    "epsilon": 0.000001,
    "minimum_improvement": 0.0,
    "seed": 0,
}
FALSE_CLAIMS = dict.fromkeys(
    (
        "quality_claim",
        "candidate_promoted",
        "default_model_changed",
        "execution_authority",
        "official_holdout_verified",
        "independent_backbone_holdout",
        "data_help_proven",
        "training_ready_for_candidate",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ),
    False,
)
PINS = {
    "scripts/onboard_navigation_stable_readout.py": (
        31669,
        "b756841607993be3df6e8d818e1c0398a70105a7e19c801d33e034df000cbbc2",
    ),
    "scripts/onboard_navigation_action_readout.py": (
        11143,
        "b4191c30b7e927fad7845347932f699e32c928ed45bca291d2f526b5e8860367",
    ),
    "scripts/evaluate_onboard_navigation_original82.py": (
        45155,
        "6d3f502df0dbad6cc511634a06b70a97b84598451a4d83f1e1f15b6f53ff2b69",
    ),
}
HISTORY = {
    "head": (
        "docs/evidence/onboard-navigation-action-readout-actual-20261008-r2.json",
        9861,
        "1643c22c804962bbb4c7ce4c36b8ff32030b58778b9e5d8e85ce3fe43cebbb25",
    ),
    "lora": (
        "docs/evidence/onboard-navigation-recovery267-terminal-20261008-r2.json",
        6446,
        "4a40567318bffbd547f68f025e5ce5f60012056df5ed0af10615f6eccf553203",
    ),
}
NEW_FILES = (
    "scripts/train_onboard_navigation_stable_cache.py",
    "tests/test_train_onboard_navigation_stable_cache.py",
)
CAP = 8 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("Duplicate JSON field")
            out[key] = value
        return out

    def invalid(value):
        raise ValueError("Nonfinite JSON value: " + value)

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def require_sha(value):
    check(
        type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
        "Canonical SHA required",
    )


def safe_path(value, *, fresh=False):
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    check(path.resolve(strict=False).is_relative_to(ROOT.resolve()), "Path escapes repository")
    cursor = ROOT
    for part in path.relative_to(ROOT).parts:
        cursor /= part
        check(
            not cursor.is_symlink()
            and not (
                cursor.exists()
                and getattr(cursor.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
            ),
            "Reparse/symlink path denied",
        )
    if fresh and path.exists():
        raise FileExistsError("Create-only path exists")
    return path


def binding(path, *, runtime=False):
    path = Path(path).resolve(strict=True) if runtime else safe_path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    check(path.is_file() and 0 < path.stat().st_size <= CAP, "Small consumed file cap")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return {
        "path": path.resolve().as_posix(),
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def verify(row, *, runtime=False):
    check(type(row) is dict and set(row) == {"path", "bytes", "sha256"}, "Exact binding required")
    require_sha(row["sha256"])
    check(type(row["bytes"]) is int and 0 < row["bytes"] <= CAP, "Small binding cap")
    actual = binding(row["path"], runtime=runtime)
    check(
        actual["bytes"] == row["bytes"] and actual["sha256"] == row["sha256"], "Bound source drift"
    )
    return Path(actual["path"])


def read_bound(row):
    return strict_json(verify(row).read_bytes())


def create_json(path, value):
    with Path(path).open("xb") as stream:
        append_row(stream, value)


def append_row(stream, value):
    raw = canonical(value) + b"\n"
    check(stream.write(raw) == len(raw), "Short durable write")
    stream.flush()
    os.fsync(stream.fileno())


def code_bindings():
    rows = []
    for name, (size, sha) in PINS.items():
        row = binding(name)
        check(row["bytes"] == size and row["sha256"] == sha, "Frozen implementation drift")
        rows.append(row)
    rows.extend(binding(name) for name in NEW_FILES)
    # The frozen target wrapper imports this small stdlib contract package.
    rows.extend(binding(path) for path in sorted((ROOT / "src/multidetect/autonomy").glob("*.py")))
    rows.append(binding("src/multidetect/__init__.py"))
    return rows


def history():
    rows, values = [], {}
    for key, (path, size, sha) in HISTORY.items():
        row = {"path": str(safe_path(path)), "bytes": size, "sha256": sha}
        values[key] = read_bound(row)
        rows.append(row)
    h = values["head"]
    check(
        h["actual_execution"]["actual_native_exit_code"] == 0
        and h["observations"]["combined_head_optimizer_steps"] == dict.fromkeys(STAGES, 800)
        and h["observations"]["inherited_base_lora_optimizer_attempts"] == 280
        and values["lora"]["complete267_verified"] is True,
        "Historical budget identity differs",
    )
    return rows


def schedule(rows):
    ids = [r["sample_id"] for r in rows]
    check(
        len(rows) == 208 and len(set(ids)) == 208 and all(r["partition"] == "train" for r in rows),
        "Exactly208 unique TRAIN rows",
    )
    episodes = list(dict.fromkeys(r["episode_id"] for r in rows))
    check(len(episodes) == 9 and len(set(episodes)) == 9, "Nine complete episode identities")
    old = ids[:86]
    check(
        Counter(r["scene_id"] for r in rows) == {"env_airsim_18": 86, "env_airsim_26": 122}
        and all(r["scene_id"] == "env_airsim_18" for r in rows[:86])
        and len({r["episode_id"] for r in rows[:86]}) == 3
        and len({r["episode_id"] for r in rows[86:]}) == 6,
        "Fixed old86/new122 episode and scene roles",
    )
    heads = []
    for number, episode in enumerate(episodes):
        held = [r["sample_id"] for r in rows if r["episode_id"] == episode]
        train = [sid for sid in ids if sid not in set(held)]
        check(len(held) > 1 and train, "Nonempty whole-episode fold")
        heads.append(
            {
                "head_id": f"episode-out-{number:02d}",
                "train_ids": train,
                "eval_ids": held,
                "evaluation_scope": "head_episode_out_not_backbone_holdout",
            }
        )
    heads.extend(
        {
            "head_id": name,
            "train_ids": selected,
            "eval_ids": selected,
            "evaluation_scope": "same_TRAIN_descriptive_not_data_help_or_holdout",
        }
        for name, selected in (("final-old86", old), ("final-all208", ids))
    )
    return heads


def load_rows(bindings):
    check(type(bindings) is list and len(bindings) == 2, "Two original typed dataset bindings")
    rows = []
    for row in bindings:
        rows.extend(
            strict_json(line) for line in verify(row).read_bytes().splitlines() if line.strip()
        )
    schedule(rows)
    return rows


def admit_terminal(path, sha, required):
    require_sha(sha)
    check(safe_path(path) == ROOT / TERMINAL_PATH, "Root-sealed fixed terminal receipt required")
    row = binding(path)
    check(row["sha256"] == sha, "External terminal receipt SHA differs")
    value = read_bound(row)
    check(value["contract"] == TERMINAL_CONTRACT, "Root terminal evidence contract differs")
    actual = value["actual_execution"]
    check(
        type(actual["actual_native_exit_code"]) is int
        and actual["actual_native_exit_code"] == 0
        and actual["worker_pid_absent"] is True
        and type(actual["relevant_model_processes_at_exit_check"]) is int
        and actual["relevant_model_processes_at_exit_check"] == 0
        and all(
            type(actual[k]) is str and actual[k].strip()
            for k in ("terminal_tool_chunk", "process_exit_check_tool_chunk")
        ),
        "Real terminal/PID gate not closed",
    )
    observed = value["observations"]
    check(
        observed["success"] is True
        and observed["feature_cache_admitted"] is True
        and type(observed["feature_count"]) is int
        and observed["feature_count"] == 208
        and observed["cache_exact_reload"] is True,
        "Root terminal cache incomplete",
    )
    declared = {str(safe_path(r["path"]).resolve()): r for r in value["bindings"]}
    check(len(declared) == len(value["bindings"]), "Duplicate terminal evidence binding")
    for source in required:
        key = str(safe_path(source["path"]).resolve())
        check(
            key in declared
            and declared[key]["bytes"] == source["bytes"]
            and declared[key]["sha256"] == source["sha256"],
            "Terminal evidence does not bind actual cache file",
        )
        verify(declared[key])
    check(all(v is False for v in value.get("claims", {}).values()), "Terminal evidence overclaims")
    return row


def admit_extractor(path, sha, terminal_path, terminal_sha):
    """Only small closed files; no model/PNG/processor or live source payload reads."""
    require_sha(sha)
    report_binding = binding(path)  # Missing report refuses before imports or output.
    check(report_binding["sha256"] == sha, "External extractor report SHA differs")
    report = read_bound(report_binding)
    check(
        report.get("contract") == EXTRACT_CONTRACT
        and report.get("success") is True
        and type(report.get("native_exit_code")) is int
        and report["native_exit_code"] == 0
        and not any(k in report for k in ("error", "failure")),
        "Closed successful extractor required",
    )
    for key in (
        "source_bindings_unchanged",
        "feature_cache_admitted",
        "model_loaded",
        "source_sessions_verified",
        "all208_prefix_metadata_equal",
        "cache_saved_and_exactly_reloaded",
    ):
        check(report.get(key) is True, "Extractor closure missing: " + key)
    check(
        report.get("feature_count") == 208
        and type(report["feature_count"]) is int
        and all(
            type(report[k]) is int and report[k] == 208
            for k in ("fused_prefill_attempts", "source_validation_sessions_verified")
        )
        and set(report["optimizer_steps"]) == set(STAGES)
        and all(
            type(report["optimizer_steps"][k]) is int and report["optimizer_steps"][k] == 0
            for k in STAGES
        )
        and not report["partial_failures"],
        "Complete208 zero-update extraction",
    )
    check(all(v is False for v in report["claims"].values()), "Extractor overclaims")
    check(
        canonical(report["source_bindings_before"]) == canonical(report["source_bindings_after"]),
        "Extractor source closure differs",
    )
    check(
        report["model_parameter_identity_before"] == report["model_parameter_identity_after"],
        "Extractor model parameters drifted",
    )
    plan = read_bound(report["plan"])
    check(plan["contract"] == EXTRACT_CONTRACT + "-plan", "Extractor plan contract")
    datasets = [plan["dataset_bindings"][key] for key in ("old86", "new122")]
    rows = load_rows(datasets)
    ids = [r["sample_id"] for r in rows]
    check(
        report["sample_ids"] == ids == plan["sample_ids"], "Extractor ordered208 identity differs"
    )
    receipt_binding = report["cache_receipt"]
    receipt = read_bound(receipt_binding)
    check(
        receipt["kind"] == "feature-cache"
        and receipt["exact_tensor_reload"] is True
        and receipt["source_bindings_unchanged"] is True
        and receipt["source_bindings_before"] == receipt["source_bindings_after"],
        "Partial cache denied",
    )
    meta = read_bound(receipt["metadata"])
    check(
        meta["sample_ids"] == ids
        and meta["partitions"] == ["train"] * 208
        and meta["episode_ids"] == [r["episode_id"] for r in rows]
        and meta["model_identity_declared"] == report["model_parameter_identity_before"],
        "Cache declared identity/order/roles differ",
    )
    check(
        meta["feature_dim"] == 2048
        and meta["tensors"]["features"]["shape"] == [208, 2048]
        and meta["tensors"]["features"]["dtype"] == "torch.float32",
        "Finite FP32 cache shape",
    )
    check(
        meta["model_identity_declared"] == plan["expected_model_identity"]
        and meta["tensors"]["features"] == report["feature_matrix"]
        and report["cache_tensor_bytes"] == 208 * 2048 * 4,
        "Model/matrix provenance differs",
    )
    for observation, sid in zip(meta["observations"], ids, strict=True):
        check(
            observation["sample_id"] == sid
            and observation["source_session_closed_verified"] is True
            and observation["gold_in_model_inputs"] is False,
            "Unverified/gold-bearing feature",
        )
    verify(receipt["weights"])
    directory = Path(receipt_binding["path"]).resolve().parent
    check(
        set(p.name for p in directory.iterdir())
        == {"receipt.json", "metadata.json", "tensors.safetensors"}
        and Path(receipt["metadata"]["path"]).resolve() == directory / "metadata.json"
        and Path(receipt["weights"]["path"]).resolve() == directory / "tensors.safetensors",
        "Exact nonrelocated cache bundle",
    )
    manifest = read_bound(report["manifest"])
    check(
        manifest["contract"] == EXTRACT_CONTRACT + "-manifest"
        and manifest["sample_ids"] == ids
        and manifest["cache_receipt"] == receipt_binding
        and manifest["feature_matrix"] == report["feature_matrix"]
        and manifest["model_identity"] == meta["model_identity_declared"]
        and manifest["plan"] == report["plan"]
        and manifest["episode_ids"] == meta["episode_ids"]
        and manifest["partitions"] == meta["partitions"],
        "Extractor manifest mismatch",
    )
    extractor_code = [
        r
        for r in plan["source_files"]
        if Path(r["path"]).name
        in {
            "extract_onboard_navigation_feature_cache.py",
            "test_extract_onboard_navigation_feature_cache.py",
        }
    ]
    check(len(extractor_code) == 2, "Extractor implementation and tests must be bound")
    terminal = admit_terminal(
        terminal_path,
        terminal_sha,
        [
            report_binding,
            report["plan"],
            receipt_binding,
            receipt["metadata"],
            receipt["weights"],
            report["manifest"],
            *extractor_code,
        ],
    )
    consumed = [
        report_binding,
        report["plan"],
        *datasets,
        receipt_binding,
        terminal,
        report["manifest"],
        *extractor_code,
        receipt["metadata"],
        receipt["weights"],
        *receipt["source_bindings_before"],
    ]
    for row in consumed:
        verify(row)
    return report, plan, rows, meta, consumed


def runtime_reference():
    rows, versions = [], {}
    for package, filename in (
        ("torch", "torch/__init__.py"),
        ("torch", "torch/optim/adamw.py"),
        ("safetensors", "safetensors/torch.py"),
        ("numpy", "numpy/__init__.py"),
    ):
        dist = importlib.metadata.distribution(package)
        versions[package] = dist.version
        rows.append(
            {
                "module": filename[:-3].replace("/", ".").removesuffix(".__init__"),
                "binding": binding(dist.locate_file(filename), runtime=True),
            }
        )
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "package_versions": versions,
        "entrypoints": rows,
        "scope": "four_imported_CPU_entrypoints_not_transitive_or_DLL_audit",
    }


def normalize_sources(rows):
    result = {}
    for row in rows:
        actual = binding(verify(row))
        check(
            actual["path"] not in result or result[actual["path"]] == actual,
            "Conflicting source binding",
        )
        result[actual["path"]] = actual
    return [result[name] for name in sorted(result)]


def build_plan(extractor_report, extractor_sha256, terminal_path, terminal_sha, output_dir=OUTPUT):
    check(
        safe_path(output_dir) == ROOT / OUTPUT,
        "One fixed create-only family output; no retry/reset",
    )
    safe_path(output_dir, fresh=True)
    report, extraction, rows, _meta, consumed = admit_extractor(
        extractor_report, extractor_sha256, terminal_path, terminal_sha
    )
    return {
        "schema_version": 1,
        "contract": CONTRACT + "-plan",
        "output_dir": str(ROOT / OUTPUT),
        "extractor_report": binding(extractor_report),
        "cache_receipt": report["cache_receipt"],
        "terminal_receipt": binding(terminal_path),
        "sample_ids": [r["sample_id"] for r in rows],
        "datasets": [extraction["dataset_bindings"][k] for k in ("old86", "new122")],
        "heads": schedule(rows),
        "rule": dict(RULE),
        "new_family_attempt_cap": NEW_CAP,
        "inherited_head_attempts": OLD_HEAD_SPENT,
        "cumulative_head_attempt_cap": HEAD_TOTAL_CAP,
        "inherited_base_lora_attempts": LORA_SPENT,
        "base_lora_new_updates": 0,
        "model_parameter_identity_inherited": report["model_parameter_identity_before"],
        "model_files_inherited": extraction["model_file_bindings"],
        "runtime": runtime_reference(),
        "sources": normalize_sources([*consumed, *code_bindings(), *history()]),
        "claims": dict(FALSE_CLAIMS),
        "resume_retry_or_best_checkpoint": False,
        "feature_model_or_prefix_readmission_performed": False,
        "source_scope": "small_consumed_metadata_code_cache_CPU_runtime_not_PNG_or_backbone",
    }


def admit_plan(path, sha):
    require_sha(sha)
    plan_binding = binding(path)
    check(plan_binding["sha256"] == sha, "External training plan SHA differs")
    plan = read_bound(plan_binding)
    expected = build_plan(
        plan["extractor_report"]["path"],
        plan["extractor_report"]["sha256"],
        plan["terminal_receipt"]["path"],
        plan["terminal_receipt"]["sha256"],
        plan["output_dir"],
    )
    check(canonical(plan) == canonical(expected), "Precommitted11-head plan differs")
    return plan, plan_binding


def capture_sources(plan, plan_binding):
    return {
        "repository": normalize_sources([*plan["sources"], plan_binding]),
        "runtime": [
            binding(verify(r["binding"], runtime=True), runtime=True)
            for r in plan["runtime"]["entrypoints"]
        ],
    }


def load_cpu_apis(plan):
    runtime = plan["runtime"]
    check(
        str(Path(sys.executable).resolve()) == runtime["python_executable"], "Actual Python differs"
    )
    for name, version in runtime["package_versions"].items():
        check(importlib.metadata.version(name) == version, "Actual CPU distribution differs")
    code_bindings()
    previous = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        stable = importlib.import_module("scripts.onboard_navigation_stable_readout")
        loss = importlib.import_module("scripts.onboard_navigation_action_readout")
        score = importlib.import_module("scripts.evaluate_onboard_navigation_original82")
        inference = importlib.import_module("multidetect.autonomy.onboard_navigation_inference")
        navigation = importlib.import_module("multidetect.autonomy.onboard_navigation_supervision")
    finally:
        sys.path[:] = previous
    for module, name in (
        (stable, "scripts/onboard_navigation_stable_readout.py"),
        (loss, "scripts/onboard_navigation_action_readout.py"),
        (score, "scripts/evaluate_onboard_navigation_original82.py"),
    ):
        check(
            Path(module.__file__).resolve() == ROOT / name,
            "Actual repository module origin differs",
        )
    actual_origins = verify_runtime_entrypoints(plan)
    cpu_guard(loss.torch)
    return stable, loss, score, inference, navigation, actual_origins


def verify_runtime_entrypoints(plan):
    actual_origins = []
    runtime = plan["runtime"]
    for row in runtime["entrypoints"]:
        expected = verify(row["binding"], runtime=True)
        module = importlib.import_module(row["module"])
        check(Path(module.__file__).resolve() == expected, "Actual imported CPU entrypoint differs")
        actual_origins.append(row)
    torch = importlib.import_module("torch")
    adamw = importlib.import_module("torch.optim.adamw")
    check(torch.optim.AdamW is adamw.AdamW, "Actual AdamW class differs from admitted entrypoint")
    return actual_origins


def cpu_guard(torch):
    check(not torch.cuda.is_initialized(), "CPU trainer must never initialize CUDA")
    check(
        not any(
            n.split(".")[0] in {"PIL", "transformers", "peft", "pyarrow", "datasets"}
            for n in sys.modules
        ),
        "CPU trainer cannot load processor/backbone/image packages",
    )


def verify_journal(path, heads, *, require_complete=False):
    counts = dict.fromkeys(STAGES, 0)
    event_count = 0
    raw = Path(path).read_bytes()
    check(not raw or raw.endswith(b"\n"), "Truncated optimizer journal")
    previous_loss = None
    for line in raw.splitlines():
        row = strict_json(line)
        check(
            set(row) == {"fit_id", "update", "stage", "global_count", "pre_step_loss"},
            "Exact optimizer event fields",
        )
        head_number, offset = divmod(event_count, RULE["updates"] * 4)
        check(head_number < len(heads), "Extra optimizer attempt")
        update, stage_number = divmod(offset, 4)
        stage = STAGES[stage_number]
        check(
            row["fit_id"] == heads[head_number]["head_id"]
            and type(row["update"]) is int
            and row["update"] == update + 1
            and row["stage"] == stage
            and type(row["global_count"]) is int
            and row["global_count"] == counts[stage] + 1,
            "Optimizer event order/count differs",
        )
        value = row["pre_step_loss"]
        check(
            type(value) in (int, float) and math.isfinite(value) and value >= 0,
            "Finite observed loss",
        )
        if stage_number:
            check(value == previous_loss, "Four events must bind the same observed loss")
        previous_loss = value
        counts[stage] += 1
        event_count += 1
    complete = counts == dict.fromkeys(STAGES, NEW_CAP)
    if require_complete:
        check(complete, "Exactly2200 completed updates required")
    return {
        "counts": counts,
        "event_count": event_count,
        "complete": complete,
        "optimizer_update_uncertain": counts["attempted"] != counts["completed"],
    }


def labels_and_samples(rows, stable, score, inference, navigation):
    samples = tuple(navigation.NavigationSupervisionSample.from_mapping(row) for row in rows)
    navigation.validate_navigation_episode_samples(samples)
    labels = {}
    names = {
        "forward": "forward",
        "yaw_left": "turn_yaw_left",
        "yaw_right": "turn_yaw_right",
        "stop": "sequence_stop",
    }
    for sample in samples:
        target = inference.parse_navigation_target(canonical(score.gold_mapping(sample)))
        category = score.primitive_class(target)
        check(category in names, "Unsupported primitive cannot enter four-class head loss")
        labels[sample.sample_id] = stable.ReadoutLabel(
            names[category],
            distance_m=target.translation_m[0] if category == "forward" else None,
            angle_magnitude_rad=abs(target.yaw_delta_rad) if category.startswith("yaw_") else None,
        )
    return labels, {sample.sample_id: sample for sample in samples}


def summarize(rows):
    groups = {
        name: [r for r in rows if r["score"]["gold_class"] == name]
        for name in sorted({r["score"]["gold_class"] for r in rows})
    }
    per_class = {
        name: {
            "support": len(group),
            "raw_classifier_correct": sum(r["raw_classifier_correct"] for r in group),
            "action_correct": sum(r["score"]["action_class_correct"] for r in group),
            "joint_correct": sum(
                r["score"]["action_class_correct"] and r["score"]["metric_correct"] is True
                for r in group
            ),
            "metric_comparable": sum(r["score"]["metric_comparable"] for r in group),
            "metric_correct": sum(r["score"]["metric_correct"] is True for r in group),
            "scale_rejected": sum(r["selected_scale_rejected"] for r in group),
        }
        for name, group in groups.items()
    }
    return {
        "support": len(rows),
        "per_class": per_class,
        "macro_action_recall_gold_present": sum(
            v["action_correct"] / v["support"] for v in per_class.values()
        )
        / len(per_class)
        if per_class
        else None,
        "raw_classifier_confusion": [
            dict(gold=a, predicted=b, count=n)
            for (a, b), n in sorted(
                Counter((r["score"]["gold_class"], r["raw_class"]) for r in rows).items()
            )
        ],
        "translation_L2_errors_m": [
            r["score"]["translation_l2_error_m"]
            for r in rows
            if r["score"]["translation_l2_error_m"] is not None
        ],
        "yaw_absolute_errors_rad": [
            r["score"]["yaw_absolute_error_rad"]
            for r in rows
            if r["score"]["yaw_absolute_error_rad"] is not None
        ],
        "label_reproduction_thresholds_not_flight_tolerances": {
            "translation_L2_m": 1e-4,
            "yaw_rad": 1e-6,
        },
    }


def train_heads(plan, features, metadata, rows, apis, output, sources, report):
    stable, loss, score, inference, navigation, _origins = apis
    torch = loss.torch
    labels, samples = labels_and_samples(rows, stable, score, inference, navigation)
    ids = plan["sample_ids"]
    index = {sid: number for number, sid in enumerate(ids)}
    ledger = stable.UpdateLedger(NEW_CAP)
    report["optimizer_steps"] = ledger.counts
    rule = stable.TrainingRule(**plan["rule"])
    all_raw = []
    try:
        with (
            (output / "head-updates.jsonl").open("xb") as journal,
            (output / "predictions.jsonl").open("xb") as raw_stream,
        ):
            for spec in plan["heads"]:
                cpu_guard(torch)
                result = stable.fit_readout(
                    features[[index[sid] for sid in spec["train_ids"]]],
                    [labels[sid] for sid in spec["train_ids"]],
                    sample_ids=spec["train_ids"],
                    partitions=["train"] * len(spec["train_ids"]),
                    rule=rule,
                    ledger=ledger,
                    event_sink=lambda row: append_row(journal, row),
                    fit_id=spec["head_id"],
                )
                directory = output / spec["head_id"]
                saved = stable.save_readout(directory, result, sources=sources)
                head, norm, saved_meta = stable.load_readout(
                    directory, receipt_sha256=saved["sha256"]
                )
                evaluation = features[[index[sid] for sid in spec["eval_ids"]]]
                with torch.no_grad():
                    original = stable.predict(result.head, result.normalization, evaluation)
                    reloaded = stable.predict(head, norm, evaluation)
                check(
                    torch.equal(original, reloaded), "Saved head/normalization raw outputs differ"
                )
                receipt = read_bound(saved)
                provenance_base = plan["model_files_inherited"]
                provenance = loss.ActionReadoutProvenance(
                    prefix_sha256="0" * 64,
                    base_weights_sha256=provenance_base["base"]["sha256"],
                    adapter_weights_sha256=provenance_base["adapter"]["sha256"],
                    head_weights_sha256=receipt["weights"]["sha256"],
                    head_config_sha256=receipt["metadata"]["sha256"],
                )
                for offset, sid in enumerate(spec["eval_ids"]):
                    from dataclasses import replace

                    obs = metadata["observations"][index[sid]]
                    declared = replace(
                        provenance,
                        prefix_sha256=hashlib.sha256(canonical(obs["prefix_tensors"])).hexdigest(),
                    )
                    try:
                        raw = loss.readout_result(
                            reloaded[offset : offset + 1], provenance=declared
                        )
                        rejected = False
                    except loss.ActionReadoutRejected as exc:
                        raw, rejected = exc.observation, True
                    row = {
                        "head_id": spec["head_id"],
                        "sample_id": sid,
                        "partition": "train",
                        "evaluation_scope": spec["evaluation_scope"],
                        "raw": raw,
                        "selected_scale_rejected": rejected,
                        "feature_observation": obs,
                        "text_generation_performed": False,
                        "gold_scored": False,
                    }
                    append_row(raw_stream, row)
                    all_raw.append(row)
                    report["raw_prediction_count"] += 1
                report["heads"].append(
                    {
                        **spec,
                        "fit_report": result.report,
                        "receipt": saved,
                        "saved_reload_raw_outputs_exact": True,
                        "normalization_train_ids": saved_meta["normalization_train_ids"],
                    }
                )
                print(
                    json.dumps(
                        {
                            "event": "stable_head_completed",
                            "head_id": spec["head_id"],
                            "completed_optimizer_updates": ledger.counts["completed"],
                        }
                    ),
                    flush=True,
                )
                del result, head, norm, original, reloaded, evaluation
        journal_state = verify_journal(
            output / "head-updates.jsonl", plan["heads"], require_complete=True
        )
        check(journal_state["counts"] == ledger.counts, "Durable WAL and in-memory ledger differ")
        # Only after every raw observation is durable may gold enter the scorer.
        scored = []
        class_names = {
            "forward": "forward",
            "turn_yaw_left": "yaw_left",
            "turn_yaw_right": "yaw_right",
            "sequence_stop": "stop",
        }
        for row in all_raw:
            predicted = row["raw"].get("target")
            score_row = score.score_prediction(
                canonical(predicted).decode() if predicted else "",
                score.gold_mapping(samples[row["sample_id"]]),
                inference,
            )
            raw_class = class_names[row["raw"]["selected_class"]]
            scored.append(
                {
                    **row,
                    "gold_scored": True,
                    "score": score_row,
                    "raw_class": raw_class,
                    "raw_classifier_correct": raw_class == score_row["gold_class"],
                }
            )
        create_json(output / "scores.json", scored)
        report["summaries"] = {
            spec["head_id"]: summarize([r for r in scored if r["head_id"] == spec["head_id"]])
            for spec in plan["heads"]
        }
        report["raw_durable_before_gold_scoring"] = True
        report["journal"] = journal_state
    except stable.FitRejected as exc:
        report["failed_fit_report"] = exc.report
        raise
    finally:
        report["optimizer_steps"] = dict(ledger.counts)
        report["combined_head_optimizer_steps"] = {
            key: OLD_HEAD_SPENT + value for key, value in ledger.counts.items()
        }
        report["remaining_new_family_attempts"] = NEW_CAP - ledger.counts["attempted"]
        report["optimizer_update_uncertain"] = ledger.optimizer_update_uncertain
        report["optimizer_API_calls"] = ledger.optimizer_api_calls


def run(args):
    plan, plan_binding = admit_plan(args.plan, args.plan_sha256)
    before = capture_sources(plan, plan_binding)
    output = safe_path(plan["output_dir"], fresh=True)
    output.mkdir(exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "plan": plan_binding,
        "success": False,
        "native_exit_code": 1,
        "pid": os.getpid(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "claims": dict(FALSE_CLAIMS),
        "source_bindings_before": before,
        "source_bindings_after": None,
        "source_bindings_unchanged": False,
        "optimizer_steps": dict.fromkeys(STAGES, 0),
        "combined_head_optimizer_steps": dict.fromkeys(STAGES, OLD_HEAD_SPENT),
        "remaining_new_family_attempts": NEW_CAP,
        "optimizer_update_uncertain": False,
        "inherited_base_lora_optimizer_attempts": LORA_SPENT,
        "base_lora_optimizer_updates": 0,
        "backbone_loaded": False,
        "processor_or_PNG_used": False,
        "feature_extraction_reperformed": False,
        "GPU_used": False,
        "budget_reset_or_refund": False,
        "heads": [],
        "raw_prediction_count": 0,
    }
    apis, old_threads = None, None
    try:
        apis = load_cpu_apis(plan)
        stable, loss, *_ = apis
        old_threads = loss.torch.get_num_threads()
        loss.torch.set_num_threads(1)
        _extract, _extraction_plan, rows, declared, _ = admit_extractor(
            plan["extractor_report"]["path"],
            plan["extractor_report"]["sha256"],
            plan["terminal_receipt"]["path"],
            plan["terminal_receipt"]["sha256"],
        )
        receipt_path = Path(plan["cache_receipt"]["path"])
        features, metadata = stable.load_feature_cache(
            receipt_path.parent,
            receipt_sha256=plan["cache_receipt"]["sha256"],
            expected_sample_ids=plan["sample_ids"],
            expected_model_identity=plan["model_parameter_identity_inherited"],
        )
        check(
            canonical(metadata) == canonical(declared), "Cache metadata changed at actual CPU load"
        )
        stable._tensor(features, shape=(208, 2048), name="actual TRAIN cache")
        report["actual_runtime_origins_before"] = apis[-1]
        train_heads(plan, features, metadata, rows, apis, output, before["repository"], report)
        check(
            report["optimizer_steps"] == dict.fromkeys(STAGES, NEW_CAP)
            and len(report["heads"]) == 11
            and report["raw_prediction_count"] == 502,
            "Complete11-head 2200 updates and502 raw required",
        )
        verify_journal(output / "head-updates.jsonl", plan["heads"], require_complete=True)
        origins_after = load_cpu_apis(plan)[-1]
        check(origins_after == apis[-1], "Actual CPU runtime origin drift")
        after = capture_sources(plan, plan_binding)
        report["source_bindings_after"] = after
        check(after == before, "Late consumed source/runtime drift")
        report.update(
            success=True,
            native_exit_code=0,
            source_bindings_unchanged=True,
            actual_runtime_origins_after=origins_after,
            numerical_training_loss_gate_passed=True,
            all_saved_heads_exactly_reloaded=True,
            predictions=binding(output / "predictions.jsonl"),
            scores=binding(output / "scores.json"),
            head_updates=binding(output / "head-updates.jsonl"),
        )
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
    finally:
        if apis is not None:
            try:
                if old_threads is not None:
                    apis[1].torch.set_num_threads(old_threads)
                cpu_guard(apis[1].torch)
            except BaseException as exc:
                report.update(success=False, native_exit_code=1)
                report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        create_json(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-plan", action="store_true")
    mode.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--extractor-report")
    parser.add_argument("--extractor-report-sha256")
    parser.add_argument("--terminal-receipt")
    parser.add_argument("--terminal-receipt-sha256")
    parser.add_argument("--plan-output")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            check(
                args.extractor_report
                and args.extractor_report_sha256
                and args.plan_output
                and args.terminal_receipt
                and args.terminal_receipt_sha256
                and not args.plan_sha256,
                "Emission requires closed report+SHA, terminal+SHA and new plan",
            )
            target = safe_path(args.plan_output, fresh=True)
            check(
                target.parent == ROOT / "configs/training"
                and target.name == "onboard-navigation-stable-cache-20261008-r1.json",
                "Fixed create-only family plan",
            )
            plan = build_plan(
                args.extractor_report,
                args.extractor_report_sha256,
                args.terminal_receipt,
                args.terminal_receipt_sha256,
            )
            create_json(target, plan)
            print(json.dumps(binding(target)))
            return 0
        check(
            args.plan_sha256
            and not any(
                (
                    args.extractor_report,
                    args.extractor_report_sha256,
                    args.terminal_receipt,
                    args.terminal_receipt_sha256,
                    args.plan_output,
                )
            ),
            "Execution requires only plan+SHA",
        )
        result = run(args)
        print(
            json.dumps(
                {
                    "success": result["success"],
                    "native_exit_code": result["native_exit_code"],
                    "optimizer_steps": result["optimizer_steps"],
                    "quality_claim": False,
                }
            )
        )
        return result["native_exit_code"]
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
