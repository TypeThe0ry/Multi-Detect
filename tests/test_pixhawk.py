from __future__ import annotations

import pytest

from multidetect.pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from multidetect.telemetry import FailClosedTelemetryProvider, with_person_detector_health


class _Message:
    def __init__(self, name: str, **values: float) -> None:
        self._name = name
        self.__dict__.update(values)

    def get_type(self) -> str:
        return self._name


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
    provider.ingest_message(_Message("ATTITUDE", roll=0.1, pitch=-0.2), received_at_s=10.0)
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

    snapshot = provider.snapshot(now_s=10.5)

    assert provider.is_read_only is True
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
    assert snapshot.position_healthy is True
    assert snapshot.link_healthy is True
    assert snapshot.in_allowed_zone is None
    assert snapshot.release_zone_clear is None


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
