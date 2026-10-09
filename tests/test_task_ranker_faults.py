import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.task_ranker import TaskRanker
from scripts.evaluate_task_ranker_faults import evaluate_faults, main
from scripts.train_task_ranker import load_dataset

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "configs/training/task-ranker-budget-v1.json"
PARENT = ROOT / "configs/training/task-ranker-baseline-v1.json"


@pytest.mark.parametrize("coefficients", [(0, 0, 0), (1e6, 1e6, 1e6), (-1e6, -1e6, -1e6)])
def test_faults_block_even_extreme_rankings(coefficients):
    groups, _, _ = load_dataset(DATASET)
    result = evaluate_faults(groups, TaskRanker(coefficients, "fault-test"))
    assert result["passed"]
    assert result["checks"] == 81
    assert result["failed_checks"] == 0
    assert all(row["base_input_hash"] != row["perturbed_input_hash"] for row in result["rows"])


def test_nominal_failure_cannot_pass_fault_gate():
    groups, _, _ = load_dataset(DATASET)
    case = groups["train"][0]
    case = replace(case, context=replace(case.context, gps_fix_type=2))
    result = evaluate_faults({"train": [case]}, TaskRanker((0, 0, 0), "negative"))
    assert not result["passed"]
    assert result["failed_checks"] == 9


def test_fault_cli_records_identity_and_preserves_output(tmp_path):
    path = tmp_path / "faults.json"
    digest = hashlib.sha256(PARENT.read_bytes()).hexdigest()
    args = ["--dataset", str(DATASET), "--candidate", str(PARENT),
            "--candidate-sha256", digest, "--output", str(path)]
    assert main(args) == 0
    raw = path.read_bytes()
    report = json.loads(raw)
    assert report["candidate_file_sha256"] == digest
    assert report["passed"]
    with pytest.raises(ValueError, match="new"):
        main(args)
    assert path.read_bytes() == raw


def test_empty_fault_gate_fails():
    assert not evaluate_faults({}, TaskRanker((0, 0, 0), "empty"))["passed"]
