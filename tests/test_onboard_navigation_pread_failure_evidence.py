"""CPU storage equality does not turn a handled full-load failure into success."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tests.onboard_navigation_evidence_support import requires_local_artifacts

ROOT = Path(__file__).resolve().parents[1]
pytestmark = requires_local_artifacts(ROOT)
RECEIPT = ROOT / "docs/evidence/onboard-navigation-pread-load-only-20261007-r1.json"


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


def artifact(value, suffix):
    return bound(next(row for row in value["native_outputs"] if row["path"].endswith(suffix)))


def test_os_error_before_constructor_return_is_failed_model_load():
    value = read(RECEIPT)
    report = read(artifact(value, "/load-only-report.json"))
    completion = read(artifact(value, "/completion.json"))
    loader = read(artifact(value, "/pread-loader-report.json"))
    wrapper = read(artifact(value, "/pread-completion.json"))
    assert report["failure_type"] == value["error_type"] == "OSError"
    assert report["failure"] == value["reported_message"]
    assert "1455" in report["failure"]
    assert report["native_exit_code"] == completion["child_returncode_raw"] == 1
    assert completion["supervisor_exit_code"] == wrapper["supervisor_exit_code"] == 1
    assert not report["backend_initialized"]
    assert not completion["child_completion_verified"]
    assert not wrapper["pread_receipt_valid"]
    assert loader["observed_calls"] == {
        "safe_open_calls_attempted": 1,
        "safe_open_calls_returned": 0,
    }
    assert (
        value["optimizer_observed_counts"]
        is completion["optimizer_journal"]["observed_counters"]
        is None
    )
    for key in ("inference_performed", "forward_backward_performed", "optimizer_performed"):
        assert value[key] is report[key] is False
    for key in (
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
    ):
        assert value[key] is False


def test_runtime_source_bindings_and_raw_loader_observations():
    value = read(RECEIPT)
    manifest = read(bound(value["runtime_manifest"]))
    completion = read(artifact(value, "/completion.json"))
    assert len(completion["sources_after"]) == value["source_binding_count"] == 53
    assert completion["sources_before"] == completion["sources_after"]
    assert completion["source_bindings_unchanged"]
    assert manifest["plan_sha256"] == value["frozen_plan"]["sha256"]
    assert manifest["package_versions"]["safetensors"] == "0.8.0"
    assert manifest["declared_loader_backend"] == "pread"
    rows = [
        read_line
        for line in artifact(value, "/stdout.bin").read_bytes().splitlines()
        if isinstance((read_line := json.loads(line)), dict)
    ]
    assert sum(row.get("event") == "safe_open_pread_call_started" for row in rows) == 1
    assert sum(row.get("event") == "safe_open_pread_call_returned" for row in rows) == 0
    for row in value["implementation"]:
        bound(row)
    bound(value["frozen_plan"])


def test_independent_cpu_and_source_audits_are_separate_from_model_acceptance():
    value = read(RECEIPT)
    for cpu in value["cpu_validations"]:
        suites = ElementTree.parse(bound(cpu["report"])).getroot().iter("testsuite")
        actual = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
        for suite in suites:
            for key in actual:
                actual[key] += int(suite.attrib[key])
        assert actual == {"tests": cpu["tests"], "failures": 0, "errors": 0, "skipped": 0}
        assert cpu["native_exit_code"] == 0
    bound(value["storage_comparison_review"])
    audit = read(artifact(value, "/independent-review.json"))
    assert audit["read_only_audit_native_exit_code"] == 0
    assert audit["model_load_native_exit_code"] == 1
    assert not audit["load_only_success"]
    prior = read(bound(value["original_load_only_failure_evidence"]))
    assert prior["native_child_exit_hex"] == "0xc0000005"
    assert value["native_error_is_not_prior_access_violation"]
    assert not value["prior_access_violation_cause_proven"]


def test_capacity_snapshot_is_post_exit_not_exact_fault_or_permission_to_change_host():
    value = read(RECEIPT)
    host = read(artifact(value, "/post-run-host-resource-observation.json"))
    assert host["drives"][0]["FreeBytes"] == value["post_run_host_C_free_bytes"] == 213458944
    assert value["resource_observation_is_post_run_not_exact_error_time"]
    assert not host["exact_error_time_commit_peak_known"]
    for key in ("pagefile_settings_changed", "external_processes_stopped", "files_deleted"):
        assert host[key] is value[key] is False
