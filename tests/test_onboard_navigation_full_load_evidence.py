"""Full weight initialization is independently verified, but is not training."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-navigation-full-load-only-20261007-r2.json"


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


def test_actual_full_cuda_weight_read_and_all_three_gates_pass():
    value = read(RECEIPT)
    report = read(artifact(value, "/load-only-report.json"))
    completion = read(artifact(value, "/completion.json"))
    stream = read(artifact(value, "/streaming-completion.json"))
    warmup = read(artifact(value, "/allocator-warmup-completion.json"))
    assert value["execution"]["native_exit_code"] == report["native_exit_code"] == 0
    assert completion["child_returncode_raw"] == completion["supervisor_exit_code"] == 0
    assert stream["supervisor_exit_code"] == warmup["supervisor_exit_code"] == 0
    assert completion["child_completion_verified"] and stream["streaming_receipt_valid"]
    assert warmup["warmup_call_evidence_valid"]
    assert report["backend_initialized"] is value["model_initialization_completed"] is True
    assert report["base_parameter_sha256"] == value["base_parameter_sha256"]
    assert report["trainable_audit"]["frozen_base_dtypes"] == ["torch.bfloat16"]
    assert report["trainable_audit"]["trainable_dtypes"] == ["torch.float32"]
    loader = read(artifact(value, "/streaming-loader-report.json"))
    assert loader["observed_calls"] == {
        "safe_open_calls_attempted": 4,
        "safe_open_calls_returned": 4,
    }
    assert len(loader["reader_openings"]) == 4
    for opening in loader["reader_openings"]:
        assert opening["final_diagnostics"]["closed"]
    cuda = loader["reader_openings"][-1]["final_diagnostics"]
    assert cuda["normalized_device"] == "cuda:0"
    assert cuda["tensor_calls_attempted"] == cuda["tensor_calls_completed"] == 625
    assert len({row["name"] for row in cuda["tensor_reads"]}) == 625
    assert (
        cuda["tensor_payload_bytes_read"] == value["full_cuda_tensor_payload_bytes"] == 4255064064
    )
    assert cuda["max_single_tensor_payload_bytes_read"] == 622329856
    assert not cuda["mmap_used"] and not cuda["whole_file_buffer_created"]
    state = warmup["warmup_report"]["warmup_observation"]
    assert state["warmup_calls_attempted"] == state["warmup_calls_skipped"] == 1
    assert state["warmup_override_restored"]


def test_loaded_base_does_not_close_training_quality_or_flight_gates():
    value = read(RECEIPT)
    report = read(artifact(value, "/load-only-report.json"))
    completion = read(artifact(value, "/completion.json"))
    assert completion["sources_before"] == completion["sources_after"]
    assert len(completion["sources_after"]) == value["source_binding_count"] == 59
    assert completion["source_bindings_unchanged"]
    assert (
        value["optimizer_observed_counts"]
        is completion["optimizer_journal"]["observed_counters"]
        is None
    )
    for key in ("optimizer_performed", "inference_performed", "forward_backward_performed"):
        assert report[key] is value[key] is False
    for key in (
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
        "external_processes_stopped",
        "pagefile_settings_changed",
        "files_deleted",
        "host_peak_memory_measured",
    ):
        assert value[key] is False
    audit = read(artifact(value, "/independent-review.json"))
    assert audit["terminal_state"]["actual_frozen_gate_helpers_revalidated"]
    assert audit["terminal_state"]["backend_initialized"]
    assert all(row["native_exit_code"] == 0 for row in audit["review_operations"])
    for row in value["implementation"]:
        bound(row)
    bound(value["frozen_plan"])
    bound(value["runtime_manifest"])
    cpu = value["cpu_validation"]
    totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in ElementTree.parse(bound(cpu["report"])).getroot().iter("testsuite"):
        for key in totals:
            totals[key] += int(suite.attrib[key])
    assert totals == {"tests": 298, "failures": 0, "errors": 0, "skipped": 0}
