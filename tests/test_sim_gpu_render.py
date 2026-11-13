from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import check_sim_gpu_render as check
from scripts.run_simulator_docker import build_docker_simulator_plan


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


def test_gpu_render_plan_adds_nvidia_egl_and_ogre2_options(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "gpu",
        gpu_render=True,
        gpu_device=0,
    )
    assert plan.gpu_render is True
    assert plan.gpu_device == "0"
    assert plan.command[plan.command.index("--gpus") : plan.command.index("--gpus") + 2] == (
        "--gpus",
        "device=0",
    )
    environment = dict(plan.environment)
    assert environment["NVIDIA_DRIVER_CAPABILITIES"] == "graphics,utility,compute"
    assert environment["NVIDIA_VISIBLE_DEVICES"] == "0"
    assert environment["EGL_PLATFORM"] == "surfaceless"
    assert environment["GZ_SIM_RENDER_ENGINE"] == "ogre2"
    assert environment["GZ_SIM_HEADLESS_RENDERING"] == "1"
    assert environment["LIBGL_ALWAYS_SOFTWARE"] == "0"
    startup = plan.command[-1]
    assert "--headless-rendering" in startup
    assert "--render-engine ogre2" in startup


def test_gpu_render_accepts_explicit_local_derived_image(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "derived",
        image="multidetect/px4-sitl-gazebo:gpu",
        gpu_render=True,
        gpu_device=1,
    )
    assert plan.image == "multidetect/px4-sitl-gazebo:gpu"


def test_gpu_worker_round_robin_skips_default_avoid_list(tmp_path: Path) -> None:
    first = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "first",
        gpu_render=True,
        worker_index=0,
    )
    second = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "second",
        gpu_render=True,
        worker_index=1,
    )
    third = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "third",
        gpu_render=True,
        worker_index=2,
    )
    assert (first.gpu_device, second.gpu_device, third.gpu_device) == ("0", "1", "0")


def test_gpu_env_pool_round_robins_four_worker_slots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MULTIDETECT_SIM_GPU", "1")
    monkeypatch.setenv("MULTIDETECT_SIM_GPU_DEVICES", "0,1")
    plans = [
        build_docker_simulator_plan(
            SCENARIO,
            repository_root=ROOT,
            job_dir=tmp_path / f"worker-{worker}",
            image="multidetect/px4-sitl-gazebo:gpu",
            worker_index=worker,
        )
        for worker in range(4)
    ]
    assert [plan.gpu_device for plan in plans] == ["0", "1", "0", "1"]


def test_gpu_render_env_can_enable_and_explicit_false_disables(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MULTIDETECT_SIM_GPU", "1")
    enabled = build_docker_simulator_plan(
        SCENARIO, repository_root=ROOT, job_dir=tmp_path / "enabled"
    )
    disabled = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "disabled",
        gpu_render=False,
    )
    assert enabled.gpu_render is True and enabled.gpu_device == "1"
    assert "--gpus" not in disabled.command
    assert disabled.gpu_render is False and disabled.gpu_device is None


def test_check_main_uses_mocked_subprocess_and_gpu_command(tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ["image", "inspect"]:
            import json
            from multidetect.simulator.runtime_image import load_contract
            c = load_contract()
            return SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': 'sha256:'+'7'*64, 'RootFS': c['rootfs'], 'Config': c['config'], **c['platform']}]), stderr='')
        if command[1:3] == ["inspect", "--format"]:
            return SimpleNamespace(returncode=0, stdout="true", stderr="")
        if command[1] == "exec" and any("eglinfo" in token for token in command):
            return SimpleNamespace(returncode=0, stdout="EGL vendor string: NVIDIA", stderr="")
        if command[1] == "logs":
            return SimpleNamespace(returncode=0, stdout="OGRE 2.3", stderr="")
        if command[1] == "exec":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(check.subprocess, "run", fake_run)
    code = check.main(
        [
            "--scenario",
            str(SCENARIO),
            "--job-dir",
            str(tmp_path / "check"),
            "--gpu-device",
            "1",
            "--frames",
            "1",
        ]
    )
    assert code == 1  # mocked stream has no complete camera frame
    assert any(command[0:3] == ["docker", "run", "-d"] for command in calls)
    launch = next(command for command in calls if command[0:3] == ["docker", "run", "-d"])
    assert launch[launch.index("--gpus") : launch.index("--gpus") + 2] == ["--gpus", "device=1"]
    assert '"gpu_rendering_active": true' in capsys.readouterr().out

