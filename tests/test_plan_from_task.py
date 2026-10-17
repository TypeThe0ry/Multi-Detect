import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import plan_from_task  # noqa: E402
from plan_from_task import compute_diagnosis  # noqa: E402

from multidetect.autonomy.mission_plan import validate_mission_plan  # noqa: E402


def _plan(*steps):
    return {
        "plan_version": 1, "goal": "test", "report": [],
        "steps": [
            {"id": f"s{i}", "skill": skill, "params": params,
             "on_success": f"s{i + 1}" if i + 1 < len(steps) else None,
             "on_fail": f"s{len(steps) - 1}" if i + 1 < len(steps) else None}
            for i, (skill, params) in enumerate(steps)
        ],
    }


@pytest.mark.parametrize("steps,refs", [
    ((('goto', {'target_ref': 'poi:missing'}), ('return_home', {})),
     ('poi:gate', 'poi:pad')),
    ((('deliver', {'target_ref': 'poi:gate', 'payload_ref': 'payload:box'}),
      ('return_home', {})), ('poi:gate',)),
])
def test_invalid_task_accepts_host_rejection(steps, refs):
    plan, _, attempts, outcome = plan_from_task.compile_task(
        lambda *args: json.dumps(_plan(*steps)), "invalid request", refs, {},
    )
    assert plan is None
    assert len(attempts) == 2
    assert isinstance(outcome, str)
    assert plan_from_task.score_task({"expect_invalid": True}, plan, outcome)["exact"]


def test_invalid_task_accepts_only_repaired_home_fallback():
    plan, _, _, outcome = plan_from_task.compile_task(
        lambda *args: json.dumps(_plan()), "cannot do this", (), {},
    )
    assert outcome == ["appended return_home"]
    assert plan_from_task.score_task({"expect_invalid": True}, plan, outcome)["exact"]
    assert not plan_from_task.score_task({"expect_invalid": True}, plan, None)["exact"]
    operational = validate_mission_plan(_plan(('hover', {'duration_s': 5}), ('return_home', {})))
    row = plan_from_task.score_task(
        {"expect_invalid": True}, operational, ["appended return_home"],
    )
    assert not row["exact"]
    assert not row["same_skills"]


def test_invalid_task_excluded_from_skill_confusion_but_counted_for_language():
    row = _row([], ['return_home'], exact=True, expect_invalid=True)
    diagnosis = compute_diagnosis([row])
    assert diagnosis['expected_skill_recall'] == {}
    assert diagnosis['substitutions'] == []
    assert diagnosis['language_exact_match_rate'] == {'en': 1.0}


@pytest.fixture
def output_dir():
    path = ROOT / "tmp" / f"plan-eval-diagnosis-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _row(expected, predicted, *, text="Inspect the gate", exact=False, **fields):
    return dict(text=text, expected=expected, predicted=predicted, exact=exact, **fields)


def test_diagnosis_recall_counts_tasks_once_and_handles_missing_plans():
    rows = [
        _row(["inspect", "inspect", "land"], ["inspect", "return_home"]),
        _row(["inspect", "land"], None),
        _row(["land"], ["land"], exact=True),
    ]
    assert compute_diagnosis(rows)["expected_skill_recall"] == {
        "inspect": 0.5,
        "land": 1 / 3,
    }


def test_diagnosis_aligns_replacements_deletions_and_insertions():
    rows = [
        _row(["inspect", "land"], ["monitor", "survey", "return_home"]),
        _row(["inspect", "monitor", "land"], ["survey", "return_home"]),
        _row(["goto", "land"], ["land"]),
        _row(["land"], ["hover", "land"]),
    ]
    substitutions = compute_diagnosis(rows)["substitutions"]
    counts = {
        (item["expected_skill"], item["predicted_skill"]): item["count"]
        for item in substitutions
    }
    assert counts == {
        ("inspect", "monitor"): 1,
        ("land", "survey"): 1,
        (None, "return_home"): 1,
        ("inspect", "survey"): 1,
        ("monitor", "return_home"): 1,
        ("land", None): 1,
        ("goto", None): 1,
        (None, "hover"): 1,
    }


def test_diagnosis_orders_substitutions_by_frequency():
    rows = [
        _row(["inspect"], ["monitor"]),
        _row(["inspect"], ["monitor"]),
        _row(["inspect"], ["survey"]),
    ]
    assert compute_diagnosis(rows)["substitutions"][0] == {
        "expected_skill": "inspect", "predicted_skill": "monitor", "count": 2,
    }


def test_diagnosis_uses_explicit_language_and_existing_exact_score():
    rows = [
        _row(["inspect"], ["inspect"], text="检查大门", exact=True),
        _row(["inspect"], None, text="检查仓库"),
        _row(["inspect"], ["inspect"], exact=True),
        _row(["inspect"], ["survey"], exact=True, language="fr"),
        _row(["inspect"], None, text="检查大门", language="en"),
    ]
    assert compute_diagnosis(rows)["language_exact_match_rate"] == {
        "en": 0.5, "fr": 1.0, "zh": 0.5,
    }


def test_empty_diagnosis():
    assert compute_diagnosis([]) == {
        "expected_skill_recall": {},
        "substitutions": [],
        "language_exact_match_rate": {},
    }


def test_main_writes_diagnosis_and_preserves_alternative_exact_score(
    output_dir, monkeypatch, capsys,
):
    tasks = output_dir / "tasks.json"
    output = output_dir / "result.json"
    tasks.write_text(json.dumps({"tasks": [{
        "text": "Inspect the gate", "skills": ["inspect", "land"],
        "alternatives": [["survey", "land"]], "language": "test",
    }]}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "scripts.eval_slow_system", SimpleNamespace(
        LlamaServerRunner=lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
        _http_json=lambda *args: None,
    ))
    monkeypatch.setattr(plan_from_task, "compile_task", lambda *args, **kwargs: (
        ["survey", "land"], "{}", [0.1], None,
    ))
    monkeypatch.setattr(plan_from_task, "success_path", lambda plan: plan)
    assert plan_from_task.main(["--tasks", str(tasks), "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["summary"]["exact_sequence_rate"] == 1.0
    assert result["diagnosis"]["language_exact_match_rate"] == {"test": 1.0}
    assert result["diagnosis"]["expected_skill_recall"] == {"inspect": 0.0, "land": 1.0}
    assert "inspect -> survey: 1" in capsys.readouterr().out


def test_main_scores_invalid_tasks_and_uses_task_refs(output_dir, monkeypatch):
    tasks = output_dir / "invalid.json"
    output = output_dir / "result.json"
    tasks.write_text(json.dumps({
        "refs": ["poi:gate"], "tasks": [
            {"text": "Deliver a parcel", "expect_invalid": True},
            {"text": "Go somewhere unknown", "expect_invalid": True,
             "refs": ["poi:pad", "poi:tower"]},
        ],
    }), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "scripts.eval_slow_system", SimpleNamespace(
        LlamaServerRunner=lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
        _http_json=lambda *args: None,
    ))
    seen_refs = []

    def compile_task(post, text, refs, schema, **kwargs):
        seen_refs.append(refs)
        return None, "{}", [0.1, 0.1], "rejected"

    monkeypatch.setattr(plan_from_task, "compile_task", compile_task)
    assert plan_from_task.main(["--tasks", str(tasks), "--output", str(output)]) == 0
    assert seen_refs == [("poi:gate",), ("poi:pad", "poi:tower")]
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["summary"]["exact_sequence_rate"] == 1.0
    assert result["summary"]["valid_rate"] == 0.0
    assert all(row["expect_invalid"] and row["exact"] for row in result["rows"])
    assert result["diagnosis"]["substitutions"] == []


def test_main_variant_cli_collects_all_retry_usage(output_dir, monkeypatch):
    tasks = output_dir / "tasks.json"
    output = output_dir / "result.json"
    tasks.write_text(json.dumps({"tasks": [
        {"text": "Return home.", "skills": ["return_home"]},
        {"text": "Come back.", "skills": ["return_home"]},
    ]}), encoding="utf-8")
    plan = json.dumps({
        "plan_version": 1, "goal": "return", "report": [],
        "steps": [{"id": "s1", "skill": "return_home", "params": {},
                   "on_success": None, "on_fail": None}],
    })
    responses = iter([
        {"choices": [{"message": {"content": "invalid json"}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 3}},
        {"choices": [{"message": {"content": plan}}],
         "usage": {"prompt_tokens": 120, "completion_tokens": 22}},
        {"choices": [{"message": {"content": plan}}]},
    ])
    requests, runners, closed = [], [], []

    def http_json(url, body):
        requests.append(body)
        return next(responses)

    def runner(*args, **kwargs):
        runners.append(kwargs)
        return SimpleNamespace(close=lambda: closed.append(True))

    monkeypatch.setitem(sys.modules, "scripts.eval_slow_system", SimpleNamespace(
        LlamaServerRunner=runner, _http_json=http_json,
    ))
    assert plan_from_task.main([
        "--tasks", str(tasks), "--output", str(output), "--prompt-variant", "compact",
        "--llama-server", str(output_dir / "llama-server"), "--device", "cpu",
        "--no-prompt-cache",
    ]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    first, second = result["rows"]
    assert len(first["request_usage"]) == 2
    assert first["prompt_tokens"] == 100
    assert first["prompt_tokens_total"] == 220
    assert first["completion_tokens_total"] == 25
    assert second["prompt_tokens"] is None and second["prompt_tokens_total"] is None
    assert second["completion_tokens_total"] is None
    assert result["summary"]["mean_prompt_tokens"] == 100
    assert result["summary"]["mean_prompt_tokens_total"] == 220
    assert result["summary"]["mean_completion_tokens_total"] == 25
    assert result["summary"]["tasks_with_prompt_usage"] == 1
    assert result["summary"]["prompt_cache"] is False
    assert all(request["cache_prompt"] is False for request in requests)
    assert all(request["messages"][0]["content"].count("Task:") == 1 for request in requests)
    assert "The previous plan was rejected:" in requests[1]["messages"][0]["content"]
    assert runners[0]["device"] == "cpu"
    assert runners[0]["executable"] == output_dir / "llama-server"
    assert closed == [True]


@pytest.mark.parametrize(("extra", "expected_mode", "expected_mmproj"), [
    ([], "configured", "projector.gguf"),
    (["--no-mmproj"], "none", None),
])
def test_main_mmproj_mode_is_explicit(output_dir, monkeypatch, extra, expected_mode, expected_mmproj):
    tasks = output_dir / "tasks.json"
    output = output_dir / "result.json"
    tasks.write_text(json.dumps({"tasks": [{"text": "Return home", "skills": ["return_home"]}]}), encoding="utf-8")
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setitem(sys.modules, "scripts.eval_slow_system", SimpleNamespace(
        LlamaServerRunner=runner, _http_json=lambda *args, **kwargs: None,
    ))
    monkeypatch.setattr(plan_from_task, "compile_task", lambda *args, **kwargs: (
        _plan(("return_home", {})), "{}", [0.01], None,
    ))
    monkeypatch.setattr(plan_from_task, "success_path", lambda plan: ["return_home"])
    argv = ["--tasks", str(tasks), "--output", str(output), "--mmproj", "projector.gguf", *extra]
    assert plan_from_task.main(argv) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["summary"]["mmproj_mode"] == expected_mode
    assert result["rows"][0]["mmproj_mode"] == expected_mode
    assert calls[0][1] == (Path(expected_mmproj) if expected_mmproj is not None else None)


def test_usage_totals_are_unknown_if_any_retry_usage_is_missing():
    assert plan_from_task._usage_total([], "prompt_tokens") is None
    assert plan_from_task._usage_total(
        [{"prompt_tokens": 100}, {"prompt_tokens": None}], "prompt_tokens"
    ) is None
