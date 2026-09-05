from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.mission_progress import (
    MissionProgressError,
    MissionProgressPhase,
    MissionProgressSnapshot,
)
from multidetect.pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from multidetect.px4_mission_feedback import (
    Px4MissionFeedbackConfig,
    Px4MissionFeedbackError,
    Px4MissionFeedbackPhase,
    Px4MissionFeedbackSnapshot,
    Px4MissionFeedbackTracker,
)


def _mission(item_count: int = 3) -> ExecutableMission:
    items = []
    for sequence in range(item_count):
        command = (
            MissionCommand.TAKEOFF
            if sequence == 0
            else MissionCommand.LAND
            if sequence == item_count - 1
            else MissionCommand.WAYPOINT
        )
        items.append(
            MissionItem(
                sequence=sequence,
                command=command,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=1.0 + sequence * 0.001,
                longitude_deg=2.0,
                altitude_m=0.0 if command is MissionCommand.LAND else 10.0,
            )
        )
    return ExecutableMission(
        mission_id="px4-feedback-fixture",
        platform_plan_hash="a" * 64,
        airframe_profile_hash="b" * 64,
        airframe_adapter_id="px4-feedback-test-v1",
        airframe_adapter_hash="c" * 64,
        items=tuple(items),
    )


def _message(message_type: str, sequence: int, *, system: int = 1, component: int = 1):
    return {
        "message_type": message_type,
        "seq": sequence,
        "source_system_id": system,
        "source_component_id": component,
    }


class _WireMessage:
    def __init__(
        self,
        message_type: str,
        sequence: object,
        *,
        system: int = 1,
        component: int = 1,
    ) -> None:
        self._message_type = message_type
        self._system = system
        self._component = component
        self.seq = sequence

    def get_type(self) -> str:
        return self._message_type

    def get_srcSystem(self) -> int:
        return self._system

    def get_srcComponent(self) -> int:
        return self._component


def test_feedback_tracks_current_reached_duplicates_and_terminal_state() -> None:
    mission = _mission()
    tracker = Px4MissionFeedbackTracker(
        mission,
        config=Px4MissionFeedbackConfig(expected_system_id=1, expected_component_id=1),
    )

    assert tracker.ingest_message(_message("MISSION_CURRENT", 0), observed_at_s=1.0)
    assert tracker.ingest_message(_message("MISSION_ITEM_REACHED", 0), observed_at_s=1.1)
    assert tracker.ingest_message(_message("MISSION_CURRENT", 1), observed_at_s=1.2)
    assert tracker.ingest_message(_message("MISSION_CURRENT", 1), observed_at_s=1.3)
    assert tracker.ingest_message(_message("MISSION_ITEM_REACHED", 1), observed_at_s=1.4)
    assert tracker.ingest_message(_message("MISSION_CURRENT", 2), observed_at_s=1.5)
    assert tracker.ingest_message(_message("MISSION_ITEM_REACHED", 2), observed_at_s=1.6)

    snapshot = tracker.snapshot(now_s=1.7)
    assert snapshot.phase is Px4MissionFeedbackPhase.TERMINAL_REACHED
    assert snapshot.qualified is True
    assert snapshot.reason == "terminal_mission_item_reached"
    assert snapshot.observed_current_sequences == (0, 1, 2)
    assert snapshot.current_sequence == 2
    assert snapshot.reached_sequences == (0, 1, 2)
    assert snapshot.feedback_age_s == pytest.approx(0.1)
    assert snapshot.source_identity_bound is True
    assert snapshot.accepted_event_count == 7
    assert snapshot.duplicate_event_count == 1
    assert snapshot.rejected_event_count == 0

    reopened = Px4MissionFeedbackSnapshot.from_mapping(snapshot.to_mapping())
    assert reopened == snapshot
    assert reopened.canonical_hash == snapshot.canonical_hash


def test_feedback_staleness_is_derived_from_last_wire_event() -> None:
    tracker = Px4MissionFeedbackTracker(_mission())
    assert tracker.ingest_message(_message("MISSION_CURRENT", 0), observed_at_s=2.0)

    fresh = tracker.snapshot(now_s=3.5)
    assert fresh.phase is Px4MissionFeedbackPhase.ACTIVE
    assert fresh.qualified is True
    assert fresh.feedback_age_s == pytest.approx(1.5)

    stale = tracker.snapshot(now_s=3.500001)
    assert stale.phase is Px4MissionFeedbackPhase.STALE
    assert stale.qualified is False
    assert stale.reason == "mission_feedback_stale"


def test_feedback_without_complete_source_identity_never_qualifies() -> None:
    tracker = Px4MissionFeedbackTracker(_mission())
    assert tracker.ingest_message({"message_type": "MISSION_CURRENT", "seq": 0}, observed_at_s=0.0)

    snapshot = tracker.snapshot(now_s=0.1)
    assert snapshot.phase is Px4MissionFeedbackPhase.ACTIVE
    assert snapshot.source_identity_bound is False
    assert snapshot.qualified is False
    assert snapshot.reason == "mission_feedback_source_unbound"


@pytest.mark.parametrize(
    ("events", "reason"),
    (
        (
            (
                (_message("MISSION_CURRENT", 0), 1.0),
                (_message("MISSION_CURRENT", 1, system=2), 1.1),
            ),
            "feedback_source_identity_changed",
        ),
        (
            (
                (_message("MISSION_CURRENT", 1), 1.0),
                (_message("MISSION_CURRENT", 0), 1.1),
            ),
            "mission_current_sequence_regressed",
        ),
        (
            (
                (_message("MISSION_CURRENT", 0), 1.0),
                (_message("MISSION_ITEM_REACHED", 1), 1.1),
            ),
            "mission_item_reached_ahead_of_current_sequence",
        ),
        (
            (
                (_message("MISSION_CURRENT", 0), 2.0),
                (_message("MISSION_CURRENT", 1), 1.0),
            ),
            "feedback_receive_timestamp_regressed",
        ),
    ),
)
def test_feedback_latches_ambiguous_wire_sequences_invalid(events, reason: str) -> None:
    tracker = Px4MissionFeedbackTracker(_mission())
    assert tracker.ingest_message(events[0][0], observed_at_s=events[0][1])
    assert tracker.ingest_message(events[1][0], observed_at_s=events[1][1]) is False

    snapshot = tracker.snapshot(now_s=2.1)
    assert snapshot.phase is Px4MissionFeedbackPhase.INVALID
    assert snapshot.qualified is False
    assert snapshot.rejected_event_count == 1
    assert snapshot.last_rejection_reason == reason
    assert snapshot.reason == reason


def test_feedback_rejects_out_of_bound_sequence_and_can_reset_session() -> None:
    tracker = Px4MissionFeedbackTracker(_mission(2))
    assert tracker.ingest_message(_message("MISSION_CURRENT", 2), observed_at_s=1.0) is False
    assert tracker.snapshot(now_s=1.0).phase is Px4MissionFeedbackPhase.INVALID

    tracker.reset()
    awaiting = tracker.snapshot(now_s=1.0)
    assert awaiting.phase is Px4MissionFeedbackPhase.AWAITING
    assert awaiting.rejected_event_count == 0
    assert tracker.ingest_message(_message("MISSION_CURRENT", 0), observed_at_s=1.1)
    assert tracker.snapshot(now_s=1.2).qualified is True


def test_unrelated_message_does_not_poison_feedback_session() -> None:
    tracker = Px4MissionFeedbackTracker(_mission())
    assert (
        tracker.ingest_message(
            {"message_type": "GLOBAL_POSITION_INT", "seq": 999},
            observed_at_s=1.0,
        )
        is False
    )
    snapshot = tracker.snapshot(now_s=1.0)
    assert snapshot.phase is Px4MissionFeedbackPhase.AWAITING
    assert snapshot.rejected_event_count == 0


def test_feedback_snapshot_rejects_tampered_qualification() -> None:
    tracker = Px4MissionFeedbackTracker(_mission())
    tracker.ingest_message(_message("MISSION_CURRENT", 0), observed_at_s=1.0)
    document = tracker.snapshot(now_s=1.1).to_mapping()
    document["qualified"] = False
    with pytest.raises(Px4MissionFeedbackError, match="qualified"):
        Px4MissionFeedbackSnapshot.from_mapping(document)


def test_feedback_freshness_budget_cannot_exceed_canonical_limit() -> None:
    with pytest.raises(Px4MissionFeedbackError, match="canonical Mission feedback"):
        Px4MissionFeedbackConfig(maximum_age_s=1.500001)

    with pytest.raises(Px4MissionFeedbackError, match="requires expected_system_id"):
        Px4MissionFeedbackConfig(expected_component_id=1)


def test_pixhawk_provider_exposes_fresh_current_reached_and_bound_feedback() -> None:
    mission = _mission()
    tracker = Px4MissionFeedbackTracker(
        mission,
        config=Px4MissionFeedbackConfig(expected_system_id=1, expected_component_id=1),
    )
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            "udp:127.0.0.1:14550",
            stale_after_seconds=1.0,
            expected_system_id=1,
        ),
        mission_feedback_tracker=tracker,
    )

    provider.ingest_message(_WireMessage("MISSION_CURRENT", 0), received_at_s=10.0)
    provider.ingest_message(_WireMessage("MISSION_ITEM_REACHED", 0), received_at_s=10.1)
    provider.ingest_message(_WireMessage("MISSION_CURRENT", 1), received_at_s=10.2)

    telemetry = provider.cached_snapshot(now_s=10.5)
    assert telemetry.mission_sequence == 1
    assert telemetry.mission_sequence_observed_at_s == pytest.approx(10.2)
    assert telemetry.mission_item_reached_sequence == 0
    assert telemetry.mission_item_reached_observed_at_s == pytest.approx(10.1)

    diagnostics = provider.diagnostics(now_s=10.5)
    assert diagnostics["mission"]["current_age_s"] == pytest.approx(0.3)
    feedback = diagnostics["mission"]["feedback"]
    assert feedback["document"]["mission_hash"] == mission.canonical_hash
    assert feedback["document"]["qualified"] is True
    assert feedback["sha256"] == tracker.snapshot(now_s=10.5).canonical_hash

    stale = provider.cached_snapshot(now_s=11.3)
    assert stale.mission_sequence is None
    assert stale.mission_item_reached_sequence is None
    assert stale.mission_sequence_observed_at_s == pytest.approx(10.2)
    assert stale.mission_item_reached_observed_at_s == pytest.approx(10.1)
    assert provider.mission_feedback_snapshot(now_s=11.8).phase is Px4MissionFeedbackPhase.STALE
    assert provider.messages_transmitted == 0


def test_pixhawk_provider_clears_ambiguous_bound_feedback_from_domain_snapshot() -> None:
    tracker = Px4MissionFeedbackTracker(_mission(2))
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig("udp:127.0.0.1:14550"),
        mission_feedback_tracker=tracker,
    )

    provider.ingest_message(_WireMessage("MISSION_CURRENT", 0), received_at_s=1.0)
    assert provider.cached_snapshot(now_s=1.0).mission_sequence == 0

    provider.ingest_message(_WireMessage("MISSION_CURRENT", 2), received_at_s=1.1)
    telemetry = provider.cached_snapshot(now_s=1.1)
    assert telemetry.mission_sequence is None
    assert telemetry.mission_sequence_observed_at_s == pytest.approx(1.1)
    feedback = provider.mission_feedback_snapshot(now_s=1.1)
    assert feedback is not None
    assert feedback.phase is Px4MissionFeedbackPhase.INVALID
    assert feedback.qualified is False


def test_unbound_pixhawk_mission_cursor_is_timestamped_and_expires() -> None:
    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig("udp:127.0.0.1:14550", stale_after_seconds=0.5)
    )
    provider.ingest_message(_WireMessage("MISSION_CURRENT", 4), received_at_s=3.0)
    assert provider.cached_snapshot(now_s=3.5).mission_sequence == 4
    assert provider.cached_snapshot(now_s=3.500001).mission_sequence is None

    provider.ingest_message(_WireMessage("MISSION_CURRENT", True), received_at_s=4.0)
    invalid = provider.cached_snapshot(now_s=4.0)
    assert invalid.mission_sequence is None
    assert invalid.mission_sequence_observed_at_s == pytest.approx(4.0)


def test_feedback_and_progress_schemas_validate_nested_hash_bound_evidence() -> None:
    root = Path(__file__).resolve().parents[1]
    feedback_schema = json.loads(
        (root / "configs/schemas/px4-mission-feedback.schema.json").read_text(encoding="utf-8")
    )
    progress_schema = json.loads(
        (root / "configs/schemas/mission-progress.schema.json").read_text(encoding="utf-8")
    )
    scene_schema = json.loads(
        (root / "configs/schemas/simulator-scene-stream.schema.json").read_text(encoding="utf-8")
    )
    for schema in (feedback_schema, progress_schema, scene_schema):
        Draft202012Validator.check_schema(schema)

    mission = _mission()
    tracker = Px4MissionFeedbackTracker(mission)
    tracker.ingest_message(_message("MISSION_CURRENT", 0), observed_at_s=1.0)
    feedback = tracker.snapshot(now_s=1.1)
    feedback_document = feedback.to_mapping()
    assert not list(Draft202012Validator(feedback_schema).iter_errors(feedback_document))

    progress = MissionProgressSnapshot(
        mission_id=mission.mission_id,
        mission_hash=mission.canonical_hash,
        phase=MissionProgressPhase.ACTIVE,
        observed_at_s=1.1,
        mission_item_count=len(mission.items),
        current_sequence=0,
        completed_sequences=(),
        active_command=mission.items[0].command.value,
        distance_to_active_m=1.0,
        altitude_error_m=1.0,
        dwell_elapsed_s=0.0,
        progress_fraction=0.0,
        mission_started=True,
        armed=True,
        link_healthy=True,
        gps_quality_valid=True,
        gps_age_s=0.0,
        reason="mission_active",
        px4_feedback=feedback,
    )
    progress_document = progress.to_mapping()
    assert not list(Draft202012Validator(progress_schema).iter_errors(progress_document))

    tampered = dict(feedback_document)
    tampered["current_sequence"] = 63
    assert list(Draft202012Validator(feedback_schema).iter_errors(tampered)) == []
    with pytest.raises(AutonomyContractError, match="current_sequence"):
        Px4MissionFeedbackSnapshot.from_mapping(tampered)

    explicit_null = progress.to_mapping()
    explicit_null["px4_feedback"] = None
    assert list(Draft202012Validator(progress_schema).iter_errors(explicit_null))
    with pytest.raises(MissionProgressError, match="must be an object"):
        MissionProgressSnapshot.from_mapping(explicit_null)
