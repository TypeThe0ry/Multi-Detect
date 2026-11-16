import json
from types import SimpleNamespace

from scripts.polaris_sim import lifecycle
from scripts.polaris_sim.lifecycle import _resolve_world, _select_gpus


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
