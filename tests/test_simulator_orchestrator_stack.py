from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from multidetect.simulator import (
    SimulatorOrchestrator,
    SimulatorOrchestratorError,
    SimulatorProcessSpec,
    SimulatorStackPorts,
    build_stack_plan,
)
from multidetect.simulator.isolation import ToolCapability

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.bind(("127.0.0.1", 0))
        return int(channel.getsockname()[1])


def test_prepare_persists_hash_bound_stack_plan_and_result(tmp_path: Path) -> None:
    orchestrator = SimulatorOrchestrator(SCENARIO, dry_run=True, job_root=tmp_path)

    result = orchestrator.start()

    assert orchestrator.stack_plan is not None
    assert result.stack_plan["event"] == "simulator_stack_plan"
    assert result.stack_plan["sha256"] == orchestrator.stack_plan.evidence_hash
    stack_path = result.job_dir / "stack-plan.json"
    stack_document = json.loads(stack_path.read_text(encoding="utf-8"))
    assert stack_document["sha256"] == orchestrator.stack_plan.evidence_hash
    assert stack_document["ports"]["px4_control_port"] == 14540
    job_document = json.loads((result.job_dir / "job.json").read_text(encoding="utf-8"))
    assert job_document["stack_plan_enabled"] is True
    assert job_document["stack_plan_sha256"] == orchestrator.stack_plan.evidence_hash
    result_document = json.loads((result.job_dir / "result.json").read_text(encoding="utf-8"))
    assert result_document["stack_plan_sha256"] == orchestrator.stack_plan.evidence_hash


def test_prepare_persists_job_local_px4_launch_plan_and_result(tmp_path: Path) -> None:
    orchestrator = SimulatorOrchestrator(SCENARIO, dry_run=True, job_root=tmp_path)

    result = orchestrator.start()

    assert orchestrator.px4_launch_enabled is True
    assert orchestrator.px4_launch_plan is not None
    assert result.px4_launch["event"] == "simulator_px4_launch_plan"
    assert result.px4_launch["readiness"]["gate_passed"] is False
    launch_path = result.job_dir / "px4-launch.json"
    assert launch_path.is_file()
    launch_document = json.loads(launch_path.read_text(encoding="utf-8"))
    assert launch_document["sha256"] == orchestrator.px4_launch_plan.evidence_hash
    assert launch_document["artifacts"]["source_checkout_modified"] is False
    job_document = json.loads((result.job_dir / "job.json").read_text(encoding="utf-8"))
    assert job_document["px4_launch_enabled"] is True
    assert job_document["px4_launch_sha256"] == orchestrator.px4_launch_plan.evidence_hash


def test_px4_launch_can_be_explicitly_disabled_for_legacy_stack(tmp_path: Path) -> None:
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        job_root=tmp_path,
        px4_launch_enabled=False,
    )

    result = orchestrator.start()

    assert orchestrator.px4_launch_plan is None
    assert result.px4_launch["event"] == "simulator_px4_launch_disabled"
    assert result.px4_launch["enabled"] is False
    assert (result.job_dir / "px4-launch.json").is_file()


def test_stack_planner_environment_is_attached_to_default_process_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import multidetect.simulator.orchestrator as orchestrator_module

    monkeypatch.setattr(
        orchestrator_module,
        "discover_simulator_tools",
        lambda: {
            "px4": ToolCapability("px4", "px4", True),
            "gazebo": ToolCapability("gazebo", "gz", True),
            "docker": ToolCapability("docker", None, False),
        },
    )
    orchestrator = SimulatorOrchestrator(SCENARIO, dry_run=True, job_root=tmp_path)

    assert orchestrator.stack_plan is not None
    assert [item.name for item in orchestrator.process_specs] == ["px4", "gazebo"]
    px4, gazebo = orchestrator.process_specs
    assert px4.command == orchestrator.stack_plan.px4_command
    assert gazebo.command == orchestrator.stack_plan.gazebo_command
    assert px4.env is not None
    assert px4.env["MD_SIM_SCENARIO_ID"] == "river_rescue_vtol_001"
    assert gazebo.env == px4.env


def test_custom_process_specs_remain_compatible_when_stack_plan_fails(
    tmp_path: Path,
) -> None:
    qgc_port = _free_udp_port()
    px4_port = _free_udp_port()
    spec = SimulatorProcessSpec("fake", ("fake-simulator",))
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        job_root=tmp_path,
        qgc_control_port=qgc_port,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_port}",
        process_specs=(spec,),
    )

    result = orchestrator.start()

    assert result.status == "planned"
    assert orchestrator.process_specs == (spec,)
    assert result.stack_plan["event"] in {
        "simulator_stack_plan",
        "simulator_stack_plan_error",
    }


def test_explicit_stack_ports_conflict_with_control_endpoint(tmp_path: Path) -> None:
    with pytest.raises(SimulatorOrchestratorError, match="PX4 control endpoint"):
        SimulatorOrchestrator(
            SCENARIO,
            dry_run=True,
            job_root=tmp_path,
            stack_ports=SimulatorStackPorts(px4_control_port=15000),
        )


def test_stack_plan_can_be_disabled_without_changing_legacy_specs(tmp_path: Path) -> None:
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        stack_plan_enabled=False,
        job_root=tmp_path,
    )

    result = orchestrator.start()

    assert orchestrator.stack_plan is None
    assert result.stack_plan["enabled"] is False
    assert result.stack_plan["event"] == "simulator_stack_plan_disabled"
    assert (result.job_dir / "stack-plan.json").is_file()


def test_supplied_stack_plan_must_match_orchestrator_ports(tmp_path: Path) -> None:
    plan = build_stack_plan(SCENARIO, repository_root=ROOT)
    with pytest.raises(SimulatorOrchestratorError, match="conflicts"):
        SimulatorOrchestrator(
            SCENARIO,
            dry_run=True,
            job_root=tmp_path,
            qgc_control_port=plan.ports.qgc_input_port + 1,
            stack_plan=plan,
        )
