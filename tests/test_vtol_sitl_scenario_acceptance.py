from __future__ import annotations

import copy
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vtol_sitl_scenario_acceptance.py"
WRAPPER = ROOT / "scripts" / "run_px4_gazebo_vtol_scenario_acceptance.ps1"
MANIFEST = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
MODEL_ROOT = ROOT / "simulation" / "gazebo" / "models"
MODEL_DIRECTORY = MODEL_ROOT / "standard_vtol_mono_cam"


def _module():
    spec = importlib.util.spec_from_file_location("vtol_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vtol_acceptance_is_digest_pinned_and_owned_container_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "px4io/px4-sitl-gazebo@sha256:" in text
    assert "bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8" in text
    assert '"multidetect.purpose=vtol-sitl-scenario-acceptance"' in text
    assert 'MODEL_NAME = "standard_vtol_mono_cam"' in text
    assert 'PX4_SIM_MODEL = f"gz_{MODEL_NAME}"' in text
    assert 'PX4_SYS_AUTOSTART = "4004"' in text
    assert 'CONTAINER_MODEL_ROOT = "/opt/multidetect-gazebo-models"' in text
    assert "PX4_GZ_MODELS = CONTAINER_MODEL_ROOT" in text
    assert '"--publish"' in text
    assert 'f"{mavlink_port}:{mavlink_port}/udp"' in text
    assert '"--mount"' in text
    assert 'f"type=bind,source={model_root},target={CONTAINER_MODEL_ROOT},readonly"' in text
    assert '"model_root_mount_whitelisted"' in text
    assert '"model_root_mount_readonly"' in text
    assert '"no_additional_mounts"' in text
    assert '"no_host_mounts"' not in text
    assert '"rm", container_name' in text
    assert "docker rm -f" not in text.lower()


def test_repository_standard_vtol_mono_cam_model_preserves_tested_composition() -> None:
    config_root = ET.parse(MODEL_DIRECTORY / "model.config").getroot()
    assert config_root.findtext("name") == "standard_vtol_mono_cam"
    assert config_root.findtext("version") == "1.0"
    sdf_reference = config_root.find("sdf")
    assert sdf_reference is not None
    assert sdf_reference.attrib == {"version": "1.9"}
    assert sdf_reference.text == "model.sdf"

    sdf_root = ET.parse(MODEL_DIRECTORY / "model.sdf").getroot()
    assert sdf_root.attrib == {"version": "1.9"}
    model = sdf_root.find("model")
    assert model is not None
    assert model.attrib == {"name": "standard_vtol_mono_cam"}
    includes = model.findall("include")
    assert [include.attrib for include in includes] == [
        {"merge": "true"},
        {"merge": "true"},
    ]
    assert [include.findtext("uri") for include in includes] == [
        "model://standard_vtol",
        "model://mono_cam",
    ]
    include_pose = includes[1].find("pose")
    assert include_pose is not None
    assert include_pose.attrib == {"relative_to": "base_link"}
    assert include_pose.text == "0.35 0 -0.02 0 0.6108652381980153 0"
    joint = model.find("joint")
    assert joint is not None
    assert joint.attrib == {"name": "CameraJoint", "type": "fixed"}
    assert joint.findtext("parent") == "base_link"
    assert joint.findtext("child") == "camera_link"
    pose = joint.find("pose")
    assert pose is not None
    assert pose.attrib == {"relative_to": "base_link"}
    assert pose.text == "0.35 0 -0.02 0 0.6108652381980153 0"


def _valid_container_inspect(module: object) -> dict[str, object]:
    port = 18570
    name = "multidetect-vtol-sitl-scenario-acceptance"
    source = str(MODEL_ROOT.resolve())
    return {
        "Name": f"/{name}",
        "Config": {
            "Image": module.PINNED_IMAGE,
            "Labels": {
                "multidetect.purpose": "vtol-sitl-scenario-acceptance",
            },
            "Env": [
                f"PX4_SIM_MODEL={module.PX4_SIM_MODEL}",
                f"PX4_SYS_AUTOSTART={module.PX4_SYS_AUTOSTART}",
                f"PX4_GZ_MODELS={module.PX4_GZ_MODELS}",
                f"GZ_SIM_RESOURCE_PATH={module.GZ_SIM_RESOURCE_PATH}",
                *(f"PX4_PARAM_{name}={value}" for name, value in module.SIM_SENSOR_PARAMETERS),
            ],
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "PortBindings": {
                f"{port}/udp": [{"HostIp": "", "HostPort": str(port)}],
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": source,
                    "Target": module.CONTAINER_MODEL_ROOT,
                    "ReadOnly": True,
                }
            ],
        },
        "State": {"Running": True},
        "Mounts": [
            {
                "Type": "bind",
                "Source": source,
                "Destination": module.CONTAINER_MODEL_ROOT,
                "RW": False,
            }
        ],
    }


def test_container_inspect_allows_only_exact_readonly_model_root() -> None:
    module = _module()
    record = _valid_container_inspect(module)
    checks = module._assert_owned_container(
        record,
        name="multidetect-vtol-sitl-scenario-acceptance",
        image=module.PINNED_IMAGE,
        port=18570,
        model_root=MODEL_ROOT.resolve(),
    )
    assert checks["model_root_mount_whitelisted"] is True
    assert checks["model_root_mount_readonly"] is True
    assert checks["no_additional_mounts"] is True
    assert checks["exact_px4_gz_models_path"] is True
    assert all(checks.values())


def test_vtol_docker_command_uses_shared_sim_sensor_fix() -> None:
    module = _module()
    command = module._docker_run_command(
        container_name="owned-vtol",
        mavlink_port=18570,
        model_root=MODEL_ROOT.resolve(),
        image=module.PINNED_IMAGE,
    )
    env_values = [
        command[index + 1] for index, value in enumerate(command[:-1]) if value == "--env"
    ]
    assert all(
        f"PX4_PARAM_{name}={value}" in env_values for name, value in module.SIM_SENSOR_PARAMETERS
    )
    startup = command[-1]
    assert "GZ_SIM_SERVER_CONFIG_PATH=/tmp/multidetect-gz-server.config" in startup
    assert "gz-sim-magnetometer-system/d" in startup
    assert "exit 70" in startup and "exit 71" in startup
    assert "ATTEMPTS=180" in startup and "exit 72" in startup


def test_container_inspect_rejects_writable_or_additional_mounts() -> None:
    module = _module()
    wrong_source = _valid_container_inspect(module)
    wrong_source["Mounts"][0]["Source"] = str(MODEL_ROOT.parent.resolve())
    wrong_source["HostConfig"]["Mounts"][0]["Source"] = str(MODEL_ROOT.parent.resolve())
    with pytest.raises(module.VtolAcceptanceError, match="model_root_mount_whitelisted"):
        module._assert_owned_container(
            wrong_source,
            name="multidetect-vtol-sitl-scenario-acceptance",
            image=module.PINNED_IMAGE,
            port=18570,
            model_root=MODEL_ROOT.resolve(),
        )

    writable = _valid_container_inspect(module)
    writable["Mounts"][0]["RW"] = True
    with pytest.raises(module.VtolAcceptanceError, match="model_root_mount_readonly"):
        module._assert_owned_container(
            writable,
            name="multidetect-vtol-sitl-scenario-acceptance",
            image=module.PINNED_IMAGE,
            port=18570,
            model_root=MODEL_ROOT.resolve(),
        )

    additional = _valid_container_inspect(module)
    additional["Mounts"].append(copy.deepcopy(additional["Mounts"][0]))
    additional["HostConfig"]["Mounts"].append(copy.deepcopy(additional["HostConfig"]["Mounts"][0]))
    with pytest.raises(module.VtolAcceptanceError, match="no_additional_mounts"):
        module._assert_owned_container(
            additional,
            name="multidetect-vtol-sitl-scenario-acceptance",
            image=module.PINNED_IMAGE,
            port=18570,
            model_root=MODEL_ROOT.resolve(),
        )


def test_vtol_acceptance_keeps_runtime_truth_and_hardware_boundaries() -> None:
    text = SCRIPT.read_text(encoding="utf-8").lower()
    assert "load_runtime_scenario" in text
    assert "load_scenario_manifest" in text
    assert '"truth_loaded_by_runtime": false' in text
    assert '"expectations_loaded_by_runtime": false' in text
    assert '"real_pixhawk_contacted": false' in text
    assert '"hardware_control_enabled": false' in text
    assert '"operator_visual_acceptance": "not_claimed"' in text
    assert "collector.wait_for_gps_fix(20.0)" in text
    assert "collector.wait_for_navigation_settle()" in text
    assert "collector.collect(2.0)" not in text


def test_runtime_channel_does_not_require_truth_or_expectations() -> None:
    runtime = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM)
    assert runtime.scenario_id == "river_rescue_vtol_001"
    assert runtime.vehicle.model == "px4_vtol_standard"
    assert len(runtime.faults) == 2
    assert not hasattr(runtime, "targets")


def test_fault_timeline_preserves_source_times_and_scales_for_ci() -> None:
    module = _module()
    runtime = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM)
    timeline = module._fault_timeline(runtime, 0.01)
    assert [item["scheduled_at_s"] for item in timeline] == [180.0, 260.0]
    assert [item["effective_at_s"] for item in timeline] == [1.8, 2.6]
    assert all(item["flight_control_output"] is False for item in timeline)


def test_wrapper_uses_project_venv_and_keeps_evidence_explicit() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert "ground-control\\MultiDetectGCS\\.venv\\Scripts\\python.exe" in text
    assert "vtol_sitl_scenario_acceptance.py" in text
    assert "EvidencePath" in text
