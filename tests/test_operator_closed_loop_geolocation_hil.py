from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_link import (
    SelectionAction,
    TargetGeolocationStatusMessage,
    TargetSelectionCommand,
    VideoGeometry,
)
from multidetect.operator_protocol import OperatorTunnelCodec, WireMessageType

ROOT = Path(__file__).resolve().parents[1]
DRIVER = (
    ROOT
    / "ground-control"
    / "MultiDetectGCS"
    / "custom"
    / "tests"
    / "operator_closed_loop_hil.py"
)


def _driver_module():
    spec = importlib.util.spec_from_file_location("multidetect_operator_closed_loop_hil", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evidence() -> dict[str, object]:
    return {
        "schema": "multidetect.gazebo-target-geolocation-acceptance",
        "schema_version": 2,
        "gate_passed": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "production_approved": False,
        "evidence_sha256": "a" * 64,
        "deterministic_result_sha256": "b" * 64,
        "scenario": {"scenario_id": "gazebo_geolocation_fixture"},
        "release_identity": {"release_label": "0.3.0-pre.8"},
        "frames": [
            {
                "frame_id": "gazebo-sim-000004",
                "captured_at_s": 638093.25,
                "detections": [{"label": "car"}],
                "predicted_target": {
                    "latitude_deg": 1.3000009803188122,
                    "longitude_deg": 103.80074105068257,
                    "horizontal_sigma_m": 3.715348170699058,
                },
                "range_solution": {
                    "validity": "degraded",
                    "reasons": ["single_absolute_range_method"],
                    "sources": ["dem_gps", "camera_ground"],
                    "rejected_sources": [],
                    "slant_range_m": 33.26982119221498,
                    "ground_range_m": 26.826997727485544,
                    "slant_range_ci95_m": [25.045155644421335, 41.49448674000862],
                    "ground_range_ci95_m": [20.235114591117895, 33.41888086385319],
                    "relative_bearing_deg": -0.23307963865627812,
                    "absolute_bearing_deg": 89.76848424186717,
                    "bearing_sigma_deg": 1.052081231048326,
                    "north_offset_m": 0.10839988335815363,
                    "east_offset_m": 26.826778720819288,
                    "data_freshness_s": 0.0,
                    "sensor_consistency": 0.5,
                    "fusion_profile": "outdoor-multimodal-v1",
                    "vehicle_profile": "multirotor",
                    "navigation_state": "gps-aided",
                    "motion_regime": "stationary",
                    "advisory_only": True,
                    "flight_control_enabled": False,
                    "physical_release_enabled": False,
                },
            }
        ],
    }


def test_passed_gazebo_evidence_is_rebased_into_source_bound_qgc_statuses(tmp_path: Path) -> None:
    module = _driver_module()
    evidence_path = tmp_path / "geolocation.json"
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")
    evidence = module._load_target_geolocation_evidence(str(evidence_path), "0.3.0-pre.8")
    assert evidence is not None

    geometry = VideoGeometry("camera-main", 1280, 720, 0)
    now = time.time()
    selection = TargetSelectionCommand(
        command_id="11111111-1111-4111-8111-111111111111",
        session_id="hil-session",
        sequence=1,
        action=SelectionAction.SELECT,
        geometry=geometry,
        issued_at_s=now,
        expires_at_s=now + 5.0,
        bbox=BoundingBox(0.3, 0.2, 0.6, 0.7),
        displayed_frame_id="display-frame",
    )
    codec = OperatorTunnelCodec(hmac_key=b"x" * 32, geometries=(geometry,))
    payloads, _challenge = module._status_payloads(codec, geometry, selection, evidence)
    decoded = [codec.decode(payload) for payload in payloads]

    range_packets = [
        packet for packet in decoded if packet.message_type is WireMessageType.RANGE_STATUS
    ]
    geolocation_packets = [
        packet
        for packet in decoded
        if packet.message_type is WireMessageType.TARGET_GEOLOCATION_STATUS
    ]
    assert len(range_packets) == 1
    assert len(geolocation_packets) == 1
    range_status = range_packets[0].message
    geolocation = geolocation_packets[0].message
    assert isinstance(geolocation, TargetGeolocationStatusMessage)
    # The compact wire format deliberately hashes identifiers; equality across
    # the two status types proves the native QGC source-binding contract.
    assert range_status.target_id == geolocation.target_id
    assert range_status.target_id.startswith("hash64:")
    assert range_status.source_frame_id == geolocation.source_frame_id
    assert range_status.source_frame_id.startswith("hash64:")
    assert range_status.motion_regime == "static"
    assert range_status.ground_range_m == pytest.approx(26.826997727485544, abs=0.051)
    assert geolocation.latitude_deg == pytest.approx(1.3000009803188122, abs=1e-7)
    assert geolocation.longitude_deg == pytest.approx(103.80074105068257, abs=1e-7)
    assert geolocation.horizontal_sigma_m == pytest.approx(3.715348170699058, abs=0.02)
    assert geolocation.advisory_only is True
    assert geolocation.flight_control_enabled is False
    assert geolocation.physical_release_enabled is False


def test_unpassed_geolocation_evidence_is_rejected_before_hil_transport(tmp_path: Path) -> None:
    module = _driver_module()
    evidence = _evidence()
    evidence["gate_passed"] = False
    evidence_path = tmp_path / "failed-geolocation.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(module.HilFailure, match="has not passed"):
        module._load_target_geolocation_evidence(str(evidence_path))


def test_wrong_release_or_non_display_range_evidence_is_rejected(tmp_path: Path) -> None:
    module = _driver_module()
    evidence = _evidence()
    evidence_path = tmp_path / "wrong-release.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(module.HilFailure, match="release label"):
        module._load_target_geolocation_evidence(str(evidence_path), "0.3.0-pre.9")

    frames = evidence["frames"]
    assert isinstance(frames, list)
    frame = frames[0]
    assert isinstance(frame, dict)
    ranging = frame["range_solution"]
    assert isinstance(ranging, dict)
    ranging["flight_control_enabled"] = True
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(module.HilFailure, match="display-only"):
        module._load_target_geolocation_evidence(str(evidence_path), "0.3.0-pre.8")


def test_geolocation_evidence_must_match_supplied_canonical_parity(tmp_path: Path) -> None:
    module = _driver_module()
    evidence = _evidence()
    parity = {
        "ui_contract_hash": "1" * 64,
        "core_algorithm_hash": "2" * 64,
        "task_contract_hash": "3" * 64,
        "safety_policy_hash": "4" * 64,
        "px4_mission_path_hash": "5" * 64,
    }
    release = evidence["release_identity"]
    assert isinstance(release, dict)
    release["parity"] = parity
    evidence_path = tmp_path / "geolocation.json"
    parity_path = tmp_path / "release-parity.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    parity_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_label": "0.3.0-pre.8",
                "parity": parity,
            }
        ),
        encoding="utf-8",
    )

    loaded = module._load_target_geolocation_evidence(
        str(evidence_path), "0.3.0-pre.8", str(parity_path)
    )
    assert loaded is not None
    assert loaded["expected_release_parity"]["file_path"] == str(parity_path.resolve())

    parity["ui_contract_hash"] = "9" * 64
    parity_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_label": "0.3.0-pre.8",
                "parity": parity,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.HilFailure, match="canonical release parity"):
        module._load_target_geolocation_evidence(
            str(evidence_path), "0.3.0-pre.8", str(parity_path)
        )
