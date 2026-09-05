from __future__ import annotations

import pytest

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.mission_progress import (
    MissionProgressConfig,
    MissionProgressError,
    MissionProgressPhase,
    MissionProgressSnapshot,
    MissionProgressTracker,
)
from multidetect.px4_mission_feedback import Px4MissionFeedbackTracker
from multidetect.simulator import FlightMode, FlightTelemetrySnapshot, SimulatorClosedLoop


def _mission(*items: MissionItem) -> ExecutableMission:
    return ExecutableMission(
        mission_id="progress-fixture",
        platform_plan_hash="a" * 64,
        airframe_profile_hash="b" * 64,
        airframe_adapter_id="fixture-adapter-v1",
        airframe_adapter_hash="c" * 64,
        items=tuple(items),
    )


def _item(
    sequence: int,
    command: MissionCommand,
    *,
    altitude_m: float,
    latitude_deg: float = 1.0,
    longitude_deg: float = 2.0,
    param1: float = 0.0,
    param2: float = 0.0,
    param3: float = 0.0,
) -> MissionItem:
    return MissionItem(
        sequence=sequence,
        command=command,
        frame=(
            MissionFrame.MISSION
            if command is MissionCommand.VTOL_TRANSITION
            else MissionFrame.GLOBAL_RELATIVE_ALT_INT
        ),
        latitude_deg=None if command is MissionCommand.VTOL_TRANSITION else latitude_deg,
        longitude_deg=None if command is MissionCommand.VTOL_TRANSITION else longitude_deg,
        altitude_m=None if command is MissionCommand.VTOL_TRANSITION else altitude_m,
        param1=param1,
        param2=param2,
        param3=param3,
    )


def _telemetry(
    observed_at_s: float,
    *,
    altitude_m: float = 10.0,
    latitude_deg: float = 1.0,
    longitude_deg: float = 2.0,
    gps_observed_at_s: float | None = None,
    link_healthy: bool | None = True,
    armed: bool | None = True,
    flight_mode: str = "AUTO.MISSION",
    mission_started: bool = True,
) -> FlightTelemetrySnapshot:
    return FlightTelemetrySnapshot(
        observed_at_s=observed_at_s,
        altitude_agl_m=altitude_m,
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0,
        gps_vertical_accuracy_m=2.0,
        satellites_visible=10,
        gps_observed_at_s=(observed_at_s if gps_observed_at_s is None else gps_observed_at_s),
        link_healthy=link_healthy,
        armed=armed,
        flight_mode=flight_mode,
        mission_started=mission_started,
    )


def test_progress_tracks_takeoff_waypoint_loiter_and_landing() -> None:
    mission = _mission(
        _item(0, MissionCommand.TAKEOFF, altitude_m=10.0),
        _item(1, MissionCommand.WAYPOINT, altitude_m=10.0, param2=5.0),
        _item(2, MissionCommand.LOITER_TIME, altitude_m=10.0, param1=1.0, param3=5.0),
        _item(3, MissionCommand.LAND, altitude_m=0.0),
    )
    tracker = MissionProgressTracker(mission)

    tracker.start()
    first = tracker.update(_telemetry(0.0))
    assert first.phase is MissionProgressPhase.ACTIVE
    assert first.current_sequence == 2
    assert first.completed_sequences == (0, 1)
    assert first.reason == "loitering"

    waiting = tracker.update(_telemetry(0.5))
    assert waiting.current_sequence == 2
    assert waiting.dwell_elapsed_s == pytest.approx(0.5)

    after_loiter = tracker.update(_telemetry(1.1))
    assert after_loiter.current_sequence == 3
    assert after_loiter.completed_sequences == (0, 1, 2)

    complete = tracker.update(_telemetry(1.2, altitude_m=0.0))
    assert complete.phase is MissionProgressPhase.COMPLETE
    assert complete.current_sequence is None
    assert complete.completed_sequences == (0, 1, 2, 3)
    assert complete.progress_fraction == 1.0


def test_progress_requires_fresh_gps_and_healthy_link_before_advancing() -> None:
    mission = _mission(_item(0, MissionCommand.TAKEOFF, altitude_m=10.0))
    tracker = MissionProgressTracker(mission)
    tracker.start()

    stale = tracker.update(_telemetry(2.0, gps_observed_at_s=0.0))
    assert stale.phase is MissionProgressPhase.WAITING_FOR_GPS
    assert stale.completed_sequences == ()
    assert stale.reason == "gps_sample_stale"

    disconnected = tracker.update(_telemetry(2.1, link_healthy=False))
    assert disconnected.phase is MissionProgressPhase.WAITING_FOR_LINK
    assert disconnected.reason == "link_not_healthy"

    fresh = tracker.update(_telemetry(2.2, altitude_m=10.0))
    assert fresh.phase is MissionProgressPhase.COMPLETE


def test_vtol_transition_uses_observed_flight_mode_without_gps_shortcut() -> None:
    mission = _mission(
        _item(0, MissionCommand.VTOL_TAKEOFF, altitude_m=10.0),
        _item(1, MissionCommand.VTOL_TRANSITION, altitude_m=0.0, param1=4.0),
        _item(2, MissionCommand.WAYPOINT, altitude_m=10.0, latitude_deg=1.001),
    )
    tracker = MissionProgressTracker(mission)
    tracker.start()

    waiting = tracker.update(_telemetry(0.0, flight_mode="MC"))
    assert waiting.current_sequence == 1
    assert waiting.reason == "awaiting_vtol_fw_transition"

    transitioned = tracker.update(_telemetry(0.1, flight_mode="FW"))
    assert transitioned.current_sequence == 2
    assert transitioned.completed_sequences == (0, 1)


def test_progress_snapshot_round_trip_and_tamper_rejection() -> None:
    mission = _mission(_item(0, MissionCommand.TAKEOFF, altitude_m=10.0))
    tracker = MissionProgressTracker(mission)
    tracker.start()
    snapshot = tracker.update(_telemetry(0.0, altitude_m=10.0))
    reopened = MissionProgressSnapshot.from_mapping(snapshot.to_mapping())
    assert reopened == snapshot
    assert reopened.canonical_hash == snapshot.canonical_hash

    tampered = snapshot.to_mapping()
    tampered["progress_fraction"] = 0.25
    with pytest.raises(MissionProgressError, match="progress_fraction"):
        MissionProgressSnapshot.from_mapping(tampered)


def test_progress_rejects_exact_pole_mission_coordinate() -> None:
    mission = _mission(_item(0, MissionCommand.TAKEOFF, altitude_m=10.0, latitude_deg=90.0))
    with pytest.raises(MissionProgressError, match="exact-pole"):
        MissionProgressTracker(mission)


def test_closed_loop_exposes_the_same_bound_progress_snapshot() -> None:
    mission = _mission(
        _item(0, MissionCommand.TAKEOFF, altitude_m=10.0, latitude_deg=0.0, longitude_deg=0.0)
    )
    sim = SimulatorClosedLoop(FlightMode.MULTIROTOR, seed=8, mission=mission)
    try:
        assert sim.arm() is True
        assert sim.start_mission() is True
        tick = sim.step()
        assert tick.mission_progress is not None
        assert tick.mission_progress.phase is MissionProgressPhase.COMPLETE
        evidence = sim.evidence()
        assert evidence["mission_progress"]["mission_id"] == mission.mission_id
        assert evidence["mission_progress"]["sha256"] == tick.mission_progress.canonical_hash
    finally:
        sim.close()


def test_progress_config_cannot_relax_canonical_gps_age() -> None:
    with pytest.raises(MissionProgressError, match="canonical GPS freshness"):
        MissionProgressConfig(gps_max_age_s=1.5001)


def _qualified_feedback(mission: ExecutableMission, *, observed_at_s: float):
    tracker = Px4MissionFeedbackTracker(mission)
    assert tracker.ingest_message(
        {
            "message_type": "MISSION_CURRENT",
            "seq": 0,
            "source_system_id": 1,
            "source_component_id": 1,
        },
        observed_at_s=observed_at_s,
    )
    return tracker


def test_progress_can_require_fresh_hash_bound_px4_feedback() -> None:
    mission = _mission(_item(0, MissionCommand.TAKEOFF, altitude_m=10.0))
    tracker = MissionProgressTracker(
        mission,
        config=MissionProgressConfig(require_px4_feedback=True),
    )
    tracker.start()

    missing = tracker.update(_telemetry(0.0))
    assert missing.phase is MissionProgressPhase.BLOCKED
    assert missing.reason == "px4_mission_feedback_required"
    assert missing.completed_sequences == ()

    feedback_tracker = _qualified_feedback(mission, observed_at_s=0.1)
    feedback = feedback_tracker.snapshot(now_s=0.1)
    complete = tracker.update(_telemetry(0.1), px4_feedback=feedback)
    assert complete.phase is MissionProgressPhase.COMPLETE
    assert complete.px4_feedback == feedback
    assert complete.to_mapping()["px4_feedback"]["qualified"] is True
    assert MissionProgressSnapshot.from_mapping(complete.to_mapping()) == complete


def test_progress_rejects_stale_or_wrong_time_px4_feedback_before_advancing() -> None:
    mission = _mission(_item(0, MissionCommand.TAKEOFF, altitude_m=10.0))
    tracker = MissionProgressTracker(mission)
    tracker.start()
    feedback_tracker = _qualified_feedback(mission, observed_at_s=0.0)
    stale = feedback_tracker.snapshot(now_s=2.0)

    blocked = tracker.update(_telemetry(2.0), px4_feedback=stale)
    assert blocked.phase is MissionProgressPhase.BLOCKED
    assert blocked.reason == "px4_mission_feedback_stale"
    assert blocked.completed_sequences == ()

    fresh = feedback_tracker.snapshot(now_s=2.0)
    with pytest.raises(MissionProgressError, match="current progress time"):
        tracker.update(_telemetry(2.1), px4_feedback=fresh)


def test_progress_rejects_feedback_bound_to_a_different_mission_hash() -> None:
    mission = _mission(_item(0, MissionCommand.TAKEOFF, altitude_m=10.0))
    other = ExecutableMission(
        mission_id="other-mission",
        platform_plan_hash=mission.platform_plan_hash,
        airframe_profile_hash=mission.airframe_profile_hash,
        airframe_adapter_id=mission.airframe_adapter_id,
        airframe_adapter_hash=mission.airframe_adapter_hash,
        items=mission.items,
    )
    feedback = _qualified_feedback(other, observed_at_s=0.0).snapshot(now_s=0.0)
    tracker = MissionProgressTracker(mission)
    tracker.start()

    with pytest.raises(MissionProgressError, match="bound Mission identity"):
        tracker.update(_telemetry(0.0), px4_feedback=feedback)
