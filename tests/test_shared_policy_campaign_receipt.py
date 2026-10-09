from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from scripts.create_shared_policy_campaign_receipt import _sha256_path, build_receipt

POLICY_HASH = "a" * 64
RANKER_HASH = "b" * 64
MISSION_HASH = "c" * 64


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    campaign = tmp_path / "campaign-input.json"
    campaign.write_text('{"schema_version":1,"tasks":[]}', encoding="utf-8")
    directory = tmp_path / "campaign"
    missions = directory / "campaign-missions"
    missions.mkdir(parents=True)
    report = missions / "mission-001.json"
    report.write_text(
        json.dumps(
            {
                "gate_passed": True,
                "container": {"id": "session-1"},
                "documents": {"executable_mission": {"sha256": MISSION_HASH}},
                "shared_outcome_shadow": {
                    "outcome": {"status": "completed", "mission_hash": MISSION_HASH}
                },
            }
        ),
        encoding="utf-8",
    )
    report.with_suffix(".transaction.jsonl").write_text('{"event":"commit"}\n', encoding="utf-8")
    policy = missions / "mission-001.policy.json"
    policy.write_text(
        json.dumps(
            {
                "decision": {
                    "action": "select",
                    "task_id": "task-1",
                    "planner_revalidated": True,
                    "truth_free": True,
                    "policy_artifact_sha256": POLICY_HASH,
                },
                "ranker_sha256": RANKER_HASH,
                "mission_hash": MISSION_HASH,
                "policy_applied_to_actuator": False,
            }
        ),
        encoding="utf-8",
    )
    summary = directory / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "status": "all_tasks_completed",
                "session_id": "session-1",
                "shared_policy_used": True,
                "planner_revalidated": True,
                "policy_artifact_sha256": POLICY_HASH,
                "ranker_sha256": RANKER_HASH,
                "completed": [
                    {
                        "task_id": "task-1",
                        "mission_hash": MISSION_HASH,
                        "planner_revalidated": True,
                        "evidence_path": str(report),
                        "policy_evidence_path": str(policy),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return summary, campaign, directory / "receipt.json"


def test_receipt_binds_mission_journal_and_policy(tmp_path: Path) -> None:
    summary, campaign, output = _fixture(tmp_path)
    receipt = build_receipt(summary, campaign, output)

    assert receipt["schema"] == "multidetect.shared-policy-campaign-receipt"
    assert receipt["sortie_count"] == 1
    assert receipt["sorties"][0]["mission_hash"] == MISSION_HASH
    assert len(receipt["sorties"][0]["transaction_journal_sha256"]) == 64
    assert receipt["sorties"][0]["policy_decision_sha256"]
    assert receipt["cleanup"]["verified"] is False
    assert receipt["final_release_allowed"] is False
    assert output.is_file()


def test_receipt_rejects_policy_task_tamper(tmp_path: Path) -> None:
    summary, campaign, output = _fixture(tmp_path)
    policy_path = summary.parent / "campaign-missions" / "mission-001.policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["decision"]["task_id"] = "wrong-task"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match="policy decision 1 failed identity checks"):
        build_receipt(summary, campaign, output)


def test_receipt_binds_outer_cleanup_evidence(tmp_path: Path) -> None:
    summary, campaign, output = _fixture(tmp_path)
    mission = summary.parent / "campaign-missions" / "mission-001.json"
    journal = mission.with_suffix(".transaction.jsonl")
    policy = summary.parent / "campaign-missions" / "mission-001.policy.json"
    runtime = summary.parent / "runtime-evidence.json"
    runtime.write_text(
        json.dumps(
            {
                "schema": "multidetect.vtol-selected-campaign",
                "gate_passed": True,
                "container": {
                    "id": "session-1",
                    "cleanup": {
                        "attempted": True,
                        "cleanup_succeeded": True,
                        "removed": True,
                        "post_remove_absence_proven": True,
                        "errors": [],
                    },
                },
                "cleanup_failures": [],
                "runtime_identity": {
                    "airframe_profile_sha256": "d" * 64,
                    "descriptor_sha256": "e" * 64,
                    "provenance_sha256": "f" * 64,
                    "descriptor": {
                        "runtime_mode": "SIM",
                        "scenario_id": "fixture-scenario",
                        "scenario_manifest_sha256": "1" * 64,
                        "stack_plan_sha256": "2" * 64,
                    },
                    "provenance": {"container_id": "session-1"},
                },
                "scenario": {
                    "scenario_id_expected": "fixture-scenario",
                    "manifest_sha256": "1" * 64,
                },
                "campaign": {
                    "summary_sha256": _sha256_path(summary),
                    "verified_sorties": [
                        {
                            "task_id": "task-1",
                            "report_sha256": _sha256_path(mission),
                            "journal_sha256": _sha256_path(journal),
                            "policy_evidence_sha256": _sha256_path(policy),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    receipt = build_receipt(summary, campaign, output, runtime)

    assert receipt["cleanup"]["verified"] is True
    assert receipt["cleanup"]["post_remove_absence_proven"] is True
    assert receipt["cleanup"]["runtime_evidence_sha256"] == _sha256_path(runtime)


def _add_sensor_binding(
    summary: Path,
) -> tuple[Path, Path]:
    root = summary.parent / "sensor"
    root.mkdir()
    payload = b"sensor-frame"
    frame_file = root / "frame-0.bgr24"
    frame_file.write_bytes(payload)
    frame_hash = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "scenario_id": "receipt-fixture",
        "frame_count": 1,
        "frames": [
            {
                "frame_id": "frame-0",
                "sequence": 0,
                "file": frame_file.name,
                "bytes": len(payload),
                "sha256": frame_hash,
                "wire_payload_sha256": frame_hash,
                "raw_bytes_sha256_matches_wire_payload": True,
                "provenance": {
                    "captured_at_s": 1.0,
                    "captured_at_clock": "host_monotonic_since_collector_start",
                    "frame_telemetry": {},
                    "wire_telemetry": {},
                },
            }
        ],
        "per_frame_sensor_evidence": {
            "record_count": 1,
            "derived_ned_values_recomputed_before_seal": True,
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
        },
        "source_attribution": {"truth_loaded_by_runtime": False},
        "claims": {"per_frame_sensor_geolocation_input": "passed"},
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    manifest_path = root / "sensor-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = summary.parent / "campaign-missions" / "mission-001.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    sensor = {
        "manifest_file_sha256": _sha256_path(manifest_path),
        "manifest_file_bytes": manifest_path.stat().st_size,
        "manifest_sha256": manifest["manifest_sha256"],
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "scenario_id": "receipt-fixture",
        "sensor_binding_hash": None,
        "frame_count": 1,
        "matched_frame_count": 1,
        "matched_frame_ids": ["frame-0"],
        "raw_bytes_sha256_verified": True,
        "truth_free": True,
        "task_id": "task-1",
        "frame_id": "frame-0",
        "frame_sequence": 0,
        "frame_sha256": frame_hash,
        "frame_bytes": len(payload),
        "frame_captured_at_s": 1.0,
        "frame_captured_at_clock": "host_monotonic_since_collector_start",
        "mission_receive_window": {
            "clock": "host_monotonic_since_collector_start",
            "arm_request_host_monotonic_s": 0.0,
            "completed_host_monotonic_s": 2.0,
            "available": True,
        },
    }
    report["mission_receive_window"] = sensor["mission_receive_window"]
    report["sensor_transport"] = sensor
    report_path.write_text(json.dumps(report), encoding="utf-8")
    summary_document = json.loads(summary.read_text(encoding="utf-8"))
    summary_document["sensor_transport_used"] = True
    summary_document["completed"][0]["sensor_transport"] = sensor
    summary.write_text(json.dumps(summary_document), encoding="utf-8")
    frame_map = summary.parent / "sensor-frame-map.json"
    frame_map.write_text(json.dumps({"task-1": "frame-0"}), encoding="utf-8")
    return manifest_path, frame_map


def test_receipt_binds_sensor_frame_to_mission_window(tmp_path: Path) -> None:
    summary, campaign, output = _fixture(tmp_path)
    manifest, frame_map = _add_sensor_binding(summary)

    receipt = build_receipt(
        summary,
        campaign,
        output,
        sensor_transport_manifest_path=manifest,
        sensor_frame_map_path=frame_map,
    )

    assert receipt["sensor_transport_used"] is True
    assert receipt["sensor_transport"]["frame_count"] == 1
    assert receipt["sorties"][0]["sensor_transport"]["frame_id"] == "frame-0"


def test_receipt_rejects_tampered_sensor_bytes(tmp_path: Path) -> None:
    summary, campaign, output = _fixture(tmp_path)
    manifest, frame_map = _add_sensor_binding(summary)
    (manifest.parent / "frame-0.bgr24").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="sensor transport input is invalid"):
        build_receipt(
            summary,
            campaign,
            output,
            sensor_transport_manifest_path=manifest,
            sensor_frame_map_path=frame_map,
        )
