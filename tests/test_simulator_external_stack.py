from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.simulator.external_stack import (
    DEFAULT_PROCESS_ORDER,
    ExternalStackError,
    SimulatorExternalStackLauncher,
    build_external_stack_plan,
)
from multidetect.simulator.stack import stack_plan_from_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _plan(*, capabilities: dict[str, object] | None = None):
    return stack_plan_from_scenario(
        SCENARIO,
        repo_root=ROOT,
        tool_capabilities=capabilities or {"px4": False, "gazebo": False},
    )


def _px4_checkout(tmp_path: Path) -> Path:
    workdir = tmp_path / "px4-build"
    (workdir / "etc" / "init.d-posix").mkdir(parents=True)
    (workdir / "etc" / "init.d-posix" / "rcS").write_text("#!/bin/sh\n", encoding="utf-8")
    return workdir


def test_dry_run_builds_ordered_process_specs_without_starting_processes(tmp_path: Path) -> None:
    result = build_external_stack_plan(_plan(), job_dir=tmp_path / "job")

    assert result.process_order == DEFAULT_PROCESS_ORDER
    assert [item.name for item in result.processes] == ["px4", "gazebo"]
    assert result.px4.depends_on == ()
    assert result.gazebo.depends_on == ("px4",)
    assert result.processes[0].command == ("px4", "-i", "0", "-s", "etc/init.d-posix/rcS")
    assert result.processes[1].command[-1].endswith("river_rescue_vtol_001.sdf")
    assert result.processes[0].env == result.processes[1].env
    assert result.processes[0].env["PX4_SIM_HOST"] == "127.0.0.1"
    assert result.processes[0].log_path == result.job_dir / "logs" / "px4.log"
    assert result.processes[1].log_path == result.job_dir / "logs" / "gazebo.log"
    assert result.to_mapping()["runtime"]["external_processes_started"] is False
    assert result.to_mapping()["runtime"]["external_io_opened"] is False
    assert result.to_mapping()["launch_order"] == ["px4", "gazebo"]
    assert result.to_mapping()["validation"]["gate_passed"] is True
    assert result.ready_to_launch is False


def test_explicit_px4_workdir_is_checked_and_propagated(tmp_path: Path) -> None:
    workdir = _px4_checkout(tmp_path)
    plan = _plan()
    plan = replace(
        plan,
        environment={**plan.environment_mapping, "PX4_WORKDIR": str(workdir)},
    )
    result = build_external_stack_plan(plan, job_dir=tmp_path / "job", px4_workdir=workdir)

    assert result.px4.cwd == workdir.resolve()
    assert result.px4_workdir == workdir.resolve()
    assert result.px4_workdir_resolved is True
    assert result.px4.environment["PX4_WORKDIR"] == str(workdir.resolve())
    assert result.to_mapping()["validation"]["px4_workdir_resolved"] is True


def test_missing_explicit_px4_workdir_is_rejected_even_for_dry_run(tmp_path: Path) -> None:
    with pytest.raises(ExternalStackError, match="not a directory"):
        build_external_stack_plan(
            _plan(),
            job_dir=tmp_path / "job",
            px4_workdir=tmp_path / "does-not-exist",
        )


def test_unresolved_conventional_workdir_is_advisory_in_dry_run_but_strict_otherwise(
    tmp_path: Path,
) -> None:
    result = build_external_stack_plan(_plan(), job_dir=tmp_path / "job", dry_run=True)
    assert result.px4_workdir_resolved is False
    assert result.px4.cwd == result.job_dir
    with pytest.raises(ExternalStackError, match="PX4_WORKDIR|tools unavailable"):
        build_external_stack_plan(_plan(), job_dir=tmp_path / "strict", dry_run=False)


def test_environment_binding_cannot_be_overridden(tmp_path: Path) -> None:
    with pytest.raises(ExternalStackError, match="bound key PX4_MAVLINK_UDP_PORT"):
        build_external_stack_plan(
            _plan(),
            job_dir=tmp_path / "job",
            env_overrides={"PX4_MAVLINK_UDP_PORT": "19999"},
        )

    result = build_external_stack_plan(
        _plan(),
        job_dir=tmp_path / "job-ok",
        env_overrides={"MD_SIM_OPERATOR_LABEL": "fixture"},
    )
    assert result.px4.environment["MD_SIM_OPERATOR_LABEL"] == "fixture"


def test_noncanonical_process_order_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExternalStackError, match="process order"):
        build_external_stack_plan(
            _plan(),
            job_dir=tmp_path / "job",
            process_order=("gazebo", "px4"),
        )


def test_logs_must_remain_inside_job_directory(tmp_path: Path) -> None:
    with pytest.raises(ExternalStackError, match="log_dir escapes"):
        build_external_stack_plan(
            _plan(),
            job_dir=tmp_path / "job",
            log_dir=tmp_path / "outside",
        )

    job = tmp_path / "job2"
    (job / "logs").mkdir(parents=True)
    (job / "logs" / "px4.log").mkdir()
    with pytest.raises(ExternalStackError, match="log path is a directory"):
        build_external_stack_plan(_plan(), job_dir=job)

    custom = tmp_path / "job3" / "evidence-logs"
    result = build_external_stack_plan(_plan(), job_dir=tmp_path / "job3", log_dir=custom)
    assert result.log_dir == custom.resolve()
    assert result.to_mapping()["log_dir"] == str(custom.resolve())


def test_commands_cannot_escape_sim_with_network_or_serial_token(tmp_path: Path) -> None:
    plan = _plan()
    unsafe = replace(
        plan,
        px4_command=("px4", "--serial", "COM3", "-s", "etc/init.d-posix/rcS"),
    )
    with pytest.raises(ExternalStackError, match="non-local endpoint"):
        build_external_stack_plan(unsafe, job_dir=tmp_path / "job")

    unsafe = replace(
        plan,
        gazebo_command=("gz", "sim", "udpout:8.8.8.8:14540", str(plan.world_path)),
    )
    with pytest.raises(ExternalStackError, match="non-local endpoint"):
        build_external_stack_plan(unsafe, job_dir=tmp_path / "job2")


def test_bare_executable_cannot_be_retargeted_by_same_basename(tmp_path: Path) -> None:
    plan = _plan()
    unsafe = replace(plan, px4_command=("C:/untrusted/px4", *plan.px4_command[1:]))
    with pytest.raises(ExternalStackError, match="executable conflicts"):
        build_external_stack_plan(unsafe, job_dir=tmp_path / "job")


def test_json_evidence_is_hash_bound_and_atomic(tmp_path: Path) -> None:
    result = build_external_stack_plan(_plan(), job_dir=tmp_path / "job")
    path = result.write_json(tmp_path / "evidence" / "external.json")
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["sha256"] == result.evidence_hash
    assert document["source_stack_plan_sha256"] == result.stack_plan.evidence_hash
    assert document["validation"]["external_processes_started"] is False
    assert not path.with_name(f".{path.name}.tmp").exists()


def test_launcher_start_is_a_dry_run_alias_and_never_calls_subprocess(tmp_path: Path) -> None:
    launcher = SimulatorExternalStackLauncher(_plan(), job_dir=tmp_path / "job")
    result = launcher.start()
    assert result is launcher.plan
    evidence = launcher.write_evidence()
    assert evidence.is_file()
    assert json.loads(evidence.read_text(encoding="utf-8"))["dry_run"] is True


def test_absolute_executables_and_px4_layout_are_resolved(tmp_path: Path) -> None:
    workdir = _px4_checkout(tmp_path)
    px4 = workdir / "bin" / "px4"
    gazebo = tmp_path / "gz"
    px4.parent.mkdir(parents=True)
    px4.write_text("", encoding="utf-8")
    gazebo.write_text("", encoding="utf-8")
    plan = _plan(capabilities={"px4": True, "gazebo": True})
    plan = replace(
        plan,
        px4_executable=str(px4),
        gazebo_executable=str(gazebo),
        px4_command=(str(px4), "-i", "0", "-s", "etc/init.d-posix/rcS"),
        gazebo_command=(str(gazebo), "sim", str(plan.world_path)),
    )
    result = build_external_stack_plan(plan, job_dir=tmp_path / "job")
    assert result.ready_to_launch is True
    assert result.px4.cwd == workdir.resolve()
    assert result.gazebo.cwd == ROOT.resolve()


def test_px4_launch_wrapper_is_opt_in_and_hash_bound_for_external_plan(
    tmp_path: Path,
) -> None:
    workdir = _px4_checkout(tmp_path)
    # A real PX4 POSIX rcS must source the rc.mavlink stanza.  The launch
    # planner now binds that source to its job-local patched copy; keep the
    # fixture representative so strict readiness exercises the real anchor.
    (workdir / "etc" / "init.d-posix" / "rcS").write_text(
        "#!/bin/sh\n. px4-rc.mavlink\n",
        encoding="utf-8",
    )
    (workdir / "etc" / "init.d-posix" / "px4-rc.mavlink").write_text(
        "#!/bin/sh\n"
        "udp_gcs_port_local=18570\n"
        "mavlink start -x -u $udp_gcs_port_local -r 4000000 -f\n",
        encoding="utf-8",
    )
    px4 = workdir / "bin" / "px4"
    px4.parent.mkdir(parents=True, exist_ok=True)
    px4.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gazebo = tmp_path / "gz"
    gazebo.write_text("", encoding="utf-8")
    plan = _plan(capabilities={"px4": True, "gazebo": True})
    plan = replace(
        plan,
        px4_executable=str(px4),
        px4_command=(str(px4), "-i", "0", "-s", "etc/init.d-posix/rcS"),
        gazebo_executable=str(gazebo),
        gazebo_command=(str(gazebo), "sim", str(plan.world_path)),
        environment={**plan.environment_mapping, "PX4_WORKDIR": str(workdir)},
    )

    result = build_external_stack_plan(
        plan,
        job_dir=tmp_path / "job",
        px4_workdir=workdir,
        dry_run=False,
        strict=True,
        px4_launch_enabled=True,
    )

    assert result.px4_launch is not None
    assert result.px4_launch.ready_to_launch is True
    assert result.ready_to_launch is True
    assert result.px4.command == result.px4_launch.command
    assert result.px4.cwd == result.px4_launch.px4_workdir
    assert result.px4.environment["MD_SIM_STACK_PLAN_SHA256"] == plan.evidence_hash
    document = result.to_mapping()
    assert document["px4_launch_sha256"] == result.px4_launch.evidence_hash
    assert document["validation"]["px4_launch_ready"] is True


def test_px4_launch_wrapper_dry_run_keeps_template_evidence_when_tools_are_missing(
    tmp_path: Path,
) -> None:
    result = build_external_stack_plan(
        _plan(),
        job_dir=tmp_path / "job",
        px4_launch_enabled=True,
        dry_run=True,
    )

    assert result.px4_launch is not None
    assert result.px4_launch.source_is_template is True
    assert result.px4_launch.ready_to_launch is False
    assert result.px4.command == result.px4_launch.command
    assert result.ready_to_launch is False
    assert result.to_mapping()["px4_launch"]["readiness"]["gate_passed"] is False
