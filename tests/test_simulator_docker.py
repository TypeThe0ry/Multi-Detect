from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.run_simulator_docker import (
    PINNED_IMAGE,
    DockerSimulatorPlanError,
    build_docker_simulator_plan,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def test_docker_plan_is_pinned_readonly_loopback_and_cleanup_bound(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
    )
    evidence = plan.to_mapping_with_hash()

    assert plan.image == PINNED_IMAGE
    assert plan.command[0:2] == ("docker", "run")
    assert ("--cap-drop", "ALL") == plan.command[
        plan.command.index("--cap-drop") : plan.command.index("--cap-drop") + 2
    ]
    assert ("--security-opt", "no-new-privileges:true") == plan.command[
        plan.command.index("--security-opt") : plan.command.index("--security-opt") + 2
    ]
    assert plan.cap_drop_all is True
    assert plan.no_new_privileges is True
    assert plan.container_security_gate_passed is True
    assert "--publish" in plan.command
    assert "127.0.0.1:14540:14540/udp" in plan.command
    # PX4 sends telemetry to the host target; publishing the same port through
    # Docker Desktop would hairpin/drop that outbound UDP stream.
    assert "127.0.0.1:14541:14541/udp" not in plan.command
    assert all("readonly" in item for item in plan.command if item.startswith("type=bind"))
    assert evidence["mounts"]["model_root"]["readonly"] is True
    assert evidence["mounts"]["world"]["readonly"] is True
    assert evidence["mounts"]["scripts"]["readonly"] is True
    assert evidence["mounts"]["scripts"]["host"] == str((ROOT / "scripts").resolve())
    assert evidence["mounts"]["scripts"]["container"] == "/workspace/scripts"
    assert plan.dem_path == (ROOT / "simulation/terrain/river_rescue_vtol_001.dem.json").resolve()
    assert evidence["host_inputs"]["terrain_dem"] == {
        "host": str(plan.dem_path),
        "sha256": plan.terrain_dem_sha256,
        "readonly": True,
        "mounted_in_container": False,
        "consumer": "gazebo_sensor_telemetry_provider",
    }
    assert evidence["validation"]["terrain_dem_hash_bound"] is True
    assert evidence["ports"]["protected_udp_14550_used"] is False
    assert evidence["ports"]["telemetry"]["published"] is False
    assert evidence["validation"]["telemetry_port_published"] is False
    assert evidence["external_processes_started"] is False
    assert evidence["external_io_opened"] is False
    assert evidence["container_security"] == {
        "cap_drop_all": True,
        "no_new_privileges": True,
        "gate_passed": True,
    }
    assert evidence["validation"]["cap_drop_all"] is True
    assert evidence["validation"]["no_new_privileges"] is True
    assert evidence["validation"]["container_security_gate_passed"] is True
    assert evidence["validation"]["gate_passed"] is True
    assert evidence["cleanup_commands"] == [
        ["docker", "stop", "--timeout", "2", "multidetect-sim"],
        ["docker", "rm", "multidetect-sim"],
    ]


@pytest.mark.parametrize(
    ("option", "value", "gate"),
    (
        ("--cap-drop", "ALL", "cap_drop_all"),
        ("--security-opt", "no-new-privileges:true", "no_new_privileges"),
    ),
)
def test_docker_plan_security_gate_is_derived_from_exact_argv(
    tmp_path: Path,
    option: str,
    value: str,
    gate: str,
) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / gate,
    )
    option_index = plan.command.index(option)
    assert plan.command[option_index + 1] == value
    weakened_command = plan.command[:option_index] + plan.command[option_index + 2 :]

    weakened_plan = replace(plan, command=weakened_command)
    evidence = weakened_plan.to_mapping()

    assert getattr(weakened_plan, gate) is False
    assert weakened_plan.container_security_gate_passed is False
    assert evidence["container_security"][gate] is False
    assert evidence["container_security"]["gate_passed"] is False
    assert evidence["validation"][gate] is False
    assert evidence["validation"]["container_security_gate_passed"] is False
    assert evidence["validation"]["gate_passed"] is False


def test_docker_plan_hash_and_json_are_stable(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(SCENARIO, repository_root=ROOT, job_dir=tmp_path / "job")
    out = plan.write_json(tmp_path / "evidence.json")
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["sha256"] == plan.evidence_hash
    assert document["stack_plan_sha256"]
    assert not (out.parent / ".evidence.json.tmp").exists()


def test_docker_plan_rejects_protected_or_duplicate_ports(tmp_path: Path) -> None:
    with pytest.raises(DockerSimulatorPlanError, match="protected"):
        build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "protected", control_port=14550)
    with pytest.raises(DockerSimulatorPlanError, match="must differ"):
        build_docker_simulator_plan(
            SCENARIO,
            job_dir=tmp_path / "duplicate",
            control_port=14540,
            telemetry_port=14540,
        )


def test_docker_plan_rejects_unpinned_image_and_unsafe_container_name(tmp_path: Path) -> None:
    with pytest.raises(DockerSimulatorPlanError, match="pinned"):
        build_docker_simulator_plan(
            SCENARIO,
            job_dir=tmp_path / "image",
            image="px4io/px4-sitl-gazebo:latest",
        )
    with pytest.raises(DockerSimulatorPlanError, match="unsupported"):
        build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "name", container_name="../escape")


def test_docker_plan_supports_fixed_wing_assets(tmp_path: Path) -> None:
    scenario = ROOT / "configs" / "scenarios" / "coastal_patrol_fixed_wing_001.scenario.json"
    plan = build_docker_simulator_plan(scenario, repository_root=ROOT, job_dir=tmp_path / "fixed")
    assert plan.scenario_id == "coastal_patrol_fixed_wing_001"
    assert plan.model_root.name == "models"
    assert plan.world_path.name == "coastal_patrol_fixed_wing_001.sdf"


def test_docker_cli_prints_json_and_writes_out_without_invoking_docker(
    tmp_path: Path, capsys
) -> None:
    out = tmp_path / "cli.json"
    code = main(
        [
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(tmp_path / "cli-job"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert printed == written
    assert printed["validation"]["docker_invoked"] is False


def test_docker_cli_strict_only_checks_discovery_and_still_does_not_start(
    tmp_path: Path, capsys
) -> None:
    code = main(
        [
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(tmp_path / "strict"),
            "--strict",
        ]
    )
    # Docker is present on the controller in normal CI; if it is absent the
    # command reports a deterministic planning error instead of attempting a
    # network pull or starting a container.
    assert code in {0, 2}
    if code == 2:
        assert "Docker Desktop" in capsys.readouterr().out


def test_airframe_profile_identity_excludes_per_run_stack_wiring(tmp_path):
    from multidetect.simulator.stack import airframe_profile_hash

    base = {
        "schema_version": 1,
        "airframe_profile_id": "px4_vtol_gazebo_v1",
        "vehicle_model": "px4_vtol_standard",
        "model_name": "standard_vtol_mono_cam",
    }
    first = airframe_profile_hash({**base, "stack_plan_sha256": "a" * 64})
    second = airframe_profile_hash({**base, "stack_plan_sha256": "b" * 64})
    assert first == second
    assert len(first) == 64
