from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.replay_set import REPLAY_SET_POSITIVE_STRATA, load_replay_set_manifest
from multidetect.replay_set_fixture import create_positive_replay_set

REPO_ROOT = Path(__file__).resolve().parents[1]
# The historical canonical tree predates the current recording-identity
# contract and is intentionally retained as immutable evidence.  v34 is the
# regenerated, hash-consistent copy used by executable fixture tests.
SIM_REPLAY_ROOT = (
    REPO_ROOT
    / "artifacts"
    / "evaluation"
    / "terminal-continuation-v34"
    / "replay-set-sim-gazebo-2026"
)


def test_positive_fixture_exercises_iou_and_keeps_deployment_gate_closed(tmp_path: Path) -> None:
    output = tmp_path / "positive-fixture"
    summary = create_positive_replay_set(
        SIM_REPLAY_ROOT,
        output,
        dataset_id="positive-fixture",
        dataset_version="v0.3.0-pre.8",
    )

    manifest = load_replay_set_manifest(output / "replay-set.manifest.json")
    accuracy = json.loads((output / "accuracy-report.json").read_text(encoding="utf-8"))
    fixture_summary = json.loads((output / "fixture-summary.json").read_text(encoding="utf-8"))

    assert manifest.claims["contains_ground_truth"] is True
    assert manifest.claims["contains_predictions"] is True
    assert manifest.claims["deployment_domain_evidence_complete"] is False
    assert manifest.is_complete is False
    assert accuracy["metrics_evaluated"] is True
    assert accuracy["positive_strata_coverage_passed"] is True
    assert accuracy["metrics_gate_passed"] is False
    assert accuracy["accuracy_validated"] is False
    assert accuracy["gate_passed"] is False
    assert accuracy["metrics"]["overall"]["precision"] == pytest.approx(1.0)
    assert accuracy["metrics"]["overall"]["recall"] == pytest.approx(1.0)
    assert accuracy["metrics"]["false_alarm_frame_count"] == 0
    assert accuracy["metrics"]["missed_detection_frame_count"] == 0
    assert set(accuracy["positive_stratum_coverage"]) == set(REPLAY_SET_POSITIVE_STRATA)
    assert all(
        value["positive_truth_object_count"] > 0 and value["positive_prediction_count"] > 0
        for value in accuracy["positive_stratum_coverage"].values()
    )
    assert summary == fixture_summary
    assert fixture_summary["source_kind"] == "synthetic_demo"
    assert fixture_summary["software_only"] is True
    assert fixture_summary["annotations_reviewed"] is False
    assert fixture_summary["deployment_domain_evidence_complete"] is False
    assert fixture_summary["accuracy_validated"] is False
    assert fixture_summary["gate_passed"] is False

    no_fire_truth = [
        json.loads(line)
        for line in (output / "no_fire" / "ground-truth.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    no_fire_predictions = [
        json.loads(line)
        for line in (output / "no_fire" / "predictions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert no_fire_truth and all(item["objects"] == [] for item in no_fire_truth)
    assert no_fire_predictions and all(item["detections"] == [] for item in no_fire_predictions)

    smoke_only_truth = [
        json.loads(line)
        for line in (output / "smoke_only" / "ground-truth.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    smoke_only_predictions = [
        json.loads(line)
        for line in (output / "smoke_only" / "predictions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert smoke_only_truth and all(
        [item["label"] for item in frame["objects"]] == ["smoke"] for frame in smoke_only_truth
    )
    assert smoke_only_predictions and all(
        [item["label"] for item in frame["detections"]] == ["smoke"]
        for frame in smoke_only_predictions
    )


def test_positive_fixture_rejects_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        create_positive_replay_set(
            SIM_REPLAY_ROOT,
            output,
            dataset_id="positive-fixture",
            dataset_version="v0.3.0-pre.8",
        )


def test_positive_fixture_rejects_output_inside_source(tmp_path: Path) -> None:
    output = SIM_REPLAY_ROOT / ".tmp-positive-fixture-test"
    try:
        with pytest.raises(ValueError, match="inside source"):
            create_positive_replay_set(
                SIM_REPLAY_ROOT,
                output,
                dataset_id="positive-fixture",
                dataset_version="v0.3.0-pre.8",
            )
    finally:
        output.rmdir() if output.is_dir() else None


def test_positive_fixture_is_byte_deterministic_across_output_roots(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    create_positive_replay_set(
        SIM_REPLAY_ROOT,
        first,
        dataset_id="deterministic-fixture",
        dataset_version="v0.3.0-pre.8",
    )
    create_positive_replay_set(
        SIM_REPLAY_ROOT,
        second,
        dataset_id="deterministic-fixture",
        dataset_version="v0.3.0-pre.8",
    )

    def tree_hashes(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert tree_hashes(first) == tree_hashes(second)
