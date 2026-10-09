import json
from dataclasses import replace
from itertools import permutations
from pathlib import Path

import pytest

from multidetect.autonomy.task_selection import TaskCandidate
from multidetect.simulation.task_episode import simulate_task_budget_episode
from scripts.benchmark_task_sequences import benchmark, exact_reference, main
from scripts.train_task_ranker import load_dataset

DATASET = Path(__file__).resolve().parents[1] / "configs/training/task-ranker-budget-v1.json"


def first_case():
    groups, _, _ = load_dataset(DATASET)
    return groups["train"][0]


def test_exact_matches_independent_permutation_enumeration():
    case = first_case()
    priorities = {c.task.task_id: c.priority for c in case.candidates}
    scores = [(0, 0, 0.0)]
    # Independent traversal: force each full permutation with unique priorities
    # and let the original whole-episode simulator skip infeasible tasks.
    for order in permutations(case.candidates):
        forced = tuple(TaskCandidate(c.task, len(order) - i) for i, c in enumerate(order))
        episode = simulate_task_budget_episode(
            forced,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
        )
        scores.append(
            (
                sum(1 + priorities[s.compilation.estimate.task_id] for s in episode.steps),
                len(episode.steps),
                -sum(s.compilation.estimate.estimated_duration_s for s in episode.steps),
            )
        )
    result = exact_reference(case)
    expected = max(scores)
    assert (result["reward"], result["completed_count"]) == expected[:2]
    assert -result["estimated_elapsed_s"] == pytest.approx(expected[2])
    assert result["nodes"] == result["compiled_edges"] + 1
    again = exact_reference(case)
    assert {k: v for k, v in result.items() if k != "search_wall_s"} == {
        k: v for k, v in again.items() if k != "search_wall_s"
    }


def test_reference_includes_greedy_as_feasible_competitor():
    groups, _, _ = load_dataset(DATASET)
    for cases in groups.values():
        for case in cases:
            result = benchmark(case)
            assert result["reward_gap"] >= 0
            assert result["same_time_reward_gap"] >= 0
            assert result["reference"]["exhaustive"]
            assert (
                len(set(result["reference"]["sequence"])) == result["reference"]["completed_count"]
            )


def test_frontier_tradeoff_does_not_hide_extra_time():
    groups, _, _ = load_dataset(DATASET)
    result = benchmark(groups["validation"][0])
    assert result["reward_gap"] == 1
    assert result["estimated_elapsed_delta_s"] > 200
    assert result["same_time_reward_gap"] == 0
    frontier = result["reference"]["pareto_frontier"]
    assert any(p["reward"] == 0 and p["estimated_elapsed_s"] == 0 for p in frontier)
    for p in frontier:
        assert not any(
            q != p
            and q["reward"] >= p["reward"]
            and q["completed_count"] >= p["completed_count"]
            and q["estimated_elapsed_s"] <= p["estimated_elapsed_s"]
            for q in frontier
        )


def test_heterogeneous_diagnostic_has_nominal_and_rejected_candidates():
    groups, _, identity = load_dataset(DATASET.with_name("task-sequence-heterogeneous-v1.json"))
    assert "not independent holdout" in identity["provenance"]
    assert sum(map(len, groups.values())) == 12
    for cases in groups.values():
        for case in cases:
            result = benchmark(case)
            assert result["greedy"]["reward"] > 0
            assert 0 < result["initial_feasible_count"] < len(case.candidates)
            assert result["initial_rejected"]
            assert result["same_time_reward_gap"] >= 0


def test_empty_frontier_includes_stop_when_all_infeasible():
    case = first_case()
    result = benchmark(replace(case, context=replace(case.context, gps_fix_type=2)))
    assert result["reference_within_greedy_time"]["sequence"] == []
    assert result["same_time_reward_gap"] == 0
    assert result["estimated_elapsed_delta_s"] == 0


def test_duration_control_changes_only_case_label_and_duration_cap():
    original = json.loads(DATASET.with_name("task-sequence-heterogeneous-v1.json").read_text())
    control = json.loads(DATASET.with_name("task-sequence-duration-control-v1.json").read_text())
    for base, changed in zip(original["cases"], control["cases"], strict=True):
        expected = json.loads(json.dumps(base))
        expected["case_id"] += "-duration-control"
        for item in expected["tasks"]:
            item["task"]["constraints"]["maximum_duration_s"] = 600
        assert changed == expected
    groups, _, _ = load_dataset(DATASET.with_name("task-sequence-duration-control-v1.json"))
    for cases in groups.values():
        for case in cases:
            result = benchmark(case)
            assert result["initial_feasible_count"] == 4
            assert result["same_time_reward_gap"] >= 0


@pytest.mark.parametrize("limit", [0, True, 1])
def test_limits_do_not_claim_optimum(limit):
    with pytest.raises(ValueError):
        exact_reference(first_case(), max_nodes=limit)


def test_infeasible_all_returns_empty_optimum():
    case = first_case()
    result = exact_reference(replace(case, context=replace(case.context, gps_fix_type=2)))
    assert result["sequence"] == []
    assert result["reward"] == 0
    assert result["nodes"] == 1
    assert result["rejected_edges"] == len(case.candidates)


def test_invalid_runtime_aborts():
    with pytest.raises(ValueError, match="missing"):
        exact_reference(replace(first_case(), runtimes={}))


def test_cli_limit_aborts_without_artifact_and_success_preserves_report(tmp_path):
    output = tmp_path / "reference.json"
    args = ["--dataset", str(DATASET), "--output", str(output)]
    with pytest.raises(ValueError, match="limit"):
        main(args + ["--max-nodes", "1"])
    assert not output.exists()
    assert main(args) == 0
    original = output.read_bytes()
    report = json.loads(original)
    assert report["reference_reward"] >= report["greedy_reward"]
    with pytest.raises(ValueError, match="new"):
        main(args)
    assert output.read_bytes() == original
