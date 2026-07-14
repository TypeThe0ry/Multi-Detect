from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "px4_sitl_gcs_heartbeat.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("px4_sitl_gcs_heartbeat", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _MavRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def heartbeat_send(self, *values: Any) -> None:
        self.calls.append(values)


class _Connection:
    def __init__(self) -> None:
        self.mav = _MavRecorder()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_loopback_endpoint_is_strict_and_rejects_real_networks() -> None:
    module = _load_script()

    assert module.loopback_port("udpout:127.0.0.1:18570") == 18570
    for endpoint in (
        "udpout:192.168.0.3:14550",
        "udp:127.0.0.1:18570",
        "udpout:localhost:18570",
        "udpout:127.0.0.1:80",
    ):
        with pytest.raises(module.SitlHeartbeatError):
            module.loopback_port(endpoint)


def test_sender_emits_only_bounded_gcs_heartbeats_and_closes() -> None:
    module = _load_script()
    connection = _Connection()
    times = iter((0.0, 0.0, 0.5, 1.0))

    result = module.send_heartbeats(
        "udpout:127.0.0.1:18570",
        1.0,
        2.0,
        connection_factory=lambda *_args, **_kwargs: connection,
        monotonic=lambda: next(times),
        sleep=lambda _seconds: None,
    )

    assert len(connection.mav.calls) == 2
    assert all(call[0] == module.mavutil.mavlink.MAV_TYPE_GCS for call in connection.mav.calls)
    assert all(
        call[1] == module.mavutil.mavlink.MAV_AUTOPILOT_INVALID for call in connection.mav.calls
    )
    assert connection.closed is True
    assert result["messages_transmitted"] == 2
    assert result["software_only"] is True
    assert result["real_v6x_contacted"] is False


def test_sender_rejects_unbounded_duration_and_rate() -> None:
    module = _load_script()

    with pytest.raises(module.SitlHeartbeatError):
        module.send_heartbeats("udpout:127.0.0.1:18570", 121.0, 2.0)
    with pytest.raises(module.SitlHeartbeatError):
        module.send_heartbeats("udpout:127.0.0.1:18570", 8.0, 11.0)


def test_cli_requires_explicit_owned_sitl_acknowledgement() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--acknowledge-owned-disposable-sitl" in text
    assert '"real_v6x_contacted": False' in text
