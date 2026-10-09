from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.export_onboard_sortie_transitions import (
    SortieTransitionExportError,
    build_proposal,
)


def _hash(letter: str) -> str:
    return letter * 64


def _acceptance() -> dict:
    binding = {
        "action": "select",
        "context_sha256": _hash("a"),
        "plan_sha256": _hash("b"),
        "wire_hash": _hash("c"),
        "executable_mission_sha256": _hash("d"),
        "policy_artifact_sha256": _hash("e"),
        "ranker_sha256": _hash("f"),
        "airframe_profile_hash": _hash("1"),
        "mission_bound": True,
        "planner_revalidated": True,
        "policy_applied_to_actuator": False,
        "truth_free": True,
    }
    decision = {
        "contract": "multidetect.onboard-decision",
        "decision_source": "model",
        "response": "replan",
        "selected_task_id": "task-1",
        "mission_bound": True,
        "planner_revalidated": True,
        "policy_applied_to_actuator": False,
        "truth_free": True,
        "policy_runtime_binding": binding,
    }
    return {
        "schema": "multidetect.external-onboard-acceptance",
        "scenario": {"scenario_id": "river_rescue_vtol_001"},
        "probe": {
            "onboard_model": {
                "bridge": {
                    "contract": "multidetect.onboard-realtime-model-bridge",
                    "truth_free": True,
                    "policy_applied_to_actuator": False,
                    "observations": [
                        {
                            "contract": "multidetect.onboard-perception-observation",
                            "observation_id": "frame-1",
                            "truth_free": True,
                        },
                        {
                            "contract": "multidetect.onboard-perception-observation",
                            "observation_id": "frame-2",
                            "truth_free": True,
                        },
                    ],
                    "revisions": [
                        {
                            "contract": "multidetect.onboard-mission-revision",
                            "sequence": 1,
                            "event": {"event_id": "frame-1"},
                            "mission_sha256": _hash("d"),
                            "trigger": "target_moved",
                            "decision": decision,
                            "truth_free": True,
                        },
                        {
                            "contract": "multidetect.onboard-mission-revision",
                            "sequence": 2,
                            "event": {"event_id": "frame-2"},
                            "decision": {"response": "replan"},
                            "truth_free": True,
                        },
                    ],
                }
            }
        },
    }


def _staging() -> dict:
    return {
        "schema": "multidetect.onboard-trajectory-staging",
        "schema_version": 1,
        "contract": "multidetect.onboard-trajectory-staging-v1",
        "training_ready": False,
        "review_required": True,
        "truth_free": True,
        "truth_injected": False,
        "policy_applied_to_actuator": False,
        "episodes": [
            {
                "episode_id": "episode-1",
                "transitions": [
                    {
                        "transition_id": "transition-1",
                        "frame_id": "frame-1",
                        "observed_at_s": 1.0,
                        "features": [0.0] * 40,
                    },
                    {
                        "transition_id": "transition-2",
                        "frame_id": "frame-2",
                        "observed_at_s": 2.0,
                        "features": [0.0] * 40,
                    },
                ],
            }
        ],
    }


def test_export_binds_only_complete_real_bridge_decisions(tmp_path: Path) -> None:
    acceptance_path = tmp_path / "acceptance.json"
    staging_path = tmp_path / "staging.json"
    acceptance_path.write_text(json.dumps(_acceptance()), encoding="utf-8")
    staging_path.write_text(json.dumps(_staging()), encoding="utf-8")

    result = build_proposal(
        _acceptance(),
        _staging(),
        acceptance_path=acceptance_path,
        staging_path=staging_path,
    )

    assert result["training_ready"] is False
    assert result["review_required"] is True
    assert result["summary"] == {
        "staging_transition_count": 2,
        "bound_transition_count": 1,
        "pending_transition_count": 1,
        "all_bound_decisions_complete": False,
        "synthetic_labels_created": False,
    }
    bound, pending = result["rows"]
    assert bound["status"] == "bound"
    assert bound["acceptance_binding"]["hashes"] == {
        "context_sha256": _hash("a"),
        "plan_sha256": _hash("b"),
        "wire_hash": _hash("c"),
        "executable_mission_sha256": _hash("d"),
        "policy_artifact_sha256": _hash("e"),
        "ranker_sha256": _hash("f"),
        "airframe_profile_hash": _hash("1"),
    }
    assert pending["status"] == "pending"
    assert pending["acceptance_binding"]["decision_complete"] is False
    assert "review_receipt" not in result
    assert "reviewer" not in json.dumps(result)


def test_export_rejects_truth_and_red_green_fixture_markers(tmp_path: Path) -> None:
    acceptance_path = tmp_path / "acceptance.json"
    staging_path = tmp_path / "staging.json"
    acceptance_path.write_text(json.dumps(_acceptance()), encoding="utf-8")
    staging_path.write_text(json.dumps(_staging()), encoding="utf-8")

    forbidden = _staging()
    forbidden["source_manifest"] = "truth.json"
    with pytest.raises(SortieTransitionExportError, match="truth|fixture"):
        build_proposal(
            _acceptance(), forbidden, acceptance_path=acceptance_path, staging_path=staging_path
        )

    forbidden_acceptance = _acceptance()
    forbidden_acceptance["scenario"]["scenario_id"] = "gazebo_red_green_search_001"
    with pytest.raises(SortieTransitionExportError, match="truth|fixture"):
        build_proposal(
            forbidden_acceptance,
            _staging(),
            acceptance_path=acceptance_path,
            staging_path=staging_path,
        )


def test_export_rejects_ready_or_truth_injected_staging(tmp_path: Path) -> None:
    acceptance_path = tmp_path / "acceptance.json"
    staging_path = tmp_path / "staging.json"
    acceptance_path.write_text(json.dumps(_acceptance()), encoding="utf-8")
    staging_path.write_text(json.dumps(_staging()), encoding="utf-8")
    staging = _staging()
    staging["training_ready"] = True
    with pytest.raises(SortieTransitionExportError, match="training_ready"):
        build_proposal(
            _acceptance(), staging, acceptance_path=acceptance_path, staging_path=staging_path
        )


def test_export_normalizes_external_rgb_transport_prefix(tmp_path: Path) -> None:
    acceptance = _acceptance()
    staging = _staging()
    frame_id = "gazebo-sim-000001"
    source_hash = _hash("e")
    acceptance["probe"]["onboard_model"]["bridge"]["observations"][1].update(
        {
            "observation_id": f"gazebo-rgb-{frame_id}",
            "source_frame_sha256": source_hash,
        }
    )
    acceptance["probe"]["onboard_model"]["bridge"]["revisions"][1]["event"][
        "event_id"
    ] = f"gazebo-rgb-{frame_id}"
    acceptance["probe"]["onboard_model"]["bridge"]["revisions"][1]["decision"] = copy.deepcopy(
        acceptance["probe"]["onboard_model"]["bridge"]["revisions"][0]["decision"]
    )
    staging["episodes"][0]["transitions"][1]["frame_id"] = frame_id
    staging["episodes"][0]["transitions"][1]["source"] = {
        "rgb_payload_sha256": source_hash,
    }
    acceptance_path = tmp_path / "acceptance.json"
    staging_path = tmp_path / "staging.json"
    acceptance_path.write_text(json.dumps(acceptance), encoding="utf-8")
    staging_path.write_text(json.dumps(staging), encoding="utf-8")

    result = build_proposal(
        acceptance, staging, acceptance_path=acceptance_path, staging_path=staging_path
    )

    assert result["summary"]["bound_transition_count"] == 2
    assert result["rows"][1]["acceptance_binding"]["observation_present"] is True
