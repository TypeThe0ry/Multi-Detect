from __future__ import annotations

import json

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    PromptEnvelope,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.serialization import AutonomyContractError, strict_json_loads


def _context() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="context-001",
        captured_at_s=10.0,
        captured_at_utc="2026-08-25T00:00:00Z",
        aircraft_position=GeoPoint(1.3, 103.8),
        home_position=GeoPoint(1.3, 103.8),
        airframe_profile_id="px4_vtol_gazebo_v1",
        battery_remaining_pct=86.0,
        estimated_flight_time_s=2100.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=2.1,
        wind_east_mps=-0.8,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.4,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _task() -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id="river-search-001",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="river-north-001",
            direction="northbound",
            length_m=2000.0,
            buffer_m=35.0,
            centerline=(GeoPoint(1.3, 103.8), GeoPoint(1.3085, 103.8012)),
            resolution_confidence=0.94,
        ),
        target_classes=("person_in_water", "red_life_jacket"),
        on_detection=(
            PlanAction.OBSERVE_TARGET,
            PlanAction.CAPTURE_EVIDENCE,
            PlanAction.REPORT_GEOLOCATION,
        ),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def test_prompt_envelope_round_trip_preserves_original_command() -> None:
    prompt = PromptEnvelope(
        prompt_id="prompt-001",
        text="沿北侧河道搜索红色救生衣",
        language="zh-CN",
        issued_at="2026-08-25T08:00:00+08:00",
        operator_id="TT",
        aircraft_id="uav-01",
    )

    assert PromptEnvelope.from_mapping(prompt.to_mapping()) == prompt
    assert json.loads(prompt.canonical_json)["text"] == prompt.text
    assert len(prompt.canonical_hash) == 64


def test_context_and_task_documents_round_trip() -> None:
    context = _context()
    task = _task()

    assert ContextSnapshot.from_mapping(context.to_mapping()) == context
    assert ResolvedTaskSpec.from_mapping(task.to_mapping()) == task
    assert task.ready_for_planning is True
    assert (
        context.canonical_hash
        == ContextSnapshot.from_mapping(
            dict(reversed(tuple(context.to_mapping().items())))
        ).canonical_hash
    )


def test_context_optional_airframe_profile_hash_is_round_tripped_and_validated() -> None:
    context = _context()
    profile_hash = "a" * 64
    bound = ContextSnapshot.from_mapping(
        {**context.to_mapping(), "airframe_profile_hash": profile_hash}
    )

    assert bound.airframe_profile_hash == profile_hash
    assert ContextSnapshot.from_mapping(bound.to_mapping()) == bound
    assert bound.to_mapping()["airframe_profile_hash"] == profile_hash

    invalid = {**context.to_mapping(), "airframe_profile_hash": "not-a-sha256"}
    with pytest.raises(AutonomyContractError, match="airframe_profile_hash"):
        ContextSnapshot.from_mapping(invalid)


def test_unresolved_assumptions_fail_the_planning_readiness_gate() -> None:
    mapping = _task().to_mapping()
    mapping["unresolved_assumptions"] = ["请选择两条候选河道中的一条"]

    assert ResolvedTaskSpec.from_mapping(mapping).ready_for_planning is False


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ('{"gate_passed":true,"gate_passed":false}', "duplicate JSON key"),
        ('{"gate_passed":NaN}', "non-finite JSON number NaN"),
    ),
)
def test_strict_json_loader_rejects_ambiguous_evidence_values(
    payload: str,
    message: str,
) -> None:
    with pytest.raises(AutonomyContractError, match=message):
        strict_json_loads(("\ufeff" + payload).encode("utf-8"), name="evidence")

    assert strict_json_loads(b'{"gate_passed":true}', name="evidence") == {"gate_passed": True}
