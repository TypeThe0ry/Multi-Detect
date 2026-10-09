"""Native storage faults remain failures even when their evidence is valid."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-navigation-load-only-failure-20261007-r1.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bound(row):
    path = ROOT / row["path"]
    assert path.resolve().is_relative_to(ROOT.resolve())
    if not path.is_file() and row["path"].startswith("artifacts/"):
        pytest.skip("native artifacts are intentionally absent from clean clones")
    assert path.stat().st_size == row["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    return path


def artifact(value, name):
    return bound(next(row for row in value["native_evidence"] if row["path"].endswith("/" + name)))


def test_native_fault_and_unknown_counters_are_not_training_success():
    value = read(RECEIPT)
    completion = read(artifact(value, "completion.json"))
    assert value["mode"] == completion["mode"] == "load-only"
    assert value["tool_exit_code"] == value["supervisor_exit_code"] == 1
    assert value["native_child_exit_code"] == completion["child_returncode_raw"] == 3221225477
    assert value["native_child_exit_hex"] == completion["child_returncode_hex32"] == "0xc0000005"
    assert (
        value["optimizer_observed_counts"]
        is completion["optimizer_journal"]["observed_counters"]
        is None
    )
    assert value["unknown_counts_not_zero"]
    assert not completion["worker_report"]["exists"]
    assert not completion["child_completion_verified"]
    for key in (
        "root_cause_proven",
        "out_of_memory_confirmed",
        "model_loading_completed",
        "training_completed",
        "quality_claim",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "execution_authority",
        "transport_created",
        "mission_created",
        "optimizer_execution_path_reached",
    ):
        assert value[key] is False


def test_worker_not_launcher_pid_matches_actual_fault_and_raw_stack():
    value = read(RECEIPT)
    event = read(artifact(value, "application-event-observation.json"))
    tree = read(artifact(value, "process-tree-observation.json"))
    audit = read(artifact(value, "independent-review.json"))
    assert value["worker_pid"] == event["process_id"] == 41400
    assert value["launcher_pid"] == event["launcher_pid"] == 7828
    assert value["worker_pid"] != value["launcher_pid"]
    assert tree["processes"][1]["ParentProcessId"] == value["launcher_pid"]
    assert audit["application_event_independently_reopened"]["matches_worker_stdout_pid"]
    stderr = artifact(value, "stderr.bin").read_text(encoding="utf-8")
    assert "storage.py" in stderr and "__getitem__" in stderr
    assert "_load_state_dict_into_meta_model" in stderr
    assert value["failure_before_base_from_pretrained_returned"]
    assert value["failure_before_lora_attachment"]


def test_frozen_sources_and_prior_failure_remain_separate():
    value = read(RECEIPT)
    completion = read(artifact(value, "completion.json"))
    assert completion["sources_before"] == completion["sources_after"]
    assert completion["source_bindings_unchanged"]
    assert len(completion["sources_after"]) == value["source_binding_count"] == 26
    plan = bound(value["frozen_plan"])
    assert (
        next(row for row in completion["sources_after"] if Path(row["path"]) == plan)["sha256"]
        == value["frozen_plan"]["sha256"]
    )
    prior = read(bound(value["original_training_failure_receipt"]))
    assert prior["tool_session_id"] == 20379 != value["tool_session_id"]
    assert prior["exact_native_faulting_operation_known"] is False
    for row in value["supervisor_source"]:
        bound(row)


def test_cpu_wrapper_validation_and_independent_audit_are_not_native_load_acceptance():
    value = read(RECEIPT)
    report = value["cpu_validation"]
    suites = ElementTree.parse(bound(report["report"])).getroot().iter("testsuite")
    counts = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.attrib[key])
    assert counts == {"tests": 40, "failures": 0, "errors": 0, "skipped": 0}
    assert report["native_exit_code"] == 0
    audit = read(artifact(value, "independent-review.json"))
    assert audit["read_only_audit_native_exit_code"] == 0
    assert not audit["load_only_success"]
    assert not audit["claim_boundaries"]["native_model_load_passed"]
