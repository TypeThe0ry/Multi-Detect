from __future__ import annotations

import math
from pathlib import Path

import pytest

from multidetect.pixhawk import (
    PixhawkDiscoveryError,
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
    resolve_pixhawk_endpoint,
)
from multidetect.telemetry import FailClosedTelemetryProvider, with_person_detector_health


class _Message:
    def __init__(
        self,
        name: str,
        *,
        source_system_id: int | None = None,
        source_component_id: int | None = None,
        **values: object,
    ) -> None:
        self._name = name
        self._source_system_id = source_system_id
        self._source_component_id = source_component_id
        self.__dict__.update(values)

    def get_type(self) -> str:
        return self._name

    def get_srcSystem(self) -> int | None:
        return self._source_system_id

    def get_srcComponent(self) -> int | None:
        return self._source_component_id


class _NoMessageConnection:
    flightmode = "AUTO"

    def recv_match(self, *, blocking: bool):
        assert blocking is False
        return None

    def close(self) -> None:
        pass


def test_pixhawk_provider_only_maps_read_only_telemetry() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider._connection = _NoMessageConnection()  # Avoid an actual transport in the unit test.
    provider.ingest_message(_Message("HEARTBEAT", base_mode=128), received_at_s=10.0)
    provider.ingest_message(
        _Message("ATTITUDE", roll=0.1, pitch=-0.2, yaw=-0.5), received_at_s=10.0
    )
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_345,
            vx=300,
            vy=400,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=12_345,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(_Message("SYS_STATUS", battery_remaining=73), received_at_s=10.0)
    provider.ingest_message(_Message("GPS_RAW_INT", satellites_visible=17), received_at_s=10.0)
    provider.ingest_message(_Message("MISSION_CURRENT", seq=4), received_at_s=10.0)
    provider.ingest_message(_Message("VFR_HUD", airspeed=16.5), received_at_s=10.1)
    provider.ingest_message(
        _Message("WIND_COV", wind_x=2.0, wind_y=-1.5),
        received_at_s=10.2,
    )

    snapshot = provider.snapshot(now_s=10.5)

    assert provider.is_read_only is True
    assert provider.messages_transmitted == 0
    assert snapshot.altitude_agl_m == pytest.approx(12.345)
    assert snapshot.ground_speed_mps == pytest.approx(5.0)
    assert snapshot.latitude_deg == pytest.approx(31.123456)
    assert snapshot.longitude_deg == pytest.approx(121.654321)
    assert snapshot.heading_deg == pytest.approx(123.45)
    assert snapshot.battery_remaining_pct == pytest.approx(73.0)
    assert snapshot.satellites_visible == 17
    assert snapshot.armed is True
    assert snapshot.flight_mode == "AUTO"
    assert snapshot.mission_sequence == 4
    assert snapshot.attitude_observed_at_s == 10.0
    assert snapshot.position_observed_at_s == 10.0
    assert snapshot.velocity_north_mps == pytest.approx(3.0)
    assert snapshot.velocity_east_mps == pytest.approx(4.0)
    assert snapshot.airspeed_mps == pytest.approx(16.5)
    assert snapshot.wind_north_mps == pytest.approx(2.0)
    assert snapshot.wind_east_mps == pytest.approx(-1.5)
    assert snapshot.velocity_observed_at_s == 10.0
    assert snapshot.airspeed_observed_at_s == 10.1
    assert snapshot.wind_observed_at_s == 10.2
    assert snapshot.position_healthy is True
    assert snapshot.link_healthy is True
    assert snapshot.in_allowed_zone is None
    assert snapshot.release_zone_clear is None


def test_pixhawk_attitude_yaw_fills_heading_when_gps_course_is_unavailable() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider._connection = _NoMessageConnection()
    provider.ingest_message(
        _Message("ATTITUDE", roll=0.0, pitch=0.0, yaw=-0.5), received_at_s=10.0
    )
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=5_000,
            vx=0,
            vy=0,
            lat=0,
            lon=0,
            hdg=65_535,
        ),
        received_at_s=10.0,
    )

    snapshot = provider.cached_snapshot(now_s=10.1)

    assert snapshot.heading_deg == pytest.approx(math.degrees(-0.5) % 360.0)


def test_pixhawk_local_ned_and_quaternion_keep_range_pose_available_without_gps() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider._connection = _NoMessageConnection()
    provider.ingest_message(
        _Message("ATTITUDE_QUATERNION", q1=1.0, q2=0.0, q3=0.0, q4=0.0),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message("LOCAL_POSITION_NED", x=12.0, y=-4.0, z=-8.5, vx=3.0, vy=4.0),
        received_at_s=10.02,
    )

    snapshot = provider.cached_snapshot(now_s=10.10)

    assert snapshot.latitude_deg != snapshot.latitude_deg  # GPS remains absent.
    assert snapshot.altitude_agl_m == pytest.approx(8.5)
    assert snapshot.altitude_reference == "local_ned_relative"
    assert snapshot.altitude_observed_at_s == pytest.approx(10.02)
    assert snapshot.local_north_m == pytest.approx(12.0)
    assert snapshot.local_east_m == pytest.approx(-4.0)
    assert snapshot.local_down_m == pytest.approx(-8.5)
    assert snapshot.local_position_observed_at_s == pytest.approx(10.02)
    assert snapshot.position_healthy is True
    assert snapshot.heading_deg == pytest.approx(0.0)
    assert snapshot.ground_speed_mps == pytest.approx(5.0)
    assert snapshot.velocity_north_mps == pytest.approx(3.0)
    assert snapshot.velocity_east_mps == pytest.approx(4.0)


def test_pixhawk_altitude_relative_supplies_vertical_timestamp_without_global_position() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message("ALTITUDE", altitude_relative=21.25), received_at_s=10.0
    )

    snapshot = provider.cached_snapshot(now_s=10.1)

    assert snapshot.altitude_agl_m == pytest.approx(21.25)
    assert snapshot.altitude_reference == "home_relative"
    assert snapshot.altitude_observed_at_s == pytest.approx(10.0)
    assert snapshot.position_healthy is None


def test_pixhawk_rejects_odometry_in_a_non_navigation_frame() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message("ODOMETRY", frame_id=12, x=1.0, y=2.0, z=-3.0, vx=4.0, vy=5.0),
        received_at_s=10.0,
    )

    snapshot = provider.cached_snapshot(now_s=10.1)

    assert math.isnan(snapshot.altitude_agl_m)
    assert snapshot.position_healthy is None


def test_pixhawk_provider_caches_all_valid_rc_channels_for_override_detection() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig("udp:127.0.0.1:14550", expected_system_id=1)
    )
    values = {f"chan{index}_raw": 1500 for index in range(1, 19)}
    values["chan4_raw"] = 1000
    provider.ingest_message(
        _Message(
            "RC_CHANNELS",
            source_system_id=1,
            chancount=18,
            **values,
        ),
        received_at_s=10.25,
    )

    sample = provider.rc_input_snapshot

    assert sample is not None
    assert sample.observed_at_s == 10.25
    assert sample.valid_channel_count == 18
    assert sample.channels_pwm[3] == 1000
    diagnostics = provider.diagnostics(now_s=10.30)
    assert diagnostics["rc_input_observed"] is True
    assert diagnostics["rc_valid_channel_count"] == 18
    assert diagnostics["rc_input_age_s"] == pytest.approx(0.05)


def test_pixhawk_stale_data_and_fail_closed_defaults_do_not_clear_safety() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig("udp:127.0.0.1:14550", stale_after_seconds=1.0)
    )
    provider._connection = _NoMessageConnection()
    provider.ingest_message(_Message("HEARTBEAT"), received_at_s=10.0)
    snapshot = provider.snapshot(now_s=11.1)
    fail_closed = FailClosedTelemetryProvider().snapshot(now_s=11.1)

    assert snapshot.link_healthy is False
    assert fail_closed.in_allowed_zone is None
    assert with_person_detector_health(fail_closed, healthy=True).person_detector_healthy is True


def test_pixhawk_cached_snapshot_never_connects_or_receives() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("serial:unused"))
    provider.ingest_message(_Message("HEARTBEAT"), received_at_s=10.0)
    provider.ingest_message(
        _Message("GLOBAL_POSITION_INT", relative_alt=0, vx=0, vy=0),
        received_at_s=10.0,
    )
    provider.connect = lambda: pytest.fail("cached_snapshot must not connect")  # type: ignore[method-assign]

    stale = provider.cached_snapshot(now_s=11.1)

    assert stale.link_healthy is False
    assert stale.position_healthy is False
    assert provider.messages_transmitted == 0


@pytest.mark.parametrize("stale_after_seconds", [float("nan"), float("inf"), True])
def test_pixhawk_config_rejects_invalid_stale_timeout(stale_after_seconds: float) -> None:
    with pytest.raises(ValueError, match="stale timeout"):
        PixhawkReadOnlyConfig("udp:127.0.0.1:14550", stale_after_seconds=stale_after_seconds)


def test_pixhawk_qualification_rejects_wrong_source_generic_and_uninitialized() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            "udp:0.0.0.0:14550",
            expected_system_id=1,
            expected_autopilot_id=12,
            expected_vehicle_type_id=1,
            require_operational_state=True,
        )
    )
    provider._connection = _NoMessageConnection()
    provider.ingest_message(
        _Message(
            "HEARTBEAT",
            source_system_id=2,
            source_component_id=1,
            autopilot=12,
            type=1,
            system_status=4,
            base_mode=0,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "HEARTBEAT",
            source_system_id=1,
            source_component_id=190,
            autopilot=8,
            type=18,
            system_status=4,
            base_mode=0,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "HEARTBEAT",
            source_system_id=1,
            source_component_id=1,
            autopilot=12,
            type=0,
            system_status=0,
            mavlink_version=3,
            base_mode=0,
        ),
        received_at_s=10.0,
    )

    snapshot = provider.snapshot(now_s=10.5)
    identity = provider.heartbeat_identity.to_document()

    assert provider.transport_link_healthy(now_s=10.5) is True
    assert snapshot.link_healthy is False
    assert provider.qualification.passed is False
    assert "vehicle type mismatch" in " ".join(provider.qualification.reasons)
    assert "MAV_STATE_UNINIT" in " ".join(provider.qualification.reasons)
    assert identity["autopilot_name"] == "MAV_AUTOPILOT_PX4"
    assert identity["vehicle_type_name"] == "MAV_TYPE_GENERIC"
    assert identity["system_status_name"] == "MAV_STATE_UNINIT"
    assert provider.rejected_system_messages == 1
    assert provider.ignored_non_autopilot_heartbeats == 1


def test_pixhawk_qualification_accepts_expected_operational_fixed_wing() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            "udp:0.0.0.0:14550",
            expected_system_id=1,
            expected_autopilot_id=12,
            expected_vehicle_type_id=1,
            require_operational_state=True,
        )
    )
    provider._connection = _NoMessageConnection()
    provider.ingest_message(
        _Message(
            "HEARTBEAT",
            source_system_id=1,
            source_component_id=1,
            autopilot=12,
            type=1,
            system_status=3,
            mavlink_version=3,
            base_mode=0,
        ),
        received_at_s=10.0,
    )

    snapshot = provider.snapshot(now_s=10.5)

    assert snapshot.link_healthy is True
    assert provider.qualification.passed is True
    assert provider.messages_received == 1
    assert provider.message_type_counts == {"HEARTBEAT": 1}


def test_diagnostics_separates_transport_health_from_task_qualification() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            "udp:0.0.0.0:14550",
            expected_system_id=1,
            expected_autopilot_id=12,
            expected_vehicle_type_id=1,
            require_operational_state=True,
        )
    )
    provider.ingest_message(
        _Message(
            "HEARTBEAT",
            source_system_id=1,
            source_component_id=1,
            autopilot=12,
            type=0,
            system_status=0,
            mavlink_version=3,
            base_mode=0,
        ),
        received_at_s=10.0,
    )

    document = provider.diagnostics(now_s=10.1)

    assert document["transport_link_healthy"] is True
    assert document["qualified_link_healthy"] is False
    assert document["position_healthy"] is None
    assert document["heartbeat_identity"]["autopilot_name"] == "MAV_AUTOPILOT_PX4"
    assert document["qualification"]["passed"] is False
    assert document["messages_received"] == 1
    assert document["messages_transmitted"] == 0
    assert document["hardware_control_enabled"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_system_id": 0},
        {"expected_system_id": 256},
        {"expected_autopilot_id": True},
        {"expected_vehicle_type_id": -1},
        {"require_operational_state": 1},
    ],
)
def test_pixhawk_config_rejects_invalid_qualification_settings(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="Pixhawk"):
        PixhawkReadOnlyConfig("udp:127.0.0.1:14550", **changes)  # type: ignore[arg-type]


def test_pixhawk_auto_discovery_prefers_stable_identifier(tmp_path: Path) -> None:
    by_id = tmp_path / "by-id"
    devices = tmp_path / "dev"
    by_id.mkdir()
    devices.mkdir()
    stable = by_id / "usb-ArduPilot_Pixhawk6X_1234-if00"
    stable.write_bytes(b"")
    (devices / "ttyACM0").write_bytes(b"")

    assert resolve_pixhawk_endpoint("auto", by_id_dir=by_id, device_dir=devices) == str(stable)


def test_pixhawk_auto_discovery_allows_one_acm_fallback(tmp_path: Path) -> None:
    by_id = tmp_path / "by-id"
    devices = tmp_path / "dev"
    by_id.mkdir()
    devices.mkdir()
    acm = devices / "ttyACM0"
    acm.write_bytes(b"")

    assert resolve_pixhawk_endpoint("auto", by_id_dir=by_id, device_dir=devices) == str(acm)


def test_pixhawk_auto_discovery_rejects_ambiguous_devices(tmp_path: Path) -> None:
    by_id = tmp_path / "by-id"
    devices = tmp_path / "dev"
    by_id.mkdir()
    devices.mkdir()
    (devices / "ttyACM0").write_bytes(b"")
    (devices / "ttyACM1").write_bytes(b"")

    with pytest.raises(PixhawkDiscoveryError, match="multiple /dev/ttyACM"):
        resolve_pixhawk_endpoint("auto", by_id_dir=by_id, device_dir=devices)


def test_pixhawk_explicit_endpoint_is_not_probed(tmp_path: Path) -> None:
    assert (
        resolve_pixhawk_endpoint(
            "/dev/ttyTHS1",
            by_id_dir=tmp_path / "missing",
            device_dir=tmp_path / "missing",
        )
        == "/dev/ttyTHS1"
    )
