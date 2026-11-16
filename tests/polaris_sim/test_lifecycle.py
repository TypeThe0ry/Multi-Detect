import json
from types import SimpleNamespace

import pytest

from multidetect.polaris_sim.camera import get_camera_profile
from scripts.polaris_sim import lifecycle
from scripts.polaris_sim.lifecycle import (
    MEMWATCH_UNIT,
    _generate_camera_overlay,
    _resolve_world,
    _select_gpus,
)


def test_auto_gpu_assignment_checks_compute_apps_and_spreads_vehicles(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--query-compute-apps=pid,used_memory,gpu_uuid" in command:
            return SimpleNamespace(returncode=0, stdout="11, 2000, GPU-1\n22, 500, GPU-2\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="1, GPU-1, 6000, 20000, 20\n2, GPU-2, 4000, 20000, 10\n",
            stderr="",
        )

    monkeypatch.setattr("scripts.polaris_sim.lifecycle.subprocess.run", run)
    assert _select_gpus("auto", 3, dry_run=False) == ["2", "1", "2"]
    assert any("--query-compute-apps=pid,used_memory,gpu_uuid" in command for command in calls)


def test_dry_run_keeps_auto_gpu_unresolved_when_driver_is_unavailable(monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi unavailable")

    monkeypatch.setattr("scripts.polaris_sim.lifecycle.subprocess.run", run)
    assert _select_gpus("auto", 2, dry_run=True) == ["auto", "auto"]


def test_world_resolution_uses_configured_dirs_and_singapore_default(tmp_path, monkeypatch):
    standard = tmp_path / "standard"
    configured = tmp_path / "configured"
    default = tmp_path / "default"
    for directory in (standard, configured, default):
        directory.mkdir()
    selected = configured / "rw_fixture__clear_noon.sdf"
    selected.write_text("<sdf/>", encoding="utf-8")
    default_sdf = default / "rw_singapore_marina_bay__clear_noon.sdf"
    default_sdf.write_text("<sdf/>", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "WORLD_DIR", standard)
    monkeypatch.setattr(lifecycle, "DEFAULT_CITY_WORLD_DIR", default)
    monkeypatch.setenv(
        "POLARIS_SIM_WORLD_DIRS", f"{tmp_path / 'unused'}:{configured}"
    )

    assert _resolve_world("rw_fixture", "clear_noon") == ("rw_fixture", selected.resolve())
    monkeypatch.delenv("POLARIS_SIM_WORLD_DIRS")
    assert _resolve_world(None, "clear_noon") == (
        "rw_singapore_marina_bay",
        default_sdf.resolve(),
    )


def test_dry_run_emits_full_docker_command_for_external_city_world(monkeypatch, capsys):
    monkeypatch.setattr(lifecycle, "_select_gpus", lambda _gpu, count, dry_run: ["2"] * count)
    image = "multidetect/px4-sitl-gazebo:gz-rendering-830-r1"
    monkeypatch.setattr(lifecycle, "_local_image", lambda _gpu: image)

    result = lifecycle.main(
        ["up", "--gpu", "2", "--vehicles", "1", "--dry-run"]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["world"] == "rw_singapore_marina_bay"
    assert output["world_sdf"].endswith(
        "/runs/cities-recovery-20261109/worlds/rw_singapore_marina_bay__clear_noon.sdf"
    )
    command = output["slots"][0]["docker_command"]
    assert output["camera_profile"] == "lite"
    assert output["video_settings"] == {"width": 640, "height": 480, "fps": 10}
    assert "psim-sitl-slot-5" in output["units"]
    assert command[:3] == ["docker", "run", "--pull=never"]
    assert "--rm" in command
    assert "--memory=16g" in command
    gpu_index = command.index("--gpus")
    assert tuple(command[gpu_index : gpu_index + 2]) == ("--gpus", "device=2")
    assert any(
        "/runs/cities-recovery-20261109/worlds" in token
        and "/opt/polaris-sim-world-resources" in token
        for token in command
    )
    assert any(
        "source=/data/scratch/polaris-sim/overlay/lite/models" in token
        and "target=/opt/polaris-sim-camera-overlay" in token
        and "readonly" in token
        for token in command
    )
    resource_path = next(item for item in command if item.startswith("GZ_SIM_RESOURCE_PATH="))
    assert resource_path.startswith("GZ_SIM_RESOURCE_PATH=/opt/polaris-sim-camera-overlay:")


def test_camera_profile_defaults_and_overlay_generation(tmp_path):
    assert get_camera_profile("lite").video_settings() == {
        "width": 640,
        "height": 480,
        "fps": 10,
    }
    assert get_camera_profile("standard").video_settings() == {
        "width": 960,
        "height": 720,
        "fps": 15,
    }
    assert get_camera_profile("hd").video_settings() == {
        "width": 1280,
        "height": 720,
        "fps": 20,
    }
    with pytest.raises(ValueError, match="camera profile"):
        get_camera_profile("ultra")
    assert _generate_camera_overlay("hd", "<sdf/>", tmp_path) is None

    source = (
        "<sdf><model name='mono_cam'><link name='camera_link'>"
        "<sensor name='camera' type='camera'><update_rate>30</update_rate>"
        "<camera><image><width>1280</width><height>960</height></image></camera>"
        "</sensor></link></model></sdf>"
    )
    destination = _generate_camera_overlay("lite", source, tmp_path)
    assert destination == tmp_path / "models/mono_cam/model.sdf"
    contents = destination.read_text(encoding="utf-8")
    assert "<width>640</width>" in contents
    assert "<height>480</height>" in contents
    assert "<update_rate>10</update_rate>" in contents


def test_down_stops_memwatch_and_clears_recorded_pid(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"phase": "running", "units": [MEMWATCH_UNIT], "memwatch_pid": 123}),
        encoding="utf-8",
    )
    monkeypatch.setenv("POLARIS_SIM_STATE_FILE", str(state_file))
    stopped = []
    monkeypatch.setattr(lifecycle, "_systemctl", lambda action, unit: stopped.append(unit) or True)

    assert lifecycle._down() == 0
    assert MEMWATCH_UNIT in stopped
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["memwatch_pid"] is None


def test_up_passes_memory_limit_and_state_path_to_supervisor(tmp_path, monkeypatch, capsys):
    world_sdf = tmp_path / "rw_fixture__clear_noon.sdf"
    world_sdf.write_text("<sdf/>", encoding="utf-8")
    scenario = tmp_path / "fixture.scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("POLARIS_SIM_STATE_FILE", str(state_file))
    monkeypatch.setenv("POLARIS_SIM_CONTAINER_MEMORY", "12g")
    monkeypatch.setattr(lifecycle, "_read_state", lambda: {})
    monkeypatch.setattr(
        lifecycle, "_resolve_world", lambda *_args: ("rw_fixture", world_sdf)
    )
    monkeypatch.setattr(lifecycle, "_scenario_path", lambda *_args: scenario)
    monkeypatch.setattr(lifecycle, "_select_gpus", lambda *_args, **_kwargs: ["2"])
    monkeypatch.setattr(lifecycle, "_local_image", lambda _gpu: "local:image")
    monkeypatch.setattr(lifecycle, "_systemctl", lambda *_args: False)
    calls = []
    monkeypatch.setattr(
        lifecycle,
        "_systemd_run",
        lambda unit, command, memory, **kwargs: calls.append((unit, kwargs)),
    )

    assert lifecycle.main(["up", "--gpu", "2", "--ready-timeout", "0"]) == 2
    assert calls[0][0] == lifecycle.SESSION_UNIT
    assert calls[0][1]["env"] == {
        "POLARIS_SIM_STATE_FILE": str(state_file),
        "POLARIS_SIM_CONTAINER_MEMORY": "12g",
    }
    assert "SITL readiness timeout" in capsys.readouterr().err
