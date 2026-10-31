"""Host-side endpoints for telemetry emitted by Docker SITL containers."""

from __future__ import annotations

import functools
import ipaddress
import json
import os
import subprocess
import warnings


def sim_docker_network(network: str | None = None) -> str:
    """Return the Docker network selected for SIM containers."""

    value = network or os.environ.get("MULTIDETECT_SIM_DOCKER_NETWORK", "bridge")
    value = value.strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError("SIM Docker network must be a non-empty network name")
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


def sim_listen_host(network: str | None = None) -> str:
    """Return a private host address that receives Docker SITL telemetry.

    ``MULTIDETECT_SIM_LISTEN_HOST`` overrides platform discovery. On Linux the
    selected Docker network's gateway is used, keeping the socket off LAN
    interfaces. Windows retains Docker Desktop's loopback behavior.
    """

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


def sim_udpin_endpoint(port: int, network: str | None = None) -> str:
    """Build a pymavlink UDP listener endpoint for a Docker SITL telemetry port."""

    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SIM telemetry port must be in 1..65535")
    return f"udpin:{sim_listen_host(network)}:{port}"

