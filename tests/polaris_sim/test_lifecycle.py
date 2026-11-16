from types import SimpleNamespace

from scripts.polaris_sim.lifecycle import _select_gpus


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
