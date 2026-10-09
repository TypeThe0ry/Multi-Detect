import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.policy_artifact import load_policy_artifact
from multidetect.autonomy.task_ranker import load_task_ranker
from scripts.train_task_ranker import load_dataset, main

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "configs/training/task-ranker-budget-v1.json"
PARENT = ROOT / "configs/training/task-ranker-baseline-v1.json"


def test_training_cli_retains_baseline_and_binds_output(tmp_path):
    output = tmp_path / "fit"
    parent_hash = hashlib.sha256(PARENT.read_bytes()).hexdigest()
    args = ["--dataset", str(DATASET), "--parent", str(PARENT), "--parent-sha256", parent_hash,
            "--output-directory", str(output)]
    assert main(args) == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    candidate = load_task_ranker(output / "candidate.json",
                                 expected_sha256=report["candidate_file_sha256"])
    parent = load_task_ranker(PARENT, expected_sha256=parent_hash)
    assert candidate == parent
    assert report["search"]["selected_trial"] == 0
    assert report["candidate_promoted"] is False
    assert report["holdout_used_for_selection"] is False
    assert report["dataset"]["sha256"] == hashlib.sha256(DATASET.read_bytes()).hexdigest()
    candidate_artifact_path = output / report["policy_artifact_files"]["candidate"]
    parent_artifact_path = output / report["policy_artifact_files"]["parent"]
    candidate_artifact = load_policy_artifact(
        candidate_artifact_path,
        expected_sha256=report["candidate_policy_artifact_file_sha256"],
        ranker=candidate,
    )
    parent_artifact = load_policy_artifact(
        parent_artifact_path,
        expected_sha256=report["parent_policy_artifact_file_sha256"],
        ranker=parent,
    )
    assert candidate_artifact.canonical_hash == report["candidate_policy_artifact_sha256"]
    assert parent_artifact.canonical_hash == report["parent_policy_artifact_sha256"]
    assert set(report["evaluations"]) == {"train", "validation", "test"}
    prior = (output / "report.json").read_bytes()
    with pytest.raises(ValueError, match="new"):
        main(args)
    assert (output / "report.json").read_bytes() == prior


def test_training_cli_can_bind_explicit_runtime_airframe_identity(tmp_path):
    output = tmp_path / "fit-runtime-profile"
    parent_hash = hashlib.sha256(PARENT.read_bytes()).hexdigest()
    runtime_airframe_hash = "a" * 64
    assert main(
        [
            "--dataset",
            str(DATASET),
            "--parent",
            str(PARENT),
            "--parent-sha256",
            parent_hash,
            "--airframe-profile-sha256",
            runtime_airframe_hash,
            "--output-directory",
            str(output),
        ]
    ) == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["airframe_profile_hash"] == runtime_airframe_hash
    assert report["airframe_profile_hash_source"] == "explicit_cli"
    artifact = load_policy_artifact(
        output / "parent-policy-artifact.json",
        expected_sha256=report["parent_policy_artifact_file_sha256"],
        ranker=load_task_ranker(PARENT, expected_sha256=parent_hash),
    )
    assert artifact.airframe_profile_hash == runtime_airframe_hash


@pytest.mark.parametrize("fault", ["family", "inputs", "empty", "unknown", "nan", "parent"])
def test_training_rejects_bad_partition_and_inputs_before_output(tmp_path, fault):
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    if fault == "family":
        data["cases"][3]["family"] = data["cases"][0]["family"]
    elif fault == "inputs":
        data["cases"][3] = dict(data["cases"][0], case_id="renamed", split="validation",
                                family="different-label")
    elif fault == "empty":
        data["cases"] = [case for case in data["cases"] if case["split"] != "test"]
    elif fault == "unknown":
        data["cases"][0]["truth"] = {}
    elif fault == "nan":
        data["coefficient_candidates"][0][0] = float("nan")
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "fit"
    if fault == "parent":
        with pytest.raises(ValueError, match="hash"):
            main(["--dataset", str(path), "--parent", str(PARENT),
                  "--parent-sha256", "0" * 64, "--output-directory", str(output)])
    else:
        with pytest.raises(ValueError):
            load_dataset(path)
    assert not output.exists()
