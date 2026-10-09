from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.simulator.scenario_matrix import (
    SimulatorScenarioMatrixError,
    discover_scenario_manifests,
    run_scenario_matrix,
    verify_scenario_matrix_document,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "configs" / "scenarios"


def test_matrix_discovers_all_airframes_and_proves_repeatability() -> None:
    result = run_scenario_matrix(SCENARIOS, frames=4, dt_s=0.1)

    assert result["gate_passed"] is True
    assert result["deterministic_gate_passed"] is True
    assert result["scenario_count"] == 3
    assert result["modes"] == ["fixed-wing", "multirotor", "vtol"]
    assert result["scenario_ids"] == [
        "coastal_patrol_fixed_wing_001",
        "river_rescue_vtol_001",
        "urban_inspection_multirotor_001",
    ]
    assert result["truth_loaded"] is False
    assert result["truth_free"] is True
    assert result["claims"]["external_3d_runtime"] == "not_claimed"
    assert len(result["sha256"]) == 64
    for item in result["scenarios"]:
        assert item["gate_passed"] is True
        assert item["completed_frames"] == 4
        assert item["evidence_hash"] == item["repeat_evidence_hash"]
        assert item["truth_loaded"] is False
        assert item["truth_free"] is True
        assert item["runtime_descriptor"]["mode"] == "sim"
        assert item["runtime_descriptor"]["deterministic"] is True
        assert item["flight_model_mode"] == item["mode"]
        assert item["ai_attempted_steps"] == 4
        assert item["ai_accepted_steps"] == 4
        assert item["ai_rejected_steps"] == 0
        assert item["control_messages_sent"] >= 4


def test_matrix_paths_and_hash_are_portable_across_checkout_roots(tmp_path: Path) -> None:
    migrated = tmp_path / "migrated-checkout"
    shutil.copytree(ROOT / "configs" / "scenarios", migrated / "configs" / "scenarios")
    shutil.copytree(
        ROOT / "simulation" / "gazebo" / "worlds",
        migrated / "simulation" / "gazebo" / "worlds",
    )

    original = run_scenario_matrix(SCENARIOS, frames=2)
    moved = run_scenario_matrix(migrated / "configs" / "scenarios", frames=2)

    assert original["sha256"] == moved["sha256"]
    assert original["scenario_directory"] == "configs/scenarios"
    assert moved["scenario_directory"] == "configs/scenarios"
    assert original["scenarios"][0]["manifest"] == moved["scenarios"][0]["manifest"]
    assert not Path(moved["scenario_directory"]).is_absolute()


def test_matrix_explicit_selection_is_stable_and_records_faults() -> None:
    fixed_wing = SCENARIOS / "coastal_patrol_fixed_wing_001.scenario.json"
    result = run_scenario_matrix(
        SCENARIOS,
        scenarios=[fixed_wing],
        frames=2,
        repeat=False,
    )

    assert result["gate_passed"] is True
    assert result["repeatability_checked"] is False
    item = result["scenarios"][0]
    assert item["scenario_id"] == "coastal_patrol_fixed_wing_001"
    assert item["repeat_evidence_hash"] is None
    assert item["active_fault_ids"] == [
        "gps-drift-001",
        "airspeed-freeze-001",
        "wind-gust-001",
        "camera-loss-001",
    ]


def test_matrix_rejects_empty_directory_and_duplicate_selection(tmp_path: Path) -> None:
    with pytest.raises(SimulatorScenarioMatrixError, match="no .*scenario.json"):
        discover_scenario_manifests(tmp_path)

    manifest = SCENARIOS / "river_rescue_vtol_001.scenario.json"
    with pytest.raises(SimulatorScenarioMatrixError, match="duplicate"):
        run_scenario_matrix(SCENARIOS, scenarios=[manifest, manifest], frames=1)


def test_matrix_no_arm_is_fail_closed_for_ai_steps() -> None:
    manifest = SCENARIOS / "river_rescue_vtol_001.scenario.json"
    result = run_scenario_matrix(
        SCENARIOS,
        scenarios=[manifest],
        frames=2,
        arm_on_start=False,
    )

    # The process-level matrix remains deterministic, but the shared model
    # intentionally stays disarmed and therefore does not claim an AI control
    # path was exercised.
    assert result["gate_passed"] is True
    assert result["claims"]["shared_ai_authority_watchdog_path"] is False
    assert result["scenarios"][0]["ai_attempted_steps"] == 2
    assert result["scenarios"][0]["ai_accepted_steps"] == 0
    assert result["scenarios"][0]["closed"] is True


def test_matrix_without_ai_does_not_claim_ai_path() -> None:
    manifest = SCENARIOS / "river_rescue_vtol_001.scenario.json"
    result = run_scenario_matrix(
        SCENARIOS,
        scenarios=[manifest],
        frames=2,
        enable_ai=False,
    )

    assert result["gate_passed"] is True
    assert result["claims"]["shared_ai_authority_watchdog_path"] is False
    assert result["scenarios"][0]["ai_attempted_steps"] == 0
    assert result["scenarios"][0]["ai_accepted_steps"] == 0


def test_matrix_cover_faults_extends_budget_and_requires_every_event() -> None:
    fixed_wing = SCENARIOS / "coastal_patrol_fixed_wing_001.scenario.json"
    result = run_scenario_matrix(
        SCENARIOS,
        scenarios=[fixed_wing],
        frames=2,
        dt_s=0.1,
        repeat=True,
        cover_faults=True,
    )

    assert result["gate_passed"] is True
    assert result["fault_coverage_required"] is True
    assert result["fault_coverage_auto_extended"] is True
    assert result["fault_recovery_required"] is True
    assert result["requested_frames_before_fault_coverage"] == 2
    assert result["requested_frames"] == 1852
    coverage = result["scenarios"][0]["fault_coverage"]
    assert coverage["declared_event_count"] == 4
    assert coverage["fired_event_count"] == 4
    assert coverage["all_declared_faults_exercised"] is True
    assert coverage["uncovered_event_ids"] == []
    recovery = result["scenarios"][0]["fault_recovery"]
    assert recovery["all_finite_faults_recovered"] is True
    assert recovery["recovered_event_count"] == 4
    assert recovery["unrecovered_event_ids"] == []


def test_matrix_fault_coverage_requirement_fails_closed_when_budget_is_short() -> None:
    fixed_wing = SCENARIOS / "coastal_patrol_fixed_wing_001.scenario.json"
    result = run_scenario_matrix(
        SCENARIOS,
        scenarios=[fixed_wing],
        frames=2,
        dt_s=0.1,
        repeat=False,
        require_fault_coverage=True,
    )

    assert result["gate_passed"] is False
    assert result["scenarios"][0]["fault_coverage"]["fired_event_count"] == 0
    assert result["scenarios"][0]["failure_reasons"]


def test_final_release_policy_rejects_nominal_matrix_and_accepts_coverage_matrix() -> None:
    fixed_wing = SCENARIOS / "coastal_patrol_fixed_wing_001.scenario.json"
    nominal = run_scenario_matrix(
        SCENARIOS,
        scenarios=[fixed_wing],
        frames=2,
        dt_s=0.1,
        repeat=True,
    )
    nominal_verification = verify_scenario_matrix_document(
        nominal,
        expected_release_label=nominal["release_label"],
        require_all_airframes=False,
        require_fault_coverage=True,
    )
    assert nominal_verification["gate_passed"] is False
    assert nominal_verification["checks"]["fault_coverage_required_for_release"] is False
    assert nominal_verification["checks"]["fault_coverage_auto_extended_for_release"] is False
    assert any(
        "final release requires fault_coverage_required=true" in reason
        for reason in nominal_verification["reasons"]
    )

    covered = run_scenario_matrix(
        SCENARIOS,
        scenarios=[fixed_wing],
        frames=2,
        dt_s=0.1,
        repeat=True,
        cover_faults=True,
    )
    covered_verification = verify_scenario_matrix_document(
        covered,
        expected_release_label=covered["release_label"],
        require_all_airframes=False,
        require_fault_coverage=True,
    )
    assert covered_verification["gate_passed"] is True
    assert covered_verification["checks"]["fault_coverage_required_for_release"] is True
    assert covered_verification["checks"]["fault_coverage_auto_extended_for_release"] is True


def test_matrix_wrapper_runs_without_pythonpath() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_simulator_scenario_matrix.py"),
            "--scenario",
            str(SCENARIOS / "river_rescue_vtol_001.scenario.json"),
            "--frames",
            "1",
            "--help",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "--scenario-directory" in completed.stdout


def test_matrix_verifier_wrapper_runs_without_pythonpath(tmp_path: Path) -> None:
    evidence = tmp_path / "matrix.json"
    document = run_scenario_matrix(SCENARIOS, frames=2)
    evidence.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_simulator_scenario_matrix.py"),
            str(evidence),
            "--expected-release-label",
            "0.3.0-pre.8",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    verified = json.loads(completed.stdout)
    assert verified["event"] == "simulator_scenario_matrix_verified"
    assert verified["gate_passed"] is True
    assert verified["scenario_count"] == 3
    assert verified["modes"] == ["fixed-wing", "multirotor", "vtol"]
    assert verified["claims"]["shared_ai_authority_watchdog_path"] is True


def test_matrix_cli_writes_same_json_as_stdout(tmp_path: Path) -> None:
    output = tmp_path / "matrix.json"
    result = run_scenario_matrix(
        SCENARIOS,
        scenarios=[SCENARIOS / "urban_inspection_multirotor_001.scenario.json"],
        frames=1,
    )
    # Keep this assertion tied to the public shape before exercising the
    # subprocess; CLI output ordering is intentionally canonical JSON.
    assert result["gate_passed"] is True

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_simulator_scenario_matrix.py"),
            "--scenario",
            str(SCENARIOS / "urban_inspection_multirotor_001.scenario.json"),
            "--frames",
            "1",
            "--out",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    stdout_document = json.loads(completed.stdout)
    file_document = json.loads(output.read_text(encoding="utf-8"))
    assert stdout_document == file_document
    assert stdout_document["gate_passed"] is True
