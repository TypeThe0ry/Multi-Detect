"""A CPU-approved hook installation is not a model initialization result."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tests.onboard_navigation_evidence_support import requires_local_artifacts

ROOT = Path(__file__).resolve().parents[1]
pytestmark = requires_local_artifacts(ROOT)
RECEIPT = ROOT / "docs/evidence/onboard-navigation-early-import-failure-20261007-r1.json"


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


def test_import_order_failure_never_becomes_a_training_or_gpu_result():
    value = read(RECEIPT)
    completion = read(artifact(value, "/completion.json"))
    stderr = artifact(value, "/stderr.bin").read_text(encoding="utf-8")
    assert "install_constructor_traceback" in stderr
    assert "ModuleNotFoundError: No module named 'multidetect'" in stderr
    assert value["error_type"] == "ModuleNotFoundError"
    assert value["execution"]["native_exit_code"] == completion["child_returncode_raw"] == 1
    assert value["supervisor_exit_codes"] == {"original": 1, "streaming": 1, "warmup": 1}
    assert not completion["child_completion_verified"]
    assert not completion["worker_report"]["exists"]
    assert not completion["optimizer_journal"]["exists"]
    assert (
        value["optimizer_observed_counts"]
        is completion["optimizer_journal"]["observed_counters"]
        is None
    )
    assert value["missing_report_fields_are_not_observed_zero_or_false"]
    for key in (
        "backend_constructor_entered",
        "model_loading_completed",
        "worker_report_exists",
        "candidate_promoted",
        "quality_claim",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "execution_authority",
        "transport_created",
        "mission_created",
        "external_processes_stopped",
        "pagefile_settings_changed",
        "files_deleted",
    ):
        assert value[key] is False
    audit = read(artifact(value, "/independent-review.json"))
    assert audit["terminal_state"]["backend_constructor_entered"] is False
    assert audit["terminal_state"]["not_previous_access_violation_or_pagefile_or_cuda_oom"]
    assert audit["actual_events"]["constructor_observation"] is None
    assert value["independent_read_only_audit_exit_code"] == 0


def test_installed_hooks_and_cpu_checks_do_not_imply_weight_reads():
    value = read(RECEIPT)
    completion = read(artifact(value, "/completion.json"))
    assert completion["sources_before"] == completion["sources_after"]
    assert len(completion["sources_after"]) == value["source_binding_count"] == 57
    assert completion["source_bindings_unchanged"] is value["source_bindings_unchanged"] is True
    rows = [json.loads(line) for line in artifact(value, "/stdout.bin").read_bytes().splitlines()]
    assert [row["event"] for row in rows] == [
        "streaming_runtime_verified",
        "explicit_streaming_loader_installed",
        "allocator_warmup_override_installed",
        "explicit_streaming_worker_finished",
        "allocator_warmup_override_restored",
        "no_warmup_worker_finished",
    ]
    assert rows[1]["safe_open_calls_attempted"] == rows[1]["safe_open_calls_returned"] == 0
    assert rows[3]["observed_calls"] is value["streaming_finished_observed_calls"] is None
    assert rows[3]["reader_openings"] == []
    assert rows[4]["warmup_calls_attempted"] == rows[4]["warmup_calls_skipped"] == 0
    assert rows[4]["warmup_override_restored"] is value["warmup_hook_restored"] is True
    for key in ("frozen_plan", "runtime_manifest", "entrypoint"):
        bound(value[key])
    cpu = value["cpu_validation"]
    totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in ElementTree.parse(bound(cpu["report"])).getroot().iter("testsuite"):
        for key in totals:
            totals[key] += int(suite.attrib[key])
    assert totals == {"tests": 281, "failures": 0, "errors": 0, "skipped": 0}
    assert cpu["native_exit_code"] == 0
