from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-continuation-development-20261007-r1.json"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_record(record):
    path = ROOT / record["path"]
    assert path.stat().st_size == record["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]


def test_continuation_receipt_binds_exact_new_sources_and_unchanged_prior_baselines():
    receipt = _json(RECEIPT)
    for record in receipt["sources"] + receipt["baselines"]:
        _assert_record(record)
    prior = _json(ROOT / receipt["prior_execution_receipt"]["path"])
    _assert_record(receipt["prior_execution_receipt"])
    for record in prior["sources"] + prior["baselines"]:
        _assert_record(record)


def test_continuation_compile_and_receive_admission_are_not_actual_transport_or_flight():
    receipt = _json(RECEIPT)
    assert receipt["release_label"] == "0.3.0-pre.8"
    assert receipt["software_validation"]["native_exit_code"] == 0
    assert receipt["software_validation"]["continuation_compiler_implemented"] is True
    assert receipt["software_validation"]["source_fixed_target_software_verified"] is True
    assert receipt["software_validation"]["original_parent_receive_admission_implemented"] is True
    assert receipt["software_validation"]["child_readback_cursor_admission_implemented"] is True
    for name in (
        "execution_authority",
        "candidate_promoted",
        "final_release_allowed",
        "physical_performance_claimed",
        "actual_flight_verified",
        "gazebo_flight_performed",
        "flight_action_training_performed",
        "native_vlm_generation_performed",
        "host_replacement_transport_implemented",
        "replacement_parent_chain_verified",
        "generation_cancellation_verified",
        "hold_watchdog_implemented",
        "live_route_clearance_producer_implemented",
        "live_collector_receiver_join_implemented",
        "live_original_authoring_producer_implemented",
        "authenticated_original_authoring_source_verified",
        "compile_dispatch_causal_lineage_verified",
        "arbitrary_prompt_task_resolution_verified",
        "onboard_policy_reference_changed",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
    ):
        assert receipt[name] is False


def test_software_test_count_matches_original_native_junit_artifact():
    validation = _json(RECEIPT)["software_validation"]
    _assert_record(validation["junit_artifact"])
    report = ElementTree.parse(ROOT / validation["junit_artifact"]["path"])
    suite = report.getroot().find("testsuite")
    assert suite is not None
    assert int(suite.attrib["tests"]) == validation["root_tests_passed"] == 911
    assert int(suite.attrib["failures"]) == int(suite.attrib["errors"]) == 0
    assert int(suite.attrib["skipped"]) == 0


def test_cluster_success_only_collected_a_failed_launch_diagnostic():
    receipt = _json(RECEIPT)
    cluster = receipt["cluster_diagnostic"]
    for record in cluster["artifacts"]:
        _assert_record(record)
        assert record["controller_hash_and_bytes_verified"] is True
    assert cluster["collector_native_exit_code"] == 0
    assert cluster["probe_native_exit_code"] == 2
    report = _json(ROOT / cluster["artifacts"][0]["path"])
    assert report["launcher"]["which_path"] == "/snap/bin/docker"
    assert report["launcher"]["real_path"] == "/usr/bin/snap"
    assert report["client_version"]["exit_code"] == 1
    assert report["docker_info"]["exit_code"] == 1
    assert report["diagnosis"] == "launcher_profile_restriction_before_daemon_query"
    for name in (
        "ready",
        "service_lifecycle_changed",
        "gazebo_verified",
        "flight_verified",
        "image_inspection_performed",
        "training_ready",
        "final_release_allowed",
    ):
        assert report[name] is False


def test_local_docker_client_success_does_not_upgrade_timed_out_daemon():
    receipt = _json(RECEIPT)
    local = receipt["local_diagnostic"]
    _assert_record(local["artifact"])
    assert local["probe_native_exit_code"] == 2
    assert local["collector_native_exit_code"] == 0
    report = _json(ROOT / local["artifact"]["path"])
    assert report["client_version"]["exit_code"] == 0
    assert report["docker_info"]["timed_out"] is True
    assert report["docker_info"]["exit_code"] is None
    assert report["diagnosis"] == "daemon_query_timed_out_state_unknown"
    assert report["ready"] is False
    assert report["service_lifecycle_changed"] is False


def test_progress_points_to_exact_new_receipt_and_keeps_actual_gates_false():
    progress = _json(ROOT / "docs/current-progress.json")
    entry = progress["onboard_continuation_development_20261007"]
    _assert_record(
        {
            "path": entry["evidence"],
            "bytes": entry["evidence_bytes"],
            "sha256": entry["evidence_sha256"],
        }
    )
    assert entry["continuation_compiler_implemented"] is True
    assert entry["host_replacement_transport_implemented"] is False
    assert entry["actual_flight_verified"] is False
    assert entry["final_release_allowed"] is False


def test_document_explains_segments_without_claiming_task_completion_or_hardware():
    document = (ROOT / "docs/onboard-continuation.md").read_text(encoding="utf-8")
    assert "航段完成" in document
    assert "不等于任务完成" in document
    assert "不是已经接通" in document
    assert "MIS_TKO_LAND_REQ" in document
    assert "不是经 Jetson" in document
