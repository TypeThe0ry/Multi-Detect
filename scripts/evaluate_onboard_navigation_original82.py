"""Create-only offline candidate evaluation on 17+42 original TRAIN dev rows.

No training, new baseline, realtime scope/clock, download, retry, or promotion.
The root-sealed native train+fresh-reload receipt must exist before plan emission.
Heavy dependencies and image decoding are lazy and only the explicit run loads
the exact saved candidate. Software fixtures do not prove native model results.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import sys
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-original82-offline-evaluation-v1"
PLAN_CONTRACT = CONTRACT + "-plan"
TERMINAL_CONTRACT = "multidetect.onboard-navigation-recovery267-terminal-v1"
R2_PLAN = "configs/training/onboard-navigation-recovery-20261008-r2.json"
R2_SHA = "0c50e812781e4cbbf0f4935714486ae78e65e252a7fd0e33d2c321bb10602979"
PINNED_CODE = {
    "scripts/train_onboard_navigation_recovery_v2.py": (
        "03360b614a1199ca4c4de25f66a7defde16e319a5a3e9bf5b2fb2658cb2f5a65"
    ),
    "scripts/build_onboard_openfly_original82_supervision.py": (
        "655113d9a27bd893f5de3617fbc7ce47433e705895c1c96591eb7ab64d6c7d59"
    ),
}
NEW_FILES = (
    "scripts/evaluate_onboard_navigation_original82.py",
    "tests/test_evaluate_onboard_navigation_original82.py",
)
EXTRA_CODE = (
    "src/multidetect/autonomy/onboard_navigation_inference.py",
    "scripts/onboard_navigation_inference_backend.py",
    "scripts/verify_onboard_navigation_actor_inputs.py",
)
FALSE_CLAIMS = (
    "optimizer_performed",
    "forward_backward_performed",
    "training_performed",
    "old_baseline_rerun",
    "official_holdout_verified",
    "independent_official_holdout",
    "quality_claim",
    "candidate_promoted",
    "default_model_changed",
    "execution_authority",
    "final_release_allowed",
    "flight_performed",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "online_freshness_verified",
    "end_to_end_online_latency_measured",
    "hard_generation_deadline_enforced",
    "budget_reset",
    "exact_resume",
)
TARGET_CONTRACT = "multidetect.onboard-planned-navigation-target-v1"
STAGES = ("attempted", "returned", "synchronized", "completed")
ROLES = (
    ("env_airsim_26", "train", 23),
    ("env_airsim_16", "dev-validation", 17),
    ("env_airsim_23", "dev-test", 42),
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def strict_json(raw):
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def constant(_value):
        raise ValueError("Nonfinite JSON")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    canonical(value)
    return value


def require_sha(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Explicit canonical SHA-256 required")


def record(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def bound_path(row, *, runtime=False):
    if type(row) is not dict or set(row) != {"path", "bytes", "sha256"}:
        raise ValueError("Exact path/bytes/SHA binding required")
    if type(row["path"]) is not str or type(row["bytes"]) is not int or row["bytes"] <= 0:
        raise ValueError("Invalid source binding")
    require_sha(row["sha256"])
    path = (ROOT / row["path"]).resolve(strict=True)
    if not path.is_file() or (not runtime and not path.is_relative_to(ROOT)):
        raise ValueError("Bound repository source escaped ROOT")
    return path


def verify(row, *, runtime=False):
    path = bound_path(row, runtime=runtime)
    if record(path) != {**row, "path": str(path)}:
        raise ValueError("Bound file bytes changed")
    return path


def read_bound(row):
    if row["bytes"] > 2 * 1024 * 1024:
        raise ValueError("Small JSON artifact exceeds its cap")
    return strict_json(verify(row).read_bytes())


def explicit_binding(path, digest):
    require_sha(digest)
    row = record(ROOT / path)
    if row["sha256"] != digest:
        raise ValueError("Explicit artifact SHA changed")
    bound_path(row)
    return row


def normalize(rows, *, runtime=False):
    result = {}
    for row in rows:
        path = bound_path(row, runtime=runtime)
        current = {**row, "path": str(path)}
        if str(path) in result and result[str(path)] != current:
            raise ValueError("Conflicting source binding")
        result[str(path)] = current
    return [result[k] for k in sorted(result)]


def authenticate_code(rows):
    for row in rows:
        path = bound_path(row)
        if path.suffix != ".py" or row["bytes"] > 2 * 1024 * 1024:
            raise ValueError("Only bounded repository Python sources may bootstrap imports")
        verify(row)


def apis():
    previous = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        from multidetect.autonomy import onboard_navigation_actor as actor
        from multidetect.autonomy import onboard_navigation_inference as inference
        from multidetect.autonomy import onboard_navigation_supervision as navigation
        from scripts import onboard_navigation_inference_backend as shared_backend
        from scripts import train_onboard_navigation_recovery_v2 as recovery
        from scripts import train_onboard_navigation_streamed as old
        from scripts import train_onboard_vlm_navigation_pilot as pilot
        from scripts import verify_onboard_navigation_actor_inputs as prefix
    finally:
        sys.path[:] = previous
    return SimpleNamespace(
        actor=actor,
        inference=inference,
        navigation=navigation,
        shared_backend=shared_backend,
        recovery=recovery,
        old=old,
        pilot=pilot,
        prefix=prefix,
    )


def primitive_class(target):
    if target.kind == "stop":
        return "stop"
    if target.kind == "turn_yaw":
        if target.yaw_delta_rad is None:
            return "yaw_unknown"
        return "yaw_right" if target.yaw_delta_rad > 0 else "yaw_left"
    if target.translation_m is None:
        return "translation_unknown"
    axes = [(i, v) for i, v in enumerate(target.translation_m) if v != 0]
    if len(axes) != 1:
        return "mixed_translation"
    axis, value = axes[0]
    return (("forward", "backward"), ("right", "left"), ("down", "up"))[axis][value < 0]


def gold_mapping(sample):
    action = sample.planned_action
    return {
        "schema_version": 1,
        "contract": TARGET_CONTRACT,
        "kind": action.kind,
        "translation_frame": "body_frd",
        "yaw_convention": "clockwise_right_positive",
        "translation_m": None if action.translation_m is None else list(action.translation_m),
        "yaw_delta_rad": action.yaw_delta_rad,
    }


def score_prediction(raw, gold, inference):
    target = inference.parse_navigation_target(canonical(gold))
    result = {
        "json_valid": False,
        "strict_target_valid": False,
        "gold_class": primitive_class(target),
        "predicted_class": "invalid",
        "kind_correct": False,
        "action_class_correct": False,
        "metric_comparable": False,
        "metric_correct": None,
        "translation_l2_error_m": None,
        "translation_component_absolute_error_m": None,
        "yaw_absolute_error_rad": None,
        "yaw_wrapped_absolute_error_rad": None,
        "predicted_translation_is_null": None,
        "predicted_yaw_is_null": None,
    }
    try:
        if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= 64 * 1024:
            raise ValueError("Raw response byte cap")
        strict_json(raw)
        result["json_valid"] = True
        predicted = inference.parse_navigation_target(raw)
        result.update(
            strict_target_valid=True,
            predicted_class=primitive_class(predicted),
            kind_correct=predicted.kind == target.kind,
            predicted_translation_is_null=predicted.translation_m is None,
            predicted_yaw_is_null=predicted.yaw_delta_rad is None,
        )
        result["action_class_correct"] = result["predicted_class"] == result["gold_class"]
        if (
            predicted.kind == target.kind == "translate"
            and predicted.translation_m is not None
            and target.translation_m is not None
        ):
            errors = [
                abs(a - b)
                for a, b in zip(predicted.translation_m, target.translation_m, strict=True)
            ]
            distance = math.sqrt(sum(e * e for e in errors))
            result.update(
                metric_comparable=True,
                metric_correct=distance <= 1e-4,
                translation_l2_error_m=distance,
                translation_component_absolute_error_m=errors,
            )
        elif (
            predicted.kind == target.kind == "turn_yaw"
            and predicted.yaw_delta_rad is not None
            and target.yaw_delta_rad is not None
        ):
            delta = predicted.yaw_delta_rad - target.yaw_delta_rad
            result.update(
                metric_comparable=True,
                metric_correct=abs(delta) <= 1e-6,
                yaw_absolute_error_rad=abs(delta),
                yaw_wrapped_absolute_error_rad=abs(math.atan2(math.sin(delta), math.cos(delta))),
            )
        elif predicted.kind == target.kind == "stop":
            result.update(metric_comparable=True, metric_correct=True)
    except (ValueError, TypeError, KeyError):
        result["rejection"] = "not_a_strict_bounded_seven_field_navigation_target"
    return result


def validate_generated(generated):
    if type(generated) is not dict or set(generated) != {
        "raw_output",
        "output_token_ids",
        "generated_tokens",
        "token_cap_reached",
        "elapsed_s",
        "peak_allocated_bytes",
        "peak_reserved_bytes",
    }:
        raise ValueError("Frozen generation observation schema differs")
    ids = generated["output_token_ids"]
    if (
        type(generated["raw_output"]) is not str
        or type(ids) is not list
        or any(type(token) is not int or token < 0 for token in ids)
        or type(generated["generated_tokens"]) is not int
        or generated["generated_tokens"] != len(ids)
        or not 0 < len(ids) <= 192
        or type(generated["token_cap_reached"]) is not bool
        or generated["token_cap_reached"] != (len(ids) >= 192)
        or type(generated["elapsed_s"]) not in (int, float)
        or not math.isfinite(generated["elapsed_s"])
        or generated["elapsed_s"] < 0
        or any(
            type(generated[k]) is not int or generated[k] < 0
            for k in ("peak_allocated_bytes", "peak_reserved_bytes")
        )
    ):
        raise ValueError("Actual token/count/cap/time/memory observations differ")
    canonical(generated)


def percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def generation_eligibility(generated):
    validate_generated(generated)
    cap = generated["token_cap_reached"]
    timeout = generated["elapsed_s"] > 30.0
    rejection = (
        "token_cap_and_cooperative_timeout"
        if cap and timeout
        else "token_cap_reached"
        if cap
        else "cooperative_elapsed_exceeded"
        if timeout
        else None
    )
    return {
        "bounded_generation_eligible": not cap and not timeout,
        "budget_rejection": rejection,
        "execution_authorized": False,
        "cooperative_elapsed_not_hard_watchdog": True,
    }


def summarize(rows):
    elapsed = [row["generated"]["elapsed_s"] for row in rows]
    errors = [
        row["score"]["translation_l2_error_m"]
        for row in rows
        if row["score"]["translation_l2_error_m"] is not None
    ]
    yaw = [
        row["score"]["yaw_absolute_error_rad"]
        for row in rows
        if row["score"]["yaw_absolute_error_rad"] is not None
    ]
    confusion = Counter(
        (row["score"]["gold_class"], row["score"]["predicted_class"]) for row in rows
    )
    eligibility = [generation_eligibility(row["generated"]) for row in rows]
    eligible = [
        row
        for row, gate in zip(rows, eligibility, strict=True)
        if gate["bounded_generation_eligible"]
    ]
    return {
        "sample_count": len(rows),
        "unmasked_counts_scope": (
            "raw_offline_diagnostics_not_budget_eligible_or_execution_acceptance"
        ),
        **{
            key + "_count": sum(row["score"][key] is True for row in rows)
            for key in (
                "json_valid",
                "strict_target_valid",
                "kind_correct",
                "action_class_correct",
                "metric_comparable",
                "metric_correct",
                "predicted_translation_is_null",
                "predicted_yaw_is_null",
            )
        },
        "gold_class_counts": dict(Counter(row["score"]["gold_class"] for row in rows)),
        "predicted_class_counts": dict(Counter(row["score"]["predicted_class"] for row in rows)),
        "confusion": [
            {"gold": a, "predicted": b, "count": n} for (a, b), n in sorted(confusion.items())
        ],
        "token_cap_reached_count": sum(row["generated"]["token_cap_reached"] for row in rows),
        "elapsed_over_cooperative_30s_count": sum(value > 30.0 for value in elapsed),
        "bounded_generation_eligible_count": len(eligible),
        "budget_rejection_counts": dict(
            Counter(
                gate["budget_rejection"]
                for gate in eligibility
                if gate["budget_rejection"] is not None
            )
        ),
        "eligible_correct_count": sum(
            row["score"]["strict_target_valid"] is True
            and row["score"]["action_class_correct"] is True
            and row["score"]["metric_correct"] is True
            for row in eligible
        ),
        "eligible_diagnostic_counts": {
            key + "_count": sum(row["score"][key] is True for row in eligible)
            for key in (
                "strict_target_valid",
                "kind_correct",
                "action_class_correct",
                "metric_correct",
            )
        },
        "generation_elapsed_s": {
            "count": len(elapsed),
            "mean": sum(elapsed) / len(elapsed) if elapsed else None,
            "min": min(elapsed) if elapsed else None,
            "max": max(elapsed) if elapsed else None,
            "p50": percentile(elapsed, 0.5),
            "p95": percentile(elapsed, 0.95),
        },
        "translation_l2_error_m": {
            "count": len(errors),
            "mean": sum(errors) / len(errors) if errors else None,
        },
        "yaw_absolute_error_rad": {"count": len(yaw), "mean": sum(yaw) / len(yaw) if yaw else None},
        "quality_claim": False,
        "official_holdout_verified": False,
    }


def verify_terminal(evidence, recovery_plan, api):
    execution = evidence.get("actual_execution", {})
    if (
        evidence.get("schema_version") != 1
        or type(evidence.get("schema_version")) is not int
        or evidence.get("contract") != TERMINAL_CONTRACT
        or any(
            evidence.get(k) is not True
            for k in (
                "complete267_verified",
                "fresh_saved_adapter_reload_verified",
                "saved_parameter_digest_verified",
                "source_bindings_unchanged",
            )
        )
        or any(
            evidence.get(k) is not False
            for k in (
                "quality_claim",
                "candidate_promoted",
                "official_holdout_verified",
                "execution_authority",
                "final_release_allowed",
            )
        )
        or execution.get("processes_terminated") is not True
        or any(
            type(execution.get(p + "_native_exit_code")) is not int
            or execution[p + "_native_exit_code"] != 0
            or type(execution.get(p + "_terminal_tool_chunk")) is not str
            or not execution[p + "_terminal_tool_chunk"]
            for p in ("train", "reload")
        )
    ):
        raise ValueError("Root-sealed actual native train+fresh-reload terminal evidence required")
    bindings = {row["path"]: row for row in normalize(evidence["bindings"])}
    run = Path(recovery_plan["output_dir"]).resolve(strict=True)
    expected = {
        "recovery_plan": ROOT / R2_PLAN,
        "train_report": run / "train/report.json",
        "reload_report": run / "reload/report.json",
        "journal": run / "train/training-events.jsonl",
        "weights": run / "train/adapter/adapter_model.safetensors",
        "config": run / "train/adapter/adapter_config.json",
        "candidate86": run / "train/candidate-all86.json",
        "reloaded86": run / "reload/reloaded-all86.json",
    }
    admitted = {}
    for name, path in expected.items():
        row = bindings.get(str(path.resolve(strict=True)))
        if row is None or row["bytes"] > 16 * 1024 * 1024:
            raise ValueError("Terminal receipt misses one bounded actual recovery artifact")
        verify(row)
        admitted[name] = row
    if admitted["recovery_plan"]["sha256"] != R2_SHA:
        raise ValueError("Only the exact frozen r2 campaign may be evaluated")
    train, reload = read_bound(admitted["train_report"]), read_bound(admitted["reload_report"])
    declared = normalize(recovery_plan["source_files"] + [admitted["recovery_plan"]], runtime=True)
    for phase, report in (("train", train), ("reload", reload)):
        before = normalize(report["sources_before"], runtime=True)
        after = normalize(report["sources_after"], runtime=True)
        if (
            report.get("contract") != api.recovery.CONTRACT
            or report.get("phase") != phase
            or type(report.get("native_exit_code")) is not int
            or report["native_exit_code"] != 0
            or report.get("plan_sha256") != R2_SHA
            or report.get("model_loaded") is not True
            or report.get("source_bindings_unchanged") is not True
            or not before
            or before != after
            or any(row not in before for row in declared)
            or any(report.get(key) is not False for key in api.recovery.FALSE_CLAIMS)
            or report.get("base_parameter_sha256_after")
            != recovery_plan["expected_base_parameter_sha256"]
        ):
            raise ValueError("Closed phase/report/source/base admission differs")
    if normalize(train["sources_before"], runtime=True) != normalize(
        reload["sources_before"], runtime=True
    ):
        raise ValueError("Train and fresh reload source inventories differ")
    journal = api.recovery.verify_journal(
        bound_path(admitted["journal"]), recovery_plan["schedule"], require_complete=True
    )
    if (
        canonical(journal) != canonical(train.get("journal"))
        or train.get("optimizer_update_uncertain") is not False
        or train.get("training_completed") is not True
        or train.get("optimizer_performed") is not True
        or train.get("cumulative_campaign_attempts") != 280
        or any(
            type(train.get("optimizer_steps_" + s)) is not int
            or train["optimizer_steps_" + s] != 267
            for s in STAGES
        )
        or reload.get("fresh_saved_adapter_reload_verified") is not True
        or reload.get("optimizer_performed") is not False
        or any(
            type(reload.get("optimizer_steps_" + s)) is not int
            or reload["optimizer_steps_" + s] != 0
            for s in STAGES
        )
        or train.get("saved_adapter_weights") != admitted["weights"]
        or train.get("saved_adapter_config") != admitted["config"]
        or train.get("candidate_all86") != admitted["candidate86"]
    ):
        raise ValueError("Completed267 journal, saved files, or fresh reload differs")
    digest = train["adapter_parameter_sha256_after"]
    require_sha(digest)
    if (
        reload.get("adapter_parameter_sha256_before") != digest
        or reload.get("adapter_parameter_sha256_after") != digest
    ):
        raise ValueError("Fresh reload does not retain exact saved candidate parameters")
    candidate, reloaded = read_bound(admitted["candidate86"]), read_bound(admitted["reloaded86"])
    if (
        len(candidate) != 86
        or len(reloaded) != 86
        or len({r["sample_id"] for r in candidate}) != 86
    ):
        raise ValueError("Original86 exact reload comparison is incomplete")
    for prior, current in zip(candidate, reloaded, strict=True):
        for key in ("sample_id", "actor_identity_sha256", "actual_prefix_tensors", "diagnostic"):
            if canonical(prior[key]) != canonical(current[key]):
                raise ValueError("Original86 fresh reload input/diagnosis differs")
        for key in ("raw_output", "output_token_ids"):
            if prior["generated"][key] != current["generated"][key]:
                raise ValueError("Original86 fresh reload raw generation differs")
    return admitted, train, reload


def admit_dataset(report, api):
    if (
        report.get("contract") != "multidetect.openfly-original82-supervision-bridge-v1"
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or type(report.get("sample_count")) is not int
        or report["sample_count"] != 82
        or report.get("partition_counts") != {part: count for _, part, count in ROLES}
        or report.get("source_validation_session_verified") is not True
        or report.get("sources_unchanged") is not True
        or report.get("pose_future_truth_or_label_in_actor_input") is not False
        or report.get("aggregate_annotation_actions_used_as_gold") is not False
        or report.get("training_ready") is not False
        or report.get("optimizer_authorized") is not False
        or report.get("official_seen_unseen_holdout_verified") is not False
        or canonical(report["source_bindings_before"]) != canonical(report["source_bindings_after"])
        or report["converter"]["sha256"]
        != PINNED_CODE["scripts/build_onboard_openfly_original82_supervision.py"]
    ):
        raise ValueError("Actual original82 normal verified bridge receipt required")
    file = verify(report["dataset"])
    if file.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Typed original82 metadata exceeds cap")
    samples = tuple(
        api.navigation.NavigationSupervisionSample.from_json(line)
        for line in file.read_bytes().splitlines()
        if line.strip()
    )
    api.navigation.validate_navigation_episode_samples(samples)
    expected = [(scene, part, step) for scene, part, count in ROLES for step in range(count)]
    if [(s.scene_id, s.partition, s.step_index) for s in samples] != expected:
        raise ValueError("Only original ordered23/17/42 complete scene roles are admitted")
    actors = report["sample_actor_target_bindings"]
    if len(actors) != 82 or [r["sample_id"] for r in actors] != [s.sample_id for s in samples]:
        raise ValueError("Bridge Actor/target bindings do not cover exact original82 order")
    for sample, row in zip(samples, actors, strict=True):
        require_sha(row["actor_identity_sha256"])
        if (
            sample.source.original_split != "train"
            or sample.source.review_receipt is not None
            or row["ordered_frame_steps"] != [f.step_index for f in sample.frames]
            or canonical(row["target"]) != canonical(gold_mapping(sample))
        ):
            raise ValueError("Original TRAIN/Actor history/planned gold binding differs")
    selected = tuple(s for s in samples if s.partition != "train")
    if len(selected) != 59:
        raise ValueError("Exactly17+42 development rows, not60 or82")
    return samples, selected, {r["sample_id"]: r for r in actors}


def fresh_output(path):
    output = (ROOT / path).resolve(strict=False)
    if (
        output.parent != ROOT / "artifacts/evaluation"
        or not output.name.startswith("onboard-navigation-original82-")
        or output.exists()
        or output.is_symlink()
    ):
        raise ValueError("One NEW create-only offline evaluation directory required")
    return output


def build_plan(terminal_evidence, terminal_sha, dataset_report, dataset_sha, output_dir):
    terminal_binding = explicit_binding(terminal_evidence, terminal_sha)
    evidence = read_bound(terminal_binding)  # No missing-terminal fallback or active-run inference.
    recovery_binding = explicit_binding(R2_PLAN, R2_SHA)
    recovery_plan = read_bound(recovery_binding)
    code = [
        row
        for row in normalize(recovery_plan["source_files"], runtime=True)
        if Path(row["path"]).suffix == ".py" and Path(row["path"]).is_relative_to(ROOT)
    ]
    code += [explicit_binding(name, digest) for name, digest in PINNED_CODE.items()]
    code += [record(ROOT / name) for name in (*NEW_FILES, *EXTRA_CODE)]
    authenticate_code(normalize(code))
    api = apis()
    terminal_rows, train, _reload = verify_terminal(evidence, recovery_plan, api)
    bridge_binding = explicit_binding(dataset_report, dataset_sha)
    bridge = read_bound(bridge_binding)
    samples, selected, actor_bindings = admit_dataset(bridge, api)
    classes = dict(
        Counter(
            primitive_class(api.inference.parse_navigation_target(canonical(gold_mapping(s))))
            for s in selected
        )
    )
    if classes != {"forward": 54, "yaw_right": 1, "up": 1, "down": 1, "stop": 2}:
        raise ValueError(
            "Actual original59 class coverage differs; fixture categories are not real samples"
        )
    legacy = read_bound(recovery_plan["legacy_plan"])
    runtime = read_bound(legacy["runtime_reference"])
    source_rows = [
        *recovery_plan["source_files"],
        *code,
        terminal_binding,
        bridge_binding,
        *terminal_rows.values(),
        *bridge["source_bindings_before"],
        bridge["dataset"],
        legacy["runtime_reference"],
        legacy["original_plan"],
        recovery_plan["legacy_plan"],
        legacy["original_training_report"],
        *recovery_plan["warm_start"].values(),
    ]
    for sample in samples:
        source_rows += [
            v
            for v in sample.source.to_mapping().values()
            if type(v) is dict and set(v) == {"path", "bytes", "sha256"}
        ]
        source_rows += [
            {k: f.to_mapping()[k] for k in ("path", "bytes", "sha256")} for f in sample.frames
        ]
    inventory = normalize(source_rows, runtime=True)
    repo_inventory = [row for row in inventory if Path(row["path"]).is_relative_to(ROOT)]
    external_inventory = [row for row in inventory if not Path(row["path"]).is_relative_to(ROOT)]
    return {
        "schema_version": 1,
        "contract": PLAN_CONTRACT,
        "artifact_root": str(ROOT),
        "terminal_evidence": terminal_binding,
        "recovery_plan": recovery_binding,
        "dataset_report": bridge_binding,
        "dataset": bridge["dataset"],
        "recovery_artifacts": terminal_rows,
        "legacy_plan": recovery_plan["legacy_plan"],
        "expected_base_parameter_sha256": recovery_plan["expected_base_parameter_sha256"],
        "expected_candidate_parameter_sha256": train["adapter_parameter_sha256_after"],
        "sample_ids": [s.sample_id for s in selected],
        "sample_count": 59,
        "partitions": {"dev-validation": 17, "dev-test": 42},
        "gold_class_counts": classes,
        "actor_identity_sha256": {
            s.sample_id: actor_bindings[s.sample_id]["actor_identity_sha256"] for s in selected
        },
        "source_files": repo_inventory,
        "runtime_sources": normalize(external_inventory + runtime["source_files"], runtime=True),
        "runtime_reference": legacy["runtime_reference"],
        "model_budget": recovery_plan["model_budget"],
        "output_dir": str(fresh_output(output_dir)),
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "split_scope": "original_TRAIN_internal_cross_scene_development_not_official_holdout",
        "loader_selector": (
            "frozen_reload_branch_original_r2_reload_path_read_only_no_original_phase_rerun"
        ),
        "generation": {
            "max_new_tokens": 192,
            "cooperative_max_time_s": 30.0,
            "do_sample": False,
            "translation_tolerance_m": 1e-4,
            "yaw_tolerance_rad": 1e-6,
        },
        "old86_reference_records_reused_not_rerun": {
            "base": recovery_plan["base_round"],
            "warm": recovery_plan["warm_round"],
        },
    }


def admit_plan(path, digest):
    binding = explicit_binding(path, digest)
    plan = read_bound(binding)
    authenticate_code([row for row in plan["source_files"] if Path(row["path"]).suffix == ".py"])
    expected = build_plan(
        bound_path(plan["terminal_evidence"]),
        plan["terminal_evidence"]["sha256"],
        bound_path(plan["dataset_report"]),
        plan["dataset_report"]["sha256"],
        plan["output_dir"],
    )
    if canonical(plan) != canonical(expected):
        raise ValueError(
            "Evaluation plan differs from exact completed candidate/dataset/source admission"
        )
    return plan, binding


def capture_sources(plan, plan_binding):
    repo = normalize(plan["source_files"] + [plan_binding])
    runtime = normalize(plan["runtime_sources"], runtime=True)
    for row in repo:
        verify(row)
    for row in runtime:
        verify(row, runtime=True)
    return {"repository": repo, "runtime": runtime}


def create_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def append_row(stream, value):
    stream.write(canonical(value) + b"\n")
    stream.flush()
    os.fsync(stream.fileno())


def actor_for_sample(sample, api):
    frames = []
    for frame in sample.frames:
        source = api.navigation.NavigationSourceFile(frame.path, frame.bytes, frame.sha256)
        raw = source.read(ROOT, maximum_bytes=8 * 1024 * 1024)
        frames.append(
            api.actor.NavigationActorFrame.from_observation(
                sequence=frame.step_index,
                image_bytes=raw,
                byte_count=frame.bytes,
                sha256=frame.sha256,
                width=frame.width,
                height=frame.height,
                encoding=frame.encoding,
            )
        )
    return api.actor.NavigationActorInput.from_observation(
        prompt_original=sample.prompt.text_original,
        current_sequence=sample.step_index,
        frames=tuple(frames),
    )


def generate_observed(backend, inputs):
    """Observe one unmodified generation call; restore the instance hook on all exits."""
    model, torch = backend.model, backend.torch
    original = model.generate
    owned_attribute = "generate" in model.__dict__
    previous_attribute = model.__dict__.get("generate")
    prefix_ids = inputs["input_ids"][0].detach().cpu().tolist()
    observation = {"calls": 0, "hook_restored": False}

    def observed(**kwargs):
        observation["calls"] += 1
        if observation["calls"] != 1 or any(
            kwargs.get(key) != value
            for key, value in {
                "do_sample": False,
                "max_new_tokens": 192,
                "max_time": 30.0,
                "use_cache": True,
            }.items()
        ):
            raise ValueError("One unchanged frozen deterministic generation call required")
        output = original(**kwargs)
        if (
            output.dtype != torch.int64
            or output.ndim != 2
            or output.shape[0] != 1
            or not len(prefix_ids) < output.shape[1] <= len(prefix_ids) + 192
        ):
            raise ValueError("Actual generation must be one bounded prefix-plus-suffix int64 row")
        actual = output[0].detach().cpu().tolist()
        if actual[: len(prefix_ids)] != prefix_ids:
            raise ValueError("Actual generated prefix differs from the exact supplied CPU prefix")
        observation.update(
            actual_prefix_tokens=len(prefix_ids),
            actual_output_tokens=len(actual),
            actual_output_prefix_verified=True,
            suffix_ids=actual[len(prefix_ids) :],
        )
        return output

    model.generate = observed
    try:
        generated = backend.generate({"prefix": inputs}, parent=False)
    finally:
        if owned_attribute:
            model.generate = previous_attribute
        else:
            delattr(model, "generate")
        observation["hook_restored"] = model.generate == original
        if observation["hook_restored"] is not True:
            raise ValueError("Request-local model.generate observation hook was not restored")
    validate_generated(generated)
    if observation["calls"] != 1 or observation.get("suffix_ids") != generated["output_token_ids"]:
        raise ValueError("Frozen backend suffix tokens differ from actual observed generation")
    raw = backend.processor.tokenizer.decode(
        observation["suffix_ids"], skip_special_tokens=True, clean_up_tokenization_spaces=False
    )
    if raw != generated["raw_output"]:
        raise ValueError("Frozen backend raw text differs from exact observed suffix decoding")
    del observation["suffix_ids"]
    observation["raw_decode_independently_rechecked"] = True
    return generated, observation


def predict_rows(backend, samples, plan, api, stream, report):
    """Gold is not touched here; durable raw predictions precede offline scoring."""
    rows = []
    for sample in samples:
        actor = actor_for_sample(sample, api)
        if actor.identity_sha256 != plan["actor_identity_sha256"][sample.sample_id]:
            raise ValueError("Actual current/latest-past RGB Actor differs from sealed bridge")
        inputs = api.prefix.process_actor(actor, backend.processor, api.actor)
        prefix = api.prefix.validate_prefix_inputs(
            inputs,
            processor=backend.processor,
            image_count=len(sample.frames),
            torch_module=backend.torch,
            tensor_record=api.pilot.tensor_record,
        )
        del actor
        api.shared_backend.PreloadedNavigationInferenceBackend._check_adapter_state(backend.model)
        report["generation_attempts"] += 1
        generated, generation_observation = generate_observed(backend, inputs)
        report["generation_returns"] += 1
        del inputs
        validate_generated(generated)
        row = {
            "sample_id": sample.sample_id,
            "partition": sample.partition,
            "scene_id": sample.scene_id,
            "step_index": sample.step_index,
            "actor_identity_sha256": plan["actor_identity_sha256"][sample.sample_id],
            "prefix": prefix,
            "generated": generated,
            "generation_observation": generation_observation,
            **generation_eligibility(generated),
            "source_capture_time_unknown": True,
            "online_freshness_exercised": False,
        }
        append_row(stream, row)
        rows.append(row)
        report["durable_prediction_count"] += 1
        api.pilot.check_memory(backend, backend.budget)
        api.shared_backend.PreloadedNavigationInferenceBackend._check_adapter_state(backend.model)
        print(
            json.dumps(
                {
                    "event": "original82_prediction_saved",
                    "sample_id": sample.sample_id,
                    "completed": len(rows),
                    "required": 59,
                }
            ),
            flush=True,
        )
    return rows


def run(args):
    plan, plan_binding = admit_plan(args.plan, args.plan_sha256)
    output = fresh_output(plan["output_dir"])
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "contract": CONTRACT,
        "plan_sha256": args.plan_sha256,
        "pid": os.getpid(),
        "started_at_utc": datetime.now(UTC).isoformat(),
        "native_exit_code": 1,
        "model_loaded": False,
        "generation_attempts": 0,
        "generation_returns": 0,
        "durable_prediction_count": 0,
        "source_bindings_unchanged": False,
        "sources_before": None,
        "sources_after": None,
        "host_memory_observations": [],
        **plan["claims"],
    }
    backend = None
    try:
        report["sources_before"] = capture_sources(plan, plan_binding)
        api = apis()
        runtime = read_bound(plan["runtime_reference"])
        if Path(sys.executable).resolve() != Path(runtime["python_executable"]).resolve():
            raise ValueError("Pinned actual navigation runtime executable required")
        for package, version in runtime["package_versions"].items():
            if importlib.metadata.version(package) != version:
                raise ValueError("Actual navigation runtime distribution differs")
        from scripts.onboard_navigation_host_memory import snapshot

        def observe(stage):
            report["host_memory_observations"].append({"stage": stage, "snapshot": snapshot()})

        bridge = read_bound(plan["dataset_report"])
        _all, samples, _actors = admit_dataset(bridge, api)
        if [s.sample_id for s in samples] != plan["sample_ids"]:
            raise ValueError("Only the bound59 development rows may enter the model")
        recovery = read_bound(plan["recovery_plan"])
        legacy = read_bound(plan["legacy_plan"])
        old = read_bound(legacy["original_plan"])
        run_dir = Path(recovery["output_dir"]).resolve(strict=True)
        budget = api.recovery.RecoveryModelBudget(**plan["model_budget"])
        budget.validate()
        loader = {
            "phase": "reload",
            "pid": os.getpid(),
            "scope": "read_only_existing_candidate_loader_selector_not_original_reload_phase_rerun",
        }
        report["loader_observation"] = loader
        observe("load_before")
        # The old output argument is solely a path selector; these functions do
        # not create/write original r2 reload artifacts. All NEW writes use output.
        with api.old.loading_scope(legacy, old, run_dir / "reload", loader):
            backend = api.old.load_backend(
                legacy, budget, old, api.pilot, "reload", run_dir / "reload", loader
            )
        api.recovery.check_loader_cleanup(loader, legacy, old, run_dir)
        report["model_loaded"] = loader["model_loaded"] is True
        if (
            loader["adapter_parameter_sha256_before"] != plan["expected_candidate_parameter_sha256"]
            or loader["base_parameter_sha256_before"] != plan["expected_base_parameter_sha256"]
        ):
            raise ValueError("Actual exact candidate/base parameter identity differs")
        observe("load_after")
        with (output / "predictions.jsonl").open("xb") as stream:
            predictions = predict_rows(backend, samples, plan, api, stream, report)
        if len(predictions) != 59:
            raise ValueError("Incomplete original59 prediction round")
        # All raw predictions are durable before any gold is passed to scoring.
        scored = [
            {
                **row,
                "gold": gold_mapping(sample),
                "score": score_prediction(
                    row["generated"]["raw_output"], gold_mapping(sample), api.inference
                ),
            }
            for row, sample in zip(predictions, samples, strict=True)
        ]
        create_json(output / "scores.json", scored)
        report["partition_summaries"] = {
            part: summarize([r for r in scored if r["partition"] == part])
            for part in ("dev-validation", "dev-test")
        }
        report["summary"] = summarize(scored)
        report["base_parameter_sha256_after"] = api.pilot.core.parameter_digest(
            backend.model, trainable=False
        )
        report["adapter_parameter_sha256_after"] = api.pilot.core.parameter_digest(
            backend.model, trainable=True
        )
        if (
            report["base_parameter_sha256_after"] != plan["expected_base_parameter_sha256"]
            or report["adapter_parameter_sha256_after"]
            != plan["expected_candidate_parameter_sha256"]
        ):
            raise ValueError("Offline inference changed exact base/candidate parameters")
        api.shared_backend.PreloadedNavigationInferenceBackend._check_adapter_state(backend.model)
        backend = None
        gc.collect()
        observe("model_released")
        report["sources_after"] = capture_sources(plan, plan_binding)
        if report["sources_before"] != report["sources_after"]:
            raise ValueError("Evaluation sources changed before sealing")
        report.update(
            native_exit_code=0,
            source_bindings_unchanged=True,
            predictions=record(output / "predictions.jsonl"),
            scores=record(output / "scores.json"),
            timing_scope=(
                "frozen_generate_timer_after_H2D_includes_actual_output_prefix_observer_"
                "generate_sync_D2H_decode_not_online_end_to_end_or_uninstrumented_benchmark"
            ),
            actual_output_prefix_independently_verified=True,
            generic_fixture_only_classes_not_actual_observed_coverage=True,
            gold_scored_only_after_all_raw_predictions_durable=True,
            split_scope=plan["split_scope"],
            original_train_rows_entered_model=0,
            old86_reference_records_reused_not_rerun=plan[
                "old86_reference_records_reused_not_rerun"
            ],
        )
    except BaseException as error:
        report.update(
            failure_type=type(error).__name__,
            failure=str(error)[:4096],
            partial_outputs_preserved=True,
        )
        traceback.print_exc()
    finally:
        # A cleanup failure after exact loading must not erase that observation.
        report["model_loaded"] = (
            report["model_loaded"]
            or report.get("loader_observation", {}).get("model_loaded") is True
        )
        backend = None
        gc.collect()
        report["ended_at_utc"] = datetime.now(UTC).isoformat()
        create_json(output / "report.json", report)
    print(
        json.dumps(
            {
                "event": "original82_offline_evaluation_finished",
                "native_exit_code": report["native_exit_code"],
                "report": record(output / "report.json"),
            }
        ),
        flush=True,
    )
    return report["native_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--terminal-evidence")
    parser.add_argument("--terminal-evidence-sha256")
    parser.add_argument("--dataset-report")
    parser.add_argument("--dataset-report-sha256")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    if args.emit_plan:
        if not all(
            (
                args.terminal_evidence,
                args.terminal_evidence_sha256,
                args.dataset_report,
                args.dataset_report_sha256,
                args.output_dir,
            )
        ):
            parser.error(
                "Emission requires actual sealed terminal and bridge reports/SHA + NEW output"
            )
        print(
            canonical(
                build_plan(
                    args.terminal_evidence,
                    args.terminal_evidence_sha256,
                    args.dataset_report,
                    args.dataset_report_sha256,
                    args.output_dir,
                )
            ).decode("utf-8")
        )
        return 0
    if not all((args.plan, args.plan_sha256)):
        parser.error("Explicit emitted plan and SHA required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
