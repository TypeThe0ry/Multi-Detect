"""Host-side endpoints for telemetry emitted by Docker SITL containers."""

from __future__ import annotations

import functools
import ipaddress
import json
import os
import subprocess
import sys
import warnings


def sim_docker_network(network: str | None = None) -> str:
    """Return the Docker network selected for SIM containers."""

    value = network or os.environ.get("MULTIDETECT_SIM_DOCKER_NETWORK", "bridge")
    value = value.strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError("SIM Docker network must be a non-empty network name")
    return value


def sim_network_mode(mode: str | None = None) -> str:
    """Resolve the SITL container network mode."""

    value = mode if mode is not None else os.environ.get("MULTIDETECT_SIM_NETWORK", "bridge")
    if not isinstance(value, str):
        raise ValueError("SIM network mode must be 'bridge' or 'host'")
    value = value.strip().lower()
    if value not in {"bridge", "host"}:
        raise ValueError("SIM network mode must be 'bridge' or 'host'")
    if value == "host" and (os.name == "nt" or not sys.platform.startswith("linux")):
        raise ValueError("SIM network mode 'host' is supported on Linux only")
    return value


@functools.lru_cache(maxsize=16)
def _docker_network_gateway(network: str) -> str:
    result = subprocess.run(
        ["docker", "network", "inspect", network],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    records = json.loads(result.stdout)
    configs = records[0]["IPAM"]["Config"]
    for config in configs:
        gateway = config.get("Gateway")
        if gateway:
            address = ipaddress.ip_address(str(gateway))
            if address.is_unspecified:
                raise ValueError(f"Docker network {network!r} returned a wildcard gateway")
            return str(address)
    raise ValueError(f"Docker network {network!r} has no configured gateway")


def sim_listen_host(network: str | None = None, *, mode: str | None = None) -> str:
    """Return a private host address that receives Docker SITL telemetry.

    Host mode always uses loopback. Otherwise ``MULTIDETECT_SIM_LISTEN_HOST``
    overrides platform discovery; on Linux the selected Docker network's
    gateway is used, keeping the socket off LAN interfaces. Windows retains
    Docker Desktop's loopback behavior.
    """

    if sim_network_mode(mode) == "host":
        return "127.0.0.1"
    override = os.environ.get("MULTIDETECT_SIM_LISTEN_HOST", "").strip()
    if override:
        if override in {"0.0.0.0", "::"}:
            raise ValueError("MULTIDETECT_SIM_LISTEN_HOST cannot be a wildcard address")
        return override
    if os.name == "nt":
        return "127.0.0.1"
    selected_network = sim_docker_network(network)
    try:
        gateway = _docker_network_gateway(selected_network)
        if gateway in {"0.0.0.0", "::"}:
            raise ValueError("Docker returned a wildcard gateway")
        return gateway
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
        warnings.warn(
            f"Cannot inspect Docker network {selected_network!r} for the SITL telemetry "
            f"listener ({exc}); falling back to 127.0.0.1. On Linux, set "
            "MULTIDETECT_SIM_LISTEN_HOST to the reachable Docker gateway address.",
            RuntimeWarning,
            stacklevel=2,
        )
        return "127.0.0.1"


def sim_udpin_endpoint(
    port: int, network: str | None = None, *, mode: str | None = None
) -> str:
    """Build a pymavlink UDP listener endpoint for a Docker SITL telemetry port."""

    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SIM telemetry port must be in 1..65535")
    host = sim_listen_host(network) if mode is None else sim_listen_host(network, mode=mode)
    return f"udpin:{host}:{port}"

