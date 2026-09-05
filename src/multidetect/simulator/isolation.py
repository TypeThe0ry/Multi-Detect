"""Capability discovery and endpoint/port isolation for simulator runs."""

from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class SimulationIsolationError(ValueError):
    """Raised when a SIM configuration could reach a REAL endpoint."""


@dataclass(frozen=True, slots=True)
class ToolCapability:
    name: str
    executable: str | None
    available: bool
    version: str | None = None
    reason: str | None = None
    # ``docker.exe --version`` only proves that the client is installed.  Keep
    # the daemon probe separate so callers can distinguish a usable Docker
    # runtime from a PATH/install-only result.  These fields are ``None`` for
    # tools which do not have a background daemon (PX4/Gazebo).
    daemon_available: bool | None = None
    daemon_version: str | None = None
    daemon_reason: str | None = None


def validate_simulation_endpoint(uri: str) -> None:
    """Allow only simulator schemes and loopback transports."""
    if not isinstance(uri, str) or not uri.strip():
        raise SimulationIsolationError("simulation endpoint must be a non-empty URI")
    value = uri.strip()
    parsed = urlparse(value)
    lowered = value.lower()
    if lowered.startswith(("serial:", "com:", "\\\\.\\com", "/dev/tty", "usb:")):
        raise SimulationIsolationError(f"REAL serial endpoint is forbidden in SIM: {uri}")
    if parsed.scheme in {"gazebo", "sim", "mock", "udp"}:
        if parsed.scheme == "udp" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise SimulationIsolationError("SIM UDP endpoint must bind to loopback")
        return
    if parsed.scheme in {"tcp", "rtsp", "ws", "wss", "http", "https"}:
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise SimulationIsolationError("SIM network endpoint must bind to loopback")
        return
    raise SimulationIsolationError(
        f"unsupported or unsafe SIM endpoint scheme: {parsed.scheme or '<none>'}"
    )


def ensure_port_available(host: str, port: int) -> None:
    if not 1 <= int(port) <= 65535:
        raise SimulationIsolationError(f"invalid port: {port}")
    try:
        with socket.socket(
            socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM
        ) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, int(port)))
    except OSError as exc:
        raise SimulationIsolationError(f"SIM port {host}:{port} is unavailable: {exc}") from exc


def _short_process_error(result: object) -> str | None:
    """Return a bounded, non-sensitive subprocess error summary."""

    for field in ("stderr", "stdout"):
        value = getattr(result, field, "")
        if isinstance(value, str):
            for line in value.splitlines():
                line = line.strip()
                if line:
                    return line[:240]
    return None


def _probe_docker_daemon(executable: str) -> tuple[bool, str | None, str | None]:
    """Probe Docker Server health without creating containers or opening ports."""

    try:
        result = subprocess.run(
            [executable, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, f"daemon probe failed: {exc.__class__.__name__}"

    if getattr(result, "returncode", 1) != 0:
        detail = _short_process_error(result)
        return False, None, detail or "Docker daemon returned a non-zero exit code"
    output = getattr(result, "stdout", "")
    if not isinstance(output, str):
        output = ""
    server_version = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not server_version:
        return False, None, "Docker daemon returned no Server version"
    return True, server_version[:120], None


def _tool(name: str, candidates: tuple[str, ...]) -> ToolCapability:
    executable = next((shutil.which(item) for item in candidates if shutil.which(item)), None)
    if executable is None and name == "docker":
        windows_docker = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
        if windows_docker.is_file():
            executable = str(windows_docker)
    if executable is None:
        return ToolCapability(
            name,
            None,
            False,
            reason="executable not found on PATH",
            daemon_available=False if name == "docker" else None,
            daemon_reason="Docker client executable not found" if name == "docker" else None,
        )
    try:
        completed = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=3, check=False
        )
        version = (completed.stdout or completed.stderr).splitlines()[0][:200] or None
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolCapability(
            name,
            executable,
            True,
            reason=f"version probe failed: {exc}",
            daemon_available=False if name == "docker" else None,
            daemon_reason="Docker client version probe failed" if name == "docker" else None,
        )
    if name != "docker":
        return ToolCapability(name, executable, True, version=version)
    daemon_available, daemon_version, daemon_reason = _probe_docker_daemon(executable)
    return ToolCapability(
        name,
        executable,
        True,
        version=version,
        daemon_available=daemon_available,
        daemon_version=daemon_version,
        daemon_reason=daemon_reason,
    )


def discover_simulator_tools() -> dict[str, ToolCapability]:
    return {
        "px4": _tool("px4", ("px4", "px4-sitl")),
        "gazebo": _tool("gazebo", ("gz", "gazebo", "ign")),
        "docker": _tool("docker", ("docker",)),
    }


__all__ = [
    "SimulationIsolationError",
    "ToolCapability",
    "discover_simulator_tools",
    "ensure_port_available",
    "validate_simulation_endpoint",
]
