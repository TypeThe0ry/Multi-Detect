from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.evaluate_shared_policy_external_repeats import _metric, evaluate
from scripts.make_task_order_campaign import make_variant

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("section, field, invariant", [
    ("scenario", "manifest_sha256", "same_scenario_manifest"),
    ("runtime_identity", "airframe_profile_sha256", "same_stable_airframe_profile"),
    ("campaign_input", "sha256", "same_campaign_identity"),
])
@pytest.mark.parametrize("value", [None, "", "g" * 64, "a" * 63, 1, []])
def test_missing_or_invalid_identity_is_not_equal_evidence(
    tmp_path: Path, section, field, invariant, value
) -> None:
    config = json.loads(
        (ROOT / "configs/evaluation/shared-policy-external-repeat-v1.json").read_text()
    )
    paths = []
    for index in range(2):
        path = tmp_path / f"run-{index}.json"
        path.write_text(json.dumps({
            "run_id": index, section: {field: value},
            "campaign": {"verified_sorties": [{"task_id": "task"}]},
        }), encoding="utf-8")
        paths.append(path)
    result = evaluate(config=config, parent_paths=paths[:1], candidate_paths=paths[1:])
    assert result["invariants"][invariant] is False
    assert result["promotion_ready"] is False


@pytest.mark.parametrize("sorties", [[], [{}], [None], [{"task_id": " "}],
    [{"task_id": "a"}, {"task_id": "a"}], [{"task_id": "a"}, {}]])
def test_invalid_task_records_are_not_completed_tasks(tmp_path: Path, sorties) -> None:
    config = json.loads(
        (ROOT / "configs/evaluation/shared-policy-external-repeat-v2-sim-energy.json").read_text()
    )
    config["primary_metric"]["path"] = "campaign.verified_sorties"
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"campaign": {"verified_sorties": sorties}}), encoding="utf-8")
    result = evaluate(config=config, parent_paths=[path], candidate_paths=[path])
    assert result["metric_ready"] is False
    assert result["invariants"]["same_task_set_for_metric"] is False
    assert result["invariants"]["same_task_order_for_metric"] is False
    assert result["promotion_ready"] is False


@pytest.mark.parametrize("values, expected", [
    ([], None),
    ([float("nan")], None),
    ([float("inf")], None),
    ([float("-inf")], None),
    ([-0.1], None),
    ([True], None),
    (["0.1"], None),
    ([10 ** 400], None),
    ([1e308, 1e308], None),
    ([0.0], 0.0),
    ([1.25, 2.5], 3.75),
])
def test_energy_metric_requires_nonempty_finite_nonnegative_values(
    tmp_path: Path, values, expected
) -> None:
    completed = []
    for index, value in enumerate(values):
        path = tmp_path / f"sortie-{index}.json"
        path.write_text(json.dumps({
            "planning_energy": {"physical_calibration_claimed": False},
            "mission_receive_window": {
                "full_mission_energy_claimed": True,
                "window_energy_wh": value,
            },
        }), encoding="utf-8")
        completed.append({"evidence_path": str(path)})
    mission = tmp_path / "mission.json"
    mission.write_text(json.dumps({"completed": completed}), encoding="utf-8")
    assert _metric(
        {"paths": {"mission_evidence": str(mission)}},
        {"path": "mission_completed.planning_energy.newly_consumed_wh"},
    ) == expected


@pytest.mark.parametrize("duplicate", [None, "path", "copy", "cross_group"])
def test_repeat_gate_requires_distinct_report_contents(tmp_path: Path, duplicate) -> None:
    config = {
        "evaluation_id": "repeat-integrity-fixture",
        "product_version": "0.3.0-pre.8",
        "runtime_mode": "SIM",
        "minimum_parent_runs": 3,
        "minimum_candidate_runs": 3,
        # Uniqueness must hold even if configurable invariants are disabled.
        "required_invariants": [],
        "primary_metric": {
            "path": "campaign.verified_sorties",
            "direction": "maximize",
            "minimum_candidate_margin": 1,
        },
    }
    groups = []
    for group, count in (("parent", 1), ("candidate", 3)):
        paths = []
        for index in range(3):
            path = tmp_path / f"{group}-{index}.json"
            path.write_text(json.dumps({
                "run_id": f"{group}-{index}",
                "campaign": {"verified_sorties": [{"task_id": str(i)} for i in range(count)]},
            }), encoding="utf-8")
            paths.append(path)
        groups.append(paths)
    parent, candidate = groups
    if duplicate == "path":
        parent[1] = parent[0]
    elif duplicate == "copy":
        parent[1].write_bytes(parent[0].read_bytes())
    elif duplicate == "cross_group":
        candidate[0].write_bytes(parent[0].read_bytes())
    result = evaluate(config=config, parent_paths=parent, candidate_paths=candidate)
    assert result["primary_metric"]["gain_proven"] is True
    assert result["distinct_run_reports"] is (duplicate is None)
    assert result["minimum_run_counts_met"] is (duplicate is None)
    assert result["promotion_ready"] is (duplicate is None)
    assert result["candidate_promoted"] is False
    assert result["final_release_allowed"] is False


def test_current_two_runs_stay_fail_closed() -> None:
    config = json.loads(
        (ROOT / "configs/evaluation/shared-policy-external-repeat-v1.json").read_text()
    )
    result = evaluate(
        config=config,
        parent_paths=[ROOT / "artifacts/evaluation/vtol-two-task-parent-run-20260919-g.json"],
        candidate_paths=[ROOT / "artifacts/evaluation/vtol-two-task-candidate-run-20260919-c.json"],
    )
    assert result["invariants_met"] is True
    assert result["minimum_run_counts_met"] is False
    assert result["metric_ready"] is True
    assert result["independent_candidate_gain_proven"] is False
    assert result["candidate_promoted"] is False
    assert result["invariants"]["same_task_order_for_metric"] is False
    assert result["invariants"]["same_task_set_for_metric"] is True
    assert result["promotion_ready"] is False


def test_missing_metric_is_not_promotion_evidence(tmp_path: Path) -> None:
    config = json.loads(
        (ROOT / "configs/evaluation/shared-policy-external-repeat-v1.json").read_text()
    )
    source = json.loads(
        (ROOT / "artifacts/evaluation/vtol-two-task-parent-run-20260919-g.json").read_text()
    )
    source["campaign"].pop("verified_sorties")
    path = tmp_path / "missing-metric.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    result = evaluate(config=config, parent_paths=[path], candidate_paths=[path])
    assert result["metric_ready"] is False
    assert result["independent_candidate_gain_proven"] is False
    assert result["candidate_promoted"] is False
    assert result["promotion_ready"] is False


def test_sim_energy_metric_is_recomputed_from_bound_mission_evidence() -> None:
    config = json.loads(
        (ROOT / "configs/evaluation/shared-policy-external-repeat-v2-sim-energy.json").read_text()
    )
    result = evaluate(
        config=config,
        parent_paths=[ROOT / "artifacts/evaluation/vtol-two-task-parent-run-20260919-g.json"],
        candidate_paths=[ROOT / "artifacts/evaluation/vtol-two-task-candidate-run-20260919-c.json"],
    )
    assert result["primary_metric"]["parent_values"][0] is None
    assert result["primary_metric"]["candidate_values"][0] is None
    assert result["metric_ready"] is False
    assert result["primary_metric"]["validity"] == (
        "post_mission_receive_window_model_only_not_physical_calibration"
    )
    assert result["invariants"]["same_task_order_for_metric"] is False
    assert result["invariants"]["same_task_set_for_metric"] is True
    assert result["invariants_met"] is True
    assert result["candidate_promoted"] is False


def test_order_variant_preserves_ranker_and_changes_only_priorities() -> None:
    source = json.loads(
        (ROOT / "artifacts/evaluation/vtol-two-task-parent-campaign-pre.8.json").read_text()
    )
    variant = make_variant(source, first_task_id="synthetic-high")
    assert variant["ranker"] == source["ranker"]
    assert [item["task"] for item in variant["tasks"]] == [
        item["task"] for item in source["tasks"]
    ]
    priorities = {item["task"]["task_id"]: item["priority"] for item in variant["tasks"]}
    assert priorities == {"synthetic-high": 1, "synthetic-low": 10}
