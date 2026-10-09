"""SIM-only endpoint guards."""

from __future__ import annotations

from urllib.parse import urlparse

from multidetect.simulator.isolation import SimulationIsolationError


def validate_local_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise SimulationIsolationError("endpoint must be non-empty")
    value = endpoint.strip()
    p = urlparse(value)
    if p.scheme in {"gazebo", "sim", "mock"}:
        return
    if p.scheme in {"udp", "tcp", "rtsp", "ws", "wss", "http", "https"} and p.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        return
    raise SimulationIsolationError("SIM endpoint must target localhost")


def assert_localhost(endpoint: str) -> str:
    validate_local_endpoint(endpoint)
    return endpoint


__all__ = ["SimulationIsolationError", "validate_local_endpoint", "assert_localhost"]
