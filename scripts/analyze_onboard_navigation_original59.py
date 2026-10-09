"""Read-only, stdlib analysis of a closed original59 offline evaluation.

No model, image decoder, live WAL, weights, or original annotations are read.
The fixed 3 m forward reference is mathematical, not a model execution. Scores
are rederived from raw predictions against SHA-bound typed bridge metadata.
Reported phase native codes are not independent observations of tool exits.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-original59-result-analysis-v1"
EVALUATOR = "scripts/evaluate_onboard_navigation_original82.py"
PINNED = {
    EVALUATOR: (45155, "6d3f502df0dbad6cc511634a06b70a97b84598451a4d83f1e1f15b6f53ff2b69"),
    "src/multidetect/__init__.py": (
        28021,
        "127ab1e3babbde50f21bc5e64baccfe2addc03144088fc9fc643893f52b7c74c",
    ),
    "src/multidetect/autonomy/__init__.py": (
        6518,
        "b69698b69dedbca3a0ebecb76c71098f3c5944dfd41d871a675eaa7d24990df7",
    ),
    "src/multidetect/autonomy/onboard_navigation_inference.py": (
        18843,
        "3838a9ee4ee1eef58a680369076157263cfe824bc75c6dcbfe265a977564954f",
    ),
    "src/multidetect/autonomy/onboard_navigation_supervision.py": (
        52186,
        "bd1adc75a6954c2eb2453ae358536cae2db65305dd0c3afcf57d24a1711bb14e",
    ),
    "src/multidetect/autonomy/onboard_navigation_actor.py": (
        13167,
        "eaefc34cd199295c43cab927d3c51182bfa728fd1753c7ac2e4150a1400a83e1",
    ),
    "src/multidetect/autonomy/onboard_history.py": (
        12216,
        "76d9c780306e773f8f32a1c528c031b1614773d4773885d2c335d854e2706dbb",
    ),
    "src/multidetect/autonomy/onboard_multimodal.py": (
        25597,
        "3dffd99197e1cdb204ef83862b00861d19ebe9e5ff1d20c95c46ae2d7227b659",
    ),
    "src/multidetect/autonomy/serialization.py": (
        8656,
        "719578fb4c0a66ca63ee407ac9a05738515cb590946cc9a5cd2625da2762ab56",
    ),
}
GOLD_COUNTS = {"forward": 54, "yaw_right": 1, "up": 1, "down": 1, "stop": 2}
BLOCKED = {"torch", "transformers", "peft", "accelerate", "numpy", "PIL", "pyarrow"}
SMALL_LIMIT = 2 * 1024 * 1024
RESULT_LIMIT = 4 * 1024 * 1024


def check(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("Nonfinite JSON constant")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    canonical(result)
    return result


def path_in_repo(value, *, exists=True):
    check(type(value) is str or isinstance(value, Path), "Path must be text")
    original = ROOT / value
    resolved = original.resolve(strict=exists)
    check(resolved.is_relative_to(ROOT.resolve()), "Path escapes repository")
    cursor = original.absolute()
    while cursor.is_relative_to(ROOT.absolute()) and cursor != ROOT.absolute():
        check(not cursor.is_symlink(), "Symlink evidence path is not admitted")
        cursor = cursor.parent
    return resolved


def require_sha(value):
    check(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "Canonical SHA required")


def binding(path, *, limit=SMALL_LIMIT):
    path = path_in_repo(path)
    size = path.stat().st_size
    check(path.is_file() and 0 < size <= limit, "Small artifact byte budget exceeded")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(65536):
            digest.update(data)
    check(path.stat().st_size == size, "Artifact changed while hashing")
    return {"path": str(path), "bytes": size, "sha256": digest.hexdigest()}


def declared(row, *, external=False):
    check(type(row) is dict and set(row) == {"path", "bytes", "sha256"}, "Exact binding required")
    check(
        type(row["path"]) is str and type(row["bytes"]) is int and row["bytes"] > 0,
        "Binding field types differ",
    )
    require_sha(row["sha256"])
    path = (ROOT / row["path"]).resolve(strict=False)
    check(external or path.is_relative_to(ROOT.resolve()), "Declared source escapes repository")
    return {**row, "path": str(path)}


def inventory(rows, *, external=False):
    check(type(rows) is list and len(rows) <= 4096, "Bound source inventory required")
    result = {}
    for row in rows:
        normalized = declared(row, external=external)
        path = normalized["path"]
        check(path not in result or result[path] == normalized, "Conflicting source declarations")
        result[path] = normalized
    return [result[path] for path in sorted(result)]


def read_bound(row, consumed, *, jsonl=False, limit=SMALL_LIMIT):
    row = declared(row)
    check(row["bytes"] <= limit, "Bound small result exceeds cap")
    check(binding(row["path"], limit=limit) == row, "Bound artifact bytes changed")
    raw = Path(row["path"]).read_bytes()
    check(binding(row["path"], limit=limit) == row, "Artifact changed during reading")
    consumed.append(row)
    if jsonl:
        check(raw.endswith(b"\n"), "JSONL is truncated")
        lines = raw.splitlines()
        check(all(line.strip() for line in lines), "Blank JSONL row is not admitted")
        return [strict_json(line) for line in lines]
    return strict_json(raw)


def code_bindings():
    rows = []
    for name, expected in PINNED.items():
        row = binding(name)
        check((row["bytes"], row["sha256"]) == expected, "Frozen scoring source differs: " + name)
        rows.append(row)
    return rows


def load_scoring():
    before = code_bindings()
    check(not BLOCKED.intersection(sys.modules), "Analysis requires a no-ML Python process")
    spec = importlib.util.spec_from_file_location(
        "_original59_authenticated_eval", ROOT / EVALUATOR
    )
    check(spec is not None and spec.loader is not None, "Cannot import frozen evaluator")
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from multidetect.autonomy import onboard_navigation_inference as inference
        from multidetect.autonomy import onboard_navigation_supervision as navigation
    finally:
        sys.path[:] = previous
    for module, name in (
        (inference, "onboard_navigation_inference"),
        (navigation, "onboard_navigation_supervision"),
    ):
        check(
            Path(module.__file__).resolve() == ROOT / f"src/multidetect/autonomy/{name}.py",
            "Cached parser is not the authenticated repository module",
        )
    check(code_bindings() == before, "Scoring source changed during import")
    check(not BLOCKED.intersection(sys.modules), "Scoring imported an ML/image dependency")
    return SimpleNamespace(evaluation=evaluation, inference=inference, navigation=navigation)


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def joint(score):
    return all(
        score[key] is True
        for key in ("strict_target_valid", "action_class_correct", "metric_correct")
    )


def error_statistics(values, api):
    return {
        "count": len(values),
        "mean": ratio(sum(values), len(values)),
        "p50": api.evaluation.percentile(values, 0.5),
        "p95": api.evaluation.percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def statistics(scores, api, gates=None):
    n = len(scores)
    classes = {}
    for name in GOLD_COUNTS:
        group = [s for s in scores if s["gold_class"] == name]
        support = len(group)
        tp = sum(s["action_class_correct"] is True for s in group)
        predicted = sum(s["predicted_class"] == name for s in scores)
        entry = {
            "support": support,
            "true_positive": tp,
            "false_negative": support - tp,
            "false_positive": predicted - tp,
            "recall": ratio(tp, support),
            "precision": ratio(tp, predicted),
            "f1": ratio(2 * tp, support + predicted),
            "raw_joint_correct_count": sum(joint(s) for s in group),
            "raw_joint_correct_rate_all_support": ratio(sum(joint(s) for s in group), support),
            "metric_coverage": metric_coverage(group, api),
        }
        for key in ("strict_target_valid", "action_class_correct", "metric_correct"):
            entry["raw_" + key + "_count"] = sum(s[key] is True for s in group)
        if gates is not None:
            eligible = [
                s
                for s, gate in zip(scores, gates, strict=True)
                if s["gold_class"] == name and gate["bounded_generation_eligible"]
            ]
            entry.update(
                eligible_count=len(eligible),
                budget_rejected_count=support - len(eligible),
                eligible_joint_correct_count=sum(joint(s) for s in eligible),
                eligible_joint_correct_rate_all_support=ratio(
                    sum(joint(s) for s in eligible), support
                ),
                eligible_joint_correct_rate_conditional=ratio(
                    sum(joint(s) for s in eligible), len(eligible)
                ),
            )
            for key in ("action_class_correct", "metric_correct"):
                entry["eligible_" + key + "_count"] = sum(s[key] is True for s in eligible)
        classes[name] = entry
    supported = [entry for entry in classes.values() if entry["support"]]
    result = {
        "sample_count": n,
        "by_class": classes,
        "raw_action_correct_count": sum(s["action_class_correct"] is True for s in scores),
        "raw_joint_correct_count": sum(joint(s) for s in scores),
        "raw_action_accuracy": ratio(sum(s["action_class_correct"] is True for s in scores), n),
        "macro_recall_gold_present_classes": ratio(
            sum(e["recall"] for e in supported), len(supported)
        ),
        "macro_class_count": len(supported),
        "macro_joint_correct_rate": ratio(
            sum(e["raw_joint_correct_rate_all_support"] for e in supported), len(supported)
        ),
        "confusion": [
            {"gold": a, "predicted": b, "count": count}
            for (a, b), count in sorted(
                Counter((s["gold_class"], s["predicted_class"]) for s in scores).items()
            )
        ],
        "metric_coverage": metric_coverage(scores, api),
    }
    if gates is not None:
        eligible = [
            s for s, gate in zip(scores, gates, strict=True) if gate["bounded_generation_eligible"]
        ]
        result.update(
            eligible_count=len(eligible),
            budget_rejected_count=n - len(eligible),
            eligible_joint_correct_count=sum(joint(s) for s in eligible),
            eligible_joint_correct_rate_all_support=ratio(sum(joint(s) for s in eligible), n),
            macro_eligible_joint_correct_rate=ratio(
                sum(e["eligible_joint_correct_rate_all_support"] for e in supported), len(supported)
            ),
            budget_rejection_counts=dict(
                Counter(g["budget_rejection"] for g in gates if g["budget_rejection"] is not None)
            ),
        )
    return result


def metric_coverage(scores, api):
    n = len(scores)
    noncomparable = sum(s["metric_comparable"] is not True for s in scores)
    components = [
        s["translation_component_absolute_error_m"]
        for s in scores
        if s["translation_component_absolute_error_m"] is not None
    ]
    return {
        "support": n,
        "comparable_count": n - noncomparable,
        "noncomparable_count": noncomparable,
        "noncomparable_rate": ratio(noncomparable, n),
        "wrong_kind_count": sum(
            s["strict_target_valid"] is True and s["kind_correct"] is False for s in scores
        ),
        "invalid_target_count": sum(s["strict_target_valid"] is False for s in scores),
        "predicted_translation_null_count": sum(
            s["predicted_translation_is_null"] is True for s in scores
        ),
        "predicted_yaw_null_count": sum(s["predicted_yaw_is_null"] is True for s in scores),
        "same_kind_unknown_metric_count": sum(
            s["strict_target_valid"] is True
            and s["kind_correct"] is True
            and s["metric_comparable"] is False
            for s in scores
        ),
        "translation_l2_error_m": error_statistics(
            [
                s["translation_l2_error_m"]
                for s in scores
                if s["translation_l2_error_m"] is not None
            ],
            api,
        ),
        "translation_component_absolute_error_m": {
            name: error_statistics([v[index] for v in components], api)
            for index, name in enumerate(("forward", "right", "down"))
        },
        **{
            key: error_statistics([s[key] for s in scores if s[key] is not None], api)
            for key in ("yaw_absolute_error_rad", "yaw_wrapped_absolute_error_rad")
        },
    }


def paired(candidate, baseline, key, candidate_gates=None):
    flags = [
        (joint(a), joint(b))
        if key == "joint"
        else (a["action_class_correct"] is True, b["action_class_correct"] is True)
        for a, b in zip(candidate, baseline, strict=True)
    ]
    if candidate_gates is not None:
        flags = [
            (correct and gate["bounded_generation_eligible"], reference)
            for (correct, reference), gate in zip(flags, candidate_gates, strict=True)
        ]
    return {
        "support": len(flags),
        "candidate_wins": sum(a and not b for a, b in flags),
        "candidate_losses": sum(b and not a for a, b in flags),
        "both_correct": sum(a and b for a, b in flags),
        "both_wrong": sum(not a and not b for a, b in flags),
    }


def analyze(eval_report, eval_report_sha256):
    require_sha(eval_report_sha256)
    report_path = path_in_repo(eval_report)
    check(
        report_path.name == "report.json"
        and report_path.parent.parent == ROOT / "artifacts/evaluation"
        and report_path.parent.name.startswith("onboard-navigation-original82-"),
        "Original offline evaluation report path required",
    )
    consumed = []
    report_binding = binding(report_path)
    check(report_binding["sha256"] == eval_report_sha256, "Explicit evaluation report SHA differs")
    report = read_bound(report_binding, consumed)
    check(
        report.get("contract") == "multidetect.onboard-navigation-original82-offline-evaluation-v1"
        and type(report.get("native_exit_code")) is int
        and report["native_exit_code"] == 0
        and report.get("model_loaded") is True
        and "failure" not in report
        and "failure_type" not in report,
        "A closed successful reported native0 evaluation is required",
    )
    for key in ("generation_attempts", "generation_returns", "durable_prediction_count"):
        check(type(report.get(key)) is int and report[key] == 59, "Complete59 counter differs")
    check(
        type(report.get("original_train_rows_entered_model")) is int
        and report["original_train_rows_entered_model"] == 0,
        "Original23 train rows must remain excluded",
    )
    check(
        all(
            report.get(key) is True
            for key in (
                "source_bindings_unchanged",
                "actual_output_prefix_independently_verified",
                "gold_scored_only_after_all_raw_predictions_durable",
            )
        ),
        "Evaluation closure flags differ",
    )
    start, end = (datetime.fromisoformat(report[key]) for key in ("started_at_utc", "ended_at_utc"))
    check(
        start.tzinfo is not None and end.tzinfo is not None and end >= start,
        "Closed chronology required",
    )
    before = report["sources_before"]
    check(
        type(before) is dict
        and set(before) == {"repository", "runtime"}
        and canonical(before) == canonical(report["sources_after"]),
        "Source arrays did not close",
    )
    require_sha(report["plan_sha256"])
    matching = [row for row in before["repository"] if row["sha256"] == report["plan_sha256"]]
    check(len(matching) == 1, "Exactly one SHA-bound evaluation plan is required")
    check(Path(matching[0]["path"]).suffix == ".json", "Bound plan must be small JSON")
    plan = read_bound(matching[0], consumed)
    expected_inventory = {
        "repository": inventory(plan["source_files"] + [matching[0]]),
        "runtime": inventory(plan["runtime_sources"], external=True),
    }
    check(
        canonical(expected_inventory) == canonical(before),
        "Phase source arrays differ from full plan inventory",
    )
    check(
        plan.get("contract") == report["contract"] + "-plan"
        and path_in_repo(plan["output_dir"]) == report_path.parent,
        "Closed plan contract/output identity differs",
    )
    api = load_scoring()
    code = code_bindings()
    for key in api.evaluation.FALSE_CLAIMS:
        check(
            report.get(key) is False and plan["claims"].get(key) is False,
            "Evaluation overstates claim: " + key,
        )
    declared_code = {row["path"]: row for row in expected_inventory["repository"]}
    for name in (EVALUATOR, "src/multidetect/autonomy/onboard_navigation_inference.py"):
        actual = binding(name)
        check(
            declared_code.get(actual["path"]) == actual, "Scorer is not a bound evaluation source"
        )
    bridge = read_bound(plan["dataset_report"], consumed)
    dataset_binding = declared(plan["dataset"])
    check(
        dataset_binding == declared(bridge["dataset"]),
        "Bridge/plan sample metadata binding differs",
    )
    read_bound(
        dataset_binding, consumed, jsonl=True
    )  # Byte admission before typed parse; no images.
    all_samples, samples, actors = api.evaluation.admit_dataset(bridge, api)
    ids = [sample.sample_id for sample in samples]
    check(
        plan.get("sample_ids") == ids
        and type(plan.get("sample_count")) is int
        and plan["sample_count"] == 59
        and plan.get("partitions") == {"dev-validation": 17, "dev-test": 42}
        and plan.get("gold_class_counts") == GOLD_COUNTS,
        "Fixed59 plan IDs/roles/classes differ",
    )
    check(len(all_samples) == 82 and len(samples) == 59, "Typed82/selected59 coverage differs")
    for key, name in (("predictions", "predictions.jsonl"), ("scores", "scores.json")):
        check(
            path_in_repo(report[key]["path"]) == report_path.parent / name,
            "Result artifact path differs",
        )
    predictions = read_bound(report["predictions"], consumed, jsonl=True, limit=RESULT_LIMIT)
    stored = read_bound(report["scores"], consumed, limit=RESULT_LIMIT)
    check(
        type(stored) is list and len(predictions) == len(stored) == 59,
        "Raw/scored59 coverage differs",
    )
    check(
        [row["sample_id"] for row in predictions] == ids,
        "Raw59 IDs are missing, duplicated, reordered, or include train",
    )
    scored, scores, gates = [], [], []
    for sample, prediction, old_score in zip(samples, predictions, stored, strict=True):
        check(
            "gold" not in prediction and "score" not in prediction,
            "Raw predictions contain gold or scores",
        )
        check(
            prediction["partition"] == sample.partition
            and prediction["scene_id"] == sample.scene_id
            and type(prediction["step_index"]) is int
            and prediction["step_index"] == sample.step_index
            and prediction["actor_identity_sha256"]
            == actors[sample.sample_id]["actor_identity_sha256"]
            == plan["actor_identity_sha256"][sample.sample_id],
            "Raw role/Actor identity differs",
        )
        check(
            prediction.get("source_capture_time_unknown") is True
            and prediction.get("online_freshness_exercised") is False,
            "Offline clock scope differs",
        )
        gold = api.evaluation.gold_mapping(sample)
        score = api.evaluation.score_prediction(
            prediction["generated"]["raw_output"], gold, api.inference
        )
        gate = api.evaluation.generation_eligibility(prediction["generated"])
        check(
            all(
                prediction.get(key) == value and type(prediction.get(key)) is type(value)
                for key, value in gate.items()
            ),
            "Stored generation budget gate differs",
        )
        current = {**prediction, "gold": gold, "score": score}
        check(
            canonical(old_score) == canonical(current),
            "Stored score/raw/gold differs from independent rederivation",
        )
        scored.append(current)
        scores.append(score)
        gates.append(gate)
    check(
        dict(Counter(s["gold_class"] for s in scores)) == GOLD_COUNTS,
        "Actual59 class coverage differs",
    )
    check(
        canonical(api.evaluation.summarize(scored)) == canonical(report["summary"]),
        "Stored overall summary differs",
    )
    partition_summary = {
        part: api.evaluation.summarize([row for row in scored if row["partition"] == part])
        for part in ("dev-validation", "dev-test")
    }
    check(
        canonical(partition_summary) == canonical(report["partition_summaries"]),
        "Stored partition summary differs",
    )
    reference = {
        "schema_version": 1,
        "contract": api.evaluation.TARGET_CONTRACT,
        "kind": "translate",
        "translation_frame": "body_frd",
        "yaw_convention": "clockwise_right_positive",
        "translation_m": [3, 0, 0],
        "yaw_delta_rad": None,
    }
    baseline = [
        api.evaluation.score_prediction(canonical(reference).decode(), row["gold"], api.inference)
        for row in scored
    ]

    def group(indices):
        candidate = [scores[i] for i in indices]
        naive = [baseline[i] for i in indices]
        candidate_gates = [gates[i] for i in indices]
        return {
            "candidate": statistics(candidate, api, candidate_gates),
            "mathematical_always_forward": statistics(naive, api),
            "paired_raw_action": paired(candidate, naive, "action"),
            "paired_raw_joint": paired(candidate, naive, "joint"),
            "paired_budget_qualified_candidate_action_vs_mathematical_reference": paired(
                candidate, naive, "action", candidate_gates
            ),
            "paired_budget_qualified_candidate_joint_vs_mathematical_reference": paired(
                candidate, naive, "joint", candidate_gates
            ),
            "paired_scope": "candidate_cap_or_timeout_is_failure_reference_has_no_execution_budget",
        }

    result = {
        "contract": CONTRACT,
        "schema_version": 1,
        "reported_evaluation_native_exit_code": 0,
        "actual_tool_exit_status_independently_verified_by_analysis": False,
        "raw_scores_and_summaries_independently_rederived": True,
        "overall": group(range(59)),
        "by_partition": {
            part: group([i for i, row in enumerate(scored) if row["partition"] == part])
            for part in ("dev-validation", "dev-test")
        },
        "minority_nonforward": group(
            [i for i, score in enumerate(scores) if score["gold_class"] != "forward"]
        ),
        "stop_rows": [
            {
                "sample_id": row["sample_id"],
                "predicted_class": row["score"]["predicted_class"],
                "bounded_generation_eligible": gate["bounded_generation_eligible"],
                "joint_correct": joint(row["score"]),
            }
            for row, gate in zip(scored, gates, strict=True)
            if row["score"]["gold_class"] == "stop"
        ],
        "small_support_is_not_generalization_evidence": True,
        "zero_support_fixture_only_classes_not_model_coverage": True,
        "mathematical_reference": {
            "target": reference,
            "model_executed": False,
            "token_or_latency_observations_created": False,
            "budget_eligibility_not_assigned": True,
            "distance_scope": "fixed_existing_3m_label_scale_not_fit_to_predictions",
        },
        "metric_correct_thresholds": {
            "translation_l2_m": 1e-4,
            "yaw_absolute_rad": 1e-6,
            "scope": "strict_label_reproduction_not_flight_tolerance",
        },
        "generation_timing_scope": report["timing_scope"],
        "live_freshness_or_online_latency_measured_by_analysis": False,
        "binding_review_scope": (
            "small_consumed_results_and_scoring_sources_not_original_large_assets"
        ),
        "consumed_bindings": consumed,
        "scoring_source_bindings": code,
        "scoring_identity_scope": "nine_selected_entrypoints_not_all_transitive_python_or_stdlib",
        "model_loaded_by_analysis": False,
        "processor_called_by_analysis": False,
        "image_decoded_by_analysis": False,
        "GPU_used_by_analysis": False,
        **dict.fromkeys(api.evaluation.FALSE_CLAIMS, False),
    }
    for row in consumed:
        check(
            binding(row["path"], limit=RESULT_LIMIT) == row,
            "Consumed result changed during analysis",
        )
    check(
        code_bindings() == code and not BLOCKED.intersection(sys.modules),
        "Scoring source/dependency scope changed",
    )
    return result


def output_target(value):
    path = path_in_repo(value, exists=False)
    check(
        path.parent == ROOT / "artifacts/diagnostics"
        and path.name.startswith("onboard-navigation-original59-analysis-")
        and not path.exists(),
        "Fresh create-only original59 diagnostics directory required",
    )
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-report", required=True)
    parser.add_argument("--eval-report-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        output = output_target(args.output_dir)
        own = [binding(__file__), binding("tests/test_analyze_onboard_navigation_original59.py")]
        report = analyze(args.eval_report, args.eval_report_sha256)
        check([binding(row["path"]) for row in own] == own, "Analysis source changed")
        report.update(
            analysis_source_bindings=own,
            analysis_source_bindings_unchanged=True,
            created_at_utc=datetime.now(UTC).isoformat(),
        )
        output.mkdir(exist_ok=False)
        with (output / "report.json").open("xb") as stream:
            stream.write(canonical(report) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(
            json.dumps(
                {
                    "event": "original59_analysis_finished",
                    "native_exit_code": 0,
                    "report": binding(output / "report.json"),
                }
            ),
            flush=True,
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "original59_analysis_rejected",
                    "native_exit_code": 1,
                    "failure_type": type(error).__name__,
                    "failure": str(error)[:2048],
                    "quality_claim": False,
                    "candidate_promoted": False,
                    "execution_authority": False,
                    "final_release_allowed": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
