from __future__ import annotations

import struct

import pytest

from multidetect.simulator import (
    SimulatorControlAuthority,
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
    SimulatorTelemetryBridge,
    SimulatorTelemetrySnapshot,
)
from multidetect.simulator.ai_executor import SimulatorAiExecutor


class _Mav:
    def __init__(self) -> None:
        self.messages: list[tuple[int, int, int, int, int, int]] = []

    def manual_control_send(
        self, target: int, x: int, y: int, z: int, r: int, buttons: int
    ) -> None:
        self.messages.append((target, x, y, z, r, buttons))


class _Connection:
    def __init__(self) -> None:
        self.mav = _Mav()

    def close(self) -> None:
        pass


def _v1_frame(message_id: int, payload: bytes, *, system: int = 1, component: int = 1) -> bytes:
    return (
        bytes([0xFE, len(payload), 0, system, component, message_id & 0xFF])
        + payload
        + b"\x00\x00"
    )


def _heartbeat(*, armed: bool = True, system: int = 1) -> bytes:
    payload = struct.pack("<IBBBBB", 0, 2, 12, 128 if armed else 0, 4, 3)
    return _v1_frame(0, payload, system=system)


def _gps(*, fix_type: int = 3, satellites_visible: int = 15) -> bytes:
    # MAVLink GPS_RAW_INT wire layout: time, lat, lon, alt, eph, epv,
    # velocity/course, fix type, satellites.  Extensions are not needed.
    payload = struct.pack(
        "<QiiiHHHHBB",
        1_000_000,
        312345678,
        1215432100,
        12345,
        80,
        120,
        250,
        9000,
        fix_type,
        satellites_visible,
    )
    return _v1_frame(24, payload)


def _global_position() -> bytes:
    payload = struct.pack(
        "<IiiiihhhH",
        2000,
        312345678,
        1215432100,
        12345,
        45000,
        100,
        -50,
        0,
        9000,
    )
    return _v1_frame(33, payload)


def test_bridge_folds_truth_free_px4_frames_into_shared_ai_executor() -> None:
    connection = _Connection()
    authority = SimulatorControlAuthority()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:24540"),
        connection=connection,
    )
    bridge = SimulatorTelemetryBridge(SimulatorAiExecutor(adapter), clock=lambda: 100.0)
    bridge.enable_ai()
    assert bridge.ingest_datagram(_heartbeat(), timestamp_s=100.0)
    bridge.ingest_datagram(_gps(), timestamp_s=100.1)
    bridge.ingest_datagram(_global_position(), timestamp_s=100.2)
    evidence = bridge.evidence()
    assert evidence["frames_observed"] == 3
    assert evidence["frames_accepted"] == 3
    assert evidence["frames_rejected"] == 0
    assert evidence["heartbeat_seen"] is True
    assert evidence["snapshots"] == 3
    assert evidence["ai_accepted"] >= 1
    snapshot = evidence["last_snapshot"]
    assert snapshot["latitude_deg"] == pytest.approx(31.2345678)
    assert snapshot["longitude_deg"] == pytest.approx(121.54321)
    assert snapshot["altitude_agl_m"] == pytest.approx(45.0)
    assert snapshot["gps_fix_type"] == 3
    assert snapshot["ground_speed_mps"] == pytest.approx(1.118, rel=1e-3)
    assert snapshot["truth_free"] is True
    assert connection.mav.messages


def test_bridge_rejects_wrong_identity_and_truncated_known_frame() -> None:
    authority = SimulatorControlAuthority()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:24541"),
        connection=_Connection(),
    )
    bridge = SimulatorTelemetryBridge(SimulatorAiExecutor(adapter), clock=lambda: 10.0)
    bridge.ingest_datagram(_heartbeat(system=42), timestamp_s=10.0)
    bridge.ingest_datagram(_v1_frame(24, b"\x00" * 4), timestamp_s=10.1)
    evidence = bridge.evidence()
    assert evidence["frames_observed"] == 2
    assert evidence["frames_rejected"] == 2
    reasons = [item["reason"] for item in evidence["rejections"]]
    assert "source_system_mismatch" in reasons
    assert "unsupported_or_truncated_message" in reasons


def test_numeric_mapping_message_id_is_normalized_without_mutation() -> None:
    authority = SimulatorControlAuthority()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:24542"),
        connection=_Connection(),
    )
    bridge = SimulatorTelemetryBridge(SimulatorAiExecutor(adapter), clock=lambda: 20.0)
    bridge.enable_ai()
    raw = {
        "message_id": 0,
        "source_system_id": 1,
        "source_component_id": 1,
        "base_mode": 128,
    }
    bridge.ingest(raw, timestamp_s=20.0)
    assert "type" not in raw
    assert bridge.evidence()["heartbeat_seen"] is True


def _make_bridge(
    *,
    heartbeat_timeout_s: float | None = 2.0,
    minimum_gps_fix_type: int = 3,
) -> tuple[
    SimulatorTelemetryBridge, SimulatorControlAuthority, _Connection
]:
    authority = SimulatorControlAuthority()
    connection = _Connection()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(
            endpoint="udpout:127.0.0.1:24543",
            watchdog_s=10.0,
        ),
        connection=connection,
    )
    bridge = SimulatorTelemetryBridge(
        SimulatorAiExecutor(adapter),
        heartbeat_timeout_s=heartbeat_timeout_s,
        minimum_gps_fix_type=minimum_gps_fix_type,
    )
    bridge.enable_ai()
    return bridge, authority, connection


def test_heartbeat_freshness_gate_fails_closed_once_and_records_age() -> None:
    bridge, authority, connection = _make_bridge(heartbeat_timeout_s=1.0)

    # An armed heartbeat starts the shared AI stream and emits the first
    # bounded hover command.  Position packets alone must not extend the
    # flight-controller heartbeat lease.
    first = bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=10.0)
    assert first[0] is not None
    assert first[0].mode.value == "hover"
    before = len(connection.mav.messages)

    stale = bridge.ingest_datagram(_gps(), timestamp_s=11.0)
    assert stale[0] is not None
    assert stale[0].mode.value == "failsafe"
    assert stale[0].reason == "flight_controller_link_unhealthy"
    assert authority.state.value == "failsafe"
    assert connection.mav.messages[-1] == (1, 0, 0, 500, 0, 0)
    assert len(connection.mav.messages) == before + 1

    # More stale telemetry is accepted for evidence but cannot spam neutral
    # output or advance the executor decision stream.
    again = bridge.ingest_datagram(_global_position(), timestamp_s=11.5)
    assert again[0] is None
    evidence = bridge.evidence()
    assert evidence["heartbeat_timeout_s"] == pytest.approx(1.0)
    assert evidence["heartbeat_stale"] is True
    assert evidence["heartbeat_fresh"] is False
    assert evidence["heartbeat_gate"] == "stale"
    assert evidence["heartbeat_stale_events"] == 1
    assert evidence["heartbeat_age_s"] == pytest.approx(1.5)
    assert evidence["heartbeat_stale_since_s"] == pytest.approx(11.0)
    assert evidence["heartbeat_last_decision"]["reason"] == (
        "flight_controller_link_unhealthy"
    )


def test_raw_gps_frame_preserves_velocity_for_truth_free_speed_snapshot() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=12.0)
    decision = bridge.ingest_datagram(_gps(), timestamp_s=12.1)
    assert decision[0] is not None
    snapshot = bridge.evidence()["last_snapshot"]
    # _gps() encodes GPS_RAW_INT.vel=250 cm/s.  The raw MavlinkFrame decoder
    # must retain that field just like a pymavlink object/mapping would.
    assert snapshot["ground_speed_mps"] == pytest.approx(2.5)


def test_live_bridge_keeps_raw_gps_timestamp_separate_from_global_position_timestamp() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=15.0)
    bridge.ingest_datagram(_gps(fix_type=3, satellites_visible=15), timestamp_s=15.1)
    raw_snapshot = bridge.evidence()["last_snapshot"]
    assert raw_snapshot["gps_observed_at_s"] == pytest.approx(15.1)
    assert raw_snapshot["position_observed_at_s"] == pytest.approx(15.1)

    # GLOBAL_POSITION_INT refreshes the fused position cursor but must not
    # rewrite the dedicated GPS_RAW_INT sample timestamp.
    bridge.ingest_datagram(_global_position(), timestamp_s=15.2)
    fused_snapshot = bridge.evidence()["last_snapshot"]
    assert fused_snapshot["position_observed_at_s"] == pytest.approx(15.2)
    assert fused_snapshot["gps_observed_at_s"] == pytest.approx(15.1)

    # A newer degraded raw sample invalidates the raw GPS timestamp instead
    # of leaving stale quality metadata attached to the fused coordinate.
    bridge.ingest_datagram(_gps(fix_type=2, satellites_visible=15), timestamp_s=15.3)
    degraded_snapshot = bridge.evidence()["last_snapshot"]
    assert degraded_snapshot["gps_observed_at_s"] is None


def test_raw_gps_missing_altitude_clears_coordinates_and_freshness() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=16.0)
    bridge.ingest_datagram(_gps(), timestamp_s=16.1)

    # GPS_RAW_INT.alt is required to bind the raw coordinate to a complete
    # navigation observation.  A newer frame with the field omitted must not
    # refresh the old cursor or leave an old coordinate on the map.
    bridge.ingest(
        {
            "type": "GPS_RAW_INT",
            "lat": 312345678,
            "lon": 1215432100,
            "fix_type": 3,
            "satellites_visible": 15,
            "eph": 80,
            "epv": 120,
            "source_system_id": 1,
            "source_component_id": 1,
        },
        timestamp_s=16.2,
    )

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["gps_observed_at_s"] is None
    assert snapshot["position_observed_at_s"] is None
    assert snapshot["latitude_deg"] is None
    assert snapshot["longitude_deg"] is None
    assert snapshot["gps_quality_valid"] is False


def test_raw_gps_missing_vertical_accuracy_never_qualifies_quality_gate() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=17.0)
    bridge.ingest(
        {
            "type": "GPS_RAW_INT",
            "lat": 312345678,
            "lon": 1215432100,
            "alt": 12345,
            "fix_type": 3,
            "satellites_visible": 15,
            "eph": 80,
            # ``epv`` intentionally omitted: the raw cursor can identify a
            # fresh sample, but the complete outdoor quality gate cannot.
            "source_system_id": 1,
            "source_component_id": 1,
        },
        timestamp_s=17.1,
    )

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["gps_observed_at_s"] == pytest.approx(17.1)
    assert snapshot["latitude_deg"] == pytest.approx(31.2345678)
    assert snapshot["gps_quality_valid"] is False


@pytest.mark.parametrize("latitude_e7", [-900000000, 900000000])
def test_raw_gps_exact_poles_fail_closed(latitude_e7: int) -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=18.0)
    bridge.ingest(
        {
            "type": "GPS_RAW_INT",
            "lat": latitude_e7,
            "lon": 0,
            "alt": 12345,
            "fix_type": 3,
            "satellites_visible": 15,
            "eph": 80,
            "epv": 120,
            "source_system_id": 1,
            "source_component_id": 1,
        },
        timestamp_s=18.1,
    )

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["gps_observed_at_s"] is None
    assert snapshot["latitude_deg"] is None
    assert snapshot["longitude_deg"] is None
    assert snapshot["gps_quality_valid"] is False


def test_configured_bridge_fix_threshold_is_enforced() -> None:
    bridge, _, _ = _make_bridge(
        heartbeat_timeout_s=None,
        minimum_gps_fix_type=4,
    )
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=19.0)
    bridge.ingest_datagram(_gps(fix_type=3), timestamp_s=19.1)

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["gps_fix_type"] == 3
    assert snapshot["gps_observed_at_s"] is None
    assert snapshot["gps_quality_valid"] is False


@pytest.mark.parametrize("fix_type", [7, 8])
def test_raw_gps_static_and_ppp_fix_types_reach_shared_ai_snapshot(fix_type: int) -> None:
    """MAVLink enum values STATIC(7) and PPP(8) remain valid end to end."""

    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=13.0)
    decision = bridge.ingest_datagram(
        _gps(fix_type=fix_type, satellites_visible=15), timestamp_s=13.1
    )

    assert decision[0] is not None
    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["gps_fix_type"] == fix_type
    assert snapshot["satellites_visible"] == 15
    assert snapshot["gps_quality_valid"] is True


def test_raw_gps_unknown_satellite_sentinel_clears_previous_count() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=14.0)
    bridge.ingest_datagram(_gps(fix_type=8, satellites_visible=15), timestamp_s=14.1)
    bridge.ingest_datagram(_gps(fix_type=8, satellites_visible=255), timestamp_s=14.2)

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["gps_fix_type"] == 8
    assert snapshot["satellites_visible"] is None
    assert snapshot["gps_quality_valid"] is False


@pytest.mark.parametrize(
    "missing_field",
    ["lat", "lon"],
)
def test_global_position_missing_coordinate_cannot_pair_with_fresh_raw_quality(
    missing_field: str,
) -> None:
    """A partial GLOBAL_POSITION_INT frame is an atomic GPS-quality barrier."""

    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=60.0)
    bridge.ingest_datagram(_gps(fix_type=8, satellites_visible=15), timestamp_s=60.1)
    bridge.ingest_datagram(_global_position(), timestamp_s=60.2)

    malformed = {
        "type": "GLOBAL_POSITION_INT",
        "lat": 31_234_5678,
        "lon": 121_543_2100,
        "relative_alt": 45_000,
        "vx": 100,
        "vy": -50,
        "hdg": 9_000,
        "source_system_id": 1,
        "source_component_id": 1,
    }
    malformed.pop(missing_field)
    bridge.ingest(malformed, timestamp_s=60.3)

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["latitude_deg"] is None
    assert snapshot["longitude_deg"] is None
    assert snapshot["position_observed_at_s"] is None
    assert snapshot["gps_quality_valid"] is False
    assert snapshot["gps_fix_type"] is None
    assert snapshot["satellites_visible"] is None


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (2_147_483_647, 1_215_432_100),  # INT32_MAX latitude sentinel.
        (312_345_678, 2_147_483_647),  # INT32_MAX longitude sentinel.
        (1_000_000_000, 1_215_432_100),  # 100 degrees latitude.
        (312_345_678, 1_900_000_000),  # 190 degrees longitude.
    ],
)
def test_global_position_wire_sentinels_and_out_of_domain_coordinates_fail_closed(
    lat: int,
    lon: int,
) -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=61.0)
    bridge.ingest_datagram(_gps(fix_type=8, satellites_visible=15), timestamp_s=61.1)
    bridge.ingest(
        {
            "type": "GLOBAL_POSITION_INT",
            "lat": lat,
            "lon": lon,
            "relative_alt": 45_000,
            "vx": 100,
            "vy": -50,
            "hdg": 9_000,
            "source_system_id": 1,
            "source_component_id": 1,
        },
        timestamp_s=61.2,
    )

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["latitude_deg"] is None
    assert snapshot["longitude_deg"] is None
    assert snapshot["gps_quality_valid"] is False
    assert snapshot["gps_fix_type"] is None


def test_global_position_accepts_legal_zero_zero_coordinate() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=62.0)
    bridge.ingest(
        {
            "type": "GLOBAL_POSITION_INT",
            "lat": 0,
            "lon": 0,
            "relative_alt": 1_000,
            "vx": 0,
            "vy": 0,
            "hdg": 0,
            "source_system_id": 1,
            "source_component_id": 1,
        },
        timestamp_s=62.1,
    )

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["latitude_deg"] == pytest.approx(0.0)
    assert snapshot["longitude_deg"] == pytest.approx(0.0)
    assert snapshot["position_observed_at_s"] == pytest.approx(62.1)


def test_global_position_sentinels_do_not_overwrite_valid_motion_fields() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=63.0)
    bridge.ingest(
        {
            "type": "GLOBAL_POSITION_INT",
            "lat": 312_345_678,
            "lon": 1_215_432_100,
            "relative_alt": 45_000,
            "vx": 100,
            "vy": -50,
            "hdg": 9_000,
            "source_system_id": 1,
            "source_component_id": 1,
        },
        timestamp_s=63.1,
    )
    bridge.ingest(
        {
            "type": "GLOBAL_POSITION_INT",
            "lat": 312_345_679,
            "lon": 1_215_432_101,
            "relative_alt": 2_147_483_647,
            "vx": 32_767,
            "vy": -32_768,
            "hdg": 65_535,
            "source_system_id": 1,
            "source_component_id": 1,
        },
        timestamp_s=63.2,
    )

    snapshot = bridge.evidence()["last_snapshot"]
    assert snapshot["altitude_agl_m"] == pytest.approx(45.0)
    assert snapshot["ground_speed_mps"] == pytest.approx(1.118, rel=1e-3)
    assert snapshot["heading_deg"] == pytest.approx(90.0)
    assert snapshot["altitude_observed_at_s"] == pytest.approx(63.1)


@pytest.mark.parametrize("timestamp", [True, -1.0, float("nan"), float("inf")])
def test_bridge_rejects_invalid_telemetry_timestamp(timestamp: object) -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    with pytest.raises(ValueError, match="telemetry timestamp"):
        bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=timestamp)  # type: ignore[arg-type]


def test_watchdog_tick_enforces_heartbeat_freshness_without_new_frames() -> None:
    bridge, authority, connection = _make_bridge(heartbeat_timeout_s=0.5)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=20.0)
    before = len(connection.mav.messages)

    decision = bridge.watchdog_tick(now_s=20.5)
    assert decision is not None
    assert decision.mode.value == "failsafe"
    assert decision.reason == "flight_controller_link_unhealthy"
    assert authority.state.value == "failsafe"
    assert len(connection.mav.messages) == before + 1
    assert bridge.evidence()["heartbeat_stale_events"] == 1


def test_fresh_heartbeat_recovers_gate_but_requires_explicit_authority_recovery() -> None:
    bridge, authority, connection = _make_bridge(heartbeat_timeout_s=0.5)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=30.0)
    stale = bridge.watchdog_tick(now_s=30.6)
    assert stale is not None and stale.mode.value == "failsafe"
    neutral_count = len(connection.mav.messages)

    # A fresh heartbeat clears only the telemetry lease.  The executor remains
    # in terminal FAILSAFE until the operator explicitly releases/re-enables
    # authority; stale state is never silently resumed.
    recovered = bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=31.0)
    assert recovered[0] is not None
    assert recovered[0].mode.value == "failsafe"
    evidence = bridge.evidence()
    assert evidence["heartbeat_stale"] is False
    assert evidence["heartbeat_fresh"] is True
    assert evidence["heartbeat_recoveries"] == 1
    assert evidence["heartbeat_gate"] == "fresh"
    assert len(connection.mav.messages) == neutral_count
    assert authority.state.value == "failsafe"


def test_startup_disarmed_heartbeat_stale_wait_does_not_trip_ai_authority() -> None:
    bridge, authority, connection = _make_bridge(heartbeat_timeout_s=0.5)
    assert bridge.ingest_datagram(_heartbeat(armed=False), timestamp_s=40.0) == (None,)
    assert bridge.watchdog_tick(now_s=40.6) is None
    evidence = bridge.evidence()
    assert evidence["heartbeat_stale"] is True
    assert evidence["heartbeat_startup_stale_wait"] == 1
    assert authority.state.value == "ai"
    assert connection.mav.messages == []

    # First armed heartbeat both recovers the lease and releases the existing
    # startup gate; normal AI execution starts from this fresh observation.
    decision = bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=41.0)
    assert decision[0] is not None
    assert decision[0].mode.value == "hover"
    evidence = bridge.evidence()
    assert evidence["heartbeat_stale"] is False
    assert evidence["heartbeat_recoveries"] == 1
    assert evidence["armed_observed"] is True


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_heartbeat_timeout_validation(value: object) -> None:
    authority = SimulatorControlAuthority()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:24544"),
        connection=_Connection(),
    )
    with pytest.raises(ValueError, match="heartbeat_timeout_s"):
        SimulatorTelemetryBridge(SimulatorAiExecutor(adapter), heartbeat_timeout_s=value)  # type: ignore[arg-type]


def test_heartbeat_timeout_can_be_disabled_for_replay_fixtures() -> None:
    bridge, authority, connection = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=50.0)
    decision = bridge.ingest_datagram(_gps(), timestamp_s=55.0)
    assert decision[0] is not None
    assert decision[0].mode.value == "hover"
    evidence = bridge.evidence()
    assert evidence["heartbeat_timeout_s"] is None
    assert evidence["heartbeat_gate"] == "disabled"
    assert evidence["heartbeat_fresh"] is None
    assert evidence["heartbeat_stale"] is False
    assert authority.state.value == "ai"
    assert len(connection.mav.messages) == 2


def test_perception_merge_whitelists_target_and_runs_shared_track_path() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=70.0)
    bridge.ingest_datagram(_gps(), timestamp_s=70.1)
    before = bridge.evidence()["last_snapshot"]

    # Deliberately include forged vehicle fields in the provider snapshot.
    # ``ingest_perception`` must copy only the target tuple and keep PX4's
    # observed navigation/authority state as the source of truth.
    observation = SimulatorTelemetrySnapshot(
        observed_at_s=70.2,
        latitude_deg=88.0,
        longitude_deg=-170.0,
        gps_fix_type=0,
        link_healthy=False,
        armed=False,
        target_visible=True,
        target_bearing_deg=18.0,
        target_distance_m=90.0,
        target_confidence=0.86,
    )
    decision = bridge.ingest_perception(observation, timestamp_s=70.2)

    assert decision is not None
    assert decision.mode.value == "track"
    assert decision.accepted is True
    after = bridge.evidence()
    snapshot = after["last_snapshot"]
    assert snapshot["target_visible"] is True
    assert snapshot["target_bearing_deg"] == pytest.approx(18.0)
    assert snapshot["target_distance_m"] == pytest.approx(90.0)
    assert snapshot["target_confidence"] == pytest.approx(0.86)
    assert snapshot["latitude_deg"] == before["latitude_deg"]
    assert snapshot["longitude_deg"] == before["longitude_deg"]
    assert snapshot["gps_fix_type"] == before["gps_fix_type"]
    assert snapshot["link_healthy"] is True
    assert snapshot["armed"] is True
    assert after["perception_observations"] == 1
    assert after["perception_accepted"] == 1
    assert after["perception_rejected"] == 0
    assert after["target_observed_at_s"] == pytest.approx(70.2)


def test_perception_lease_expires_before_a_later_telemetry_step() -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=71.0)
    decision = bridge.ingest_perception(
        {
            "observed_at_s": 71.1,
            "target_visible": True,
            "target_bearing_deg": -12.0,
            "target_distance_m": 40.0,
            "target_confidence": 0.9,
        },
        timestamp_s=71.1,
    )
    assert decision is not None and decision.mode.value == "track"
    assert bridge.evidence()["last_snapshot"]["target_visible"] is True

    # No new perception frame arrives.  The next vehicle frame crosses the
    # 1.5 s lease and must clear the target before policy evaluation.
    later = bridge.ingest(
        {
            "type": "VFR_HUD",
            "source_system_id": 1,
            "groundspeed": 4.0,
            "heading": 90,
        },
        timestamp_s=72.7,
    )
    assert later is not None and later.mode.value == "hover"
    evidence = bridge.evidence()
    assert evidence["last_snapshot"]["target_visible"] is False
    assert evidence["last_snapshot"]["target_confidence"] is None
    assert evidence["target_observed_at_s"] is None
    assert evidence["perception_expired"] == 1
    assert evidence["last_perception"]["reason"] == "target_lease_expired"


@pytest.mark.parametrize(
    ("observation", "timestamp_s", "reason"),
    (
        (
            {"observed_at_s": 80.0, "target_visible": True},
            79.0,
            "future_observation",
        ),
        (
            {"observed_at_s": 80.0, "target_visible": True},
            82.0,
            "stale_observation",
        ),
    ),
)
def test_perception_timestamp_gate_rejects_future_or_stale_frames(
    observation: dict[str, object], timestamp_s: float, reason: str
) -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=79.0)
    assert bridge.ingest_perception(observation, timestamp_s=timestamp_s) is None
    evidence = bridge.evidence()
    assert evidence["perception_rejected"] == 1
    assert evidence["perception_rejections"][-1]["reason"] == reason
    assert evidence["last_snapshot"]["target_visible"] is False


@pytest.mark.parametrize(
    ("observation", "timestamp_s", "reason"),
    (
        (
            {"observed_at_s": 81.0, "target_visible": True},
            80.5,
            "future_observation",
        ),
        (
            {"observed_at_s": 79.9, "target_visible": True},
            80.1,
            "out_of_order_observation",
        ),
        (
            {"observed_at_s": 78.0, "target_visible": True},
            80.0,
            "stale_observation",
        ),
    ),
)
def test_rejected_perception_clears_an_existing_target_lease(
    observation: dict[str, object], timestamp_s: float, reason: str
) -> None:
    bridge, _, _ = _make_bridge(heartbeat_timeout_s=None)
    bridge.ingest_datagram(_heartbeat(armed=True), timestamp_s=79.0)
    accepted = bridge.ingest_perception(
        {
            "observed_at_s": 80.0,
            "target_visible": True,
            "target_bearing_deg": 5.0,
            "target_distance_m": 20.0,
            "target_confidence": 0.8,
        },
        timestamp_s=80.0,
    )
    assert accepted is not None and accepted.mode.value == "track"
    assert bridge.evidence()["last_snapshot"]["target_visible"] is True

    assert bridge.ingest_perception(observation, timestamp_s=timestamp_s) is None
    evidence = bridge.evidence()
    assert evidence["perception_rejections"][-1]["reason"] == reason
    assert evidence["last_snapshot"]["target_visible"] is False
    assert evidence["last_snapshot"]["target_bearing_deg"] is None
    assert evidence["last_snapshot"]["target_distance_m"] is None
    assert evidence["target_observed_at_s"] is None
