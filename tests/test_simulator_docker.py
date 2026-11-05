from __future__ import annotations

import json
import os
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
    assert dict(plan.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "default"
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
    assert plan.command[plan.command.index("--add-host") + 1] == (
        "host.docker.internal:host-gateway"
    )
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


@pytest.mark.parametrize("mode", ["default", "off"])
def test_logger_mode_is_recorded_and_off_disables_sdlog(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("MULTIDETECT_PX4_LOGGER_MODE", mode)
    plan = build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / mode)
    environment = dict(plan.environment)
    assert environment["MULTIDETECT_PX4_LOGGER_MODE"] == mode
    assert plan.to_mapping()["environment"]["MULTIDETECT_PX4_LOGGER_MODE"] == mode
    if mode == "off":
        assert environment["SDLOG_BACKEND"] == "0"
        assert environment["PX4_PARAM_SDLOG_BACKEND"] == "0"
    else:
        assert "SDLOG_BACKEND" not in environment


def test_logger_mode_rejects_poll_and_unknown_values(tmp_path, monkeypatch):
    for mode in ("poll", "unknown"):
        monkeypatch.setenv("MULTIDETECT_PX4_LOGGER_MODE", mode)
        with pytest.raises(DockerSimulatorPlanError, match="default or off"):
            build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / mode)


def test_gimbal_airframe_uses_px4_gimbal_model_without_starting_docker(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "gimbal",
        airframe_profile_id="x500_gimbal",
    )
    env = dict(plan.environment)
    assert env["PX4_SYS_AUTOSTART"] == "4019"
    assert env["PX4_SIM_MODEL"] == "gz_x500_gimbal"
    assert env["PX4_GZ_MODELS"] == "/opt/px4-gazebo/share/gz/models"


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


def test_docker_plan_uses_selected_network_for_container_and_px4_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MULTIDETECT_SIM_DOCKER_NETWORK", "sitl-private")
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "selected-network",
    )
    assert plan.container_network_mode == "sitl-private"
    assert plan.command[plan.command.index("--network") + 1] == "sitl-private"
    startup = plan.command[-1]
    if os.name == "nt":
        assert "host.docker.internal" in startup
    else:
        assert "ip -4 route" not in startup  # the PX4 image has no iproute2
        assert 'DOCKER_HOST_IP="' in startup


def test_host_network_plan_uses_loopback_partition_and_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MULTIDETECT_SIM_NETWORK", raising=False)
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "host",
        container_name="r15-worker-1",
        control_port=14570,
        telemetry_port=14571,
        sim_network="host",
        sim_instance=1,
    )
    assert plan.container_network_mode == "host"
    assert ("--network", "host") == plan.command[
        plan.command.index("--network") : plan.command.index("--network") + 2
    ]
    assert "--publish" not in plan.command
    assert "--add-host" not in plan.command
    assert dict(plan.environment)["GZ_PARTITION"] == "r15-worker-1"
    assert dict(plan.environment)["PX4_SIM_HOST"] == "127.0.0.1"
    startup = plan.command[-1]
    assert "mavlink start -x -t 127.0.0.1" in startup
    assert "export PX4_NET_INTERFACE=lo;" in startup
    assert "px4 -i 1" in startup


def test_host_network_gpu_render_plan_keeps_both_runtime_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MULTIDETECT_SIM_NETWORK", raising=False)
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "host-gpu",
        container_name="r15-worker-1",
        control_port=14570,
        telemetry_port=14571,
        image="multidetect/px4-sitl-gazebo:gpu",
        sim_network="host",
        sim_instance=1,
        gpu_render=True,
        gpu_device=2,
    )
    assert plan.command[plan.command.index("--gpus") : plan.command.index("--gpus") + 2] == (
        "--gpus",
        "device=2",
    )
    assert plan.command[plan.command.index("--network") : plan.command.index("--network") + 2] == (
        "--network",
        "host",
    )
    assert "--publish" not in plan.command
    assert "--add-host" not in plan.command
    environment = dict(plan.environment)
    assert environment["NVIDIA_VISIBLE_DEVICES"] == "2"
    assert environment["EGL_PLATFORM"] == "surfaceless"
    assert environment["PX4_SIM_HOST"] == "127.0.0.1"
    assert environment["GZ_PARTITION"] == "r15-worker-1"
    startup = plan.command[-1]
    assert "mavlink start -x -t 127.0.0.1" in startup
    assert "export PX4_NET_INTERFACE=lo;" in startup
    assert "--headless-rendering" in startup
    assert "--render-engine ogre2" in startup
    assert "px4 -i 1" in startup


def test_host_network_env_switch_and_windows_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MULTIDETECT_SIM_NETWORK", "host")
    plan = build_docker_simulator_plan(SCENARIO, repository_root=ROOT, job_dir=tmp_path / "env")
    assert plan.container_network_mode == "host"
    monkeypatch.setattr(
        "scripts.run_simulator_docker.sim_network_mode",
        lambda _mode=None: (_ for _ in ()).throw(ValueError("SIM network mode 'host' is supported on Linux only")),
    )
    with pytest.raises(ValueError, match="Linux only"):
        build_docker_simulator_plan(SCENARIO, repository_root=ROOT, job_dir=tmp_path / "win")


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


@pytest.mark.parametrize("invalid,error", [
    ("hash", "hash mismatch"), ("path", "relative and contained"),
])
def test_external_manifest_must_keep_runtime_channel_bound(tmp_path: Path, invalid, error) -> None:
    manifest = json.loads(SCENARIO.read_text(encoding="utf-8"))
    reference = manifest["channels"]["runtime_input"]
    runtime = tmp_path / reference["path"]
    runtime.write_bytes((SCENARIO.parent / reference["path"]).read_bytes())
    if invalid == "hash":
        payload = json.loads(runtime.read_text(encoding="utf-8"))
        payload["seed"] += 1
        runtime.write_text(json.dumps(payload), encoding="utf-8")
    else:
        reference["path"] = "../outside.runtime_input.json"
    scenario = tmp_path / "exported.scenario.json"
    scenario.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DockerSimulatorPlanError, match=error):
        build_docker_simulator_plan(scenario, repository_root=ROOT, job_dir=tmp_path / "job")


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
        assert "Docker" in capsys.readouterr().out


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


@pytest.mark.parametrize(
    "scenario",
    (
        SCENARIO,
        ROOT / "configs" / "scenarios" / "coastal_patrol_fixed_wing_001.scenario.json",
        ROOT / "configs" / "scenarios" / "fixed-wing-v2" / "gen_v2_w0031.scenario.json",
    ),
)
def test_docker_plan_sets_sim_sensor_parameters_for_every_vehicle_class(
    scenario: Path, tmp_path: Path
) -> None:
    # Gazebo Harmonic's magnetometer exchanges roll and pitch; PX4's own SIM
    # magnetometer must be the only one EKF2 sees, for every vehicle class, and
    # EKF2 must not expect real-hardware sensor latency.
    plan = build_docker_simulator_plan(scenario, repository_root=ROOT, job_dir=tmp_path / "job")
    environment = dict(plan.environment)
    assert environment["PX4_PARAM_SENS_EN_MAGSIM"] == "1"
    # The simulated GNSS and airspeed arrive without latency.
    assert environment["PX4_PARAM_SENS_GPS0_DELAY"] == "0"
    assert environment["PX4_PARAM_EKF2_ASP_DELAY"] == "0"
    assert "PX4_PARAM_SENS_EN_MAGSIM=1" in plan.command
    assert plan.to_mapping()["environment"]["PX4_PARAM_SENS_EN_MAGSIM"] == "1"
    # Both magnetometers share device id 197388, so the Gazebo one is removed at
    # the source: Gazebo runs without its magnetometer system.
    assert not any(key.startswith("PX4_PARAM_CAL_MAG") for key in environment)
    startup = plan.command[-1]
    drop = (
        "sed '/gz-sim-magnetometer-system/d' /opt/px4-gazebo/share/gz/server.config "
        "> /tmp/multidetect-gz-server.config; "
    )
    assert drop in startup
    assert "export GZ_SIM_SERVER_CONFIG_PATH=/tmp/multidetect-gz-server.config; " in startup
    assert startup.index(drop) < startup.index("exec /opt/px4-gazebo/bin/px4")
    # Gazebo gets 180 s to load a generated world before PX4 gives up on it.
    wait = "sed -i 's/^ATTEMPTS=30$/ATTEMPTS=180/' /opt/px4-gazebo/etc/init.d-posix/px4-rc.gzsim; "
    assert wait in startup and "|| exit 72; " in startup
    assert startup.index(wait) < startup.index("exec /opt/px4-gazebo/bin/px4")
