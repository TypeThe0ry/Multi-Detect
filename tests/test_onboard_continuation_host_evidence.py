"""Bind development receipts without upgrading kernel IPC to flight proof."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/evidence/onboard-continuation-host-development-20261007-r1.json"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _record(record):
    path = ROOT / record["path"]
    assert path.stat().st_size == record["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
    return path


def test_host_development_receipt_binds_sources_and_preserves_previous_seals():
    receipt = _json(RECEIPT)
    for record in receipt["sources"] + receipt["baselines"]:
        _record(record)
    prior = _json(_record(receipt["prior_receipt"]))
    for record in prior["sources"] + prior["baselines"]:
        _record(record)


def test_host_development_receipt_keeps_model_runtime_and_flight_gates_separate():
    receipt = _json(RECEIPT)
    assert receipt["release_label"] == "0.3.0-pre.8"
    for name in (
        "development_host_wire_implemented",
        "original_authoring_producer_implemented",
        "kernel_udp_emulation_verified",
        "generation_cancellation_software_verified",
        "original_raw_fact_expiry_software_verified",
    ):
        assert receipt[name] is True
    for name in (
        "whole_sortie_live_dispatcher_connected",
        "actual_px4_gazebo_continuation_verified",
        "command_causality_proven",
        "real_source_registry_authenticated",
        "live_route_clearance_producer_implemented",
        "durable_pre_pause_source_consumption_verified",
        "repeated_parent_chain_verified",
        "hold_watchdog_flight_return_verified",
        "native_vlm_generation_performed",
        "flight_action_training_performed",
        "world_model_local_inference_measured",
        "onboard_policy_reference_changed",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert receipt[name] is False


def test_host_test_counts_match_original_native_reports_without_counting_frames_as_tests():
    receipt = _json(RECEIPT)
    for name in ("software_validation", "kernel_udp_validation"):
        validation = receipt[name]
        suite = ElementTree.parse(_record(validation["junit_artifact"])).getroot().find("testsuite")
        assert suite is not None
        assert int(suite.attrib["tests"]) == validation["tests_passed"]
        assert validation["native_exit_code"] == 0
        assert int(suite.attrib["failures"]) == int(suite.attrib["errors"]) == 0
        assert int(suite.attrib["skipped"]) == 0


def test_kernel_udp_transcripts_reopen_exact_raw_bytes_but_do_not_prove_flight():
    receipt = _json(RECEIPT)
    root = ElementTree.parse(_record(receipt["kernel_udp_validation"]["junit_artifact"])).getroot()
    properties = root.findall(".//property")
    mappings = [
        json.loads(item.attrib["value"])
        for item in properties
        if "udp_session" in item.attrib["name"]
    ]
    assert len(mappings) == receipt["kernel_udp_validation"]["raw_session_properties"] > 0
    raw_packets = 0
    resumed = 0
    for mapping in mappings:
        wire = mapping.get("wire", mapping)
        assert wire["flight_acceptance_proven"] is False
        assert wire["command_causality_proven"] is False
        if "actual_flight_verified" in mapping:
            assert mapping["actual_flight_verified"] is False
        resumed += int(wire["state"] == "resumed")
        for packet in wire["receipts"]:
            raw = base64.b64decode(packet["wire_bytes_base64"], validate=True)
            assert len(raw) == packet["wire_bytes"]
            assert hashlib.sha256(raw).hexdigest() == packet["wire_sha256"]
            assert packet["source_system_id"] == packet["source_component_id"] == 1
            raw_packets += 1
    assert raw_packets == receipt["kernel_udp_validation"]["raw_received_packets"]
    assert resumed == receipt["kernel_udp_validation"]["receiver_confirmed_resumes"] > 0


def test_latest_runtime_probe_remains_unknown_and_previous_native_failure_is_retained():
    receipt = _json(RECEIPT)
    diagnostic = receipt["local_runtime_diagnostic"]
    report = _json(_record(diagnostic["artifact"]))
    assert diagnostic["probe_native_exit_code"] == 2
    assert report["client_version"]["exit_code"] == 0
    assert report["docker_info"]["timed_out"] is True
    assert report["diagnosis"] == "daemon_query_timed_out_state_unknown"
    assert report["ready"] is report["image_inspection_performed"] is False
    assert report["service_lifecycle_changed"] is False
    profile = receipt["retained_pre_fix_profile"]
    path = Path(profile["absolute_path"])
    assert path.stat().st_size == profile["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == profile["sha256"]
    assert _json(path)["native_pytest_exit_code"] == profile["native_exit_code"] == 1


def test_progress_and_user_entry_link_exact_current_receipt_without_upgrading_release():
    entry = _json(ROOT / "docs/current-progress.json")[
        "onboard_continuation_host_development_20261007"
    ]
    _record(
        {
            "path": entry["evidence"],
            "bytes": entry["evidence_bytes"],
            "sha256": entry["evidence_sha256"],
        }
    )
    assert entry["kernel_udp_emulation_verified"] is True
    assert entry["actual_px4_gazebo_continuation_verified"] is False
    assert entry["final_release_allowed"] is False
    for path in ("README.md", "docs/current-progress.md", "docs/implementation-roadmap.md"):
        assert "onboard-continuation-host.md" in (ROOT / path).read_text(encoding="utf-8")
    page = (ROOT / "docs/onboard-continuation-host.md").read_text(encoding="utf-8")
    for text in ("没有飞机动力学", "不是已经完成", "不证明", "本轮没有新增训练"):
        assert text in page
