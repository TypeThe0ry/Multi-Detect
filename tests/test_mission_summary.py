"""Recorded report snapshots and offline summary integration checks."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from onboard_factories import make_policy

from multidetect.autonomy.mission_executor import plan_for_count_task, plan_for_find_task
from multidetect.autonomy.mission_plan import MissionPlan, MissionReport, MissionStep
from multidetect.autonomy.mission_summary import summarize_mission, summary_json

FIXTURES = Path(__file__).parent / "fixtures" / "mission_summary"


def recorded_summary(name):
    fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    # Legacy recordings retain skills/outcomes but not parameters or graph edges.
    # The projection uses only recorded metadata, never a reconstructed flight plan.
    plan = MissionPlan(
        1,
        fixture["task"],
        tuple(
            MissionStep(h["step"], h["skill"], {}, None, None) for h in fixture["report"]["history"]
        ),
        (),
    )
    return summarize_mission(fixture["report"], plan, fixture["task"])


@pytest.mark.parametrize("name", ["count-cones", "monitor-backpacks", "inspect-postbox"])
def test_recorded_snapshots(name):
    summary, zh, en = recorded_summary(name)
    expected = json.loads((FIXTURES / f"{name}.snapshot.json").read_text(encoding="utf-8"))
    assert {"summary": summary, "zh": zh, "en": en} == expected
    assert len(summary_json(summary).encode("utf-8")) <= 2048


def test_structured_findings_rounding_and_no_mutation():
    report = {
        "history": [{"step": "count", "state": "succeeded", "reason": "budget_reached"}],
        "counts": {"count": 2},
        "findings": [
            {
                "step": "count",
                "query": "cone",
                "position_ne": [1.26, -2.34],
                "frame_id": "frame_7.png",
            }
        ],
        "evidence": ["unrelated-inspection.png", "frame_7.png"],
    }
    original = copy.deepcopy(report)
    summary, zh, en = summarize_mission(report, plan_for_count_task("cone"), "数交通锥")
    assert report == original
    assert summary["findings"][0]["position_ne"] == [1.3, -2.3]
    assert summary["findings"][0]["frame_id"] == "frame_7.png"
    assert "至少 2" in zh and "at least 2" in en
    assert "已返航" not in zh
    assert summary["steps"][1]["state"] == "unknown"


def test_bare_report_never_guesses_evidence_or_completion():
    report = MissionReport(
        findings=("postbox",),
        positions=("nan,3",),
        evidence_frame_ids=("search.png", "inspect.png"),
    )
    summary, zh, _ = summarize_mission(report, plan_for_find_task("postbox"), "检查邮筒")
    assert summary["findings"][0]["frame_id"] is None
    assert summary["findings"][0]["position_ne"] is None
    assert all(s["state"] == "unknown" for s in summary["steps"])
    assert "已返航" not in zh


def test_size_budget_is_utf8_and_explicit_about_omissions():
    report = {
        "findings": [{"query": "锥" * 5000, "position_ne": [2, 3], "frame_id": "x" * 5000}] * 100,
        "alerts": ["警" * 5000] * 20,
        "answers": ["答" * 5000] * 20,
        "evidence": ["e" * 5000] * 100,
    }
    summary, zh, en = summarize_mission(report, plan_for_find_task("cone"), "任务" * 5000)
    assert len(summary_json(summary).encode("utf-8")) <= 2048
    assert summary["omitted"]["findings"] == 100
    assert summary["omitted"]["task_chars"] > 0
    assert len(zh) < 4000 and len(en) < 4000
    assert "excerpt; see report" in zh and "more in report" in en
    assert set(summary) == {
        "task",
        "skills",
        "steps",
        "counts",
        "findings",
        "alerts",
        "answers",
        "coverage",
        "evidence",
        "not_done",
        "omitted",
    }


def test_partial_coverage_and_lost_target_are_not_hidden():
    plan = MissionPlan(
        1,
        "test",
        (
            MissionStep("survey", "survey", {}, None, None),
            MissionStep("follow", "follow", {}, None, None),
        ),
        (),
    )
    report = {
        "history": [
            {"step": "survey", "state": "succeeded", "reason": "region_covered"},
            {"step": "follow", "state": "succeeded", "reason": "target_lost_timeout"},
        ],
        "answers": ["survey:coverage:0.50"],
    }
    summary, zh, _ = summarize_mission(report, plan, "测试")
    assert summary["coverage"] == {"survey": 0.5}
    assert "未完整覆盖（0.5）" in zh
    assert "目标丢失超时" in zh


def test_landing_commanded_unconfirmed_is_not_reported_as_plain_success():
    plan = MissionPlan(
        1, "land", (MissionStep("land", "land", {}, None, None),), (),
    )
    report = {"history": [{
        "step": "land", "skill": "land", "state": "succeeded",
        "reason": "landing_commanded_unconfirmed",
    }]}
    summary, zh, en = summarize_mission(report, plan, "land")
    assert summary["status"] == "landing_commanded_unconfirmed"
    assert "landing commanded but not confirmed" in en
    assert "已发出降落指令，但尚未确认落地" in zh
    assert summary["steps"][0]["state"] == "succeeded"


def _policy(tmp_path):
    plan = plan_for_find_task("cone")
    executor = SimpleNamespace(
        plan=plan,
        history=[],
        findings=[],
        counts={},
        coverage={},
        report=MissionReport,
    )
    policy = make_policy(work_dir=tmp_path / "episode-0001-onboard-frames")
    policy.work_dir.rmdir()
    policy.executor = executor
    policy.task_text = "找交通锥"
    policy.slow = SimpleNamespace(close=lambda: None)
    return policy


def test_close_writes_beside_labels_even_when_interrupted(tmp_path):
    policy = _policy(tmp_path)
    directory = tmp_path / "episodes" / "episode-0001"
    directory.mkdir(parents=True)
    labels = directory / "steps.jsonl"
    labels.write_text("preserve labels", encoding="utf-8")
    policy.close()
    summary = json.loads((directory / "mission_summary.json").read_text(encoding="utf-8"))
    assert all(step["state"] == "unknown" for step in summary["steps"])
    text = (directory / "mission_summary.txt").read_text(encoding="utf-8")
    assert "任务：" in text and "Task:" in text
    assert "已返航" not in text
    assert labels.read_text(encoding="utf-8") == "preserve labels"
    policy.close()
    assert json.loads((directory / "mission_summary.json").read_text(encoding="utf-8")) == summary


def test_close_preserves_nonmission_behavior(tmp_path):
    policy = _policy(tmp_path)
    policy.executor = None
    closed = []
    policy.slow.close = lambda: closed.append(True)
    policy.close()
    assert closed == [True]
    assert list(tmp_path.iterdir()) == []


def test_summary_io_failure_does_not_break_cleanup(tmp_path):
    policy = _policy(tmp_path)
    (tmp_path / "episodes").write_text("blocked", encoding="utf-8")
    with pytest.warns(UserWarning, match="Could not write mission summary"):
        policy.close()
