from __future__ import annotations

import socket
import struct
import time
from pathlib import Path

import pytest

from multidetect.runtime import RuntimeMode, RuntimeProfile
from multidetect.simulator import (
    AiControlEnvelope,
    ControlAuthorityError,
    ControlAuthorityState,
    SimulationIsolationError,
    SimulatorControlAuthority,
    SimulatorControlGateway,
    SimulatorControlGatewayConfig,
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
    SimulatorMavlinkControlError,
    SimulatorOrchestrator,
    SimulatorOrchestratorError,
    SimulatorProcessSpec,
    ensure_port_available,
    validate_simulation_endpoint,
)
from multidetect.simulator.control_gateway import _frame_v1_manual_control

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


class _FakeMav:
    def __init__(self) -> None:
        self.messages: list[tuple[int, int, int, int, int, int]] = []

    def manual_control_send(
        self, target: int, x: int, y: int, z: int, r: int, buttons: int
    ) -> None:
        self.messages.append((target, x, y, z, r, buttons))


class _FakeConnection:
    def __init__(self) -> None:
        self.mav = _FakeMav()
        self.closed = False

    def close(self) -> None:
            self.closed = True


class _FakeProcess:
    _next_pid = 5000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def send_signal(self, _signal: int) -> None:
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self._returncode = 0
        return 0

    def kill(self) -> None:
        self._returncode = -9


def test_authority_requires_explicit_ai_and_manual_wins() -> None:
    authority = SimulatorControlAuthority()
    assert authority.state is ControlAuthorityState.PAUSED
    with pytest.raises(ControlAuthorityError, match="explicit"):
        authority.submit_ai(roll=0, pitch=0, yaw=0, throttle=0)
    authority.enable_ai()
    authority.release_manual_control()
    assert authority.state is ControlAuthorityState.AI
    authority.submit_ai(roll=0.1, pitch=0, yaw=0, throttle=0.5, timestamp_s=1)
    authority.take_manual_control()
    assert authority.state is ControlAuthorityState.MANUAL
    with pytest.raises(ControlAuthorityError, match="priority"):
        authority.submit_ai(roll=0, pitch=0, yaw=0, throttle=0)
    authority.submit_manual(roll=0, pitch=0, yaw=0, throttle=0, timestamp_s=2)
    authority.release_manual_control()
    assert authority.state is ControlAuthorityState.AI


def test_authority_pause_and_failsafe_clear_stale_setpoint() -> None:
    authority = SimulatorControlAuthority(initial=ControlAuthorityState.MANUAL)
    authority.submit_manual(roll=0, pitch=0, yaw=0, throttle=0.2)
    authority.pause()
    assert authority.last_setpoint is None
    authority.failsafe("test")
    assert authority.state is ControlAuthorityState.FAILSAFE


def test_authority_rejects_invalid_setpoint_without_mutating_state() -> None:
    authority = SimulatorControlAuthority()
    with pytest.raises(ControlAuthorityError, match=r"within \[-1, 1\]"):
        authority.submit_manual(roll=2.0, pitch=0.0, yaw=0.0, throttle=0.5)
    assert authority.state is ControlAuthorityState.PAUSED
    assert authority.manual_override is False
    assert authority.last_setpoint is None

    authority.enable_ai()
    authority.release_manual_control()
    with pytest.raises(ControlAuthorityError, match="throttle"):
        authority.submit_ai(roll=0.0, pitch=0.0, yaw=0.0, throttle=-0.1)
    assert authority.state is ControlAuthorityState.AI
    assert authority.manual_override is False
    assert authority.last_setpoint is None


def test_simulator_dry_run_binds_sim_profile_and_seed(tmp_path: Path) -> None:
    orchestrator = SimulatorOrchestrator(SCENARIO, dry_run=True, job_root=tmp_path)
    assert orchestrator.runtime_descriptor.mode is RuntimeMode.SIM
    assert orchestrator.runtime_descriptor.profile == RuntimeProfile.sim()
    result = orchestrator.start()
    assert result.status == "planned"
    assert (result.job_dir / "runtime-descriptor.json").is_file()
    assert (result.job_dir / "result.json").is_file()
    assert result.control["literal_loopback_only"] is True
    assert (result.job_dir / "control.json").is_file()
    assert (result.job_dir / "control-gateway.json").is_file()


def test_simulator_starts_and_stops_shared_control_gateway(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        control_port = listener.getsockname()[1]
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=False,
        job_root=tmp_path,
        qgc_control_port=control_port,
        process_factory=lambda *_args, **_kwargs: _FakeProcess(),
        process_specs=(SimulatorProcessSpec("fake", ("fake",)),),
    )
    result = orchestrator.start()
    try:
        assert result.status == "running"
        deadline = time.monotonic() + 1
        while orchestrator._gateway_thread is not None and time.monotonic() < deadline:
            if orchestrator._gateway_error is not None:
                break
            time.sleep(0.01)
        assert orchestrator._gateway_error is None
    finally:
        orchestrator.stop()
    assert orchestrator._gateway_thread is None
    assert (result.job_dir / "control-gateway.json").is_file()


def test_simulator_rejects_seed_mismatch(tmp_path: Path) -> None:
    with pytest.raises(SimulatorOrchestratorError, match="does not match"):
        SimulatorOrchestrator(SCENARIO, seed=1, dry_run=True, job_root=tmp_path)


def test_simulation_endpoint_rejects_real_and_non_loopback() -> None:
    with pytest.raises(SimulationIsolationError):
        validate_simulation_endpoint("serial://COM3")
    with pytest.raises(SimulationIsolationError):
        validate_simulation_endpoint("udp://192.0.2.10:14540")
    validate_simulation_endpoint("gazebo://river/front-camera")


def test_ensure_port_available_detects_bound_port() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        with pytest.raises(SimulationIsolationError):
            ensure_port_available("127.0.0.1", port)


def test_mavlink_control_maps_manual_axes_and_manual_revokes_ai() -> None:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)

    authority.enable_ai()
    authority.release_manual_control()
    adapter.submit_ai(roll=0.2, pitch=-0.1, yaw=0.3, throttle=0.5, timestamp_s=10)
    receipt = adapter.submit_manual(roll=0.4, pitch=-0.2, yaw=0.1, throttle=0.6, timestamp_s=11)

    assert receipt.source == "manual"
    assert authority.state is ControlAuthorityState.MANUAL
    assert connection.mav.messages == [(1, -100, 200, 500, 300, 0), (1, -200, 400, 600, 100, 0)]
    with pytest.raises(ControlAuthorityError, match="priority"):
        adapter.submit_ai(roll=0, pitch=0, yaw=0, throttle=0.5, timestamp_s=12)


def test_mavlink_control_rejects_unsafe_endpoint_and_ai_envelope() -> None:
    with pytest.raises(SimulationIsolationError, match="literal"):
        SimulatorMavlinkControlConfig(endpoint="udpout:localhost:14540")
    authority = SimulatorControlAuthority()
    authority.enable_ai()
    authority.release_manual_control()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(ai_envelope=AiControlEnvelope(max_roll=0.2)),
        connection=_FakeConnection(),
    )
    with pytest.raises(SimulatorMavlinkControlError, match="roll"):
        adapter.submit_ai(roll=0.3, pitch=0, yaw=0, throttle=0.5)


def test_mavlink_config_validates_optional_manual_watchdog() -> None:
    config = SimulatorMavlinkControlConfig(manual_watchdog_s=0.75)
    assert config.manual_watchdog_s == pytest.approx(0.75)
    assert SimulatorMavlinkControlConfig(manual_watchdog_s=None).manual_watchdog_s is None
    with pytest.raises(ValueError, match="manual_watchdog_s"):
        SimulatorMavlinkControlConfig(manual_watchdog_s=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="manual_watchdog_s"):
        SimulatorMavlinkControlConfig(manual_watchdog_s=0.0)
    with pytest.raises(ValueError, match="manual_watchdog_s"):
        SimulatorMavlinkControlConfig(manual_watchdog_s=float("nan"))


def test_mavlink_config_rejects_bool_numeric_values() -> None:
    for field in ("target_system", "source_system", "source_component"):
        with pytest.raises(ValueError, match=field):
            SimulatorMavlinkControlConfig(**{field: True})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="watchdog_s"):
        SimulatorMavlinkControlConfig(watchdog_s=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite numbers"):
        AiControlEnvelope(max_roll=True)  # type: ignore[arg-type]


def test_mavlink_adapter_rejects_bool_timestamps() -> None:
    authority = SimulatorControlAuthority()
    authority.enable_ai()
    authority.release_manual_control()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=_FakeConnection())

    with pytest.raises(ValueError, match="timestamp_s"):
        adapter.submit_ai(roll=0, pitch=0, yaw=0, throttle=0.5, timestamp_s=True)  # type: ignore[arg-type]

    adapter.submit_ai(roll=0, pitch=0, yaw=0, throttle=0.5, timestamp_s=1.0)
    with pytest.raises(ValueError, match="now_s"):
        adapter.check_watchdog(now_s=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timestamp_s"):
        adapter.shutdown(timestamp_s=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timestamp_s"):
        adapter.request_failsafe("test", timestamp_s=True)  # type: ignore[arg-type]


def test_mavlink_control_watchdog_neutralizes_stale_ai_command() -> None:
    authority = SimulatorControlAuthority()
    authority.enable_ai()
    authority.release_manual_control()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)
    adapter.submit_ai(roll=0.1, pitch=0.2, yaw=-0.3, throttle=0.5, timestamp_s=5)

    assert adapter.check_watchdog(now_s=5.49) is None
    receipt = adapter.check_watchdog(now_s=5.5)

    assert receipt is not None
    assert receipt.source == "watchdog"
    assert authority.state is ControlAuthorityState.FAILSAFE
    assert connection.mav.messages[-1] == (1, 0, 0, 500, 0, 0)


def test_mavlink_control_manual_watchdog_neutralizes_stale_qgc_command() -> None:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(manual_watchdog_s=0.5),
        connection=connection,
    )

    receipt = adapter.submit_manual(
        roll=0.2,
        pitch=-0.1,
        yaw=0.3,
        throttle=0.6,
        timestamp_s=5.0,
    )
    assert receipt.source == "manual"
    assert adapter.check_watchdog(now_s=5.49) is None
    watchdog = adapter.check_watchdog(now_s=5.5)

    assert watchdog is not None
    assert watchdog.source == "manual_watchdog"
    assert watchdog.state == ControlAuthorityState.FAILSAFE.value
    assert authority.state is ControlAuthorityState.FAILSAFE
    assert connection.mav.messages[-1] == (1, 0, 0, 500, 0, 0)
    assert adapter.check_watchdog(now_s=6.0) is None
    evidence = adapter.evidence()
    assert evidence["manual_watchdog_s"] == pytest.approx(0.5)
    assert evidence["manual_watchdog_neutral_count"] == 1
    assert evidence["manual_control_active"] is False


def test_mavlink_control_manual_watchdog_ignores_released_manual_stream() -> None:
    authority = SimulatorControlAuthority()
    authority.enable_ai()
    authority.release_manual_control()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(manual_watchdog_s=0.5),
        connection=_FakeConnection(),
    )
    adapter.submit_manual(roll=0, pitch=0, yaw=0, throttle=0.5, timestamp_s=5.0)

    authority.release_manual_control()
    assert authority.state is ControlAuthorityState.AI
    assert adapter.check_watchdog(now_s=50.0) is None
    assert adapter.evidence()["manual_control_active"] is False


def test_mavlink_control_shutdown_neutralizes_active_manual_stream() -> None:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(manual_watchdog_s=0.5),
        connection=connection,
    )
    adapter.submit_manual(roll=0.1, pitch=0, yaw=0, throttle=0.55, timestamp_s=2.0)

    shutdown = adapter.shutdown(timestamp_s=2.1)
    assert shutdown is not None
    assert shutdown.source == "shutdown"
    assert authority.state is ControlAuthorityState.FAILSAFE
    assert connection.mav.messages[-1] == (1, 0, 0, 500, 0, 0)


def test_mavlink_control_signed_mode_uses_zero_for_neutral() -> None:
    authority = SimulatorControlAuthority()
    authority.enable_ai()
    authority.release_manual_control()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        connection=connection,
        config=SimulatorMavlinkControlConfig(
            throttle_wire_mode="signed",
            watchdog_s=0.25,
        ),
    )
    adapter.submit_ai(roll=0.0, pitch=0.0, yaw=0.0, throttle=0.5, timestamp_s=2.0)
    receipt = adapter.check_watchdog(now_s=2.25)
    assert receipt is not None
    assert receipt.z == 0
    assert connection.mav.messages[-1] == (1, 0, 0, 0, 0, 0)


def test_sim_control_gateway_ticks_shared_ai_watchdog() -> None:
    authority = SimulatorControlAuthority()
    authority.enable_ai()
    authority.release_manual_control()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)
    gateway = SimulatorControlGateway(
        adapter,
        config=SimulatorControlGatewayConfig(
            qgc_input_port=14655,
            px4_output_endpoint=adapter.config.endpoint,
        ),
    )
    adapter.submit_ai(roll=0.1, pitch=0, yaw=0, throttle=0.5, timestamp_s=5)

    assert gateway.poll_watchdog(now_s=5.49) is None
    receipt = gateway.poll_watchdog(now_s=5.5)
    assert receipt is not None
    assert receipt.source == "watchdog"
    assert connection.mav.messages[-1] == (1, 0, 0, 500, 0, 0)


def test_sim_control_gateway_routes_qgc_manual_control_through_shared_adapter() -> None:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)
    gateway = SimulatorControlGateway(
        adapter,
        config=SimulatorControlGatewayConfig(
            qgc_input_port=14651,
            px4_output_endpoint=adapter.config.endpoint,
        ),
    )

    receipts = gateway.process_datagram(
        _frame_v1_manual_control(pitch=-0.25, roll=0.4, yaw=0.1, throttle=0.65),
        source=("127.0.0.1", 51321),
        timestamp_s=12,
    )

    assert len(receipts) == 1
    assert receipts[0].source == "manual"
    assert authority.state is ControlAuthorityState.MANUAL
    assert connection.mav.messages == [(1, -250, 400, 650, 100, 0)]
    evidence = gateway.evidence()
    assert evidence["manual_frames_accepted"] == 1
    assert evidence["rejected_frames"] == 0
    assert evidence["authority"]["source_counts"] == {"manual": 1}


def test_sim_control_gateway_rejects_non_qgc_and_non_loopback_frames() -> None:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)
    gateway = SimulatorControlGateway(
        adapter,
        config=SimulatorControlGatewayConfig(
            qgc_input_port=14652,
            px4_output_endpoint=adapter.config.endpoint,
        ),
    )

    gateway.process_datagram(
        _frame_v1_manual_control(pitch=0, roll=0, yaw=0, throttle=0.5, source_system=42),
        source=("127.0.0.1", 1),
    )
    gateway.process_datagram(
        _frame_v1_manual_control(pitch=0, roll=0, yaw=0, throttle=0.5),
        source=("192.0.2.10", 1),
    )

    assert connection.mav.messages == []
    evidence = gateway.evidence()
    assert evidence["manual_frames_accepted"] == 0
    assert evidence["rejected_frames"] == 2
    assert evidence["non_loopback_datagrams_blocked"] == 1


def test_sim_control_gateway_rejects_truncated_and_out_of_range_manual_control() -> None:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)
    gateway = SimulatorControlGateway(
        adapter,
        config=SimulatorControlGatewayConfig(
            qgc_input_port=14653,
            px4_output_endpoint=adapter.config.endpoint,
        ),
    )

    gateway.process_datagram(b"\xfe\x00\x00\xff\xbe\x45\x00\x00")
    payload = struct.pack("<hhhhHB", 1001, 0, 500, 0, 0, 1)
    gateway.process_datagram(bytes([0xFE, len(payload), 0, 255, 190, 69]) + payload + b"\x00\x00")

    assert connection.mav.messages == []
    assert gateway.evidence()["rejected_frames"] == 2
