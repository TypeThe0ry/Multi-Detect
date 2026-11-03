from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from multidetect.simulator import (
    SimulatorOrchestrator,
    SimulatorOrchestratorError,
    SimulatorProcessSpec,
)
from multidetect.simulator.readiness import (
    SimulatorHeartbeatObservation,
    SimulatorHeartbeatObserver,
    SimulatorProcessReadinessProbe,
    SimulatorReadinessError,
    SimulatorReadinessProbe,
    parse_readiness_endpoint,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _heartbeat(*, sequence: int = 1, system_id: int = 1, component_id: int = 1) -> bytes:
    # MAVLink v1 heartbeat payload is nine bytes.  The decoder intentionally
    # does not require a CRC for this parser-level readiness fixture.
    return bytes((0xFE, 9, sequence & 0xFF, system_id, component_id, 0)) + bytes(9) + bytes(2)


class _Process:
    def __init__(self, *, returncode: int | None = None, pid: int = 1001) -> None:
        self.returncode = returncode
        self.pid = pid

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, _signal: int) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class _Mav:
    def manual_control_send(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.mav = _Mav()

    def close(self) -> None:
        return None


class _FakeSocket:
    already_bound = True

    def __init__(self, datagrams: list[tuple[bytes, tuple[str, int]]]) -> None:
        self.datagrams = list(datagrams)
        self.bound: tuple[str, int] | None = None
        self.closed = False
        self.timeout: float | None = None

    def bind(self, endpoint: tuple[str, int]) -> None:
        self.bound = endpoint

    def setblocking(self, _value: bool) -> None:
        return None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        if not self.datagrams:
            raise TimeoutError
        return self.datagrams.pop(0)

    def close(self) -> None:
        self.closed = True


class _EmptySocket(_FakeSocket):
    def __init__(self) -> None:
        super().__init__([])

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        raise TimeoutError


def test_endpoint_parser_requires_literal_loopback() -> None:
    assert parse_readiness_endpoint("udp://127.0.0.1:14541") == ("127.0.0.1", 14541)
    assert parse_readiness_endpoint("udpout:127.0.0.1:14541") == ("127.0.0.1", 14541)
    assert parse_readiness_endpoint("[::1]:14541") == ("::1", 14541)
    with pytest.raises(SimulatorReadinessError, match="literal loopback"):
        parse_readiness_endpoint("udp://localhost:14541")
    with pytest.raises(SimulatorReadinessError, match="literal loopback"):
        parse_readiness_endpoint("udp://192.0.2.1:14541")


def test_heartbeat_observer_filters_identity_and_preserves_evidence() -> None:
    observer = SimulatorHeartbeatObserver(expected_system_id=1, expected_component_ids={1})
    assert observer.feed(_heartbeat(system_id=2), source=("127.0.0.1", 14541)) == 0
    assert observer.feed(_heartbeat(), source=("127.0.0.1", 14541)) == 1
    observation = observer.observation()
    assert observation.observed is True
    assert observation.heartbeat_frames == 1
    assert observation.frames_received == 2
    assert observation.source_system_ids == (1,)
    assert "unexpected_source_system" in observation.errors
    assert observation.to_mapping()["observed"] is True


def test_probe_reports_required_process_health_and_missing_processes() -> None:
    probe = SimulatorReadinessProbe()
    result = probe.probe(
        {"px4": _Process(pid=42), "gazebo": _Process(returncode=1, pid=43)},
        expected_processes=("px4", "gazebo", "qgc"),
    )
    assert result.process_ready is False
    assert result.required_processes_present is False
    by_name = {item["name"]: item for item in result.to_mapping()["processes"]}
    assert by_name["px4"]["healthy"] is True
    assert by_name["gazebo"]["returncode"] == 1
    assert by_name["qgc"]["present"] is False
    assert result.gate_passed is False
    assert result.evidence_hash == result.evidence_hash


def test_probe_accepts_injected_datagrams_without_starting_processes() -> None:
    process = _Process(pid=77)
    probe = SimulatorReadinessProbe()
    result = probe.probe(
        {"px4": process},
        expected_processes=("px4",),
        heartbeat_endpoint=("127.0.0.1", 15441),
        heartbeat_datagrams=[_heartbeat()],
        require_heartbeat=True,
    )
    assert process.returncode is None
    assert result.ready is True
    assert result.heartbeat.observed is True
    encoded = result.to_mapping_with_hash()
    assert encoded["sha256"] == result.evidence_hash
    json.dumps(encoded)


def test_probe_uses_fake_udp_socket_and_can_close_owned_fixture() -> None:
    fake = _FakeSocket([(_heartbeat(), ("127.0.0.1", 15442))])
    probe = SimulatorReadinessProbe(poll_interval_s=0)
    result = probe.probe(
        [("px4", _Process(pid=88))],
        expected_processes=("px4",),
        heartbeat_endpoint=("127.0.0.1", 15442),
        heartbeat_socket=fake,
        require_heartbeat=True,
        timeout_s=0.1,
        close_socket=True,
    )
    assert fake.bound == ("127.0.0.1", 15442)
    assert fake.closed is True
    assert result.ready is True
    assert result.heartbeat.datagrams_received == 1


def test_probe_zero_argument_socket_factory_and_empty_timeout_are_bounded() -> None:
    fake = _EmptySocket()
    probe = SimulatorReadinessProbe(socket_factory=lambda: fake, poll_interval_s=0)
    result = probe.probe(
        {"px4": _Process()},
        expected_processes=("px4",),
        heartbeat_endpoint=("127.0.0.1", 15443),
        heartbeat_socket=None,
        require_heartbeat=True,
        timeout_s=0.1,
    )
    assert result.ready is False
    assert result.heartbeat.observed is False
    assert fake.closed is True


def test_observation_endpoint_is_validated_even_when_object_is_injected() -> None:
    observation = SimulatorHeartbeatObservation(
        endpoint="127.0.0.1:15444",
        heartbeat_frames=1,
        frames_received=1,
    )
    with pytest.raises(SimulatorReadinessError, match="conflicts"):
        SimulatorReadinessProbe().probe(
            {"px4": _Process()},
            expected_processes=("px4",),
            heartbeat_endpoint=("127.0.0.1", 15445),
            heartbeat_observation=observation,
            require_heartbeat=True,
        )
    with pytest.raises(SimulatorReadinessError, match="literal loopback"):
        SimulatorHeartbeatObservation.from_mapping(
            {"heartbeat_frames": 1, "frames_received": 1},
            endpoint="192.0.2.1:15444",
        )


def test_probe_rejects_non_loopback_injected_source() -> None:
    observer = SimulatorHeartbeatObserver()
    assert observer.feed(_heartbeat(), source=("203.0.113.2", 14541)) == 0
    assert observer.observation().observed is False
    assert observer.observation().errors == ("non_loopback_source",)


def test_alias_is_public_and_mapping_observation_adapts_router_counters() -> None:
    assert SimulatorProcessReadinessProbe is SimulatorReadinessProbe
    observation = SimulatorHeartbeatObservation.from_mapping(
        {
            "px4_message_ids": {"0": 2, "33": 5},
            "px4_frames_received": 7,
            "px4_datagrams_received": 3,
        },
        endpoint="127.0.0.1:14541",
        source="interactive_router",
    )
    assert observation.heartbeat_frames == 2
    assert observation.frames_received == 7
    assert observation.datagrams_received == 3
    result = SimulatorReadinessProbe().probe(
        {"px4": _Process()},
        expected_processes=("px4",),
        heartbeat_observation=observation,
        require_heartbeat=True,
    )
    assert result.ready is True


def test_mapping_observation_errors_cannot_be_hidden_by_heartbeat_counter() -> None:
    observation = SimulatorHeartbeatObservation(
        endpoint="127.0.0.1:15446",
        heartbeat_frames=1,
        frames_received=1,
        rejected_frames=1,
        errors=("unexpected_source_system",),
    )
    result = SimulatorReadinessProbe().probe(
        {"px4": _Process()},
        expected_processes=("px4",),
        heartbeat_endpoint="127.0.0.1:15446",
        heartbeat_observation=observation,
        require_heartbeat=True,
    )
    assert result.heartbeat_ready is True
    assert result.ready is False
    assert "unexpected_source_system" in result.errors
    assert "heartbeat_rejected_frames" in result.errors


@pytest.mark.usefixtures("require_loopback_sockets")
def test_orchestrator_publishes_process_readiness_and_final_return_codes(
    tmp_path: Path,
) -> None:
    qgc_port = _free_port()
    px4_port = _free_port()
    processes: list[_Process] = []

    def factory(*_args: object, **_kwargs: object) -> _Process:
        process = _Process(pid=2000 + len(processes))
        processes.append(process)
        return process

    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=False,
        job_root=tmp_path,
        qgc_control_port=qgc_port,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_port}",
        process_factory=factory,
        process_specs=(
            SimulatorProcessSpec("px4", ("fake-px4",)),
            SimulatorProcessSpec("gazebo", ("fake-gazebo",)),
        ),
        control_connection=_Connection(),
    )
    result = orchestrator.start()
    assert result.readiness["event"] == "simulator_runtime_readiness"
    assert result.readiness["ready"] is True
    assert result.readiness["required_processes_alive"] is True
    readiness_path = result.job_dir / "readiness.json"
    document = json.loads(readiness_path.read_text(encoding="utf-8"))
    assert document["sha256"] == result.readiness["sha256"]
    assert {item["name"] for item in document["processes"]} == {"gazebo", "px4"}

    orchestrator.stop()
    assert orchestrator.last_result is not None
    stopped = orchestrator.last_result
    assert stopped.readiness["event"] == "simulator_runtime_readiness"
    stopped_by_name = {item["name"]: item for item in stopped.readiness["processes"]}
    assert stopped_by_name["px4"]["returncode"] == 0
    assert stopped_by_name["gazebo"]["returncode"] == 0


@pytest.mark.usefixtures("require_loopback_sockets")
def test_orchestrator_strict_readiness_rejects_process_that_exited_on_start(
    tmp_path: Path,
) -> None:
    qgc_port = _free_port()
    px4_port = _free_port()
    with pytest.raises(SimulatorOrchestratorError, match="runtime readiness"):
        SimulatorOrchestrator(
            SCENARIO,
            dry_run=False,
            job_root=tmp_path,
            qgc_control_port=qgc_port,
            px4_control_endpoint=f"udpout:127.0.0.1:{px4_port}",
            process_factory=lambda *_args, **_kwargs: _Process(returncode=1),
            process_specs=(SimulatorProcessSpec("px4", ("dead-px4",)),),
            control_connection=_Connection(),
            readiness_strict=True,
        ).start()


def test_orchestrator_rejects_boolean_readiness_timeout(tmp_path: Path) -> None:
    with pytest.raises(SimulatorOrchestratorError, match="readiness_timeout_s"):
        SimulatorOrchestrator(SCENARIO, dry_run=True, job_root=tmp_path, readiness_timeout_s=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.bind(("127.0.0.1", 0))
        return int(channel.getsockname()[1])
