from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.run_simulator_3d_control_acceptance as acceptance_module
from scripts.run_simulator_3d_control_acceptance import main, run_acceptance
from scripts.verify_simulator_3d_control_evidence import main as verify_3d_main

ROOT = Path(__file__).resolve().parents[1]
FIXED_WING_MANIFEST = ROOT / "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json"
MULTIROTOR_MANIFEST = ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json"
V56_EVIDENCE_DIR = ROOT / "artifacts/evaluation/terminal-continuation-v56"
V56_VTOL_EVIDENCE = (
    V56_EVIDENCE_DIR / "simulator-3d-control-v56-camera-frame-vtol-pre8.json"
)
V56_FIXED_WING_EVIDENCE = (
    V56_EVIDENCE_DIR / "simulator-3d-control-v56-camera-frame-fixed-wing-pre8.json"
)
V56_MULTIROTOR_EVIDENCE = (
    V56_EVIDENCE_DIR / "simulator-3d-control-v56-camera-frame-multirotor-pre8.json"
)


def test_simulator_3d_control_gate_proves_shared_manual_ai_path() -> None:
    result = run_acceptance()
    assert result["product"] == "Multi-Detect"
    assert result["release_label"].startswith("0.3.0-pre.")
    assert result["runtime_mode"] == "sim"
    assert result["gate_passed"] is True
    assert result["claims"]["manual_and_ai_share_authority_path"] is True
    assert result["claims"]["native_qgc_interactive_path"] is True
    assert result["claims"]["qgc_is_sole_operator_ui"] is True
    assert result["claims"]["visual_acceptance"] == "not_claimed"
    assert result["three_d_runtime"]["world_name"] == "default"
    assert result["three_d_runtime"]["wgs84"]["latitude_deg"] == 1.3
    assert result["interactive_qgc_path"]["gate_passed"] is True
    assert result["stack_plan_sha256"] == result["stack_plan"]["sha256"]
    assert len(result["stack_plan_sha256"]) == 64
    assert result["stack_plan"]["event"] == "simulator_stack_plan"
    assert result["stack_plan"]["scenario_id"] == result["scenario"]["scenario_id"]
    assert (
        result["stack_plan"]["scenario_manifest_hash"]
        == result["scenario"]["manifest_canonical_sha256"]
    )
    assert result["stack_plan"]["repository"]["truth_loaded"] is False
    assert result["stack_plan"]["truth_free"] is True
    assert result["stack_plan_binding"]["gate_passed"] is True
    assert result["stack_plan_binding"]["dry_run"] is True
    assert result["control_contract"]["gate_passed"] is True
    assert result["deterministic_gate_passed"] is True
    assert result["external_runtime_gate_passed"] is False
    assert result["claims"]["stack_plan_hash_bound"] is True
    assert result["claims"]["manual_control_evidence"] is True
    assert result["claims"]["ai_evidence"] is True
    assert result["claims"]["watchdog_evidence"] is True
    assert result["claims"]["px4_gazebo_acceptance"] == "not_claimed"

    # The launch planner is deliberately a dry-run preflight here.  A missing
    # PX4/Gazebo installation may make readiness false, but must never be
    # serialized as a real 3-D/visual acceptance.
    readiness = result["launch_readiness"]
    assert readiness["event"] == "simulator_3d_runtime_readiness"
    assert readiness["stack_plan"]["dry_run"] == result["stack_plan"]["runtime"]["dry_run"]
    assert readiness["px4_launch"]["dry_run"] is True
    assert readiness["px4_launch"]["dry_run_blocks_external_gate"] is True
    assert readiness["px4_launch"]["external_process_started"] is False
    assert readiness["px4_launch"]["external_3d_acceptance"] == "not_claimed"
    assert readiness["external_runtime"]["acceptance"] == "not_claimed"
    assert readiness["external_runtime"]["process_started"] is False
    assert readiness["external_runtime"]["dry_run_blocks_external_gate"] is True
    assert result["gate_scope"] == "deterministic_protocol_asset_preflight"
    assert result["external_runtime_required_for_real_3d"] is True

    contract = result["control_contract"]
    assert contract["deterministic_fixture"] is True
    assert contract["manual"]["accepted_frame_count"] == 1
    assert contract["manual"]["forwarded_to_px4_count"] == 1
    assert contract["manual"]["gate_passed"] is True
    assert contract["ai"]["accepted_step_count"] >= 1
    assert contract["ai"]["suppressed_step_count"] >= 1
    assert contract["ai"]["gate_passed"] is True
    assert contract["watchdog"]["tick_count"] == 1
    assert contract["watchdog"]["neutral_frame_count"] == 1
    assert contract["watchdog"]["gate_passed"] is True
    assert contract["qgc_only"]["sole_operator_ui"] is True
    assert contract["qgc_only"]["independent_simulator_window_created"] is False
    assert contract["qgc_only"]["gate_passed"] is True


def test_simulator_3d_control_gate_selects_fixed_wing_assets_and_mode() -> None:
    result = run_acceptance(FIXED_WING_MANIFEST)
    assert result["gate_passed"] is True
    assert result["scenario"]["scenario_id"] == "coastal_patrol_fixed_wing_001"
    assert result["scenario"]["vehicle"]["model"] == "px4_fixed_wing_standard"
    assert result["three_d_runtime"]["mode"] == "fixed-wing"
    assert result["three_d_runtime"]["world_name"] == "default"
    assert result["three_d_runtime"]["model_dependencies"] == [
        "model://mono_cam",
        "model://rc_cessna",
    ]
    assert result["interactive_qgc_path"]["modes"][0]["mode"] == "fixed-wing"
    assert result["stack_plan"]["vehicle"]["class"] == "fixed-wing"
    assert result["stack_plan"]["vehicle_model"] == "px4_fixed_wing_standard"
    assert result["control_contract"]["vehicle_mode"] == "fixed-wing"
    assert result["control_contract"]["manual"]["authority_takeover"] is True
    assert result["control_contract"]["ai"]["resumed_after_release"] is True
    assert result["control_contract"]["watchdog"]["neutral_frame"] is True
    assert (
        result["launch_readiness"]["px4_launch"]["stack_plan_sha256"]
        == (result["stack_plan"]["sha256"])
    )
    assert (
        result["launch_readiness"]["px4_launch"]["scenario_manifest_sha256"]
        == (result["scenario"]["manifest_canonical_sha256"])
    )


def test_simulator_3d_control_gate_selects_multirotor_x500_assets_and_mode() -> None:
    result = run_acceptance(MULTIROTOR_MANIFEST)
    assert result["gate_passed"] is True
    assert result["scenario"]["vehicle"]["model"] == "px4_multirotor_standard"
    assert result["three_d_runtime"]["mode"] == "multirotor"
    assert result["three_d_runtime"]["model_dependencies"] == [
        "model://mono_cam",
        "model://x500",
    ]
    assert result["stack_plan"]["vehicle"]["class"] == "multirotor"
    assert result["stack_plan"]["vehicle_model"] == "px4_multirotor_standard"
    assert result["stack_plan"]["environment"]["PX4_SYS_AUTOSTART"] == "4001"
    assert result["control_contract"]["vehicle_mode"] == "multirotor"


def test_simulator_3d_control_gate_rejects_unknown_scenario(tmp_path: Path) -> None:
    manifest = tmp_path / "unknown.json"
    manifest.write_text('{"scenario_id":"unknown"}', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported 3-D scenario identity"):
        run_acceptance(manifest)


def test_simulator_3d_control_cli_writes_hash_bound_stack_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "simulator-3d.json"
    exit_code = main(
        [
            "--scenario-manifest",
            str(FIXED_WING_MANIFEST),
            "--launch-job-dir",
            str(tmp_path / "launch-job"),
            "--out",
            str(output),
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == printed
    assert written["gate_passed"] is True
    assert written["stack_plan_binding"]["gate_passed"] is True
    assert written["stack_plan_sha256"] == written["stack_plan"]["sha256"]
    assert written["stack_plan"]["runtime"]["loopback_only"] is True
    assert 14550 not in written["stack_plan"]["ports"]["all_ports"]
    assert written["px4_launch"]["plan_generated"] is True
    assert written["px4_launch"]["artifacts_verified"] is True
    assert written["launch_readiness"]["external_3d_acceptance"] == "not_claimed"


def test_simulator_3d_control_gate_reports_launch_planner_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("synthetic launch planner failure")

    monkeypatch.setattr(acceptance_module, "build_px4_launch_plan", _fail)
    result = run_acceptance(
        FIXED_WING_MANIFEST,
        launch_job_dir=tmp_path / "launch-job",
    )

    assert result["gate_passed"] is False
    assert result["deterministic_gate_passed"] is False
    launch = result["px4_launch"]
    assert launch["plan_generated"] is False
    assert launch["ready_to_launch"] is False
    assert launch["external_process_started"] is False
    assert launch["external_3d_acceptance"] == "not_claimed"
    assert "synthetic launch planner failure" in launch["error"]


def test_simulator_3d_control_cli_rejects_non_object_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "invalid.json"
    manifest.write_text("[]", encoding="utf-8")

    assert main(["--scenario-manifest", str(manifest)]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["event"] == "simulator_3d_control_acceptance_failed"
    assert "JSON object" in error["error"]


def test_simulator_3d_control_verifier_cli_binds_three_airframes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = [
        V56_VTOL_EVIDENCE,
        V56_FIXED_WING_EVIDENCE,
        V56_MULTIROTOR_EVIDENCE,
    ]
    output = tmp_path / "simulator-3d-binding.json"
    exit_code = verify_3d_main(
        [
            "--bind",
            "--expected-release-label",
            "0.3.0-pre.8",
            "--manifest-directory",
            str(ROOT / "artifacts/evaluation"),
            "--out",
            str(output),
            *(str(path) for path in evidence),
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == printed
    assert written["event"] == "simulator_3d_control_binding_verified"
    assert written["gate_passed"] is True
    assert written["modes"] == ["fixed-wing", "multirotor", "vtol"]


def test_simulator_3d_control_verifier_cli_requires_bind_for_three_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = str(V56_VTOL_EVIDENCE)
    assert verify_3d_main([evidence, evidence]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["event"] == "simulator_3d_control_verification_failed"
    assert "--bind" in error["reasons"][0]


@pytest.mark.parametrize(
    "script_name",
    (
        "run_simulator_ai_acceptance.py",
        "run_simulator_closed_loop_acceptance.py",
        "run_simulator_control_acceptance.py",
        "run_simulator_interactive_acceptance.py",
        "run_simulator_3d_control_acceptance.py",
        "verify_simulator_3d_control_evidence.py",
        "run_simulator_docker.py",
        "run_simulator_docker_acceptance.py",
        "run_simulator_docker_ai_acceptance.py",
    ),
)
def test_documented_simulator_scripts_run_without_pythonpath(script_name: str) -> None:
    """A clean checkout must support the README's direct script form.

    The wrappers add only this repository's ``src`` directory when executed
    as files.  Clearing PYTHONPATH here catches accidental dependence on the
    developer's editable install or shell profile before a user reaches the
    actual acceptance command.
    """

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "--help" in completed.stdout


def test_three_d_control_module_runs_as_a_direct_python_module() -> None:
    """The package entry point must expose the verifier CLI as documented."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "multidetect.simulator.three_d_control", "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "--expected-release-label" in completed.stdout
    assert "RuntimeWarning" not in completed.stderr
