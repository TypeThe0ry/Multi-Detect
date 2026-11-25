from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import run_simulator_docker as simulator_docker
from multidetect.autonomy.serialization import canonical_sha256
from scripts.run_simulator_docker import (
    PINNED_IMAGE,
    DockerSimulatorPlanError,
    build_docker_simulator_plan,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
WIDE_AREA_SCENARIO = (
    ROOT
    / "configs/scenarios/realworld/rc_cessna_mono_cam"
    / "rw_farm_iowa_w6km__clear_noon.scenario.json"
)


def test_docker_plan_is_pinned_readonly_loopback_and_cleanup_bound(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
    )
    evidence = plan.to_mapping_with_hash()

    assert plan.image == PINNED_IMAGE
    assert plan.command[0:2] == ("docker", "run")
    assert dict(plan.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "off"
    assert dict(plan.environment)["MULTIDETECT_PX4_DAEMON"] == "1"
    assert "px4_daemon_mode" in plan.to_mapping()
    assert plan.command[-1].endswith("-d")
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


def test_logger_mode_is_off_when_environment_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MULTIDETECT_PX4_LOGGER_MODE", raising=False)
    plan = build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "unset")
    assert dict(plan.environment)["MULTIDETECT_PX4_LOGGER_MODE"] == "off"
    assert dict(plan.environment)["SDLOG_BACKEND"] == "0"


def test_logger_mode_rejects_poll_and_unknown_values(tmp_path, monkeypatch):
    for mode in ("poll", "unknown"):
        monkeypatch.setenv("MULTIDETECT_PX4_LOGGER_MODE", mode)
        with pytest.raises(DockerSimulatorPlanError, match="default or off"):
            build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / mode)


@pytest.mark.parametrize(("mode", "expected"), [("1", True), ("0", False)])
def test_px4_daemon_mode_controls_startup_and_is_recorded(
    tmp_path, monkeypatch, mode, expected
):
    monkeypatch.setenv("MULTIDETECT_PX4_DAEMON", mode)
    plan = build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / f"daemon-{mode}")
    environment = dict(plan.environment)
    assert environment["MULTIDETECT_PX4_DAEMON"] == mode
    assert plan.to_mapping()["px4_daemon_mode"] is expected
    assert plan.command[-1].endswith(" -d") is expected


def test_px4_daemon_mode_rejects_values_other_than_zero_or_one(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_PX4_DAEMON", "true")
    with pytest.raises(DockerSimulatorPlanError, match="must be 0 or 1"):
        build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "invalid-daemon")


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


def test_gimbal_runtime_model_selects_device_model_and_topic_name(tmp_path: Path) -> None:
    runtime = json.loads(
        (ROOT / "configs/scenarios/urban_inspection_multirotor_001.runtime.json").read_text(
            encoding="utf-8"
        )
    )
    runtime["vehicle"]["model"] = "x500_gimbal"
    runtime_path = tmp_path / "x500-gimbal.runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    channels = {
        "runtime_input": {
            "path": runtime_path.name,
            "sha256": canonical_sha256(runtime),
        }
    }
    for name in ("truth", "expectations"):
        source_path = ROOT / f"configs/scenarios/urban_inspection_multirotor_001.{name}.json"
        copied_path = tmp_path / source_path.name
        shutil.copy2(source_path, copied_path)
        channels[name] = {
            "path": copied_path.name,
            "sha256": canonical_sha256(json.loads(copied_path.read_text(encoding="utf-8"))),
        }
    manifest_path = tmp_path / "x500-gimbal.scenario.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario_id": runtime["scenario_id"],
                "scenario_version": runtime["scenario_version"],
                "channels": channels,
            }
        ),
        encoding="utf-8",
    )

    plan = build_docker_simulator_plan(
        manifest_path,
        repository_root=ROOT,
        job_dir=tmp_path / "gimbal-runtime",
    )

    env = dict(plan.environment)
    assert plan.vehicle_model == "x500_gimbal"
    assert plan.model_name == "x500_gimbal"
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


@pytest.mark.parametrize("relative", [False, True])
def test_fuel_resolved_override_is_mounted_readonly_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    # Keep the relative case on the repository drive on Windows, where the
    # pytest temporary directory can be on a different drive.
    resolved_root = ROOT / "simulation/gazebo/models" if relative else tmp_path / "private-resolved"
    if not relative:
        resolved_root.mkdir()
    working_dir = tmp_path / "unrelated-cwd"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    selected = os.path.relpath(resolved_root, ROOT) if relative else str(resolved_root)
    monkeypatch.setenv("MULTIDETECT_SIM_FUEL_RESOLVED_ROOT", selected)
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")

    plan = build_docker_simulator_plan(
        SCENARIO, repository_root=ROOT, job_dir=tmp_path / "job"
    )

    assert plan.fuel_resolved_root == resolved_root.resolve()
    container_root = "/opt/multidetect-gz-fuel-resolved"
    mount = f"type=bind,source={resolved_root.resolve()},target={container_root},readonly"
    assert mount in plan.command
    resource_path = dict(plan.environment)["GZ_SIM_RESOURCE_PATH"]
    assert resource_path.split(":")[0] == container_root
    assert f"export GZ_SIM_RESOURCE_PATH={container_root}:" in plan.command[-1]
    assert plan.to_mapping()["external_processes_started"] is False


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_fuel_resolved_override_rejects_invalid_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    resolved_root = tmp_path / "private-resolved"
    if kind == "file":
        resolved_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("MULTIDETECT_SIM_FUEL_RESOLVED_ROOT", str(resolved_root))
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")

    with pytest.raises(DockerSimulatorPlanError, match="MULTIDETECT_SIM_FUEL_RESOLVED_ROOT"):
        build_docker_simulator_plan(
            SCENARIO, repository_root=ROOT, job_dir=tmp_path / "job"
        )


def test_fuel_resolved_default_keeps_shared_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MULTIDETECT_SIM_FUEL_RESOLVED_ROOT", raising=False)
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")
    plan = build_docker_simulator_plan(
        SCENARIO, repository_root=ROOT, job_dir=tmp_path / "job"
    )
    default_root = (ROOT / "artifacts/cache/gz-fuel/resolved").resolve()
    expected_root = default_root if default_root.is_dir() else None
    assert plan.fuel_resolved_root == expected_root
    container_root = "/opt/multidetect-gz-fuel-resolved"
    resolved_mounts = [
        token for token in plan.command if f"target={container_root}," in token
    ]
    assert resolved_mounts == (
        [f"type=bind,source={default_root},target={container_root},readonly"]
        if expected_root is not None else []
    )


def test_rtf_model_root_is_mounted_readonly_ahead_of_shared_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "rtf-models"
    (model_root / "rw_farm_iowa").mkdir(parents=True)
    (model_root / "rw_farm_iowa/model.config").write_text("{}", encoding="utf-8")
    fuel_root = tmp_path / "fuel-cache"
    fuel_root.mkdir()
    monkeypatch.setenv("MULTIDETECT_RTF_MODEL_ROOT", str(model_root))
    monkeypatch.setenv("MULTIDETECT_SIM_FUEL_RESOLVED_ROOT", str(fuel_root))
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")

    plan = build_docker_simulator_plan(
        SCENARIO, repository_root=ROOT, job_dir=tmp_path / "rtf-model-plan"
    )

    container_root = "/opt/multidetect-rtf-models"
    mount = f"type=bind,source={model_root.resolve()},target={container_root},readonly"
    assert mount in plan.command
    resource_path = dict(plan.environment)["GZ_SIM_RESOURCE_PATH"].split(":")
    assert resource_path[:2] == [container_root, "/opt/multidetect-gz-fuel-resolved"]
    assert (
        f"export GZ_SIM_RESOURCE_PATH={container_root}:/opt/multidetect-gz-fuel-resolved:"
        in plan.command[-1]
    )
    evidence = plan.to_mapping()
    assert evidence["mounts"]["rtf_model_overlay"] == {
        "host": str(model_root.resolve()),
        "container": container_root,
        "readonly": True,
        "sha256": plan.rtf_model_root_sha256,
    }
    assert len(plan.rtf_model_root_sha256) == 64
    assert evidence["external_processes_started"] is False


def test_w6km_world_defaults_to_wide_area_camera_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MULTIDETECT_RTF_MODEL_ROOT", raising=False)
    assert simulator_docker._resolve_rtf_model_root(
        ROOT, Path("rw_farm_iowa_w6km__clear_noon.sdf")
    ) == (ROOT / "simulation/gazebo/models-wide-area").resolve()
    assert simulator_docker._resolve_rtf_model_root(
        ROOT, Path("rw_farm_iowa_w2km__clear_noon.sdf")
    ) is None
    assert simulator_docker._resolve_rtf_model_root(
        ROOT, Path("rw_farm_iowa_w6km-clear_noon.sdf")
    ) is None


@pytest.mark.skipif(
    not WIDE_AREA_SCENARIO.is_file(),
    reason="wide-area scenario fixture is not present in this checkout",
)
def test_w6km_plan_mounts_default_overlay_and_server_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not (ROOT / "configs/scenarios/airframes/rw_farm_iowa_w6km__clear_noon.sdf").is_file():
        pytest.skip("generated world SDF is not present in this checkout")
    monkeypatch.delenv("MULTIDETECT_RTF_MODEL_ROOT", raising=False)
    monkeypatch.delenv("MULTIDETECT_RTF_SERVER_CONFIG", raising=False)
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")

    plan = build_docker_simulator_plan(
        WIDE_AREA_SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "w6km-default-overlay",
    )

    overlay_root = (ROOT / "simulation/gazebo/models-wide-area").resolve()
    server_config = (ROOT / "simulation/gazebo/server-wide-area.config").resolve()
    server_config_sha256 = hashlib.sha256(server_config.read_bytes()).hexdigest()
    mount_target = "/opt/multidetect-rtf-server.config"
    evidence = plan.to_mapping()
    assert plan.world_path.name == "rw_farm_iowa_w6km__clear_noon.sdf"
    assert plan.rtf_model_root == overlay_root
    assert evidence["mounts"]["rtf_model_overlay"]["sha256"] == (
        plan.rtf_model_root_sha256
    )
    assert plan.rtf_server_config == server_config
    assert plan.rtf_server_config_sha256 == server_config_sha256
    assert evidence["mounts"]["rtf_server_config"] == {
        "host": str(server_config),
        "container": mount_target,
        "readonly": True,
        "sha256": server_config_sha256,
    }
    assert plan.evidence_hash != replace(plan, rtf_server_config_sha256="0" * 64).evidence_hash
    assert f"type=bind,source={server_config},target={mount_target},readonly" in plan.command
    assert dict(plan.environment)["GZ_SIM_SERVER_CONFIG_PATH"] == mount_target
    assert f"sed '/gz-sim-magnetometer-system/d' {mount_target} " in plan.command[-1]


@pytest.mark.skipif(
    not WIDE_AREA_SCENARIO.is_file(),
    reason="wide-area scenario fixture is not present in this checkout",
)
def test_explicit_rtf_server_config_overrides_w6km_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not (ROOT / "configs/scenarios/airframes/rw_farm_iowa_w6km__clear_noon.sdf").is_file():
        pytest.skip("generated world SDF is not present in this checkout")
    config = tmp_path / "override.config"
    config.write_bytes(b"<server_config><plugins/></server_config>\n")
    monkeypatch.setenv("MULTIDETECT_RTF_SERVER_CONFIG", str(config))
    monkeypatch.delenv("MULTIDETECT_RTF_MODEL_ROOT", raising=False)

    plan = build_docker_simulator_plan(
        WIDE_AREA_SCENARIO, repository_root=ROOT, job_dir=tmp_path / "override-plan"
    )

    assert plan.rtf_model_root == (ROOT / "simulation/gazebo/models-wide-area").resolve()
    assert plan.rtf_server_config == config.resolve()
    assert plan.to_mapping()["mounts"]["rtf_server_config"]["sha256"] == (
        hashlib.sha256(config.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("name", ["rw_farm_iowa_w2km__clear_noon.sdf", "rw_farm_iowa_w6km-clear_noon.sdf"])
def test_rtf_server_config_pattern_rejects_near_misses(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MULTIDETECT_RTF_SERVER_CONFIG", raising=False)
    assert simulator_docker._resolve_rtf_server_config(ROOT, Path(name)) is None


def test_explicit_rtf_model_root_overrides_w6km_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override_root = tmp_path / "explicit-rtf-models"
    override_root.mkdir()
    monkeypatch.setenv("MULTIDETECT_RTF_MODEL_ROOT", str(override_root))
    assert simulator_docker._resolve_rtf_model_root(
        ROOT, Path("rw_farm_iowa_w6km__clear_noon.sdf")
    ) == override_root.resolve()


def test_non_w6km_world_does_not_auto_mount_wide_area_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MULTIDETECT_RTF_MODEL_ROOT", raising=False)
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")

    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "non-w6km-default-overlay",
    )

    assert plan.world_path.name != "rw_farm_iowa_w6km__clear_noon.sdf"
    assert plan.rtf_model_root is None
    assert "rtf_model_overlay" not in plan.to_mapping()["mounts"]


def test_optional_rtf_server_config_is_hash_bound_mounted_readonly_and_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "server.config"
    config.write_text("<server_config><plugins/></server_config>", encoding="utf-8")
    monkeypatch.setenv("MULTIDETECT_RTF_SERVER_CONFIG", str(config))
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "server-config-plan",
    )
    evidence = plan.to_mapping()
    mount = evidence["mounts"]["rtf_server_config"]
    assert mount["host"] == str(config.resolve())
    assert mount["container"] == "/opt/multidetect-rtf-server.config"
    assert mount["readonly"] is True
    assert mount["sha256"] == plan.rtf_server_config_sha256
    assert dict(plan.environment)["GZ_SIM_SERVER_CONFIG_PATH"] == mount["container"]
    assert any(
        token == "type=bind,source=" + str(config.resolve()) + ",target=" + mount["container"] + ",readonly"
        for token in plan.command
    )
    startup = plan.command[-1]
    assert "sed '/gz-sim-magnetometer-system/d' " + mount["container"] in startup
    assert "export GZ_SIM_SERVER_CONFIG_PATH=/tmp/multidetect-gz-server.config; " in startup


def test_rtf_server_config_is_optional_and_must_be_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MULTIDETECT_RTF_SERVER_CONFIG", raising=False)
    default_plan = build_docker_simulator_plan(
        SCENARIO, repository_root=ROOT, job_dir=tmp_path / "server-config-default"
    )
    assert "rtf_server_config" not in default_plan.to_mapping()["mounts"]
    assert "GZ_SIM_SERVER_CONFIG_PATH" not in dict(default_plan.environment)

    missing = tmp_path / "missing.config"
    monkeypatch.setenv("MULTIDETECT_RTF_SERVER_CONFIG", str(missing))
    with pytest.raises(DockerSimulatorPlanError, match="is not a file"):
        build_docker_simulator_plan(
            SCENARIO, repository_root=ROOT, job_dir=tmp_path / "server-config-missing"
        )


def test_x1_wind_airspeed_mount_requires_opt_in_and_replacement_config(tmp_path, monkeypatch):
    from multidetect.sim_teacher.x1_scenario import wind_server_config

    scenario_id = "rw_farm_iowa_w6km__clear_noon"
    scenario = ROOT / f"configs/scenarios/realworld/delta_tailsitter_gimbal_cam/{scenario_id}.scenario.json"
    world = tmp_path / f"{scenario_id}.sdf"
    world.write_text(
        '<sdf version="1.9"><world name="default"><spherical_coordinates>'
        '<surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation>'
        '<latitude_deg>42.015</latitude_deg><longitude_deg>-93.71</longitude_deg>'
        '<elevation>318.23</elevation></spherical_coordinates></world></sdf>',
        encoding="utf-8",
    )
    source = ROOT / "simulation/gazebo/server-wide-area.config"
    enabled_config = tmp_path / "enabled.config"
    plugin = tmp_path / "host-plugin.so"
    plugin.write_bytes(b"mount-test-fixture")
    monkeypatch.setenv("MULTIDETECT_RTF_SERVER_CONFIG", str(source))
    env = simulator_docker.X1_WIND_AIRSPEED_ENV
    monkeypatch.delenv(env, raising=False)

    def plan(job):
        return build_docker_simulator_plan(scenario, repository_root=ROOT,
                                           job_dir=tmp_path / job, world_sdf_path=world)

    nominal = plan("nominal")
    assert "GZ_SIM_SYSTEM_PLUGIN_PATH" not in nominal.command[-1]
    assert "x1_wind_airspeed" not in nominal.to_mapping()["mounts"]
    monkeypatch.setenv(env, str(plugin))
    with pytest.raises(DockerSimulatorPlanError, match="single replacement AirSpeed"):
        plan("stock-rejected")

    wind_server_config(source, enabled_config, wind_relative_airspeed=True)
    monkeypatch.setenv("MULTIDETECT_RTF_SERVER_CONFIG", str(enabled_config))
    enabled = plan("enabled")
    mount = enabled.to_mapping()["mounts"]["x1_wind_airspeed"]
    assert mount == {"host": str(plugin), "container": simulator_docker.CONTAINER_X1_WIND_AIRSPEED,
                     "readonly": True, "sha256": hashlib.sha256(plugin.read_bytes()).hexdigest()}
    assert f'type=bind,source={plugin},target={mount["container"]},readonly' in enabled.command
    assert ('export GZ_SIM_SYSTEM_PLUGIN_PATH="/opt/multidetect-x1-plugins'
            '${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"') in enabled.command[-1]
    monkeypatch.setenv(env, str(tmp_path / "missing.so"))
    with pytest.raises(DockerSimulatorPlanError, match="is not a file"):
        plan("missing-rejected")


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
        gpu_device=0,
    )
    assert plan.command[plan.command.index("--gpus") : plan.command.index("--gpus") + 2] == (
        "--gpus",
        "device=0",
    )
    assert plan.command[plan.command.index("--network") : plan.command.index("--network") + 2] == (
        "--network",
        "host",
    )
    assert "--publish" not in plan.command
    assert "--add-host" not in plan.command
    environment = dict(plan.environment)
    assert environment["NVIDIA_VISIBLE_DEVICES"] == "0"
    assert environment["EGL_PLATFORM"] == "surfaceless"
    assert environment["PX4_SIM_HOST"] == "127.0.0.1"
    assert environment["GZ_PARTITION"] == "r15-worker-1"
    startup = plan.command[-1]
    assert "mavlink start -x -t 127.0.0.1" in startup
    assert "export PX4_NET_INTERFACE=lo;" in startup
    assert "--headless-rendering" in startup
    assert 'set -- "$@" --render-engine "$render_engine"' in startup
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


def test_docker_plan_supports_complete_local_image_id(tmp_path: Path) -> None:
    image = "sha256:" + "a" * 64
    plan = build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "immutable", image=image)
    assert plan.image == image
    assert image in plan.command


@pytest.mark.parametrize("image", ["sha256:abc", "sha256:" + "g" * 64,
                                   "sha256:" + "a" * 65, "sha256:" + "a" * 64 + ";echo x"])
def test_docker_plan_rejects_incomplete_or_malformed_local_image_id(tmp_path: Path, image) -> None:
    with pytest.raises(DockerSimulatorPlanError, match="pinned"):
        build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "bad-image", image=image)


def test_docker_plan_supports_fixed_wing_assets(tmp_path: Path) -> None:
    scenario = ROOT / "configs" / "scenarios" / "coastal_patrol_fixed_wing_001.scenario.json"
    plan = build_docker_simulator_plan(scenario, repository_root=ROOT, job_dir=tmp_path / "fixed")
    assert plan.scenario_id == "coastal_patrol_fixed_wing_001"
    assert plan.model_root.name == "models"
    assert plan.world_path.name == "coastal_patrol_fixed_wing_001.sdf"


def test_docker_plan_mounts_explicit_external_world_and_resource_directory(
    tmp_path: Path, monkeypatch,
) -> None:
    baseline = build_docker_simulator_plan(
        SCENARIO, repository_root=ROOT, job_dir=tmp_path / "baseline"
    )
    external_dir = tmp_path / "external-worlds"
    external_dir.mkdir()
    external_world = external_dir / baseline.world_path.name
    shutil.copyfile(baseline.world_path, external_world)
    resolved_models = tmp_path / "resolved-models"
    resolved_models.mkdir()
    monkeypatch.setenv("MULTIDETECT_SIM_FUEL_RESOLVED_ROOT", str(resolved_models))

    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "external",
        world_sdf_path=external_world,
    )

    assert plan.world_path == external_world.resolve()
    assert plan.external_world_sdf is True
    assert plan.external_world_resource_root == external_dir.resolve()
    assert plan.to_mapping()["mounts"]["world"]["external"] is True
    assert plan.to_mapping()["mounts"]["external_world_resources"] == {
        "host": str(external_dir.resolve()),
        "container": "/opt/polaris-sim-world-resources",
        "readonly": True,
    }
    assert "/opt/polaris-sim-world-resources" in dict(plan.environment)["GZ_SIM_RESOURCE_PATH"]
    assert "/opt/multidetect-gz-fuel-resolved" in dict(plan.environment)["GZ_SIM_RESOURCE_PATH"]
    command_pairs = tuple(zip(plan.command, plan.command[1:], strict=False))
    world_resources_mount = (
        "--mount",
        "type=bind,"
        f"source={external_dir.resolve()},"
        "target=/opt/polaris-sim-world-resources,readonly",
    )
    fuel_cache_mount = (
        "--mount",
        "type=bind,"
        f"source={resolved_models.resolve()},"
        "target=/opt/multidetect-gz-fuel-resolved,readonly",
    )
    assert world_resources_mount in command_pairs
    assert fuel_cache_mount in command_pairs


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


def test_gpu_allow_list_defaults_to_zero_and_one_and_can_be_widened(monkeypatch):
    from scripts import run_simulator_docker as rsd

    for name in ("MULTIDETECT_SIM_GPU", "MULTIDETECT_SIM_GPU_DEVICES", "MULTIDETECT_SIM_GPU_AVOID", "MULTIDETECT_SIM_ALLOW_GPUS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(rsd.DockerSimulatorPlanError):
        rsd._resolve_gpu_selection(gpu_render=True, gpu_device="2", gpu_devices=None, worker_index=0)
    monkeypatch.setenv("MULTIDETECT_SIM_ALLOW_GPUS", "0,1,2,3")
    enabled, device, _ = rsd._resolve_gpu_selection(gpu_render=True, gpu_device="2", gpu_devices=None, worker_index=0)
    assert enabled and device == "2"
    monkeypatch.setenv("MULTIDETECT_SIM_ALLOW_GPUS", "0,1")
    with pytest.raises(rsd.DockerSimulatorPlanError):
        rsd._resolve_gpu_selection(gpu_render=True, gpu_device="3", gpu_devices=None, worker_index=0)
