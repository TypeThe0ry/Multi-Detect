from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.fixed_wing_aim_control import (
    FixedWingAimConfig,
    FixedWingAimController,
    FixedWingAimExecutor,
    FixedWingAimState,
    FixedWingAimTarget,
    PixhawkFlightControlConfig,
    PixhawkFlightControlProvider,
)
from multidetect.multimodal_ranging import CameraCalibration
from multidetect.pixhawk import (
    PIXHAWK_AUTOPILOT_IDS,
    PIXHAWK_VEHICLE_TYPE_IDS,
    PixhawkRcInputSnapshot,
    PixhawkReadOnlyConfig,
)
from multidetect.unified_tracking import UnifiedTrackState


def _calibration() -> CameraCalibration:
    return CameraCalibration("fixed-rgb-v1", 1280, 720, 900.0, 900.0, 640.0, 360.0)


def _telemetry(**changes: object) -> VehicleTelemetry:
    values: dict[str, object] = {
        "altitude_agl_m": 60.0,
        "roll_deg": 2.0,
        "pitch_deg": 1.0,
        "ground_speed_mps": 21.0,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "position_healthy": True,
        "link_healthy": True,
        "flight_mode_allows_deploy": True,
        "release_zone_clear": True,
        "heading_deg": 82.0,
        "armed": True,
        "flight_mode": "AUTO",
        "attitude_observed_at_s": 10.0,
        "position_observed_at_s": 10.0,
        "airspeed_mps": 19.0,
        "airspeed_observed_at_s": 10.0,
    }
    values.update(changes)
    return VehicleTelemetry(**values)


def _target(**changes: object) -> FixedWingAimTarget:
    values: dict[str, object] = {
        "target_id": "vehicle-7",
        "target_revision": 3,
        "bbox": BoundingBox(0.70, 0.58, 0.80, 0.68),
        "observed_at_s": 10.0,
        "state": UnifiedTrackState.TRACKING,
        "locked": True,
        "primary": True,
    }
    values.update(changes)
    return FixedWingAimTarget(**values)


def test_controller_centers_with_bounded_attitude_and_holds_original_heading() -> None:
    controller = FixedWingAimController(_calibration())
    first = controller.evaluate(
        target=_target(),
        telemetry=_telemetry(),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )
    assert first.state is FixedWingAimState.ACTIVE
    assert first.setpoint is not None
    assert first.setpoint.roll_deg > 2.0
    assert first.setpoint.pitch_deg < 1.0
    assert abs(first.setpoint.roll_deg) <= 20.0
    assert abs(first.setpoint.pitch_deg) <= 15.0
    assert first.setpoint.yaw_deg == 82.0
    assert sum(value * value for value in first.setpoint.quaternion_wxyz) == pytest.approx(1.0)

    second = controller.evaluate(
        target=_target(observed_at_s=10.15),
        telemetry=_telemetry(heading_deg=89.0, attitude_observed_at_s=10.15),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.2,
    )
    assert second.setpoint is not None
    assert second.setpoint.yaw_deg == 82.0
    assert abs(second.setpoint.roll_deg - first.setpoint.roll_deg) <= 3.5 + 1e-9
    assert abs(second.setpoint.pitch_deg - first.setpoint.pitch_deg) <= 2.5 + 1e-9


@pytest.mark.parametrize(
    ("target", "telemetry", "mode3", "confirmed", "reason"),
    [
        (_target(), _telemetry(), False, True, "mode3_inactive"),
        (_target(), _telemetry(), True, False, "execution_confirmation_required"),
        (None, _telemetry(), True, True, "locked_target_unavailable"),
        (_target(locked=False), _telemetry(), True, True, "target_not_locked_primary"),
        (_target(state=UnifiedTrackState.LOST), _telemetry(), True, True, "target_lost"),
        (_target(observed_at_s=9.0), _telemetry(), True, True, "target_stale"),
        (_target(), _telemetry(armed=False), True, True, "vehicle_not_armed"),
        (_target(), _telemetry(link_healthy=False), True, True, "flight_controller_link_unhealthy"),
        (_target(), _telemetry(airspeed_mps=8.0), True, True, "airspeed_below_control_minimum"),
        (_target(), _telemetry(altitude_agl_m=4.0), True, True, "altitude_below_control_minimum"),
    ],
)
def test_controller_gates_real_control(
    target: FixedWingAimTarget | None,
    telemetry: VehicleTelemetry,
    mode3: bool,
    confirmed: bool,
    reason: str,
) -> None:
    decision = FixedWingAimController(_calibration()).evaluate(
        target=target,
        telemetry=telemetry,
        mode3_active=mode3,
        execution_confirmed=confirmed,
        now_s=10.1,
    )
    assert decision.state is FixedWingAimState.INHIBITED
    assert decision.reason == reason
    assert decision.setpoint is None


def test_reacquisition_continues_bounded_aim_without_changing_heading_reference() -> None:
    controller = FixedWingAimController(_calibration())
    active = controller.evaluate(
        target=_target(),
        telemetry=_telemetry(),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )
    reacquiring = controller.evaluate(
        target=_target(
            state=UnifiedTrackState.REACQUIRING,
            observed_at_s=10.15,
            bbox=BoundingBox(0.62, 0.48, 0.72, 0.58),
        ),
        telemetry=_telemetry(heading_deg=95.0, attitude_observed_at_s=10.15),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.2,
    )
    assert active.setpoint is not None
    assert reacquiring.state is FixedWingAimState.REACQUIRING
    assert reacquiring.setpoint is not None
    assert reacquiring.setpoint.reacquiring is True
    assert reacquiring.setpoint.yaw_deg == active.setpoint.yaw_deg == 82.0


class _RecordingTransport:
    def __init__(self) -> None:
        self.setpoints = []
        self.modes: list[str] = []
        self.rc_input_snapshot = _rc_input(10.0)

    def send_attitude_target(self, setpoint) -> None:
        self.setpoints.append(setpoint)

    def request_mode(self, mode: str) -> None:
        self.modes.append(mode)


def _rc_input(observed_at_s: float, *, channel: int = 1, pwm: int = 1500) -> PixhawkRcInputSnapshot:
    channels: list[int | None] = [1500] * 18
    channels[channel - 1] = pwm
    return PixhawkRcInputSnapshot(observed_at_s, tuple(channels))


def test_executor_prestreams_then_enters_offboard_and_returns_on_lck_loss() -> None:
    transport = _RecordingTransport()
    controller = FixedWingAimController(
        _calibration(),
        FixedWingAimConfig(prestream_setpoints=3),
    )
    executor = FixedWingAimExecutor(controller, transport)
    states = []
    for index in range(3):
        now_s = 10.1 + index * 0.05
        states.append(
            executor.step(
                target=_target(observed_at_s=now_s - 0.01),
                telemetry=_telemetry(attitude_observed_at_s=now_s - 0.01),
                mode3_active=True,
                execution_confirmed=True,
                now_s=now_s,
            ).state
        )
    assert states == [
        FixedWingAimState.PRESTREAM,
        FixedWingAimState.PRESTREAM,
        FixedWingAimState.ACTIVE,
    ]
    assert len(transport.setpoints) == 3
    assert transport.modes == ["OFFBOARD"]

    stopped = executor.step(
        target=_target(state=UnifiedTrackState.LOST, observed_at_s=10.25),
        telemetry=_telemetry(flight_mode="OFFBOARD", attitude_observed_at_s=10.25),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.3,
    )
    assert stopped.state is FixedWingAimState.INHIBITED
    assert transport.modes == ["OFFBOARD", "AUTO"]


def test_executor_restores_the_mode_observed_before_offboard() -> None:
    transport = _RecordingTransport()
    executor = FixedWingAimExecutor(
        FixedWingAimController(_calibration(), FixedWingAimConfig(prestream_setpoints=1)),
        transport,
    )
    executor.step(
        target=_target(),
        telemetry=_telemetry(flight_mode="POSCTL"),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )
    executor.step(
        target=None,
        telemetry=_telemetry(flight_mode="OFFBOARD", attitude_observed_at_s=10.15),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.2,
    )
    assert transport.modes == ["OFFBOARD", "POSCTL"]


def test_any_meaningful_rc_channel_change_cancels_aim_and_restores_entry_mode() -> None:
    transport = _RecordingTransport()
    executor = FixedWingAimExecutor(
        FixedWingAimController(
            _calibration(),
            FixedWingAimConfig(prestream_setpoints=1, rc_cancel_threshold_us=50),
        ),
        transport,
    )
    active = executor.step(
        target=_target(),
        telemetry=_telemetry(flight_mode="POSCTL"),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )
    transport.rc_input_snapshot = _rc_input(10.15, channel=7, pwm=1600)
    cancelled = executor.step(
        target=_target(observed_at_s=10.14),
        telemetry=_telemetry(flight_mode="OFFBOARD", attitude_observed_at_s=10.14),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.16,
    )

    assert active.state is FixedWingAimState.ACTIVE
    assert cancelled.state is FixedWingAimState.INHIBITED
    assert cancelled.reason == "pilot_input_cancelled"
    assert transport.modes == ["OFFBOARD", "POSCTL"]
    assert len(transport.setpoints) == 1


def test_rc_mode_switch_cancels_aim_without_overriding_the_pilot_selected_mode() -> None:
    transport = _RecordingTransport()
    executor = FixedWingAimExecutor(
        FixedWingAimController(
            _calibration(),
            FixedWingAimConfig(prestream_setpoints=1),
        ),
        transport,
    )
    executor.step(
        target=_target(),
        telemetry=_telemetry(flight_mode="AUTO"),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )
    transport.rc_input_snapshot = _rc_input(10.15, channel=8, pwm=1700)
    cancelled = executor.step(
        target=_target(observed_at_s=10.14),
        telemetry=_telemetry(flight_mode="MANUAL", attitude_observed_at_s=10.14),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.16,
    )

    assert cancelled.reason == "pilot_input_cancelled"
    assert transport.modes == ["OFFBOARD"]


def test_missing_or_stale_rc_input_blocks_fixed_wing_aim_setpoints() -> None:
    transport = _RecordingTransport()
    transport.rc_input_snapshot = _rc_input(9.0)
    executor = FixedWingAimExecutor(FixedWingAimController(_calibration()), transport)

    decision = executor.step(
        target=_target(),
        telemetry=_telemetry(),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )

    assert decision.state is FixedWingAimState.INHIBITED
    assert decision.reason == "rc_input_unavailable_or_stale"
    assert transport.setpoints == []


class _FakeMav:
    def __init__(self) -> None:
        self.attitude_calls: list[tuple[object, ...]] = []
        self.mode_calls: list[tuple[object, ...]] = []
        self.command_long_calls: list[tuple[object, ...]] = []

    def set_attitude_target_send(self, *args: object) -> None:
        self.attitude_calls.append(args)

    def set_mode_send(self, *args: object) -> None:
        self.mode_calls.append(args)

    def command_long_send(self, *args: object) -> None:
        self.command_long_calls.append(args)


class _FakeConnection:
    def __init__(self) -> None:
        self.mav = _FakeMav()
        self.flightmode = "AUTO"

    @staticmethod
    def recv_match(*, blocking: bool):
        assert blocking is False
        return None

    @staticmethod
    def mode_mapping() -> dict[str, int]:
        return {"AUTO": 4 << 16, "OFFBOARD": 6 << 16}

    @staticmethod
    def close() -> None:
        return None


def _flight_provider() -> tuple[PixhawkFlightControlProvider, _FakeConnection]:
    provider = PixhawkFlightControlProvider(
        PixhawkFlightControlConfig(
            PixhawkReadOnlyConfig(
                "udp:0.0.0.0:14550",
                expected_system_id=1,
                expected_autopilot_id=PIXHAWK_AUTOPILOT_IDS["px4"],
                expected_vehicle_type_id=PIXHAWK_VEHICLE_TYPE_IDS["fixed_wing"],
            )
        )
    )
    connection = _FakeConnection()
    provider._connection = connection
    provider.ingest_message(
        SimpleNamespace(
            get_type=lambda: "HEARTBEAT",
            get_srcSystem=lambda: 1,
            get_srcComponent=lambda: 1,
            autopilot=PIXHAWK_AUTOPILOT_IDS["px4"],
            type=PIXHAWK_VEHICLE_TYPE_IDS["fixed_wing"],
            system_status=4,
            mavlink_version=3,
            base_mode=128,
        ),
        received_at_s=10.0,
    )
    return provider, connection


def test_pixhawk_flight_provider_sends_real_mavlink_attitude_and_mode_messages() -> None:
    provider, connection = _flight_provider()
    decision = FixedWingAimController(_calibration()).evaluate(
        target=_target(),
        telemetry=_telemetry(),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )
    assert decision.setpoint is not None
    provider.send_attitude_target(decision.setpoint)
    provider.request_mode("OFFBOARD")

    assert provider.is_read_only is False
    assert provider.messages_transmitted == 2
    assert len(connection.mav.attitude_calls) == 1
    attitude = connection.mav.attitude_calls[0]
    assert attitude[1:4] == (1, 1, 71)
    assert attitude[4] == decision.setpoint.quaternion_wxyz
    assert connection.mav.mode_calls == [(1, 1, 6 << 16)]
    assert provider.diagnostics(now_s=10.1)["hardware_control_enabled"] is True


def test_flight_provider_requires_qualified_pixhawk_identity() -> None:
    provider, _connection = _flight_provider()
    provider._heartbeat_identity = replace(provider.heartbeat_identity, vehicle_type_id=2)
    decision = FixedWingAimController(_calibration()).evaluate(
        target=_target(),
        telemetry=_telemetry(),
        mode3_active=True,
        execution_confirmed=True,
        now_s=10.1,
    )
    assert decision.setpoint is not None
    with pytest.raises(RuntimeError, match="identity qualification"):
        provider.send_attitude_target(decision.setpoint)


def test_flight_provider_requests_rc_channels_at_control_rate() -> None:
    provider, connection = _flight_provider()

    provider.snapshot(now_s=10.1)

    assert len(connection.mav.command_long_calls) == 1
    request = connection.mav.command_long_calls[0]
    assert request[:4] == (1, 1, 511, 0)
    assert request[4] == 65.0
    assert request[5] == 50_000.0
