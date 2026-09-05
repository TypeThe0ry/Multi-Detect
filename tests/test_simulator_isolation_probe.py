from __future__ import annotations

from types import SimpleNamespace

import multidetect.simulator.isolation as isolation


def _fake_which(name: str) -> str | None:
    return "docker" if name == "docker" else None


def test_discover_simulator_tools_reports_docker_daemon_health(monkeypatch) -> None:
    monkeypatch.setattr(isolation.shutil, "which", _fake_which)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1] == "--version":
            return SimpleNamespace(returncode=0, stdout="Docker version 28.4.0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="28.4.0\n", stderr="")

    monkeypatch.setattr(isolation.subprocess, "run", fake_run)
    capabilities = isolation.discover_simulator_tools()

    docker = capabilities["docker"]
    assert docker.available is True
    assert docker.executable == "docker"
    assert docker.daemon_available is True
    assert docker.daemon_version == "28.4.0"
    assert docker.daemon_reason is None
    assert [argv[1] for argv in calls] == ["--version", "version"]


def test_discover_simulator_tools_keeps_client_available_when_daemon_is_down(monkeypatch) -> None:
    monkeypatch.setattr(isolation.shutil, "which", _fake_which)

    def fake_run(argv, **_kwargs):
        if argv[1] == "--version":
            return SimpleNamespace(returncode=0, stdout="Docker version 28.4.0\n", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon at npipe://...",
        )

    monkeypatch.setattr(isolation.subprocess, "run", fake_run)
    docker = isolation.discover_simulator_tools()["docker"]

    assert docker.available is True
    assert docker.version == "Docker version 28.4.0"
    assert docker.daemon_available is False
    assert docker.daemon_version is None
    assert docker.daemon_reason == "Cannot connect to the Docker daemon at npipe://..."


def test_probe_cli_includes_daemon_fields(monkeypatch, capsys) -> None:
    import multidetect.simulator.__main__ as simulator_cli

    monkeypatch.setattr(
        simulator_cli,
        "discover_simulator_tools",
        lambda: {
            "docker": isolation.ToolCapability(
                "docker",
                "docker.exe",
                True,
                version="Docker version 28.4.0",
                daemon_available=True,
                daemon_version="28.4.0",
            )
        },
    )
    from multidetect.simulator.__main__ import main

    assert main(["probe", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"daemon_available": true' in output
    assert '"daemon_version": "28.4.0"' in output
