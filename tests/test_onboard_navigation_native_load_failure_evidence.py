"""A native initialization failure is not a completed training experiment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tests.onboard_navigation_evidence_support import requires_local_artifacts

ROOT = Path(__file__).resolve().parents[1]
pytestmark = requires_local_artifacts(ROOT)
RECEIPT = ROOT / "docs/evidence/onboard-navigation-native-load-failure-20261007-r1.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bound(row):
    path = ROOT / row["path"]
    if not path.is_file() and row["path"].startswith("artifacts/"):
        pytest.skip("native artifacts are intentionally absent from a clean clone")
    assert path.resolve().is_relative_to(ROOT.resolve())
    assert path.stat().st_size == row["bytes"]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    assert digest.hexdigest() == row["sha256"]
    return path


def test_failure_not_success_or_zero_optimizer_updates():
    value = read(RECEIPT)
    observed = read(bound(value["native_observation"]))
    assert value["tool_exit_code"] == observed["terminal_tool_exit_code"] == 1
    assert value["tool_session_id"] == observed["tool_session_id"] == 20379
    assert value["windows_exception_code"] == observed["application_event"]["exception_code"]
    assert value["windows_exception_is_not_tool_exit_code"]
    assert value["windows_event_correlation_not_exact_child_pid_verified"]
    for key in ("optimizer_steps_attempted", "optimizer_steps_completed", "optimizer_performed"):
        assert value[key] is observed[key] is None
    assert value["optimizer_counts_unknown_not_zero"]
    for key in (
        "training_report_available", "adapter_available", "model_training_completed",
        "exact_native_faulting_operation_known", "out_of_memory_confirmed",
        "candidate_promoted", "quality_claim", "physical_performance_claimed",
        "hil_real_evidence", "outdoor_gnss_evidence", "final_release_allowed",
    ):
        assert value[key] is False
    assert value["original_failure_preserved"]
    assert not value["original_report_synthesized"]
    directory = ROOT / observed["attempt_directory"]
    assert not (directory / "report.json").exists()
    assert not (directory / "training-events.jsonl").exists()
    assert not (directory / "adapter").exists()


def test_frozen_plan_original_invocation_and_source_snapshots():
    value = read(RECEIPT)
    plan = read(bound(value["training_plan"]))
    invocation = read(bound(value["invocation"]))
    assert invocation["plan"]["sha256"] == value["training_plan"]["sha256"]
    assert value["planned_optimization"] == plan["optimization"]
    assert value["planned_train_samples"] == plan["expected_samples"] == 86
    assert not value["independent_holdout_available"]
    assert value["source_planned_not_executed"]
    assert value["sources"] == plan["source_files"]
    assert len(value["implementation_snapshots"]) == len(value["sources"]) == 8
    for source, snapshot in zip(value["sources"], value["implementation_snapshots"], strict=True):
        assert snapshot["path"].endswith("/" + source["path"])
        assert snapshot["sha256"] == source["sha256"]
        assert snapshot["bytes"] == source["bytes"]
        bound(snapshot)


def test_cpu_validation_count_is_not_gpu_training_or_quality():
    value = read(RECEIPT)["trainer_cpu_validation"]
    assert value["native_exit_code"] == 0
    suites = ElementTree.parse(bound(value["report"])).getroot().iter("testsuite")
    actual = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in suites:
        for key in actual:
            actual[key] += int(suite.attrib[key])
    assert actual == {"tests": 50, "failures": 0, "errors": 0, "skipped": 0}


def test_separate_data_review_and_official_research_remain_bound():
    value = read(RECEIPT)
    dataset = read(bound(value["dataset_evidence"]))
    review = read(bound(value["training_review"]))
    assert dataset["sample_count"] == 86
    assert review["allowed_training_use"] == "planned_navigation_train_only_resubstitution_pilot"
    assert review["independent_quality_evidence"] is False
    for row in value["new_research_documents"]:
        bound(row)
