from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_shared_policy_campaign import main, run_campaign

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "configs/training/task-ranker-budget-v1.json"
EVAL = ROOT / "configs/training/task-policy-independent-holdout-v1.json"
OVERLAP_EVAL = ROOT / "configs/training/task-sequence-heterogeneous-v1.json"
PARENT = ROOT / "configs/training/task-ranker-baseline-v1.json"


def _args(output: Path) -> list[str]:
    return [
        "--training-dataset",
        str(TRAIN),
        "--evaluation-dataset",
        str(EVAL),
        "--parent",
        str(PARENT),
        "--parent-sha256",
        hashlib.sha256(PARENT.read_bytes()).hexdigest(),
        "--output",
        str(output),
    ]


def test_independent_campaign_binds_policy_and_keeps_candidate_quarantined(tmp_path: Path) -> None:
    output = tmp_path / "campaign.json"
    assert main(_args(output)) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["method"] == "hash_bound_shared_policy_independent_sim_campaign_v1"
    assert report["input_hashes_disjoint"] is True
    assert report["scenario_families_disjoint"] is True
    assert report["training_input_count"] == 3
    assert report["evaluation_input_count"] == 12
    assert report["truth_free"] is True
    assert report["planner_revalidated"] is True
    assert report["candidate_promoted"] is False
    assert report["final_release_allowed"] is False
    for key in ("parent", "candidate", "independent_parent", "independent_candidate"):
        ranker_kind = key.split("_")[-1] if key.startswith("independent_") else key
        expected_artifact = report[f"{ranker_kind}_policy_artifact_sha256"]
        assert set(report["evaluations"][key]) == {"train", "validation", "test"}
        assert all(
            row["policy_artifact_sha256"] == expected_artifact
            for split in report["evaluations"][key].values()
            for row in split["cases"]
        )


def test_independent_campaign_rejects_overlapping_input_hashes(tmp_path: Path) -> None:
    overlap = tmp_path / "overlap.json"
    overlap.write_bytes(TRAIN.read_bytes())
    with pytest.raises(ValueError, match="overlap"):
        run_campaign(
            training_dataset=TRAIN,
            evaluation_dataset=overlap,
            parent_path=PARENT,
            parent_sha256=hashlib.sha256(PARENT.read_bytes()).hexdigest(),
        )


def test_independent_campaign_rejects_overlapping_families(tmp_path: Path) -> None:
    data = json.loads(OVERLAP_EVAL.read_text(encoding="utf-8"))
    for case in data["cases"]:
        case["context"]["snapshot_id"] = f"family-overlap-{case['case_id']}"
    overlap = tmp_path / "family-overlap.json"
    overlap.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="scenario families"):
        run_campaign(
            training_dataset=TRAIN,
            evaluation_dataset=overlap,
            parent_path=PARENT,
            parent_sha256=hashlib.sha256(PARENT.read_bytes()).hexdigest(),
        )
