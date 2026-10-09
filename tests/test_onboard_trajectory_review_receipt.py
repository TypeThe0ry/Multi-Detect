from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_train_onboard_model import _trajectory_document

from scripts.train_onboard_model import (
    _canonical_sha256,
    _trajectory_samples,
    _verify_training_ready_label_receipt,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _training_ready_fixture(tmp_path: Path) -> tuple[Path, dict, Path, dict]:
    document = _trajectory_document(training_ready=True)

    # The receipt helper only accepts a declared, materialized manifest path;
    # RGB/depth/raw replay verification is exercised by the full loader.
    rgb_bytes = b"{}"
    rgb_path = tmp_path / "rgb-manifest.json"
    rgb_path.write_bytes(rgb_bytes)
    rgb_hash = _sha256(rgb_bytes)
    raw_path = tmp_path / "recording.manifest.json"
    raw_path.write_bytes(b"{}")
    raw_hash = _sha256(raw_path.read_bytes())
    document["source_manifests"][0]["sha256"] = rgb_hash
    document["source_manifests"][0]["recording_manifest"] = {
        "path": raw_path.name,
        "sha256": raw_hash,
    }
    for episode in document["episodes"]:
        for transition in episode["transitions"]:
            transition["source"]["rgb_manifest_sha256"] = rgb_hash

            proof = transition["label_proof"]
            proof_artifacts = {}
            for artifact_name, proof_field in (
                ("annotation_policy", "annotation_policy_sha256"),
                ("decision", "decision_sha256"),
                ("plan", "plan_sha256"),
                ("wire", "wire_hash"),
                ("executable_mission", "executable_mission_sha256"),
            ):
                artifact_path = tmp_path / (
                    f"{transition['transition_id']}-{artifact_name}.proof"
                )
                artifact_bytes = (
                    f"{transition['transition_id']}:{artifact_name}\n"
                ).encode()
                artifact_path.write_bytes(artifact_bytes)
                artifact_hash = _sha256(artifact_bytes)
                proof[proof_field] = artifact_hash
                proof_artifacts[artifact_name] = {
                    "path": artifact_path.name,
                    "sha256": artifact_hash,
                }
            proof["proof_artifacts"] = proof_artifacts

    rows: list[dict] = []
    for episode in document["episodes"]:
        for transition in episode["transitions"]:
            source = transition["source"]
            proof = transition["label_proof"]
            rows.append(
                {
                    "episode_group_sha256": episode["episode_group_sha256"],
                    "episode_id": episode["episode_id"],
                    "split": episode["split"],
                    "recording_id": episode["recording_id"],
                    "recording_hash": episode["recording_hash"],
                    "frame_id": transition["frame_id"],
                    "captured_at_s": transition["observed_at_s"],
                    "transition_id": transition["transition_id"],
                    "context_sha256": source["context_sha256"],
                    "event_sha256": source["event_sha256"],
                    "task_action": document["task_action_order"][transition["task_label"]],
                    "flight_action": document["flight_action_order"][transition["flight_label"]],
                    "regression": transition["regression"],
                    "label_source": document["label_source"],
                    "planner_revalidated": True,
                    "decision_sha256": proof["decision_sha256"],
                    "plan_sha256": proof["plan_sha256"],
                    "wire_hash": proof["wire_hash"],
                    "executable_mission_sha256": proof["executable_mission_sha256"],
                }
            )
    label_bytes = (
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    ).encode("utf-8")
    label_path = tmp_path / "labels.jsonl"
    label_path.write_bytes(label_bytes)
    document["review_receipt"]["path"] = "receipt.json"
    receipt: dict = {
        "schema": "multidetect.onboard-trajectory-label-review-receipt",
        "schema_version": 1,
        "contract": "multidetect.onboard-trajectory-label-review-v1",
        "event": "onboard_trajectory_labels_reviewed",
        "dataset_id": "dataset-1",
        "dataset_version": "v1",
        "label_manifest": {
            "path": label_path.name,
            "sha256": _sha256(label_bytes),
            "bytes": len(label_bytes),
            "row_count": len(rows),
            "canonical_sha256": _canonical_sha256(
                {"schema_version": 1, "rows": sorted(rows, key=lambda row: row["transition_id"])}
            ),
        },
        "source_recordings": [
            {
                "manifest_path": raw_path.name,
                "manifest_sha256": raw_hash,
                "recording_id": episode["recording_id"],
                "recording_hash": episode["recording_hash"],
                "scenario_manifest_sha256": episode["scenario_manifest_sha256"],
                "airframe_profile_sha256": episode["airframe_profile_sha256"],
                "frame_count": 1,
            }
            for episode in document["episodes"]
        ],
        "episode_groups": [],
        "review_policy_sha256": "e" * 64,
        "annotator": "annotator",
        "reviewer": "reviewer",
        "reviewed_at_utc": "2026-10-05T00:00:00Z",
        "claims": {
            "labels_complete": True,
            "independent_review_recorded": True,
            "truth_free_inputs": True,
            "no_scenario_truth_labels": True,
            "planner_evidence_bound": True,
            "test_never_used_for_training": True,
            "release_ready": False,
        },
    }
    for episode in document["episodes"]:
        episode_rows = [row for row in rows if row["episode_id"] == episode["episode_id"]]
        receipt["episode_groups"].append(
            {
                "episode_group_sha256": episode["episode_group_sha256"],
                "episode_id": episode["episode_id"],
                "split": episode["split"],
                "transition_count": len(episode_rows),
                "transition_manifest_sha256": _canonical_sha256(
                    {
                        "episode_group_sha256": episode["episode_group_sha256"],
                        "rows": sorted(episode_rows, key=lambda row: row["transition_id"]),
                    }
                ),
            }
        )
    receipt["canonical_hash"] = _canonical_sha256(receipt)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_hash = _sha256(receipt_path.read_bytes())
    document["review_receipt"]["sha256"] = receipt_hash
    for episode in document["episodes"]:
        for transition in episode["transitions"]:
            transition["label_proof"]["review_receipt_sha256"] = receipt_hash
    dataset_path = tmp_path / "trajectory.json"
    dataset_path.write_text(json.dumps(document), encoding="utf-8")
    return dataset_path, document, receipt_path, receipt


def test_training_ready_receipt_positive_path_binds_labels_sources_and_groups(
    tmp_path: Path,
) -> None:
    dataset_path, document, receipt_path, receipt = _training_ready_fixture(tmp_path)

    _verify_training_ready_label_receipt(
        dataset_path,
        document,
        receipt_path,
        receipt,
        document["source_manifests"],
        document["episodes"],
    )


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda receipt: receipt["label_manifest"].update(row_count=1),
            "row count",
        ),
        (
            lambda receipt: receipt["claims"].update(test_never_used_for_training=False),
            "claim",
        ),
        (
            lambda receipt: receipt["episode_groups"][0].update(split="test"),
            "split binding",
        ),
    ],
)
def test_training_ready_receipt_rejects_mutated_bindings(
    tmp_path: Path, mutation, message: str
) -> None:
    dataset_path, document, receipt_path, receipt = _training_ready_fixture(tmp_path)
    mutated = copy.deepcopy(receipt)
    mutation(mutated)
    unsigned = dict(mutated)
    unsigned.pop("canonical_hash", None)
    mutated["canonical_hash"] = _canonical_sha256(unsigned)
    with pytest.raises(ValueError, match=message):
        _verify_training_ready_label_receipt(
            dataset_path,
            document,
            receipt_path,
            mutated,
            document["source_manifests"],
            document["episodes"],
        )


def test_training_ready_loader_still_fails_before_receipt_on_non_materialized_fixture(
    tmp_path: Path,
) -> None:
    dataset_path, document, _, _ = _training_ready_fixture(tmp_path)
    document["materialized_sources"] = True
    dataset_path.write_text(json.dumps(document), encoding="utf-8")
    # This proves the full training path does not accidentally treat a valid
    # receipt as enough to admit fake/raw-less recordings.
    with pytest.raises(ValueError, match="materialized RGB manifest schema"):
        _trajectory_samples(dataset_path)


def test_training_ready_receipt_rejects_tampered_proof_artifact(tmp_path: Path) -> None:
    dataset_path, document, _, receipt = _training_ready_fixture(tmp_path)
    transition = document["episodes"][0]["transitions"][0]
    artifact_name = transition["label_proof"]["proof_artifacts"]["plan"]["path"]
    (tmp_path / artifact_name).write_bytes(b"tampered-proof")

    with pytest.raises(ValueError, match="plan proof artifact hash mismatch"):
        _verify_training_ready_label_receipt(
            dataset_path,
            document,
            tmp_path / "receipt.json",
            receipt,
            document["source_manifests"],
            document["episodes"],
        )


def test_training_ready_receipt_rejects_proof_artifact_escape(tmp_path: Path) -> None:
    dataset_path, document, _, receipt = _training_ready_fixture(tmp_path)
    transition = document["episodes"][0]["transitions"][0]
    transition["label_proof"]["proof_artifacts"]["plan"]["path"] = "../outside.proof"

    with pytest.raises(ValueError, match="path escapes"):
        _verify_training_ready_label_receipt(
            dataset_path,
            document,
            tmp_path / "receipt.json",
            receipt,
            document["source_manifests"],
            document["episodes"],
        )


def test_training_ready_loader_rejects_review_receipt_escape(tmp_path: Path) -> None:
    dataset_path, document, _, _ = _training_ready_fixture(tmp_path)
    document["review_receipt"]["path"] = "../receipt.json"
    dataset_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="review receipt path escapes"):
        _trajectory_samples(dataset_path)
