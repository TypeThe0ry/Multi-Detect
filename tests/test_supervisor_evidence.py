from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.score_onboard_loop import _supervisor_evidence, score_episode


def _episode(tmp_path: Path, records: list[dict]) -> Path:
    episode = tmp_path / "episodes" / "flight-001"
    episode.mkdir(parents=True)
    log = tmp_path / "flight-001-onboard-frames" / "mission_log" / "mission.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("".join(
        json.dumps({"mission_id": "flight", "sequence": i, **r}) + "\n"
        for i, r in enumerate(records, 1)
    ), encoding="utf-8")
    return episode


def test_durable_abort_evidence_requires_hover_then_actual_return(tmp_path: Path) -> None:
    episode = _episode(tmp_path, [
        {"kind": "decision", "action": "continue", "reason": "autonomous"},
        {"kind": "escalation", "action": "return_now", "reason": "explicit operator abort"},
        {"kind": "ground_event", "event_type": "abort"},
        {"kind": "decision", "action": "return_now", "reason": "explicit operator abort"},
        {"kind": "execution", "action": {"kind": "hold"},
         "state": {"supervisor_action": "return_now"}},
        {"kind": "execution", "action": {"kind": "translate"},
         "state": {"supervisor_action": "return_now", "skill": "return_home"}},
        {"kind": "final_summary", "summary": {
            "action": "return_now", "reason": "explicit operator abort", "history": [],
            "recovery_history": [{"skill": "return_home", "state": "succeeded"}],
        }},
    ])
    evidence = _supervisor_evidence(episode)
    assert evidence == {
        "final_decision": "return_now", "reason": "explicit operator abort",
        "return_home_started": True, "onboard_log_exists": True,
        "logged_decisions": ["continue", "return_now"],
        "logged_reasons": ["autonomous", "explicit operator abort"],
        "logged_ground_events": ["abort"], "original_mission_completed": False,
        "ground_event_count": 1,
        "abort_hover_before_return": True,
        "mission_plan_skills": [],
    }


def test_return_decision_alone_does_not_prove_return_started(tmp_path: Path) -> None:
    episode = _episode(tmp_path, [
        {"kind": "decision", "action": "return_now", "reason": "reserve"},
        {"kind": "execution", "action": {"kind": "hold"},
         "state": {"supervisor_action": "return_now"}},
    ])
    evidence = _supervisor_evidence(episode)
    assert not evidence["return_home_started"]
    assert not evidence["abort_hover_before_return"]
    assert evidence["final_decision"] is None


def test_missing_log_cannot_pass_durable_decision_expectations(tmp_path: Path) -> None:
    evidence = _supervisor_evidence(tmp_path / "episodes" / "flight")
    assert not evidence["onboard_log_exists"]
    assert evidence["logged_decisions"] == []
    assert evidence["final_decision"] is None


def test_package_plan_and_completion_are_scored_from_durable_log(tmp_path: Path) -> None:
    episode = _episode(tmp_path, [
        {"kind": "mission_started", "plan": {"steps": [
            {"id": "count", "skill": "count"}, {"id": "home", "skill": "return_home"},
        ]}},
        {"kind": "final_summary", "summary": {
            "action": "continue", "reason": "autonomous", "recovery_history": [],
            "history": [{"step": "home", "skill": "return_home", "state": "succeeded"}],
        }},
    ])
    (episode / "steps.jsonl").write_text("", encoding="utf-8")
    (episode / "episode.json").write_text('{"status": "complete"}', encoding="utf-8")
    truth = {"targets": [{"true_target": True, "pose": {"y_north_m": 0, "x_east_m": 0}}]}
    scored = score_episode(episode, truth, mission_config={"mission_package": {}})
    assert scored["mission_plan_skills"] == ["count", "return_home"]
    assert scored["mission_completed"]


def test_corrupt_log_does_not_supply_decision_evidence(tmp_path: Path) -> None:
    episode = _episode(tmp_path, [{"kind": "decision", "action": "continue", "sequence": 7}])
    evidence = _supervisor_evidence(episode)
    assert evidence["onboard_log_exists"]
    assert evidence["logged_decisions"] == []


@pytest.mark.parametrize("malformed", [
    {"kind": "execution", "state": [], "action": {"kind": "hold"}},
    {"kind": "mission_started", "plan": {"steps": ["invalid"]}},
    {"kind": "final_summary", "summary": {"history": [42]}},
])
def test_malformed_nested_log_fails_closed(tmp_path: Path, malformed: dict) -> None:
    episode = _episode(tmp_path, [
        {"kind": "decision", "action": "return_now", "reason": "abort"}, malformed,
    ])
    evidence = _supervisor_evidence(episode)
    assert evidence["onboard_log_exists"]
    assert evidence["logged_decisions"] == []
    assert evidence["final_decision"] is None


def test_v1_scores_unchanged_while_package_scores_include_missing_log(tmp_path: Path) -> None:
    episode = tmp_path / "episodes" / "flight"
    episode.mkdir(parents=True)
    (episode / "steps.jsonl").write_text("", encoding="utf-8")
    (episode / "episode.json").write_text('{"status": "complete"}', encoding="utf-8")
    truth = {"targets": [{"true_target": True, "pose": {"y_north_m": 0, "x_east_m": 0}}]}
    original = score_episode(episode, truth)
    package = score_episode(episode, truth, mission_config={"mission_package": {}})
    assert "mission_supervisor" not in original
    assert package.pop("mission_supervisor")["onboard_log_exists"] is False
    assert package == original
