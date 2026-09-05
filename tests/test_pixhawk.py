from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from multidetect.adaptive_ranging import assess_gps_quality
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


class _CommandRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def command_long_send(self, *values: object) -> None:
        self.calls.append(values)


class _RangingStreamConnection(_NoMessageConnection):
    def __init__(self) -> None:
        self.mav = _CommandRecorder()


def _autopilot_version_message(
    *,
    source_system_id: int = 1,
    source_component_id: int = 1,
    **changes: object,
) -> _Message:
    values: dict[str, object] = {
        "capabilities": (1 << 63) | 5,
        "flight_sw_version": (1 << 24) | (18 << 16) | (3 << 8) | 192,
        "middleware_sw_version": (2 << 24) | (1 << 16) | 255,
        "os_sw_version": (10 << 24) | (2 << 16) | (7 << 8) | 128,
        "board_version": 0x1234ABCD,
        "flight_custom_version": bytes.fromhex("0102030405060708"),
        "middleware_custom_version": bytes.fromhex("1112131415161718"),
        "os_custom_version": bytes.fromhex("2122232425262728"),
        "vendor_id": 9900,
        "product_id": 42,
        "uid": 0x1122334455667788,
        "uid2": bytes([0xA5] * 18),
    }
    values.update(changes)
    return _Message(
        "AUTOPILOT_VERSION",
        source_system_id=source_system_id,
        source_component_id=source_component_id,
        **values,
    )


def _ingest_px4_heartbeat(
    provider: PixhawkReadOnlyTelemetryProvider,
    *,
    received_at_s: float = 10.0,
    vehicle_type_id: int = 1,
    system_status_id: int = 3,
) -> None:
    provider.ingest_message(
        _Message(
            "HEARTBEAT",
            source_system_id=1,
            source_component_id=1,
            autopilot=12,
            type=vehicle_type_id,
            system_status=system_status_id,
            mavlink_version=3,
            base_mode=0,
        ),
        received_at_s=received_at_s,
    )


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
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=17,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(_Message("MISSION_CURRENT", seq=4), received_at_s=10.0)
    provider.ingest_message(_Message("VFR_HUD", airspeed=16.5), received_at_s=10.1)
    provider.ingest_message(
        _Message("SCALED_PRESSURE", press_diff=4.25, temperature=2350),
        received_at_s=10.15,
    )
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
    assert snapshot.gps_fix_type == 3
    assert snapshot.gps_horizontal_accuracy_m == pytest.approx(0.85)
    assert snapshot.gps_vertical_accuracy_m == pytest.approx(1.40)
    assert snapshot.gps_observed_at_s == pytest.approx(10.0)
    assert snapshot.gps_absolute_altitude_m == pytest.approx(15.0)
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
    diagnostics = provider.diagnostics(now_s=10.5)
    assert diagnostics["motion"]["differential_pressure_hpa"] == pytest.approx(4.25)
    assert diagnostics["motion"]["differential_pressure_temperature_c"] == pytest.approx(23.5)
    assert diagnostics["motion"]["differential_pressure_age_s"] == pytest.approx(0.35)
    assert snapshot.position_healthy is True
    assert snapshot.link_healthy is True
    assert snapshot.in_allowed_zone is None
    assert snapshot.release_zone_clear is None


def test_pixhawk_gps_unknown_satellite_sentinel_does_not_retain_previous_count() -> None:
    """GPS_RAW_INT.satellites_visible=255 must invalidate that sample's count.

    The value is a wire-level ``unknown`` sentinel, not a request to keep the
    previous observation.  Retaining the old count could make an outdoor GPS
    quality gate pass after the receiver has stopped reporting satellites.
    """

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=12,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=255,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=10.1,
    )
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=10.1,
    )

    snapshot = provider.cached_snapshot(now_s=10.2)

    assert snapshot.satellites_visible is None
    quality = assess_gps_quality(snapshot, now_s=10.2)
    assert quality.qualified is False
    assert quality.reason == "gps_satellites_insufficient"


def test_pixhawk_older_raw_frame_cannot_regress_newer_quality_sample() -> None:
    """Out-of-order GPS_RAW_INT must not move quality/freshness backwards."""

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=15,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=114_500,
            eph=85,
            epv=140,
        ),
        received_at_s=10.1,
    )

    # This frame is both older and degraded.  A replay/UDP reorder must not
    # clear the newer qualified raw sample or replace its diagnostic fields.
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=2,
            satellites_visible=2,
            lat=1_000_000_000,
            lon=1_900_000_000,
            alt=None,
            eph=65_000,
            epv=65_000,
        ),
        received_at_s=9.0,
    )

    snapshot = provider.cached_snapshot(now_s=10.2)
    assert snapshot.latitude_deg == pytest.approx(31.123456)
    assert snapshot.longitude_deg == pytest.approx(121.654321)
    assert snapshot.gps_fix_type == 3
    assert snapshot.satellites_visible == 15
    assert snapshot.gps_horizontal_accuracy_m == pytest.approx(0.85)
    assert snapshot.gps_vertical_accuracy_m == pytest.approx(1.40)
    assert snapshot.gps_observed_at_s == pytest.approx(10.1)
    diagnostics = provider.diagnostics(now_s=10.2)
    assert diagnostics["vertical"]["gps_raw_age_s"] == pytest.approx(0.1)
    assert diagnostics["vertical"]["gps_raw_message_age_s"] == pytest.approx(0.1)
    assert assess_gps_quality(snapshot, now_s=10.2).qualified is True


def test_pixhawk_gps_bad_coordinates_do_not_reuse_previous_raw_fix() -> None:
    """An out-of-domain GPS_RAW_INT coordinate invalidates that raw sample.

    The estimator's GLOBAL_POSITION_INT stream is a separate source and must
    remain available; only the malformed raw GPS qualification fields are
    cleared.  This prevents a new fix/satellite tuple from making an old raw
    coordinate look fresh.
    """

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=12,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=12,
            lat=1_000_000_000,  # 100 degrees: outside WGS-84 latitude domain.
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=10.1,
    )

    snapshot = provider.cached_snapshot(now_s=10.2)

    # GLOBAL_POSITION_INT remains intact, while the malformed raw source is
    # no longer eligible to qualify GPS or refresh its observed timestamp.
    assert snapshot.position_healthy is True
    assert snapshot.latitude_deg == pytest.approx(31.123456)
    assert snapshot.longitude_deg == pytest.approx(121.654321)
    assert snapshot.gps_fix_type is None
    assert snapshot.satellites_visible is None
    assert math.isnan(snapshot.gps_horizontal_accuracy_m)
    assert math.isnan(snapshot.gps_vertical_accuracy_m)
    assert math.isnan(snapshot.gps_observed_at_s)

    quality = assess_gps_quality(snapshot, now_s=10.2)
    assert quality.qualified is False
    assert quality.reason == "gps_fix_unqualified"


def test_pixhawk_gps_missing_altitude_clears_raw_freshness_and_home_fallback() -> None:
    """A raw frame without ``alt`` cannot keep an older GPS sample alive.

    The estimator position remains independent, while the raw coordinate
    timestamp and GPS-vs-home vertical fallback are invalidated atomically.
    Legal fix/satellite/accuracy fields are retained as diagnostics for the
    degraded frame.
    """

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(_Message("HOME_POSITION", altitude=102_000), received_at_s=10.0)
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=12,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=114_500,
            eph=85,
            epv=140,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=12,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=None,
            eph=90,
            epv=150,
        ),
        received_at_s=10.1,
    )

    snapshot = provider.cached_snapshot(now_s=10.2)

    assert snapshot.position_healthy is True
    assert snapshot.latitude_deg == pytest.approx(31.123456)
    assert snapshot.longitude_deg == pytest.approx(121.654321)
    assert snapshot.gps_fix_type == 3
    assert snapshot.satellites_visible == 12
    assert math.isnan(snapshot.gps_observed_at_s)
    assert math.isnan(snapshot.gps_absolute_altitude_m)
    # The independent estimator altitude survives; only the GPS-home fallback
    # that was layered on top of it is removed.
    assert snapshot.altitude_agl_m == pytest.approx(12.0)
    assert snapshot.altitude_reference == "home_relative"
    assert snapshot.altitude_observed_at_s == pytest.approx(10.0)

    quality = assess_gps_quality(snapshot, now_s=10.2)
    assert quality.qualified is False
    assert quality.reason == "gps_timestamp_unavailable"


@pytest.mark.parametrize(
    ("satellites_visible", "eph", "reason"),
    [
        (255, 85, "gps_satellites_insufficient"),
        (12, None, "gps_horizontal_accuracy_invalid"),
    ],
)
def test_pixhawk_gps_missing_quality_field_cannot_refresh_raw_cursor(
    satellites_visible: int,
    eph: int | None,
    reason: str,
) -> None:
    """A raw coordinate is not a fresh GPS fix without its quality tuple."""

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=9.9,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=15,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=satellites_visible,
            lat=31_123_4570,
            lon=121_654_3220,
            alt=15_100,
            eph=eph,
            epv=140,
        ),
        received_at_s=10.1,
    )

    snapshot = provider.cached_snapshot(now_s=10.2)
    assert snapshot.gps_observed_at_s != snapshot.gps_observed_at_s
    assert assess_gps_quality(snapshot, now_s=10.2).reason == reason


@pytest.mark.parametrize("latitude_e7", [-900_000_000, 900_000_000])
def test_pixhawk_gps_exact_pole_is_not_a_position(latitude_e7: int) -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=15,
            lat=latitude_e7,
            lon=0,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=10.0,
    )

    snapshot = provider.cached_snapshot(now_s=10.1)
    assert snapshot.latitude_deg != snapshot.latitude_deg
    assert snapshot.longitude_deg != snapshot.longitude_deg
    assert snapshot.gps_observed_at_s != snapshot.gps_observed_at_s
    assert assess_gps_quality(snapshot, now_s=10.1).reason == "gps_position_unhealthy"


def test_pixhawk_gps_downgraded_fix_clears_raw_freshness_and_home_fallback() -> None:
    """A legal but unqualified fix type must not reuse the previous raw fix."""

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(_Message("HOME_POSITION", altitude=102_000), received_at_s=10.0)
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=12,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=114_500,
            eph=85,
            epv=140,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=2,
            satellites_visible=12,
            lat=31_123_4570,
            lon=121_654_3220,
            alt=114_600,
            eph=90,
            epv=150,
        ),
        received_at_s=10.1,
    )

    snapshot = provider.cached_snapshot(now_s=10.2)

    assert snapshot.gps_fix_type == 2
    assert snapshot.satellites_visible == 12
    assert snapshot.gps_horizontal_accuracy_m == pytest.approx(0.9)
    assert snapshot.gps_vertical_accuracy_m == pytest.approx(1.5)
    assert math.isnan(snapshot.gps_observed_at_s)
    assert snapshot.position_healthy is True
    assert snapshot.altitude_agl_m == pytest.approx(12.0)
    assert snapshot.altitude_reference == "home_relative"
    quality = assess_gps_quality(snapshot, now_s=10.2)
    assert quality.qualified is False
    assert quality.reason == "gps_fix_unqualified"


@pytest.mark.parametrize("missing_field", ["lat", "lon"])
def test_pixhawk_global_position_missing_coordinate_is_atomic(missing_field: str) -> None:
    """A partial GLOBAL_POSITION_INT must not refresh or reuse its position."""

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=100,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=20.0,
    )
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=15,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=20.1,
    )
    malformed = {
        "relative_alt": 12_500,
        "vx": 100,
        "vy": 0,
        "lat": 31_123_4560,
        "lon": 121_654_3210,
        "hdg": 9_000,
    }
    malformed.pop(missing_field)
    provider.ingest_message(_Message("GLOBAL_POSITION_INT", **malformed), received_at_s=20.2)

    snapshot = provider.cached_snapshot(now_s=20.3)
    assert snapshot.latitude_deg == pytest.approx(31.123456)
    assert snapshot.longitude_deg == pytest.approx(121.654321)
    assert math.isnan(snapshot.gps_observed_at_s)
    assert snapshot.gps_fix_type is None
    assert snapshot.satellites_visible is None
    assert snapshot.position_observed_at_s == pytest.approx(20.0)
    quality = assess_gps_quality(snapshot, now_s=20.3)
    assert quality.qualified is False


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (2_147_483_647, 1_216_543_210),  # INT32_MAX latitude sentinel.
        (311_234_560, 2_147_483_647),  # INT32_MAX longitude sentinel.
        (-900_000_000, 1_216_543_210),  # Exact south pole.
        (900_000_000, 1_216_543_210),  # Exact north pole.
        (1_000_000_000, 1_216_543_210),  # 100 degrees latitude.
        (311_234_560, 1_900_000_000),  # 190 degrees longitude.
    ],
)
def test_pixhawk_global_position_sentinel_coordinates_fail_closed(lat: int, lon: int) -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=15,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=15_000,
            eph=85,
            epv=140,
        ),
        received_at_s=21.0,
    )
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=lat,
            lon=lon,
            hdg=9_000,
        ),
        received_at_s=21.1,
    )

    snapshot = provider.cached_snapshot(now_s=21.2)
    assert math.isnan(snapshot.gps_observed_at_s)
    assert snapshot.gps_fix_type is None
    assert snapshot.satellites_visible is None
    assert assess_gps_quality(snapshot, now_s=21.2).qualified is False


def test_pixhawk_global_position_accepts_legal_zero_zero_coordinate() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=1_000,
            vx=0,
            vy=0,
            lat=0,
            lon=0,
            hdg=0,
        ),
        received_at_s=22.0,
    )
    snapshot = provider.cached_snapshot(now_s=22.1)
    assert snapshot.latitude_deg == pytest.approx(0.0)
    assert snapshot.longitude_deg == pytest.approx(0.0)
    assert snapshot.position_observed_at_s == pytest.approx(22.0)
    assert snapshot.position_healthy is True


def test_pixhawk_global_position_sentinels_do_not_overwrite_motion_or_heading() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=45_000,
            vx=100,
            vy=-50,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=23.0,
    )
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=2_147_483_647,
            vx=32_767,
            vy=-32_768,
            lat=31_123_4570,
            lon=121_654_3220,
            hdg=65_535,
        ),
        received_at_s=23.1,
    )
    snapshot = provider.cached_snapshot(now_s=23.2)
    assert snapshot.altitude_agl_m == pytest.approx(45.0)
    assert snapshot.ground_speed_mps == pytest.approx(1.118, rel=1e-3)
    assert snapshot.heading_deg == pytest.approx(90.0)
    assert snapshot.altitude_observed_at_s == pytest.approx(23.0)
    assert snapshot.velocity_observed_at_s == pytest.approx(23.0)


@pytest.mark.parametrize("timestamp", [True, -1.0, float("nan"), float("inf")])
def test_pixhawk_rejects_invalid_ingest_timestamp(timestamp: object) -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    with pytest.raises(ValueError, match="received_at_s"):
        provider.ingest_message(_Message("HEARTBEAT"), received_at_s=timestamp)  # type: ignore[arg-type]


def test_pixhawk_non_positive_gps_home_delta_does_not_retain_stale_height() -> None:
    """A reset/mismatched home datum must remove the prior GPS-home fallback."""

    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=0,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=10.0,
    )
    provider.ingest_message(_Message("HOME_POSITION", altitude=102_000), received_at_s=10.0)
    provider.ingest_message(
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=12,
            lat=31_123_4560,
            lon=121_654_3210,
            alt=114_500,
            eph=85,
            epv=140,
        ),
        received_at_s=10.1,
    )
    assert provider.cached_snapshot(now_s=10.2).altitude_reference == "gps_home_relative"

    # A newer home datum above the raw altitude makes the delta non-positive.
    provider.ingest_message(_Message("HOME_POSITION", altitude=115_000), received_at_s=10.3)
    snapshot = provider.cached_snapshot(now_s=10.4)

    assert snapshot.altitude_agl_m == pytest.approx(12.0)
    assert snapshot.altitude_reference == "home_relative"
    assert snapshot.altitude_observed_at_s == pytest.approx(10.0)


def test_pixhawk_attitude_yaw_fills_heading_when_gps_course_is_unavailable() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider._connection = _NoMessageConnection()
    provider.ingest_message(_Message("ATTITUDE", roll=0.0, pitch=0.0, yaw=-0.5), received_at_s=10.0)
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


def test_pixhawk_local_ned_learns_disarmed_ground_zero_and_tracks_climb() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(_Message("HEARTBEAT", base_mode=0), received_at_s=10.0)
    provider.ingest_message(
        _Message("LOCAL_POSITION_NED", x=0.0, y=0.0, z=1.22, vx=0.0, vy=0.0),
        received_at_s=10.1,
    )

    grounded = provider.cached_snapshot(now_s=10.1)
    assert grounded.altitude_agl_m == pytest.approx(0.0)
    assert grounded.altitude_reference == "local_ned_takeoff_relative"

    provider.ingest_message(_Message("HEARTBEAT", base_mode=128), received_at_s=10.2)
    provider.ingest_message(
        _Message("LOCAL_POSITION_NED", x=4.0, y=0.0, z=-3.78, vx=12.0, vy=0.0),
        received_at_s=10.3,
    )

    airborne = provider.cached_snapshot(now_s=10.3)
    assert airborne.altitude_agl_m == pytest.approx(5.0)
    assert airborne.altitude_reference == "local_ned_takeoff_relative"
    diagnostics = provider.diagnostics(now_s=10.3)
    assert diagnostics["vertical"]["local_ground_down_m"] == pytest.approx(1.22)


def test_pixhawk_altitude_relative_supplies_vertical_timestamp_without_global_position() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(_Message("ALTITUDE", altitude_relative=21.25), received_at_s=10.0)

    snapshot = provider.cached_snapshot(now_s=10.1)

    assert snapshot.altitude_agl_m == pytest.approx(21.25)
    assert snapshot.altitude_reference == "home_relative"
    assert snapshot.altitude_observed_at_s == pytest.approx(10.0)
    assert snapshot.position_healthy is None
    diagnostics = provider.diagnostics(now_s=10.1)
    assert diagnostics["vertical"]["raw_altitude_relative_m"] == pytest.approx(21.25)
    assert diagnostics["vertical"]["altitude_message_age_s"] == pytest.approx(0.1)


def test_pixhawk_gps_and_home_position_supply_relative_height_fallback() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    provider.ingest_message(_Message("HOME_POSITION", altitude=102_000), received_at_s=10.0)
    provider.ingest_message(
        _Message("GPS_RAW_INT", fix_type=3, alt=114_500, satellites_visible=15),
        received_at_s=10.1,
    )

    snapshot = provider.cached_snapshot(now_s=10.2)

    assert snapshot.altitude_agl_m == pytest.approx(12.5)
    assert snapshot.altitude_reference == "gps_home_relative"
    assert snapshot.altitude_observed_at_s == pytest.approx(10.1)
    assert snapshot.satellites_visible == 15


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


def test_pixhawk_future_samples_are_not_reported_as_fresh() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig("udp:127.0.0.1:14550", stale_after_seconds=1.0)
    )
    provider.ingest_message(_Message("HEARTBEAT", base_mode=128), received_at_s=10.0)
    provider.ingest_message(
        _Message(
            "GLOBAL_POSITION_INT",
            relative_alt=12_000,
            vx=4,
            vy=0,
            lat=31_123_4560,
            lon=121_654_3210,
            hdg=9_000,
        ),
        received_at_s=10.0,
    )

    # Reading at an earlier clock value models a source/host clock-domain
    # mismatch.  A negative age must fail closed instead of looking fresh.
    snapshot = provider.cached_snapshot(now_s=9.9)

    assert snapshot.link_healthy is False
    assert snapshot.position_healthy is False


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


def test_pixhawk_requests_dynamic_ranging_streams_after_heartbeat_with_bounded_retries() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            "udp:127.0.0.1:14550",
            expected_system_id=1,
            request_ranging_streams=True,
        )
    )
    connection = _RangingStreamConnection()
    provider._connection = connection
    provider.ingest_message(
        _Message(
            "HEARTBEAT",
            source_system_id=1,
            source_component_id=1,
            autopilot=12,
            type=1,
            system_status=3,
            base_mode=0,
        ),
        received_at_s=10.0,
    )

    provider.snapshot(now_s=10.0)
    provider.snapshot(now_s=12.0)
    provider.snapshot(now_s=15.0)
    provider.snapshot(now_s=20.0)
    provider.snapshot(now_s=30.0)

    assert len(connection.mav.calls) == 18
    assert provider.messages_transmitted == 18
    assert {int(call[4]) for call in connection.mav.calls[:6]} == {29, 30, 32, 33, 141, 242}
    assert all(int(call[2]) == 511 for call in connection.mav.calls)
    diagnostics = provider.diagnostics(now_s=30.1)
    assert diagnostics["telemetry_configuration_enabled"] is True
    assert diagnostics["ranging_stream_requests"]["attempts"] == 3


def test_pixhawk_default_receive_only_mode_sends_no_stream_requests() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:127.0.0.1:14550"))
    connection = _RangingStreamConnection()
    provider._connection = connection
    provider.ingest_message(
        _Message("HEARTBEAT", source_system_id=1, source_component_id=1),
        received_at_s=10.0,
    )

    provider.snapshot(now_s=10.0)

    assert connection.mav.calls == []
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


def test_pixhawk_passively_records_sanitized_autopilot_version_identity() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:0.0.0.0:14550"))
    _ingest_px4_heartbeat(provider)
    provider.ingest_message(_autopilot_version_message(), received_at_s=10.25)

    identity = provider.autopilot_version_identity

    assert identity is not None
    document = identity.to_document()
    serialized = json.dumps(document, sort_keys=True)
    assert document["source_system_id"] == 1
    assert document["source_component_id"] == 1
    assert document["capabilities_hex"] == "0x8000000000000005"
    assert document["flight_sw_version_text"] == "1.18.3"
    assert document["flight_sw_version_type"] == "release_candidate"
    assert document["middleware_sw_version_text"] == "2.1.0"
    assert document["middleware_sw_version_type"] == "official"
    assert document["os_sw_version_text"] == "10.2.7"
    assert document["os_sw_version_type"] == "beta"
    assert document["device_uid_source"] == "uid2"
    assert len(str(document["device_uid_hash"])) == 64
    assert len(str(document["identity_hash"])) == 64
    assert document["raw_device_uid_serialized"] is False
    assert ("a5" * 18) not in serialized
    assert str(0x1122334455667788) not in serialized
    assert provider.autopilot_version_observed_at_s == pytest.approx(10.25)
    assert provider.autopilot_version_age_s(now_s=10.5) == pytest.approx(0.25)
    assert provider.messages_transmitted == 0


@pytest.mark.parametrize(
    ("changes", "expected_source", "uid_present"),
    [
        ({"uid2": bytes(18)}, "uid", True),
        ({"uid": 0, "uid2": bytes(18)}, "none", False),
        ({"uid": 0, "uid2": None}, "none", False),
    ],
)
def test_pixhawk_autopilot_version_uses_uid2_then_uid_fallback(
    changes: dict[str, object],
    expected_source: str,
    uid_present: bool,
) -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:0.0.0.0:14550"))
    _ingest_px4_heartbeat(provider)

    provider.ingest_message(
        _autopilot_version_message(**changes),
        received_at_s=10.25,
    )

    identity = provider.autopilot_version_identity
    assert identity is not None
    assert identity.device_uid_source == expected_source
    assert (identity.device_uid_hash is not None) is uid_present


@pytest.mark.parametrize(
    "changes",
    [
        {"capabilities": -1},
        {"flight_sw_version": float("nan")},
        {"flight_custom_version": bytes(7)},
        {"middleware_custom_version": [0] * 7 + [256]},
        {"vendor_id": 1 << 16},
        {"uid": None},
        {"uid2": bytes(17)},
    ],
)
def test_pixhawk_malformed_autopilot_version_fails_closed(
    changes: dict[str, object],
) -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:0.0.0.0:14550"))
    _ingest_px4_heartbeat(provider)
    provider.ingest_message(_autopilot_version_message(), received_at_s=10.1)
    assert provider.autopilot_version_identity is not None

    provider.ingest_message(
        _autopilot_version_message(**changes),
        received_at_s=10.2,
    )

    assert provider.autopilot_version_identity is None
    assert provider.autopilot_version_observed_at_s is None
    assert provider.autopilot_version_age_s(now_s=10.3) is None
    assert provider.rejected_autopilot_version_messages == 1


def test_pixhawk_autopilot_version_rejects_non_heartbeat_source() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:0.0.0.0:14550"))
    _ingest_px4_heartbeat(provider)

    provider.ingest_message(
        _autopilot_version_message(source_system_id=2),
        received_at_s=10.1,
    )
    provider.ingest_message(
        _autopilot_version_message(source_component_id=50),
        received_at_s=10.2,
    )

    assert provider.autopilot_version_identity is None
    assert provider.rejected_system_messages == 1
    assert provider.rejected_autopilot_version_messages == 2


def test_pixhawk_heartbeat_binding_change_clears_autopilot_version() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:0.0.0.0:14550"))
    _ingest_px4_heartbeat(provider)
    provider.ingest_message(_autopilot_version_message(), received_at_s=10.1)
    identity = provider.autopilot_version_identity
    assert identity is not None

    _ingest_px4_heartbeat(provider, received_at_s=10.2, system_status_id=4)
    assert provider.autopilot_version_identity == identity

    _ingest_px4_heartbeat(provider, received_at_s=10.3, vehicle_type_id=2)
    assert provider.autopilot_version_identity is None
    assert provider.autopilot_version_observed_at_s is None


def test_pixhawk_out_of_order_bad_autopilot_version_does_not_erase_newer_identity() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:0.0.0.0:14550"))
    _ingest_px4_heartbeat(provider)
    provider.ingest_message(_autopilot_version_message(), received_at_s=12.0)
    identity = provider.autopilot_version_identity

    provider.ingest_message(
        _autopilot_version_message(flight_custom_version=bytes(7)),
        received_at_s=11.0,
    )

    assert provider.autopilot_version_identity == identity
    assert provider.autopilot_version_observed_at_s == pytest.approx(12.0)
    assert provider.rejected_autopilot_version_messages == 1


def test_pixhawk_diagnostics_include_sanitized_autopilot_version() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(PixhawkReadOnlyConfig("udp:0.0.0.0:14550"))
    _ingest_px4_heartbeat(provider)
    provider.ingest_message(_autopilot_version_message(), received_at_s=10.25)

    document = provider.diagnostics(now_s=10.5)

    assert document["autopilot_version_identity"]["flight_sw_version_text"] == "1.18.3"
    assert document["autopilot_version_observed_at_s"] == pytest.approx(10.25)
    assert document["autopilot_version_age_s"] == pytest.approx(0.25)
    assert document["rejected_autopilot_version_messages"] == 0


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
