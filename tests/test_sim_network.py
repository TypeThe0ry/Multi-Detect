from __future__ import annotations

import json
import subprocess

import pytest

from multidetect import sim_network


def test_sim_listen_host_uses_override_before_platform(monkeypatch) -> None:
    monkeypatch.setattr(sim_network.os, "name", "nt")
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "192.168.64.1")
    assert sim_network.sim_listen_host() == "192.168.64.1"


def test_sim_listen_host_keeps_windows_loopback(monkeypatch) -> None:
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    monkeypatch.setattr(sim_network.os, "name", "nt")
    assert sim_network.sim_listen_host() == "127.0.0.1"


def test_sim_listen_host_uses_cached_selected_docker_network(monkeypatch) -> None:
    calls: list[list[str]] = []

    def inspect(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([{"IPAM": {"Config": [{"Gateway": "172.29.0.1"}]}}]),
            stderr="",
        )

    monkeypatch.setattr(sim_network.os, "name", "posix")
    monkeypatch.setenv("MULTIDETECT_SIM_DOCKER_NETWORK", "sim-net")
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    monkeypatch.setattr(sim_network.subprocess, "run", inspect)
    sim_network._docker_network_gateway.cache_clear()

    assert sim_network.sim_listen_host() == "172.29.0.1"
    assert sim_network.sim_listen_host() == "172.29.0.1"
    assert calls == [["docker", "network", "inspect", "sim-net"]]


def test_sim_listen_host_warns_and_falls_back_when_docker_inspect_fails(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise FileNotFoundError("docker unavailable")

    monkeypatch.setattr(sim_network.os, "name", "posix")
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    monkeypatch.setattr(sim_network.subprocess, "run", fail)
    sim_network._docker_network_gateway.cache_clear()

    with pytest.warns(RuntimeWarning, match="falling back to 127.0.0.1"):
        assert sim_network.sim_listen_host("custom") == "127.0.0.1"


def test_sim_udpin_endpoint_uses_helper_host(monkeypatch) -> None:
    monkeypatch.setattr(sim_network, "sim_listen_host", lambda network=None: "172.17.0.1")
    assert sim_network.sim_udpin_endpoint(14541) == "udpin:172.17.0.1:14541"


def test_sim_listen_host_rejects_wildcard_override(monkeypatch) -> None:
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="wildcard"):
        sim_network.sim_listen_host()


def test_host_network_forces_loopback_listener(monkeypatch) -> None:
    monkeypatch.setattr(sim_network.os, "name", "posix")
    monkeypatch.setenv("MULTIDETECT_SIM_NETWORK", "host")
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "192.168.64.1")
    monkeypatch.setattr(
        sim_network.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("Docker inspect is not used"),
    )
    assert sim_network.sim_listen_host() == "127.0.0.1"
    assert sim_network.sim_udpin_endpoint(14541) == "udpin:127.0.0.1:14541"


def test_host_network_is_rejected_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(sim_network.os, "name", "nt")
    with pytest.raises(ValueError, match="Linux only"):
        sim_network.sim_network_mode("host")
