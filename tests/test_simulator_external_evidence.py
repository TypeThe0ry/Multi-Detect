from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from multidetect.simulator_external_evidence import (
    REQUIRED_VEHICLES,
    SimulatorExternalEvidenceError,
    build_simulator_external_evidence,
    main,
    revalidate_simulator_external_evidence,
    write_simulator_external_evidence,
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _vehicle_names(vehicle: str) -> tuple[str, str, str]:
    if vehicle == "vtol":
        return "river_rescue_vtol_001", "px4_vtol_standard", "px4_vtol_gazebo_v1"
    if vehicle == "fixed-wing":
        return (
            "coastal_patrol_fixed_wing_001",
            "px4_fixed_wing_standard",
            "px4_fixed_wing_gazebo_v1",
        )
    return "urban_inspection_multirotor_001", "px4_multirotor_standard", "px4_multirotor_gazebo_v1"


def _common(vehicle: str, *, ai: bool = False, image: str | None = None) -> dict[str, object]:
    scenario_id, model, airframe = _vehicle_names(vehicle)
    manifest_hash = _sha(f"manifest:{vehicle}")
    image = image or "px4io/px4-sitl-gazebo@sha256:" + _sha("image")
    document: dict[str, object] = {
        "schema": "multidetect.simulator-docker-acceptance",
        "schema_version": 1,
        "release_status": "v0.3.0-pre.8; final v0.3.0 remains blocked",
        "execution_requested": True,
        "gate_passed": True,
        "preflight_gate_passed": True,
        "runtime_gate_passed": True,
        "plan": {
            "scenario_id": scenario_id,
            "scenario_manifest_hash": manifest_hash,
            "truth_free": True,
            "image": image,
            "vehicle": {"model": model, "model_name": airframe},
        },
        "scenario": {
            "scenario_id": scenario_id,
            "vehicle_model": model,
            "airframe_profile_id": airframe,
            "scenario_manifest_hash": manifest_hash,
            "manifest_sha256": _sha(f"scenario:{vehicle}"),
            "truth_loaded_by_runtime": False,
        },
        "container_runtime": {
            "image": image,
            "external_process_started": True,
            "cleanup": {"attempted": True, "removed": True, "errors": []},
        },
        "hardware": {"status": "not_claimed", "hardware_control_enabled": False},
        "safety": {
            "truth_free": True,
            "hardware_control_enabled": False,
            "real_pixhawk_contacted": False,
        },
        "telemetry": {
            "qualified": True,
            "gps": {"fix_type": 3, "satellites_visible": 10},
        },
    }
    if ai:
        document.update(
            {
                "ai_policy_gate_passed": True,
                "target_policy_gate_passed": True,
                "ai_steps": 2,
                "ai_policy_steps": 2,
                "ai": {"armed_observed": True},
                "target_exercise": {
                    "accepted": True,
                    "truth_free": True,
                    "visual_accuracy": "not_claimed",
                    "decision": {"action": "track"},
                },
            }
        )
    return document


def _write_inputs(tmp_path: Path) -> tuple[dict[str, Path], dict[str, Path], Path]:
    runtime: dict[str, Path] = {}
    ai: dict[str, Path] = {}
    for vehicle in REQUIRED_VEHICLES:
        runtime_path = tmp_path / f"{vehicle}-runtime.json"
        ai_path = tmp_path / f"{vehicle}-ai.json"
        runtime_path.write_text(json.dumps(_common(vehicle)), encoding="utf-8")
        ai_path.write_text(json.dumps(_common(vehicle, ai=True)), encoding="utf-8")
        runtime[vehicle] = runtime_path
        ai[vehicle] = ai_path
    parity_path = tmp_path / "release-parity.json"
    parity_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0-pre.8",
                "parity": {
                    field: _sha(field)
                    for field in (
                        "ui_contract_hash",
                        "core_algorithm_hash",
                        "task_contract_hash",
                        "safety_policy_hash",
                        "px4_mission_path_hash",
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    return runtime, ai, parity_path


def test_external_evidence_joins_three_vehicles_and_parity(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is True
    assert result["release_label"] == "0.3.0-pre.8"
    assert result["shared"]["image_digest_consistent"] is True
    assert result["shared"]["truth_free_perception"] is True
    assert result["claims"]["hardware_acceptance"] == "not_claimed"
    assert set(result["vehicles"]) == set(REQUIRED_VEHICLES)
    assert all(
        result["vehicles"][vehicle]["ai"]["target_exercise_accepted"]
        for vehicle in REQUIRED_VEHICLES
    )


def test_external_evidence_rejects_cross_vehicle_image_drift(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    drifted = copy.deepcopy(
        _common("multirotor", ai=True, image="px4io/other@sha256:" + _sha("other"))
    )
    ai["multirotor"].write_text(json.dumps(drifted), encoding="utf-8")

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is False
    assert "all external evidence must use one digest-pinned image" in result["reasons"]


def test_external_evidence_rejects_reused_runtime_as_ai_source(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    ai["vtol"] = runtime["vtol"]

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is False
    assert "vtol: runtime and AI evidence must be distinct files" in result["reasons"]


def test_external_evidence_rejects_cross_vehicle_source_reuse(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    ai["fixed-wing"] = runtime["vtol"]

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is False
    assert (
        "fixed-wing AI: source file is reused by vtol runtime; six runtime/AI "
        "evidence files must be distinct" in result["reasons"]
    )


def test_external_evidence_rejects_malformed_image_digest(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    malformed = _common("vtol", image="px4io/px4-sitl-gazebo@sha256:not-a-digest")
    runtime["vtol"].write_text(json.dumps(malformed), encoding="utf-8")

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is False
    assert "vtol runtime: plan.image must be a digest-pinned image" in result["reasons"]


def test_external_evidence_requires_a_release_label_in_every_input(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    missing = _common("fixed-wing")
    missing.pop("release_status")
    runtime["fixed-wing"].write_text(json.dumps(missing), encoding="utf-8")

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is False
    assert (
        "fixed-wing runtime: release_status must contain a valid release label" in result["reasons"]
    )


def test_external_evidence_accepts_final_release_label(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    for vehicle in REQUIRED_VEHICLES:
        runtime_document = json.loads(runtime[vehicle].read_text(encoding="utf-8"))
        ai_document = json.loads(ai[vehicle].read_text(encoding="utf-8"))
        runtime_document["release_status"] = "v0.3.0; final release candidate"
        ai_document["release_status"] = "v0.3.0; final release candidate"
        runtime[vehicle].write_text(json.dumps(runtime_document), encoding="utf-8")
        ai[vehicle].write_text(json.dumps(ai_document), encoding="utf-8")
    parity_document = json.loads(parity.read_text(encoding="utf-8"))
    parity_document["release_label"] = "0.3.0"
    parity.write_text(json.dumps(parity_document), encoding="utf-8")

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is True
    assert result["release_label"] == "0.3.0"


def test_external_evidence_rejects_invalid_source_manifest_hash(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    invalid = _common("multirotor")
    invalid["scenario"]["manifest_sha256"] = "invalid"  # type: ignore[index]
    runtime["multirotor"].write_text(json.dumps(invalid), encoding="utf-8")

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is False
    assert "multirotor runtime: scenario.manifest_sha256 must be SHA-256" in result["reasons"]


def test_external_evidence_rejects_unexercised_target(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    document = _common("vtol", ai=True)
    document["target_exercise"]["accepted"] = False  # type: ignore[index]
    ai["vtol"].write_text(json.dumps(document), encoding="utf-8")

    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)

    assert result["gate_passed"] is False
    assert "vtol AI: target_exercise.accepted must be true" in result["reasons"]


def test_external_evidence_requires_exact_vehicle_set(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    runtime.pop("vtol")

    with pytest.raises(SimulatorExternalEvidenceError, match="runtime evidence keys"):
        build_simulator_external_evidence(runtime, ai, parity_path=parity)


def test_external_evidence_rejects_casefolded_duplicate_vehicle_keys(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    runtime["VTOL"] = runtime["vtol"]

    with pytest.raises(SimulatorExternalEvidenceError, match="duplicate runtime evidence key"):
        build_simulator_external_evidence(runtime, ai, parity_path=parity)


def test_external_evidence_writer_is_replayable(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    result = build_simulator_external_evidence(runtime, ai, parity_path=parity)
    output = tmp_path / "evidence.json"

    written = write_simulator_external_evidence(output, result)

    assert written == output.resolve()
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["event"] == "simulator_external_runtime_ai_evidence_verified"
    assert loaded["gate_passed"] is True


def test_external_evidence_revalidation_recomputes_all_sources(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    document = build_simulator_external_evidence(runtime, ai, parity_path=parity)
    join = tmp_path / "join.json"
    write_simulator_external_evidence(join, document)

    result = revalidate_simulator_external_evidence(join, expected_release_label="0.3.0-pre.8")

    assert result["event"] == "simulator_external_runtime_ai_evidence_revalidated"
    assert result["source_recomputed"] is True
    assert result["gate_passed"] is True
    assert result["report_bytes"] == join.stat().st_size
    assert len(result["report_sha256"]) == 64


def test_external_evidence_revalidation_rejects_edited_summary(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    document = build_simulator_external_evidence(runtime, ai, parity_path=parity)
    document["vehicles"]["vtol"]["runtime"]["bytes"] += 1  # type: ignore[index]
    join = tmp_path / "edited-join.json"
    join.write_text(json.dumps(document), encoding="utf-8")

    result = revalidate_simulator_external_evidence(join)

    assert result["source_recomputed"] is True
    assert result["gate_passed"] is False
    assert "join: stored summary does not match source recomputation" in result["reasons"]


def test_external_evidence_revalidation_rejects_missing_source(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    document = build_simulator_external_evidence(runtime, ai, parity_path=parity)
    document["vehicles"]["fixed-wing"]["ai"]["path"] = str(tmp_path / "gone.json")  # type: ignore[index]
    join = tmp_path / "missing-source-join.json"
    join.write_text(json.dumps(document), encoding="utf-8")

    result = revalidate_simulator_external_evidence(join)

    assert result["source_recomputed"] is False
    assert result["gate_passed"] is False
    assert any("fixed-wing ai source does not exist" in reason for reason in result["reasons"])


def test_external_evidence_cli_can_revalidate_join(tmp_path: Path) -> None:
    runtime, ai, parity = _write_inputs(tmp_path)
    document = build_simulator_external_evidence(runtime, ai, parity_path=parity)
    join = tmp_path / "join.json"
    output = tmp_path / "revalidation.json"
    write_simulator_external_evidence(join, document)

    exit_code = main(
        [
            "--join",
            str(join),
            "--expected-release-label",
            "0.3.0-pre.8",
            "--out",
            str(output),
        ]
    )

    assert exit_code == 0
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["event"] == "simulator_external_runtime_ai_evidence_revalidated"
    assert loaded["gate_passed"] is True
