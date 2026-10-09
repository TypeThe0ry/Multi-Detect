"""The completed native increment remains a rejected development candidate."""

from __future__ import annotations

import hashlib
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-vlm-grounding-increment-20261007-r2.json"
ENTRY = "onboard_vlm_grounding_increment_20261007_r2"
FALSE_FLAGS = (
    "candidate_promoted",
    "final_release_allowed",
    "flight_action_training_performed",
    "identity_recovery_claimed",
    "autonomous_flight_verified",
    "physical_performance_claimed",
    "hil_real_evidence",
    "jetson_evidence",
    "outdoor_gnss_evidence",
    "default_policy_reference_changed",
    "original_detector_changed",
    "transport_created",
    "execution_authority",
    "world_model_local_inference_measured",
)


def read_json(path: Path) -> dict:
    from scripts.train_onboard_vlm_grounding import strict_factual_json

    result = strict_factual_json(path.read_bytes())
    assert isinstance(result, dict)
    return result


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bound(record: dict) -> Path:
    assert set(record) == {"path", "bytes", "sha256"}
    path = ROOT / record["path"]
    path.resolve().relative_to(ROOT.resolve())
    if not path.is_file() and Path(record["path"]).parts[0] == "artifacts":
        pytest.skip(f"Ignored native artifact absent in clean clone: {record['path']}")
    assert path.is_file()
    assert path.stat().st_size == record["bytes"]
    assert sha(path) == record["sha256"]
    return path


@pytest.mark.parametrize("flag", FALSE_FLAGS)
def test_increment_does_not_grant_quality_or_flight_authority(flag):
    evidence = read_json(RECEIPT)
    assert evidence["contract"] == "multidetect.onboard-vlm-grounding-increment-evidence-v1"
    assert evidence["product_version"] == "0.3.0-pre.8"
    assert evidence[flag] is False


def test_native_completion_and_quality_rejection_are_separate():
    evidence = read_json(RECEIPT)
    training = evidence["native_training"]
    audit = evidence["independent_result_audit"]
    assert training["native_exit_code"] == audit["native_exit_code"] == 0
    assert training["optimizer_steps_completed"] == 154
    assert training["base_parameter_bytes_unchanged"] is True
    assert training["saved_adapter_exact_reload"] is True
    assert audit["native_verification_completed"] is True
    assert audit["quality_passed"] is False
    assert audit["independent_gpu_replay_performed"] is False
    assert evidence["dataset"]["split_counts"] == {"train": 154, "val": 41, "test": 43}
    assert evidence["dataset"]["new_train_samples"] == 128
    assert evidence["dataset"]["original_train_replay_samples"] == 26
    assert evidence["dataset"]["holdout_inputs_unchanged"] is True
    assert evidence["dataset"]["known_development_holdout_not_blind"] is True
    assert evidence["dataset"]["temporal_identity_training_claimed"] is False


@pytest.mark.parametrize("group", ("sources", "artifacts", "legacy_baselines"))
def test_increment_sources_and_raw_artifacts_keep_exact_bytes(group):
    records = read_json(RECEIPT)[group]
    assert records and len({row["path"] for row in records}) == len(records)
    for record in records:
        bound(record)


def test_audited_metrics_and_gates_match_the_actual_bound_reports():
    from scripts.audit_onboard_vlm_grounding_increment_result import quality_gates

    evidence = read_json(RECEIPT)
    audit = read_json(bound(evidence["independent_result_audit"]["report"]))
    native = read_json(bound(evidence["native_training"]["report"]))
    assert audit["training_report"]["sha256"] == evidence["native_training"]["report"]["sha256"]
    assert native["native_exit_code"] == audit["training_native_exit_code"] == 0
    assert audit["paired_holdout_count"] == 84
    assert audit["optimizer"]["holdout_optimizer_rows"] == 0
    assert audit["optimizer"]["each_original_train_id_exactly_once"] is True
    assert audit["quality_passed"] is False
    assert audit["successfully_learned_gate"] is False
    for role in ("parent", "candidate"):
        metrics = evidence["metrics"][role]
        assert all(audit[role][key] == value for key, value in metrics.items())
        assert metrics["count"] == 84
        assert metrics["unknown_navigation_fact_count"] == 84
        assert metrics["strict_reply_count"] == native[f"{role}_evaluation"]["strict_reply_count"]
        assert metrics["exact_match_count"] == native[f"{role}_evaluation"]["exact_match_count"]
    gates = quality_gates(evidence["metrics"]["parent"], evidence["metrics"]["candidate"])
    assert gates == evidence["quality_gates"] == audit["quality_gates"]
    assert sum(gates.values()) == 3 and len(gates) == 8
    assert evidence["failed_quality_gates"] == sorted(
        key for key, value in gates.items() if not value
    )


def test_durable_progress_binds_completed_training_and_failed_passive_capture():
    progress = read_json(ROOT / "docs/current-progress.json")
    entry = progress[ENTRY]
    assert entry["evidence"] == RECEIPT.relative_to(ROOT).as_posix()
    assert entry["evidence_bytes"] == RECEIPT.stat().st_size
    assert entry["evidence_sha256"] == sha(RECEIPT)
    assert entry["native_exit_code"] == entry["independent_audit_native_exit_code"] == 0
    assert entry["quality_passed"] is False and entry["result_audited"] is True
    assert entry["candidate_promoted"] is False
    runtime = progress["gazebo_runtime_preparation_20261007"]
    runtime_evidence = read_json(
        bound(
            {
                "path": runtime["evidence"],
                "bytes": runtime["evidence_bytes"],
                "sha256": runtime["evidence_sha256"],
            }
        )
    )
    assert runtime_evidence["passive_capture"]["controller_native_exit_code"] == 1
    assert runtime["eight_rgb_depth_frames_verified"] is False
    assert runtime["gazebo_flight_verified"] is False


def test_validation_keeps_the_original_storage_environment_failure():
    validation = read_json(RECEIPT)["validation"]
    original = ElementTree.parse(bound(validation["initial_storage_environment_failure"]["report"]))
    final = ElementTree.parse(bound(validation["source_parent_regression"]["report"]))
    original_suite = original.getroot().find("testsuite")
    final_suite = final.getroot().find("testsuite")
    assert original_suite is not None and final_suite is not None
    assert int(original_suite.attrib["tests"]) == int(final_suite.attrib["tests"]) == 111
    assert int(original_suite.attrib["failures"]) == 1
    assert int(final_suite.attrib["failures"]) == int(final_suite.attrib["errors"]) == 0
    assert validation["initial_storage_environment_failure"]["native_exit_code"] == 1
    assert validation["source_parent_regression"]["native_exit_code"] == 0
