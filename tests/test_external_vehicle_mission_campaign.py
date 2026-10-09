from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_external_vehicle_mission_campaign import (
    _inputs,
    _parity_binding,
    run_current_parity_campaign,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("vehicle_class", ["fixed-wing", "multirotor", "vtol"])
def test_current_parity_vehicle_inputs_use_shared_compiler_contract(
    tmp_path: Path, vehicle_class: str
) -> None:
    compiled, context, runtime = _inputs(vehicle_class, tmp_path / vehicle_class)
    compiled_document = json.loads(compiled.read_text(encoding="utf-8"))
    context_document = json.loads(context.read_text(encoding="utf-8"))
    runtime_document = json.loads(runtime.read_text(encoding="utf-8"))
    expected_profile = {
        "fixed-wing": "px4_fixed_wing_gazebo_v1",
        "multirotor": "px4_multirotor_gazebo_v1",
        "vtol": "px4_vtol_gazebo_v1",
    }[vehicle_class]
    assert context_document["airframe_profile_id"] == expected_profile
    assert (
        compiled_document["compiled_mission"]["executable_mission"]["airframe_profile_hash"]
        == "b" * 64
    )
    assert runtime_document["schema_version"] == 1
    assert "safety_policy" in runtime_document
    binding_document = json.loads(
        (tmp_path / vehicle_class / "task-binding.json").read_text(encoding="utf-8")
    )
    assert binding_document["vehicle_class"] == vehicle_class
    assert binding_document["mission_bound"] is True
    assert binding_document["planner_revalidated"] is True
    assert binding_document["policy_applied_to_actuator"] is False
    assert binding_document["truth_free"] is True


def test_current_parity_binding_matches_release_parity() -> None:
    binding = _parity_binding(ROOT)
    assert binding["verified"] is True
    assert binding["file_sha256"] == hashlib.sha256(
        (ROOT / "configs" / "release-parity.json").read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("sensor_frame_count", "sensor_width", "sensor_height"),
    [(-1, 1280, 960), (1, 0, 960), (1, 1280, 0)],
)
def test_current_parity_campaign_rejects_invalid_sensor_capture_options(
    tmp_path: Path,
    sensor_frame_count: int,
    sensor_width: int,
    sensor_height: int,
) -> None:
    with pytest.raises(ValueError, match="sensor"):
        run_current_parity_campaign(
            root=ROOT,
            vehicle_class="multirotor",
            evidence_path=tmp_path / "evidence.json",
            run_directory=tmp_path / "run",
            sensor_frame_count=sensor_frame_count,
            sensor_width=sensor_width,
            sensor_height=sensor_height,
        )


def test_current_parity_vehicle_inputs_can_bind_the_shared_policy_artifact(
    tmp_path: Path,
) -> None:
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    ranker = TaskRanker((0.0, 0.0, 0.0), "external-vehicle-shared-policy-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="external-vehicle-shared-policy-test",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="b" * 64,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    _, _, _ = _inputs(
        "fixed-wing",
        tmp_path / "fixed-wing",
        shared_policy=SharedTaskPolicy(ranker, artifact),
    )
    binding = json.loads(
        (tmp_path / "fixed-wing" / "task-binding.json").read_text(encoding="utf-8")
    )
    assert binding["decision"]["policy_artifact_sha256"] == artifact.canonical_hash
    assert binding["decision"]["planner_revalidated"] is True
    assert binding["policy_applied_to_actuator"] is False
