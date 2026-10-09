from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_shared_policy_observed_session import (
    DEFAULT_ARTIFACT,
    DEFAULT_CAMPAIGN,
    DEFAULT_PARITY,
    run_evidence,
)


def test_observed_policy_session_replans_and_repeats_deterministically() -> None:
    evidence = run_evidence(DEFAULT_CAMPAIGN, DEFAULT_ARTIFACT)

    assert evidence["all_tasks_completed"] is True
    assert evidence["deterministic_repeat_passed"] is True
    assert evidence["truth_free"] is True
    assert evidence["backend_started"] is False
    assert evidence["mavlink_sent"] is False
    assert evidence["actuator_capability_granted"] is False
    assert evidence["session"]["selected_task_ids"] == ["synthetic-west", "synthetic-east"]
    assert evidence["session"]["remaining_task_ids"] == []
    assert evidence["session"]["step_count"] == 2
    assert evidence["planner_revalidated"] is True
    assert all(
        step["policy_artifact_sha256"] == evidence["policy_artifact_canonical_sha256"]
        for step in evidence["session"]["steps"]
    )
    assert all(
        step["planner_revalidated"] is True for step in evidence["session"]["steps"]
    )
    assert all(step["outcome"]["status"] == "completed" for step in evidence["session"]["steps"])
    assert evidence["session_sha256"] == evidence["repeat_session_sha256"]
    assert evidence["release_parity"]["path"] == "configs/release-parity.json"
    assert evidence["release_parity"]["bytes"] == DEFAULT_PARITY.stat().st_size
    assert len(evidence["release_parity"]["file_sha256"]) == 64
    assert set(evidence["release_parity"]["hashes"]) == {
        "core_algorithm_hash",
        "px4_mission_path_hash",
        "safety_policy_hash",
        "task_contract_hash",
        "ui_contract_hash",
    }


def test_observed_policy_session_rejects_unbound_artifact(tmp_path: Path) -> None:
    bad_artifact = tmp_path / "bad-policy-artifact.json"
    bad_artifact.write_text(
        DEFAULT_ARTIFACT.read_text(encoding="utf-8").replace(
            '"ranker_sha256": "a63c2ce429148d5786812325f5a65b6f8f649f5d61c6adb39fbb5791ef26d428"',
            '"ranker_sha256": "' + "0" * 64 + '"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ranker hash does not match"):
        run_evidence(DEFAULT_CAMPAIGN, bad_artifact)
