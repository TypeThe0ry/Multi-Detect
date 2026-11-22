"""Offline upgrade contracts; opt in to source checks with PX4_MAIN_SOURCE_ROOT.

No PX4 build, Docker, simulator, network or GPU is used.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN_COMMIT = "53afcc82b2a51a90424ff7e2c35583e491587eb5"
FRAME_NAMES = ("4050_gz_delta_tailsitter", "4051_gz_delta_tailsitter_gimbal")


def parameters(path):
    return dict(re.findall(r"^param set-default (\S+) (\S+)$", path.read_text(), re.M))


def test_default_profile_is_byte_identical_to_explicit_pinned(tmp_path, monkeypatch):
    from scripts.run_simulator_docker import build_docker_simulator_plan
    scenario = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
    monkeypatch.delenv("MULTIDETECT_PX4_PROFILE", raising=False)
    implicit = build_docker_simulator_plan(scenario, repository_root=ROOT, job_dir=tmp_path / "implicit", gpu_render=False)
    explicit = build_docker_simulator_plan(scenario, repository_root=ROOT, job_dir=tmp_path / "explicit", gpu_render=False, px4_profile="pinned")
    assert implicit.command == explicit.command
    assert implicit.environment == explicit.environment
    assert implicit.to_mapping()["required_sim_parameters"] == explicit.to_mapping()["required_sim_parameters"]
    assert implicit.to_mapping()["px4_profile"] == explicit.to_mapping()["px4_profile"] == "pinned"


def test_main_profile_uses_gnss_and_records_both_airframe_hashes(tmp_path):
    from scripts.run_simulator_docker import build_docker_simulator_plan
    scenario = ROOT / "configs/scenarios/airframes/delta_tailsitter_gimbal_cam_001.scenario.json"
    plan = build_docker_simulator_plan(scenario, repository_root=ROOT, job_dir=tmp_path / "main", gpu_render=False, px4_profile="main")
    env = dict(plan.environment)
    mapping = plan.to_mapping()
    assert env["PX4_PARAM_SENS_GNSS0_DELAY"] == "0"
    assert "PX4_PARAM_SENS_GPS0_DELAY" not in env
    assert mapping["px4_profile"] == "main"
    for key, path in (("custom_airframe", ROOT / "simulation/px4/airframes-main/4051_gz_delta_tailsitter_gimbal"), ("base_airframe", ROOT / "simulation/px4/airframes-main/4050_gz_delta_tailsitter")):
        assert mapping["mounts"][key]["host"] == str(path.resolve())
        assert mapping["mounts"][key]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "PX4 main parameter read-back mismatch" in plan.command[-1]


def test_parameter_readback_mismatch_raises():
    from scripts.collect_sim_teacher_episodes import _require_px4_param_readback
    with pytest.raises(RuntimeError, match="read-back mismatch"):
        _require_px4_param_readback("SENS_GNSS0_DELAY", "0", 0, "110")


def test_collector_metadata_carries_selected_profile(tmp_path, monkeypatch):
    from scripts.collect_sim_teacher_episodes import CollectionPlan
    monkeypatch.setattr("scripts.collect_sim_teacher_episodes.docker_command", lambda plan: [])
    plan = CollectionPlan("scenario", "runtime", "truth", "hover", 1, 1, 0, str(tmp_path), px4_profile="main")
    assert plan.to_mapping()["px4_profile"] == "main"


def test_navigation_parameter_names_are_profile_specific_and_fail_on_mixed_sets():
    from multidetect.autonomy.sim_navigation_reference import navigation_parameters
    assert "SENS_GPS0_OFFX" in navigation_parameters("pinned")
    assert "SENS_GNSS0_OFFX" in navigation_parameters("main")
    with pytest.raises(Exception, match="exactly one"):
        navigation_parameters(available_names={"SENS_GPS0_ID", "SENS_GNSS0_ID"})


@pytest.mark.parametrize("name", FRAME_NAMES)
def test_candidate_preserves_all_flight_and_gimbal_parameter_values(name):
    old = ROOT / "simulation/px4/airframes" / name
    candidate = ROOT / "simulation/px4/airframes-main" / name
    assert parameters(candidate) == parameters(old)
    subprocess.run(["sh", "-n", str(candidate)], check=True, capture_output=True)


@pytest.mark.parametrize("model", [None, "external_model_override"])
def test_main_gimbal_wrapper_selects_model_before_base_defaults(tmp_path, model):
    init = tmp_path / "etc/init.d"
    init.mkdir(parents=True)
    (init / "rc.vtol_defaults").write_text("")
    frames = tmp_path / "etc/init.d-posix/airframes"
    frames.mkdir(parents=True)
    for name in FRAME_NAMES:
        shutil.copyfile(ROOT / "simulation/px4/airframes-main" / name, frames / name)
    env = {key: value for key, value in os.environ.items() if key != "PX4_SIM_MODEL"}
    if model:
        env["PX4_SIM_MODEL"] = model
    script = 'param() { :; }; R="$1/"; . "$2"; printf "%s" "$PX4_SIM_MODEL"'
    result = subprocess.run(
        ["sh", "-c", script, "upgrade-test", str(tmp_path), str(frames / FRAME_NAMES[1])],
        env=env, text=True, capture_output=True, check=True,
    )
    assert result.stdout == (model or "delta_tailsitter_gimbal_cam")


@pytest.fixture
def main_source():
    source = os.environ.get("PX4_MAIN_SOURCE_ROOT")
    if not source:
        pytest.skip("set PX4_MAIN_SOURCE_ROOT to the audited PX4 main checkout")
    source = Path(source)
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == MAIN_COMMIT, "re-audit a different main revision"
    return source


def test_main_parameters_exist_in_metadata_including_generated_instances(main_source):
    # Metadata definitions, not airframe examples, establish parameter existence.
    files = subprocess.check_output(
        ["rg", "--files", str(main_source / "src"), "-g", "*.yaml", "-g", "*.c"], text=True,
    ).splitlines()
    patterns = []
    for filename in files:
        path = Path(filename)
        for line in path.read_text(errors="replace").splitlines():
            match = (re.match(r"^\s+([A-Z][A-Za-z0-9_${}]*):\s*$", line)
                     if path.suffix == ".yaml"
                     else re.search(r"PARAM_DEFINE_\w+\(([A-Z][A-Z0-9_]*),", line))
            if match:
                patterns.append(re.escape(match[1]).replace(re.escape("${i}"), r"\d+"))
    actuator = (main_source / "src/modules/simulation/gz_bridge/module.yaml").read_text()
    assert "param_prefix: SIM_GZ_EC" in actuator and "num_channels: *max_num_motors" in actuator
    missing = []
    for name in FRAME_NAMES:
        for parameter in parameters(ROOT / "simulation/px4/airframes-main" / name):
            if re.fullmatch(r"SIM_GZ_EC_(FUNC|MIN|MAX)[1-4]", parameter):
                continue
            if not any(re.fullmatch(pattern, parameter) for pattern in patterns):
                missing.append(parameter)
    assert not missing


def test_gimbal_patch_applies_and_populates_status_without_touching_main(main_source, tmp_path):
    relative = Path("src/modules/simulation/gz_bridge/GZGimbal.cpp")
    original = (main_source / relative).read_bytes()
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    patch = ROOT / "simulation/px4/patches/gz-gimbal-attitude-device-id.patch"
    subprocess.run(["git", "-C", str(main_source), "apply", "--check", str(patch)],
                   check=True, capture_output=True)
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=tmp_path,
                   check=True, capture_output=True)
    subprocess.run(["git", "apply", str(patch)], cwd=tmp_path, check=True, capture_output=True)
    body = target.read_text().split("void GZGimbal::publishDeviceAttitude()", 1)[1].split(
        "void GZGimbal::publishJointCommand", 1)[0]
    assert body.count("gimbal_att.gimbal_device_id = _gimbal_device_id;") == 1
    assert (main_source / relative).read_bytes() == original


def test_main_has_lifetime_fix_without_wall_clock_polling(main_source):
    scheduler = main_source / "platforms/posix/src/px4/common/lockstep_scheduler"
    source = (scheduler / "src/lockstep_scheduler.cpp").read_text()
    header = (scheduler / "include/lockstep_scheduler/lockstep_scheduler.h").read_text()
    assert "signal_next" in header and "_signaling_mutex" in header
    assert "MAX_WAKEUPS" not in source
    assert "int result = pthread_cond_wait(cond, lock);" in source
    assert "pthread_cond_timedwait(" not in source
    assert "std::lock_guard<std::mutex> lock_signaling(_signaling_mutex);" in source
    assert "if (_setting_time)" in source


def test_main_sensor_override_needs_gnss_name(main_source):
    translation = (main_source / "src/lib/parameters/param_translation.cpp").read_text()
    assert '{"SENS_GPS0_DELAY", "SENS_GNSS0_DELAY"}' in translation
    definitions = (main_source / "src/modules/sensors/vehicle_gps_position/module.yaml").read_text()
    assert "SENS_GNSS${i}_DELAY:" in definitions
    assert "SENS_GPS${i}_DELAY:" not in definitions
