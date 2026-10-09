"""Header compatibility and a reliable audit are not full model-load success."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-navigation-streaming-load-only-20261007-r1.json"


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


def test_native_oom_before_payload_is_not_training_or_full_load():
    value = read(RECEIPT)
    report = read(artifact(value, "/load-only-report.json"))
    completion = read(artifact(value, "/completion.json"))
    wrapper = read(artifact(value, "/streaming-completion.json"))
    assert report["failure_type"] == value["error_type"] == "OutOfMemoryError"
    assert report["failure"] == value["reported_message"]
    assert "3.96 GiB" in report["failure"] and "6.93 GiB" in report["failure"]
    assert report["native_exit_code"] == completion["child_returncode_raw"] == 1
    assert completion["supervisor_exit_code"] == wrapper["supervisor_exit_code"] == 1
    assert not report["backend_initialized"]
    assert not completion["child_completion_verified"]
    assert not wrapper["streaming_receipt_valid"]
    assert "Metadata-only" in wrapper["streaming_receipt_failure"]["message"]
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
        "allocation_failure_exact_frame_captured",
        "fragmentation_proven",
        "prior_failure_cause_proven",
    ):
        assert value[key] is False


def test_two_real_header_opens_do_not_imply_any_tensor_payload_read():
    value = read(RECEIPT)
    loader = read(artifact(value, "/streaming-loader-report.json"))
    assert loader["observed_calls"] == {
        "safe_open_calls_attempted": 2,
        "safe_open_calls_returned": 2,
    }
    assert len(loader["reader_openings"]) == 2
    for opening in loader["reader_openings"]:
        diagnostic = opening["final_diagnostics"]
        assert opening["kind"] == "base" and diagnostic["closed"]
        assert diagnostic["header_declared_bytes"] == 76240
        assert diagnostic["header_bytes_read"] == diagnostic["total_actual_bytes_read"] == 76248
        assert diagnostic["declared_tensor_count"] == 625
        assert diagnostic["tensor_calls_attempted"] == diagnostic["tensor_calls_completed"] == 0
        assert diagnostic["tensor_payload_bytes_read"] == 0 and diagnostic["tensor_reads"] == []
        for key in (
            "mmap_used",
            "rust_safe_open_used",
            "whole_file_buffer_created",
            "host_peak_memory_measured",
        ):
            assert diagnostic[key] is False
    rows = [json.loads(line) for line in artifact(value, "/stdout.bin").read_bytes().splitlines()]
    assert sum(row.get("event") == "safe_open_streaming_call_started" for row in rows) == 2
    assert sum(row.get("event") == "safe_open_streaming_call_returned" for row in rows) == 2
    assert loader["no_mmap_scope"] == value["no_mmap_scope"] == "checkpoint_reader_only"


def test_source_and_cpu_audits_remain_distinct_from_native_acceptance():
    value = read(RECEIPT)
    completion = read(artifact(value, "/completion.json"))
    assert len(completion["sources_after"]) == value["source_binding_count"] == 55
    assert completion["sources_before"] == completion["sources_after"]
    assert completion["source_bindings_unchanged"]
    audit = read(artifact(value, "/independent-review.json"))
    assert audit["read_only_final_audit_native_exit_code"] == 0
    assert audit["model_load_native_exit_code"] == 1
    assert not audit["load_only_success"]
    for cpu in value["cpu_validations"]:
        suites = ElementTree.parse(bound(cpu["report"])).getroot().iter("testsuite")
        actual = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
        for suite in suites:
            for key in actual:
                actual[key] += int(suite.attrib[key])
        assert actual == {"tests": cpu["tests"], "failures": 0, "errors": 0, "skipped": 0}
        assert cpu["native_exit_code"] == 0
    for row in value["implementation"]:
        bound(row)
    bound(value["frozen_plan"])
    bound(value["runtime_manifest"])
    for row in value["prior_failure_evidence"]:
        bound(row)


def test_post_exit_resource_snapshot_does_not_authorize_host_changes():
    value = read(RECEIPT)
    host = read(artifact(value, "/post-run-host-resource-observation.json"))
    assert host["drives"][0]["FreeBytes"] == 1334394880
    assert value["resource_observation_is_post_run_not_exact_error_time"]
    assert not host["exact_error_time_commit_peak_known"]
    for key in ("pagefile_settings_changed", "external_processes_stopped", "files_deleted"):
        assert host[key] is value[key] is False
