import json
from dataclasses import replace
from itertools import permutations
from pathlib import Path

import pytest

from multidetect.autonomy.task_sequence_reference import (
    SEQUENCE_REFERENCE_OBJECTIVE_ID,
    exact_sequence_reference,
)
from scripts.benchmark_task_sequences import exact_reference
from scripts.train_task_ranker import load_dataset

DATASET = Path(__file__).resolve().parents[1] / "configs/training/task-ranker-budget-v1.json"


def _case():
    groups, _, _ = load_dataset(DATASET)
    return groups["validation"][0]


def test_library_reference_is_explicit_and_script_delegates() -> None:
    case = _case()
    result = exact_sequence_reference(
        case.candidates,
        case.context,
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
    )
    mapping = result.to_mapping()
    delegated = exact_reference(case)
    assert mapping["objective_id"] == SEQUENCE_REFERENCE_OBJECTIVE_ID
    assert {key: value for key, value in mapping.items() if key != "search_wall_s"} == {
        key: value for key, value in delegated.items() if key != "search_wall_s"
    }
    assert mapping["exhaustive"] is True
    assert mapping["deployed"] is False
    assert mapping["physical_optimality_claimed"] is False
    assert mapping["independent_holdout_claimed"] is False
    assert not {"actuator_command", "mission_start", "execution_authority"}.intersection(mapping)


def test_reference_is_invariant_to_input_order() -> None:
    case = _case()
    results = [
        exact_sequence_reference(
            order,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
        ).to_mapping()
        for order in permutations(case.candidates)
    ]
    projected = [
        json.dumps(
            {key: value for key, value in result.items() if key != "search_wall_s"},
            sort_keys=True,
        )
        for result in results
    ]
    assert len(set(projected)) == 1


def test_reference_rejects_bound_excess_and_node_limit() -> None:
    case = _case()
    with pytest.raises(ValueError, match="requires 1..1"):
        exact_sequence_reference(
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
            max_tasks=1,
        )
    with pytest.raises(ValueError, match="node limit"):
        exact_sequence_reference(
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes=case.runtimes,
            airframe_profile_hash=case.airframe_profile_hash,
            max_nodes=1,
        )


def test_reference_keeps_gps_rejection_fail_closed() -> None:
    case = _case()
    result = exact_sequence_reference(
        case.candidates,
        replace(case.context, gps_fix_type=2),
        case.vehicle_class,
        runtimes=case.runtimes,
        airframe_profile_hash=case.airframe_profile_hash,
    )
    assert result.sequence == ()
    assert result.reward == 0
    assert result.expanded_nodes == 1
    assert result.rejected_edges == len(case.candidates)


def test_reference_validates_runtime_before_search() -> None:
    case = _case()
    with pytest.raises(ValueError, match="missing compile runtime"):
        exact_sequence_reference(
            case.candidates,
            case.context,
            case.vehicle_class,
            runtimes={},
            airframe_profile_hash=case.airframe_profile_hash,
        )
