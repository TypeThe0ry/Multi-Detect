from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from multidetect.autonomy import (
    AutonomyContractError,
    EpisodeArtifact,
    EpisodeBundle,
    EpisodeOutcome,
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
    OperatorGrant,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
    ValidationFinding,
    ValidationReport,
    ValidationVerdict,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_plan_mission_validation_grant_and_episode_round_trip() -> None:
    plan = PlatformNeutralPlan(
        plan_id="plan-001",
        task_spec_hash=HASH_A,
        context_snapshot_hash=HASH_B,
        steps=(
            PlanStep(0, PlanAction.TAKEOFF),
            PlanStep(1, PlanAction.SEARCH_CORRIDOR, "river-north", 2000.0),
            PlanStep(2, PlanAction.RETURN),
            PlanStep(3, PlanAction.LAND),
        ),
    )
    mission = ExecutableMission(
        mission_id="mission-001",
        platform_plan_hash=plan.canonical_hash,
        airframe_profile_hash=HASH_C,
        items=(
            MissionItem(
                0,
                MissionCommand.VTOL_TAKEOFF,
                MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                1.3,
                103.8,
                50.0,
            ),
            MissionItem(1, MissionCommand.RETURN_TO_LAUNCH, MissionFrame.MISSION),
        ),
        airframe_adapter_id="px4-vtol-adapter-v1",
        airframe_adapter_hash=HASH_D,
    )
    report = ValidationReport(
        report_id="validation-001",
        executable_mission_hash=mission.canonical_hash,
        verdict=ValidationVerdict.PASS,
        findings=(ValidationFinding("geofence", ValidationVerdict.PASS, "inside"),),
    )
    grant = OperatorGrant(
        grant_id="grant-001",
        transaction_id="transaction-001",
        operator_id="TT",
        prompt_hash=HASH_A,
        task_spec_hash=HASH_B,
        plan_hash=plan.canonical_hash,
        executable_mission_hash=mission.canonical_hash,
        validation_report_hash=report.canonical_hash,
        context_snapshot_hash=HASH_B,
        airframe_profile_hash=HASH_C,
        geofence_hash=HASH_D,
        vehicle_identity_hash=HASH_D,
        runtime_descriptor_hash=HASH_A,
        runtime_provenance_hash=HASH_C,
        expires_at="2026-08-25T01:00:00Z",
    )
    episode = EpisodeBundle(
        episode_id="episode-001",
        runtime_descriptor_hash=HASH_A,
        runtime_provenance_hash=HASH_C,
        task_spec_hash=HASH_B,
        plan_hash=plan.canonical_hash,
        executable_mission_hash=mission.canonical_hash,
        validation_report_hash=report.canonical_hash,
        operator_grant_hash=grant.canonical_hash,
        outcome=EpisodeOutcome.COMPLETED,
        artifacts=(EpisodeArtifact("telemetry", "telemetry.jsonl", HASH_C),),
    )

    assert PlatformNeutralPlan.from_mapping(plan.to_mapping()) == plan
    assert ExecutableMission.from_mapping(mission.to_mapping()) == mission
    assert ValidationReport.from_mapping(report.to_mapping()) == report
    assert report.accepted is True
    assert OperatorGrant.from_mapping(grant.to_mapping()) == grant
    assert {
        "prompt_hash",
        "task_spec_hash",
        "context_snapshot_hash",
        "plan_hash",
        "executable_mission_hash",
        "validation_report_hash",
        "airframe_profile_hash",
        "geofence_hash",
        "vehicle_identity_hash",
        "runtime_descriptor_hash",
        "runtime_provenance_hash",
    }.issubset(grant.to_mapping())
    grant_schema = json.loads(
        (REPO_ROOT / "configs" / "schemas" / "autonomy-operator-grant.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(grant_schema["required"]) == set(grant.to_mapping())
    assert EpisodeBundle.from_mapping(episode.to_mapping()) == episode


def test_contracts_are_frozen_and_reject_unknown_fields() -> None:
    step = PlanStep(0, PlanAction.RETURN)
    with pytest.raises(FrozenInstanceError):
        step.sequence = 2  # type: ignore[misc]

    mapping = step.to_mapping()
    mapping["pwm"] = 2000
    with pytest.raises(AutonomyContractError, match="unknown field"):
        PlanStep.from_mapping(mapping)


def test_plan_rejects_noncontiguous_steps() -> None:
    with pytest.raises(AutonomyContractError, match="contiguous"):
        PlatformNeutralPlan(
            plan_id="invalid",
            task_spec_hash=HASH_A,
            context_snapshot_hash=HASH_B,
            steps=(PlanStep(1, PlanAction.RETURN),),
        )


def test_validation_report_cannot_hide_a_denied_finding() -> None:
    with pytest.raises(AutonomyContractError, match="does not match"):
        ValidationReport(
            report_id="invalid",
            executable_mission_hash=HASH_A,
            verdict=ValidationVerdict.PASS,
            findings=(ValidationFinding("energy", ValidationVerdict.DENY, "reserve violated"),),
        )


def test_direct_construction_normalizes_sequences_text_numbers_and_hashes() -> None:
    source_steps = [PlanStep(0, PlanAction.RETURN)]
    plan = PlatformNeutralPlan(
        plan_id=" plan ",
        task_spec_hash=HASH_A.upper(),
        context_snapshot_hash=HASH_B.upper(),
        steps=source_steps,  # type: ignore[arg-type]
    )
    source_steps.append(PlanStep(1, PlanAction.LAND))

    assert plan.plan_id == "plan"
    assert plan.task_spec_hash == HASH_A
    assert plan.steps == (PlanStep(0, PlanAction.RETURN),)
    assert plan.canonical_hash == PlatformNeutralPlan.from_mapping(plan.to_mapping()).canonical_hash


def test_mission_item_allowlist_and_coordinate_rules_fail_closed() -> None:
    with pytest.raises(AutonomyContractError, match="allowed MissionCommand"):
        MissionItem(0, "MAV_CMD_COMPONENT_ARM_DISARM", MissionFrame.MISSION)  # type: ignore[arg-type]
    with pytest.raises(AutonomyContractError, match="global coordinate frame"):
        MissionItem(
            0,
            MissionCommand.WAYPOINT,
            MissionFrame.MISSION,
            1.0,
            2.0,
            30.0,
        )
    with pytest.raises(AutonomyContractError, match="requires latitude"):
        MissionItem(0, MissionCommand.WAYPOINT, MissionFrame.GLOBAL_INT)
    with pytest.raises(AutonomyContractError, match="cannot carry coordinates"):
        MissionItem(
            0,
            MissionCommand.RETURN_TO_LAUNCH,
            MissionFrame.MISSION,
            1.0,
            2.0,
            30.0,
        )
