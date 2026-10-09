"""Small CPU fixtures only: no actual59 model results or source-image decode."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import analyze_onboard_navigation_original59 as analysis

SPEC = importlib.util.spec_from_file_location(
    "_original59_frozen_fixtures",
    analysis.ROOT / "tests/test_evaluate_onboard_navigation_original82.py",
)
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
evaluation = fixtures.evaluation
API = SimpleNamespace(
    evaluation=evaluation, inference=fixtures.inference, navigation=fixtures.navigation
)


def write(path, value, *, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        b"".join(analysis.canonical(row) + b"\n" for row in value)
        if jsonl
        else analysis.canonical(value) + b"\n"
    )
    path.write_bytes(raw)
    return analysis.binding(path, limit=analysis.RESULT_LIMIT)


@pytest.fixture
def closed(tmp_path, monkeypatch):
    original_root = analysis.ROOT
    # Copy only small frozen code bytes into the fixture's repository. Imports
    # use already-loaded CPU parser functions, not these synthetic execution files.
    for name in (
        *analysis.PINNED,
        "scripts/analyze_onboard_navigation_original59.py",
        "tests/test_analyze_onboard_navigation_original59.py",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((original_root / name).read_bytes())
    monkeypatch.setattr(analysis, "ROOT", tmp_path)
    monkeypatch.setattr(
        analysis, "__file__", str(tmp_path / "scripts/analyze_onboard_navigation_original59.py")
    )
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(analysis, "load_scoring", lambda: API)
    samples = fixtures.typed_dataset(tmp_path)
    bridge = fixtures.dataset_report(tmp_path, samples)
    bridge_binding = write(tmp_path / "bridge/report.json", bridge)
    selected = [sample for sample in samples if sample.partition != "train"]
    output = tmp_path / "artifacts/evaluation/onboard-navigation-original82-fixture"
    output.mkdir(parents=True)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    plan = {
        "contract": evaluation.PLAN_CONTRACT,
        "output_dir": str(output),
        "source_files": [
            analysis.binding(analysis.EVALUATOR),
            analysis.binding("src/multidetect/autonomy/onboard_navigation_inference.py"),
            bridge_binding,
            bridge["dataset"],
        ],
        "runtime_sources": [],
        "dataset_report": bridge_binding,
        "dataset": bridge["dataset"],
        "sample_ids": [s.sample_id for s in selected],
        "sample_count": 59,
        "partitions": {"dev-validation": 17, "dev-test": 42},
        "gold_class_counts": analysis.GOLD_COUNTS.copy(),
        "actor_identity_sha256": {s.sample_id: fixtures.SHA for s in selected},
        "claims": dict.fromkeys(evaluation.FALSE_CLAIMS, False),
    }
    plan_binding = write(tmp_path / "configs/evaluation/fixture59.json", plan)
    source = {
        "repository": analysis.inventory(plan["source_files"] + [plan_binding]),
        "runtime": [],
    }
    predictions, scores = [], []
    forward = json.dumps(fixtures.target(vector=[3, 0, 0]))
    for sample in selected:
        generated = fixtures.generated(forward)
        prediction = {
            "sample_id": sample.sample_id,
            "partition": sample.partition,
            "scene_id": sample.scene_id,
            "step_index": sample.step_index,
            "actor_identity_sha256": fixtures.SHA,
            "prefix": {"fixture": "CPU metadata only, no actual tensors"},
            "generation_observation": {"fixture": "not a real model generation"},
            "generated": generated,
            **evaluation.generation_eligibility(generated),
            "source_capture_time_unknown": True,
            "online_freshness_exercised": False,
        }
        gold = evaluation.gold_mapping(sample)
        predictions.append(prediction)
        scores.append(
            {
                **prediction,
                "gold": gold,
                "score": evaluation.score_prediction(forward, gold, fixtures.inference),
            }
        )
    predictions_binding = write(output / "predictions.jsonl", predictions, jsonl=True)
    scores_binding = write(output / "scores.json", scores)
    report = {
        "contract": evaluation.CONTRACT,
        "native_exit_code": 0,
        "model_loaded": True,
        "generation_attempts": 59,
        "generation_returns": 59,
        "durable_prediction_count": 59,
        "original_train_rows_entered_model": 0,
        "source_bindings_unchanged": True,
        "actual_output_prefix_independently_verified": True,
        "gold_scored_only_after_all_raw_predictions_durable": True,
        "started_at_utc": "2026-10-08T01:00:00+00:00",
        "ended_at_utc": "2026-10-08T01:01:00+00:00",
        "plan_sha256": plan_binding["sha256"],
        "sources_before": source,
        "sources_after": copy.deepcopy(source),
        "predictions": predictions_binding,
        "scores": scores_binding,
        "summary": evaluation.summarize(scores),
        "partition_summaries": {
            part: evaluation.summarize([r for r in scores if r["partition"] == part])
            for part in ("dev-validation", "dev-test")
        },
        "timing_scope": "fixture timing is not an actual model or online observation",
        **dict.fromkeys(evaluation.FALSE_CLAIMS, False),
    }
    report_binding = write(output / "report.json", report)
    return SimpleNamespace(
        root=tmp_path,
        output=output,
        samples=samples,
        selected=selected,
        bridge=bridge,
        plan=plan,
        report=report,
        predictions=predictions,
        scores=scores,
        report_binding=report_binding,
        plan_binding=plan_binding,
    )


def run(case):
    return analysis.analyze(case.report_binding["path"], case.report_binding["sha256"])


def save_report(case):
    case.report_binding = write(case.output / "report.json", case.report)


def save_plan(case):
    old = case.plan_binding
    case.plan_binding = write(Path(old["path"]), case.plan)
    case.report["plan_sha256"] = case.plan_binding["sha256"]
    for source in (case.report["sources_before"], case.report["sources_after"]):
        source["repository"] = analysis.inventory(case.plan["source_files"] + [case.plan_binding])
    save_report(case)


def save_rows(case, *, refresh_scores=False):
    if refresh_scores:
        case.scores = []
        for sample, prediction in zip(case.selected, case.predictions, strict=True):
            prediction.update(evaluation.generation_eligibility(prediction["generated"]))
            gold = evaluation.gold_mapping(sample)
            case.scores.append(
                {
                    **prediction,
                    "gold": gold,
                    "score": evaluation.score_prediction(
                        prediction["generated"]["raw_output"], gold, fixtures.inference
                    ),
                }
            )
        case.report["summary"] = evaluation.summarize(case.scores)
        case.report["partition_summaries"] = {
            part: evaluation.summarize([r for r in case.scores if r["partition"] == part])
            for part in ("dev-validation", "dev-test")
        }
    case.report["predictions"] = write(
        case.output / "predictions.jsonl", case.predictions, jsonl=True
    )
    case.report["scores"] = write(case.output / "scores.json", case.scores)
    save_report(case)


def test_closed_source_only59_exposes_majority_baseline_and_failures(closed):
    report = run(closed)
    overall = report["overall"]
    candidate = overall["candidate"]
    reference = overall["mathematical_always_forward"]
    assert candidate["sample_count"] == 59
    assert candidate["raw_action_correct_count"] == 54
    assert candidate["raw_action_accuracy"] == pytest.approx(54 / 59)
    assert candidate["macro_recall_gold_present_classes"] == 0.2
    assert candidate["macro_class_count"] == 5
    assert candidate["by_class"]["stop"]["support"] == 2
    assert candidate["by_class"]["stop"]["recall"] == 0
    assert candidate["by_class"]["up"]["metric_coverage"]["translation_l2_error_m"][
        "mean"
    ] == pytest.approx(18**0.5)
    assert candidate["metric_coverage"]["translation_l2_error_m"]["mean"] == pytest.approx(
        2 * 18**0.5 / 56
    )
    assert candidate["metric_coverage"]["yaw_absolute_error_rad"]["mean"] is None
    assert candidate["metric_coverage"]["noncomparable_count"] == 3
    assert reference["raw_action_correct_count"] == 54
    assert "eligible_count" not in reference
    assert "generation_elapsed_s" not in reference
    assert report["minority_nonforward"]["candidate"]["sample_count"] == 5
    assert report["minority_nonforward"]["candidate"]["raw_joint_correct_count"] == 0
    assert report["by_partition"]["dev-validation"]["candidate"]["sample_count"] == 17
    assert report["by_partition"]["dev-test"]["candidate"]["sample_count"] == 42
    assert report["by_partition"]["dev-validation"]["candidate"]["macro_class_count"] == 2
    assert len(report["stop_rows"]) == 2
    assert (
        report["metric_correct_thresholds"]["scope"]
        == "strict_label_reproduction_not_flight_tolerance"
    )
    assert report["actual_tool_exit_status_independently_verified_by_analysis"] is False
    assert all(report[key] is False for key in evaluation.FALSE_CLAIMS)
    assert report["mathematical_reference"]["token_or_latency_observations_created"] is False
    assert report["mathematical_reference"]["model_executed"] is False


@pytest.mark.parametrize("count,elapsed", [(192, 7), (2, 30.0001), (192, 31)])
@pytest.mark.parametrize("kind", ["forward", "minority"])
def test_raw_correct_budget_failure_never_wins_budget_qualified_pair(closed, count, elapsed, kind):
    index = 0 if kind == "forward" else 17
    gold = evaluation.gold_mapping(closed.selected[index])
    closed.predictions[index]["generated"] = fixtures.generated(json.dumps(gold), count, elapsed)
    save_rows(closed, refresh_scores=True)
    report = run(closed)
    raw = report["overall"]["paired_raw_joint"]
    eligible = report["overall"][
        "paired_budget_qualified_candidate_joint_vs_mathematical_reference"
    ]
    if kind == "forward":
        assert raw["both_correct"] == 54 and raw["candidate_losses"] == 0
        assert eligible["both_correct"] == 53 and eligible["candidate_losses"] == 1
    else:
        assert raw["candidate_wins"] == 1 and raw["both_wrong"] == 4
        assert eligible["candidate_wins"] == 0 and eligible["both_wrong"] == 5
    action = report["overall"]["paired_budget_qualified_candidate_action_vs_mathematical_reference"]
    assert action["candidate_wins"] == eligible["candidate_wins"]
    assert action["candidate_losses"] == eligible["candidate_losses"]
    assert report["overall"]["candidate"]["budget_rejected_count"] == 1
    assert report["overall"]["mathematical_always_forward"]["raw_joint_correct_count"] == 54


def test_null_metrics_and_missing_class_support_are_never_zero_errors(closed):
    closed.predictions[0]["generated"] = fixtures.generated(json.dumps(fixtures.target()))
    save_rows(closed, refresh_scores=True)
    result = run(closed)
    coverage = result["overall"]["candidate"]["metric_coverage"]
    assert coverage["same_kind_unknown_metric_count"] == 1
    assert coverage["noncomparable_count"] == 4
    assert coverage["translation_l2_error_m"]["count"] == 55
    absent = result["by_partition"]["dev-validation"]["candidate"]["by_class"]["up"]
    assert absent["support"] == 0 and absent["recall"] is None
    assert absent["metric_coverage"]["translation_l2_error_m"]["mean"] is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("native_exit_code", False),
        ("native_exit_code", 1),
        ("model_loaded", False),
        ("generation_attempts", 60),
        ("generation_returns", 58),
        ("durable_prediction_count", True),
        ("original_train_rows_entered_model", 23),
        ("source_bindings_unchanged", False),
        ("actual_output_prefix_independently_verified", False),
        ("gold_scored_only_after_all_raw_predictions_durable", False),
        ("ended_at_utc", "2026-10-08T00:01:00+00:00"),
        ("quality_claim", True),
    ],
)
def test_closed_phase_and_authority_gate(closed, key, value):
    closed.report[key] = value
    save_report(closed)
    with pytest.raises(ValueError):
        run(closed)


@pytest.mark.parametrize(
    "mode",
    [
        "source_after",
        "source_missing",
        "source_both",
        "plan_id",
        "plan_train",
        "plan_count",
        "plan_roles",
        "plan_classes",
        "plan_actor",
        "plan_output",
        "plan_claim",
    ],
)
def test_plan_source_inventory_and_fixed59_metadata_gate(closed, mode):
    if mode == "source_after":
        closed.report["sources_after"]["repository"].pop()
    elif mode == "source_missing":
        closed.report["sources_before"]["repository"].pop()
        closed.report["sources_after"] = copy.deepcopy(closed.report["sources_before"])
    elif mode == "source_both":
        closed.report["sources_before"]["repository"][0]["bytes"] += 1
        closed.report["sources_after"] = copy.deepcopy(closed.report["sources_before"])
    else:
        if mode == "plan_id":
            closed.plan["sample_ids"].reverse()
        elif mode == "plan_train":
            closed.plan["sample_ids"][0] = closed.samples[0].sample_id
        elif mode == "plan_count":
            closed.plan["sample_count"] = 60
        elif mode == "plan_roles":
            closed.plan["partitions"] = {"dev-validation": 18, "dev-test": 41}
        elif mode == "plan_classes":
            closed.plan["gold_class_counts"]["forward"] = 53
        elif mode == "plan_actor":
            closed.plan["actor_identity_sha256"][closed.selected[0].sample_id] = "b" * 64
        elif mode == "plan_output":
            closed.plan["output_dir"] = str(closed.output.parent)
        else:
            closed.plan["claims"]["candidate_promoted"] = True
        save_plan(closed)
    save_report(closed)
    with pytest.raises(ValueError):
        run(closed)


@pytest.mark.parametrize(
    "mode",
    [
        "raw",
        "score",
        "gold",
        "prefix",
        "reverse",
        "duplicate",
        "train",
        "short",
        "role",
        "actor",
        "capture_clock",
        "budget",
        "overall_summary",
        "partition_summary",
        "token_bool",
    ],
)
def test_predictions_scores_and_summary_are_independently_rechecked(closed, mode):
    if mode == "raw":
        closed.predictions[0]["generated"]["raw_output"] = "{}"
    elif mode == "score":
        closed.scores[0]["score"]["action_class_correct"] = False
    elif mode == "gold":
        closed.scores[0]["gold"]["translation_m"] = [5, 0, 0]
    elif mode == "prefix":
        closed.scores[0]["prefix"] = {"different": True}
    elif mode == "reverse":
        closed.predictions.reverse()
    elif mode == "duplicate":
        closed.predictions[1] = copy.deepcopy(closed.predictions[0])
    elif mode == "train":
        closed.predictions[0]["sample_id"] = closed.samples[0].sample_id
    elif mode == "short":
        closed.predictions.pop()
    elif mode == "role":
        closed.predictions[0]["partition"] = "train"
    elif mode == "actor":
        closed.predictions[0]["actor_identity_sha256"] = "b" * 64
    elif mode == "capture_clock":
        closed.predictions[0]["online_freshness_exercised"] = True
    elif mode == "budget":
        closed.predictions[0]["bounded_generation_eligible"] = False
    elif mode == "token_bool":
        closed.predictions[0]["generated"]["output_token_ids"] = [True, 1]
    elif mode == "overall_summary":
        closed.report["summary"]["metric_correct_count"] += 1
    else:
        closed.report["partition_summaries"]["dev-test"]["sample_count"] = 41
    save_rows(closed)
    with pytest.raises(ValueError):
        run(closed)


def test_result_mutated_after_scoring_is_not_published(closed, monkeypatch):
    original = analysis.statistics
    changed = False

    def mutate(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            path = closed.output / "predictions.jsonl"
            path.write_bytes(path.read_bytes().replace(b'"elapsed_s":7.0', b'"elapsed_s":8.0', 1))
            changed = True
        return result

    monkeypatch.setattr(analysis, "statistics", mutate)
    with pytest.raises(ValueError, match="Consumed result changed"):
        run(closed)


def test_only_small_consumed_files_are_opened_no_images_weights_wal_or_annotations(
    closed, monkeypatch
):
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path.suffix not in {".png", ".jpg", ".safetensors"}
        assert path.name not in {"train.json", "training-events.jsonl", "fixture.txt"}
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert run(closed)["model_loaded_by_analysis"] is False


def test_cli_missing_report_refuses_before_import_or_writes(closed, monkeypatch, capsys):
    output = closed.root / "artifacts/diagnostics/onboard-navigation-original59-analysis-missing"

    def forbidden():
        pytest.fail("Missing report must not import scoring")

    monkeypatch.setattr(analysis, "load_scoring", forbidden)
    assert (
        analysis.main(
            [
                "--eval-report",
                str(closed.output / "missing/report.json"),
                "--eval-report-sha256",
                "a" * 64,
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()
    assert "original59_analysis_rejected" in capsys.readouterr().out


def test_cli_create_only_and_explicit_report_sha(closed):
    output = closed.root / "artifacts/diagnostics/onboard-navigation-original59-analysis-fixture"
    args = [
        "--eval-report",
        closed.report_binding["path"],
        "--eval-report-sha256",
        closed.report_binding["sha256"],
        "--output-dir",
        str(output),
    ]
    assert analysis.main(args) == 0
    before = (output / "report.json").read_bytes()
    assert analysis.main(args) == 1
    assert (output / "report.json").read_bytes() == before
    with pytest.raises(ValueError, match="Explicit evaluation report SHA"):
        analysis.analyze(closed.report_binding["path"], "0" * 64)
    with pytest.raises(ValueError, match="create-only"):
        analysis.output_target(closed.root / "artifacts/diagnostics/not-the-prefix")


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_strict_json_rejects_duplicates_and_nonfinite(raw):
    with pytest.raises(ValueError):
        analysis.strict_json(raw)


def test_authenticated_scoring_import_is_stdlib_only_in_isolated_interpreter():
    path = Path(analysis.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"s=importlib.util.spec_from_file_location('analysis',{str(path)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.load_scoring(); "
        "assert not m.BLOCKED.intersection(sys.modules); print('stdlib-only authenticated')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "stdlib-only authenticated" in result.stdout


@pytest.mark.parametrize(
    "name", [analysis.EVALUATOR, "src/multidetect/autonomy/onboard_navigation_inference.py"]
)
def test_changed_frozen_scoring_source_is_rejected_before_import(closed, monkeypatch, name):
    path = closed.root / name
    path.write_bytes(path.read_bytes() + b"# source drift\n")

    def forbidden(*args, **kwargs):
        pytest.fail("SHA authentication must precede import")

    monkeypatch.setattr(importlib.util, "spec_from_file_location", forbidden)
    with pytest.raises(ValueError, match="Frozen scoring source differs"):
        analysis.code_bindings()
