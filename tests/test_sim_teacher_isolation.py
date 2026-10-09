from __future__ import annotations

import pytest

from multidetect.sim_teacher.isolation import validate_local_endpoint
from multidetect.simulator.isolation import SimulationIsolationError


@pytest.mark.parametrize(
    "endpoint",
    ["udp://127.0.0.1:14540", "tcp://localhost:5760", "gazebo://sim", "sim://world"],
)
def test_isolation_accepts_local_sim_endpoints(endpoint: str) -> None:
    validate_local_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    ["udp://192.168.1.40:14540", "https://example.test/api", "tcp://10.0.0.1:1", ""],
)
def test_isolation_rejects_non_localhost(endpoint: str) -> None:
    with pytest.raises(SimulationIsolationError):
        validate_local_endpoint(endpoint)
