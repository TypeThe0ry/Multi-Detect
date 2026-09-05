from __future__ import annotations

import math
from dataclasses import dataclass, replace

import pytest

from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.multimodal_ranging import RangeSolution, RangeValidity
from multidetect.simulator import (
    SimulatorAiExecutor,
    SimulatorAiMode,
    SimulatorControlAuthority,
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
    SimulatorPerceptionBridge,
    SimulatorTelemetryBridge,
)
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


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


def _telemetry(*, gps: bool = True, fixed_wing: bool = False) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=120.0 if fixed_wing else 24.0,
        roll_deg=2.0,
        pitch_deg=-1.0,
        ground_speed_mps=28.0 if fixed_wing else 6.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=gps,
        link_healthy=True,
        flight_mode_allows_deploy=True,
        release_zone_clear=True,
        latitude_deg=31.2345678 if gps else math.nan,
        longitude_deg=121.5432100 if gps else math.nan,
        heading_deg=88.0 if fixed_wing else 12.0,
        battery_remaining_pct=82.0,
        satellites_visible=18 if gps else None,
        gps_fix_type=3 if gps else None,
        gps_horizontal_accuracy_m=1.8 if gps else math.nan,
        gps_vertical_accuracy_m=2.7 if gps else math.nan,
        gps_observed_at_s=10.0 if gps else math.nan,
        armed=True,
        flight_mode="AUTO" if fixed_wing else "GUIDED",
        altitude_observed_at_s=10.0,
        position_observed_at_s=10.0 if gps else math.nan,
    )


def _track(
    track_id: str = "target-a",
    *,
    primary: bool = True,
    confidence: float = 0.91,
    state: UnifiedTrackState = UnifiedTrackState.TRACKING,
) -> UnifiedTrackSnapshot:
    box = BoundingBox(0.40, 0.35, 0.52, 0.55)
    return UnifiedTrackSnapshot(
        track_id=track_id,
        state=state,
        label="vehicle",
        bbox=box,
        predicted_bbox=box,
        first_seen_at_s=9.0,
        last_seen_at_s=10.0,
        state_changed_at_s=9.0,
        observation_count=12,
        missed_frame_count=0,
        confidence=confidence,
        tracking_quality=0.87,
        velocity_x_s=0.02,
        velocity_y_s=-0.01,
        appearance_sample_count=3,
        last_appearance_distance=None,
        reid_confirmed=True,
        locked=primary,
        primary=primary,
        actionable=state is not UnifiedTrackState.LOST,
    )


def _range(
    target_id: str = "target-a", *, validity: RangeValidity = RangeValidity.VALID
) -> RangeSolution:
    if validity is RangeValidity.INVALID:
        return RangeSolution(
            target_id=target_id,
            frame_id="frame-10",
            calibration_id="cam-vtol",
            evaluated_at_s=10.0,
            validity=validity,
            reasons=("gps_denied_or_inconsistent",),
            sources=(),
            rejected_sources=("monocular_size",),
        )
    return RangeSolution(
        target_id=target_id,
        frame_id="frame-10",
        calibration_id="cam-vtol",
        evaluated_at_s=10.0,
        validity=validity,
        reasons=("laser_and_ground_ray_fused",),
        sources=("laser", "ground_plane"),
        rejected_sources=(),
        slant_range_m=48.0,
        ground_range_m=45.0,
        relative_bearing_deg=-14.0,
        bearing_sigma_deg=1.2,
        data_freshness_s=0.125,
        sensor_consistency=0.92,
    )


def test_vtol_production_observations_build_truth_free_snapshot() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 11.0)
    snapshot = bridge.build_snapshot(
        _telemetry(),
        _track(),
        _range(),
        {"state": "clear", "advisory_only": True},
        frame_id="frame-10",
        frame_captured_at_s=9.875,
        now_s=10.2,
        home={"latitude_deg": 31.2340000, "longitude_deg": 121.5425000},
    )

    assert snapshot.observed_at_s == pytest.approx(10.2)
    assert snapshot.altitude_agl_m == pytest.approx(24.0)
    assert snapshot.ground_speed_mps == pytest.approx(6.0)
    assert snapshot.target_visible is True
    assert snapshot.target_bearing_deg == pytest.approx(-14.0)
    assert snapshot.target_distance_m == pytest.approx(45.0)  # ground range preference
    assert snapshot.target_confidence == pytest.approx(0.87)  # min(track confidence, quality)
    assert snapshot.gps_quality_valid is True
    assert snapshot.home_distance_m is not None and snapshot.home_distance_m > 0.0
    assert snapshot.home_bearing_deg is not None
    assert snapshot.obstacle_ahead is False


def test_perception_bridge_preserves_dedicated_gps_timestamp() -> None:
    telemetry = _telemetry()
    telemetry = replace(
        telemetry,
        gps_observed_at_s=9.75,
        position_observed_at_s=10.0,
    )
    snapshot = SimulatorPerceptionBridge().build_snapshot(telemetry, _track(), _range())
    assert snapshot.gps_observed_at_s == pytest.approx(9.75)
    assert snapshot.position_observed_at_s == pytest.approx(10.0)


def test_perception_bridge_legacy_mapping_infers_gps_timestamp_only_for_complete_fix() -> None:
    bridge = SimulatorPerceptionBridge()
    complete = bridge.build_snapshot(
        {
            "observed_at_s": 12.0,
            "latitude_deg": 31.2,
            "longitude_deg": 121.5,
            "gps_fix_type": 3,
            "gps_horizontal_accuracy_m": 1.0,
            "satellites_visible": 12,
            "position_observed_at_s": 11.5,
            "position_healthy": True,
        },
        _track(),
        _range(),
    )
    assert complete.gps_observed_at_s == pytest.approx(11.5)

    denied = bridge.build_snapshot(
        {
            "observed_at_s": 13.0,
            "latitude_deg": math.nan,
            "longitude_deg": math.nan,
            "gps_fix_type": None,
            "position_observed_at_s": 12.5,
            "position_healthy": False,
        },
        _track(),
        _range(),
    )
    assert denied.gps_observed_at_s is None


def test_fixed_wing_mapping_selects_primary_and_derives_relative_bearing() -> None:
    bridge = SimulatorPerceptionBridge(prefer_ground_range=False, clock=lambda: 20.0)
    telemetry = {
        "timestamp_s": 20.0,
        "altitude_m": 150.0,
        "heading_deg": 90.0,
        "ground_speed_mps": 34.0,
        "latitude_deg": 40.0,
        "longitude_deg": -73.0,
        "gps_fix_type": 4,
        "gps_horizontal_accuracy_m": 1.2,
        "armed": True,
        "link_healthy": True,
        "battery_percent": 76.0,
        "flight_mode": "AUTO",
    }
    tracks = [
        {"track_id": "secondary", "confidence": 0.98, "tracking_quality": 0.98},
        {
            "track_id": "primary-fw",
            "frame_id": "frame-fw",
            "captured_at_s": 19.875,
            "confidence": 0.82,
            "tracking_quality": 0.80,
            "primary": True,
            "locked": True,
            "actionable": True,
            "state": "tracking",
        },
    ]
    snapshot = bridge.build_snapshot(
        telemetry,
        tracks=tracks,
        ranging={
            "target_id": "primary-fw",
            "frame_id": "frame-fw",
            "validity": "degraded",
            "slant_range_m": 210.0,
            "absolute_bearing_deg": 110.0,
            "data_freshness_s": 0.125,
            "source_captured_at_s": 19.875,
            "sensor_consistency": 0.75,
        },
    )
    assert snapshot.target_visible is True
    assert snapshot.target_distance_m == pytest.approx(210.0)
    assert snapshot.target_bearing_deg == pytest.approx(20.0)
    assert snapshot.target_confidence == pytest.approx(0.75)
    assert bridge.evidence()["selected_target_id"] == "primary-fw"


def test_no_gps_is_preserved_as_unavailable_and_does_not_make_up_home_vector() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 30.0)
    snapshot = bridge.build_snapshot(_telemetry(gps=False), _track(), _range())
    assert math.isnan(snapshot.latitude_deg)
    assert math.isnan(snapshot.longitude_deg)
    assert snapshot.gps_fix_type is None
    assert snapshot.gps_quality_valid is False
    assert snapshot.home_bearing_deg is None
    assert snapshot.home_distance_m is None


def test_invalid_range_removes_metric_target_distance_but_keeps_visual_target() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 40.0)
    snapshot = bridge.build_snapshot(_telemetry(), _track(), _range(validity=RangeValidity.INVALID))
    assert snapshot.target_visible is True
    assert snapshot.target_confidence == pytest.approx(0.87)
    assert snapshot.target_distance_m is None
    assert snapshot.target_bearing_deg is None
    assert bridge.evidence()["last_result"] is None


def test_stale_range_data_clears_metric_fields_but_keeps_visual_track() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 10.0)
    stale = replace(_range(), data_freshness_s=1.501)
    result = bridge.feed_result(_telemetry(), _track(), stale, now_s=10.0)
    snapshot = result.snapshot

    assert snapshot.target_visible is True
    assert snapshot.target_distance_m is None
    assert snapshot.target_bearing_deg is None
    assert snapshot.target_confidence == pytest.approx(0.87)
    assert "range_data_stale" in result.reasons
    assert bridge.evidence()["maximum_range_age_s"] == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("timestamp", "reason"),
    [
        (8.4, "range_source_stale"),
        (10.1, "range_source_future"),
    ],
)
def test_range_source_timestamp_is_fail_closed(
    timestamp: float, reason: str
) -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 10.0)
    result = bridge.feed_result(
        _telemetry(),
        _track(),
        {
            "target_id": "target-a",
            "frame_id": "frame-10",
            "validity": "valid",
            "distance_m": 42.0,
            "data_freshness_s": 0.125,
            "source_captured_at_s": timestamp,
        },
        now_s=10.0,
    )
    snapshot = result.snapshot

    assert snapshot.target_visible is True
    assert snapshot.target_distance_m is None
    assert snapshot.target_bearing_deg is None
    assert reason in result.reasons


def test_missing_explicit_range_source_timestamp_does_not_publish_metric_range() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 10.0)
    result = bridge.feed_result(
        _telemetry(),
        _track(),
        {
            "target_id": "target-a",
            "frame_id": "frame-10",
            "validity": "valid",
            "distance_m": 42.0,
            "data_freshness_s": 0.125,
            "source_captured_at_s": None,
        },
        now_s=10.0,
    )
    snapshot = result.snapshot

    assert snapshot.target_visible is True
    assert snapshot.target_distance_m is None
    assert "range_source_timestamp_missing" in result.reasons


def test_missing_range_data_freshness_does_not_publish_metric_range() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 10.0)
    result = bridge.feed_result(
        _telemetry(),
        _track(),
        {
            "target_id": "target-a",
            "frame_id": "frame-10",
            "validity": "valid",
            "distance_m": 42.0,
            "source_captured_at_s": 10.0,
        },
        now_s=10.0,
    )

    assert result.snapshot.target_visible is True
    assert result.snapshot.target_distance_m is None
    assert "range_data_freshness_missing" in result.reasons


def test_range_frame_id_must_match_current_perception_frame() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 10.0)
    result = bridge.feed_result(
        _telemetry(),
        _track(),
        _range(),
        frame_id="frame-new",
        frame_captured_at_s=10.0,
        now_s=10.0,
    )

    assert result.snapshot.target_visible is True
    assert result.snapshot.target_distance_m is None
    assert "range_frame_mismatch" in result.reasons


def test_range_age_boundary_is_inclusive_and_then_fails_closed() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 11.375)

    boundary = bridge.feed_result(
        _telemetry(),
        _track(),
        _range(),
        frame_id="frame-10",
        frame_captured_at_s=9.875,
        now_s=11.375,
    )
    assert boundary.snapshot.target_distance_m == pytest.approx(45.0)

    stale = bridge.feed_result(
        _telemetry(),
        _track(),
        _range(),
        frame_id="frame-10",
        frame_captured_at_s=9.875,
        now_s=11.375001,
    )
    assert stale.snapshot.target_distance_m is None
    assert "range_source_stale" in stale.reasons or "range_data_stale" in stale.reasons


def test_old_range_solution_cannot_be_relabelled_with_new_frame_capture_time() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 11.0)
    result = bridge.feed_result(
        _telemetry(),
        _track(),
        _range(),
        frame_id="frame-10",
        frame_captured_at_s=11.0,
        now_s=11.0,
    )

    assert result.snapshot.target_visible is True
    assert result.snapshot.target_distance_m is None
    assert "range_frame_timestamp_mismatch" in result.reasons


def test_negative_range_data_age_is_rejected_and_counted() -> None:
    bridge = SimulatorPerceptionBridge(clock=lambda: 10.0)
    with pytest.raises(ValueError, match="range data_freshness_s"):
        bridge.feed_result(
            _telemetry(),
            _track(),
            replace(_range(), data_freshness_s=-0.1),
            now_s=10.0,
        )
    evidence = bridge.evidence()
    assert evidence["rejected"] == 1
    assert evidence["last_error"] is not None


def test_feed_uses_shared_executor_transport_and_never_creates_second_transport() -> None:
    connection = _Connection()
    authority = SimulatorControlAuthority()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:24570"),
        connection=connection,
    )
    executor = SimulatorAiExecutor(adapter, clock=lambda: 50.0)
    feed = SimulatorPerceptionBridge(executor=executor, clock=lambda: 50.0)
    feed.enable_ai()
    result = feed.feed_result(
        _telemetry(),
        _track(),
        replace(_range(), evaluated_at_s=50.0),
        frame_id="frame-10",
        frame_captured_at_s=49.875,
        now_s=50.0,
    )
    assert result.decision is not None
    assert result.decision.mode is SimulatorAiMode.TRACK
    assert result.decision.accepted is True
    assert len(connection.mav.messages) == 1
    evidence = feed.evidence()
    assert evidence["secondary_transport_emitted"] is False
    assert evidence["gazebo_truth_read"] is False
    assert evidence["truth_free"] is True


def test_feed_with_telemetry_bridge_uses_shared_perception_gate() -> None:
    connection = _Connection()
    authority = SimulatorControlAuthority()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:24571"),
        connection=connection,
    )
    executor = SimulatorAiExecutor(adapter, clock=lambda: 50.0)
    telemetry_bridge = SimulatorTelemetryBridge(
        executor,
        clock=lambda: 50.0,
        heartbeat_timeout_s=None,
    )
    telemetry_bridge.enable_ai()
    telemetry_bridge.ingest(
        {
            "type": "HEARTBEAT",
            "source_system_id": 1,
            "base_mode": 128,
        },
        timestamp_s=50.0,
    )
    telemetry_bridge.ingest(
        {
            "type": "GPS_RAW_INT",
            "source_system_id": 1,
            "lat": 312345678,
            "lon": 1215432100,
            "alt": 12345,
            "eph": 80,
            "epv": 120,
            "vel": 250,
            "fix_type": 3,
            "satellites_visible": 15,
        },
        timestamp_s=50.1,
    )
    before = len(connection.mav.messages)

    feed = SimulatorPerceptionBridge(
        telemetry_bridge=telemetry_bridge,
        clock=lambda: 50.0,
    )
    result = feed.feed_result(
        _telemetry(),
        _track(),
        replace(_range(), evaluated_at_s=50.1),
        frame_id="frame-10",
        frame_captured_at_s=49.975,
        now_s=50.2,
    )

    assert result.decision is not None
    assert result.decision.mode is SimulatorAiMode.TRACK
    assert result.decision.accepted is True
    assert len(connection.mav.messages) == before + 1
    bridge_evidence = telemetry_bridge.evidence()
    assert bridge_evidence["perception_accepted"] == 1
    assert bridge_evidence["last_snapshot"]["target_visible"] is True
    assert feed.evidence()["telemetry_bridge_attached"] is True


def test_feed_with_telemetry_bridge_surfaces_rejected_target_as_cleared() -> None:
    connection = _Connection()
    authority = SimulatorControlAuthority()
    adapter = SimulatorMavlinkControlAdapter(
        authority,
        config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:24572"),
        connection=connection,
    )
    executor = SimulatorAiExecutor(adapter, clock=lambda: 60.0)
    telemetry_bridge = SimulatorTelemetryBridge(
        executor,
        clock=lambda: 60.0,
        heartbeat_timeout_s=None,
    )
    telemetry_bridge.enable_ai()
    telemetry_bridge.ingest(
        {
            "type": "HEARTBEAT",
            "source_system_id": 1,
            "base_mode": 128,
        },
        timestamp_s=60.0,
    )
    feed = SimulatorPerceptionBridge(
        telemetry_bridge=telemetry_bridge,
        clock=lambda: 60.0,
    )
    accepted = feed.feed_result(
        _telemetry(),
        _track(),
        replace(_range(), evaluated_at_s=60.0),
        frame_id="frame-10",
        frame_captured_at_s=59.875,
        now_s=60.0,
    )
    assert accepted.snapshot.target_visible is True
    assert telemetry_bridge.evidence()["target_observed_at_s"] == pytest.approx(60.0)

    rejected = feed.feed_result(
        _telemetry(),
        _track(),
        replace(_range(), evaluated_at_s=60.0),
        frame_id="frame-10",
        frame_captured_at_s=59.875,
        now_s=58.0,
    )
    assert rejected.decision is None
    assert rejected.snapshot.target_visible is False
    assert rejected.snapshot.target_distance_m is None
    assert "telemetry_bridge_rejected_perception" in rejected.reasons
    assert feed.evidence()["rejected"] == 1
    assert feed.evidence()["last_error"] == "telemetry_bridge_rejected_perception"
    assert telemetry_bridge.evidence()["target_observed_at_s"] is None


@pytest.mark.parametrize(
    "mutator",
    [
        lambda t: {**t, "latitude_deg": float("inf")},
        lambda t: {**t, "latitude_deg": 90.0},
        lambda t: {key: value for key, value in t.items() if key != "longitude_deg"},
        lambda t: {**t, "longitude_deg": float("nan")},
    ],
)
def test_invalid_input_is_rejected_without_a_partial_snapshot(mutator) -> None:
    telemetry = {
        "timestamp_s": 1.0,
        "altitude_agl_m": 10.0,
        "latitude_deg": 31.0,
        "longitude_deg": 121.0,
        "armed": True,
    }
    bridge = SimulatorPerceptionBridge()
    with pytest.raises(ValueError):
        bridge.feed_result(mutator(telemetry))
    evidence = bridge.evidence()
    assert evidence["rejected"] == 1
    assert evidence["last_error"] is not None
    assert evidence["last_snapshot"] is None


def test_invalid_track_confidence_is_rejected() -> None:
    with pytest.raises(ValueError):
        SimulatorPerceptionBridge().build_snapshot(
            _telemetry(),
            {"track_id": "bad", "confidence": float("inf")},
        )


@dataclass
class _TelemetryLike:
    timestamp_s: float = 2.0
    altitude_agl_m: float = 8.0
    heading_deg: float = 270.0
    ground_speed_mps: float = 3.0
    latitude_deg: float = 31.0
    longitude_deg: float = 121.0
    gps_fix_type: int = 3
    gps_horizontal_accuracy_m: float = 2.0
    armed: bool = True
    link_healthy: bool = True


def test_object_like_inputs_are_supported_without_truth_fields() -> None:
    snapshot = SimulatorPerceptionBridge().observe(
        _TelemetryLike(),
        {
            "track_id": "obj",
            "frame_id": "frame-obj",
            "captured_at_s": 1.875,
            "primary": True,
            "confidence": 0.7,
            "state": "tracking",
        },
        {
            "target_id": "obj",
            "frame_id": "frame-obj",
            "validity": "valid",
            "distance_m": 20.0,
            "bearing_deg": 5.0,
            "data_freshness_s": 0.125,
            "source_captured_at_s": 1.875,
        },
        {"state": "avoid"},
    )
    assert snapshot.target_distance_m == pytest.approx(20.0)
    assert snapshot.target_bearing_deg == pytest.approx(5.0)
    assert snapshot.obstacle_ahead is True
    assert "truth" not in snapshot.to_mapping()
