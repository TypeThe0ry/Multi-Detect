"""Small CPU contract fixtures, not observed flight or training evidence."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from math import cos, pi, sin, sqrt
from pathlib import Path

import pytest

from multidetect.autonomy import onboard_executed_trajectory as trajectory
from multidetect.autonomy.onboard_navigation_inference import PlannedNavigationTarget
from multidetect.autonomy.serialization import AutonomyContractError, canonical_sha256

SHA = "a" * 64
CMD = "12345678-1234-4234-8234-123456789abc"


def binding(name="records.json", pointer="/sample", source_id="fixture-observer"):
    return trajectory.EvidenceBinding(name, 30, SHA, source_id, pointer)


def observation(sequence, at_s, position=(0, 0, 0), quaternion=(1, 0, 0, 0), frame="ned"):
    pose = trajectory.ObservedPose(
        position,
        quaternion,
        frame,
        "fixture-local-origin",
        trajectory.QUATERNION_CONVENTION,
        "simulated_observation",
        at_s,
        "fixture-clock",
        binding(pointer=f"/pose/{sequence}"),
    )
    return trajectory.ActualObservation(
        f"frame-{sequence}",
        sequence,
        at_s,
        "fixture-clock",
        binding(f"images/{sequence}.png", None, "fixture-camera"),
        pose,
        binding("frames.json", f"/frames/{sequence}", "fixture-camera-record"),
    )


def stage(phase, at_s, target):
    return trajectory.ActionStage(
        phase,
        at_s,
        "fixture-clock",
        canonical_sha256(target.to_mapping()),
        binding(pointer=f"/events/{phase}"),
        CMD if phase == "submitted" else None,
    )


def make_step(*, target=None, before=None, after=None, status="observed_success"):
    target = target or PlannedNavigationTarget("translate", (3, 0, 0))
    before = before or observation(0, 0.5)
    after = after or observation(1, 4.0, (2, 1, -1))
    return trajectory.ExecutedTrajectoryStep(
        "fixture-step",
        "fixture-episode",
        "fixture-vehicle",
        "fixture-clock",
        SHA,
        SHA,
        target,
        stage("proposed", 1.0, target),
        stage("approved", 2.0, target),
        stage("submitted", 3.0, target),
        trajectory.ExecutionOutcome(
            status,
            5.0,
            "fixture-clock",
            canonical_sha256(target.to_mapping()),
            CMD,
            binding(pointer="/outcomes/0"),
            before.canonical_hash,
            after.canonical_hash,
        ),
        before,
        after,
        0.1,
    )


def remap_endpoints(step, *, before=None, after=None):
    before = before or step.before
    after = after or step.after
    outcome = replace(
        step.outcome,
        before_observation_sha256=before.canonical_hash,
        after_observation_sha256=after.canonical_hash,
    )
    return replace(step, before=before, after=after, outcome=outcome)


def test_strict_roundtrip_and_immutable_records():
    step = make_step()
    assert trajectory.ExecutedTrajectoryStep.from_json(step.to_json()) == step
    assert trajectory.ExecutedTrajectoryStep.from_mapping(step.to_mapping()) == step
    assert step.canonical_hash == canonical_sha256(step.to_mapping())
    with pytest.raises(FrozenInstanceError):
        step.before.pose.origin = "planned_pose"
    mapping = step.to_mapping()
    mapping["teacher_target"]["translation_m"][0] = 99
    assert step.teacher_target.translation_m == (3, 0, 0)


def test_observed_command_is_not_actual_displacement_or_training_admission():
    step = make_step()
    motion = step.observed_motion()
    assert step.teacher_target.translation_m == (3, 0, 0)
    assert motion["translation_m"] == [2, 1, -1]
    assert motion["dt_s"] == 3.5
    assert motion["origin"] == "actual_endpoint_difference_not_teacher_command"
    assert motion["causal_execution_verified"] is False
    status = step.audit_status()
    assert status["training_evidence_structurally_eligible"] is True
    assert status["provenance_verified"] is False
    assert status["train_admitted"] is False
    assert status["execution_authority"] is False
    assert status["mission_complete_verified"] is False


@pytest.mark.parametrize("phase", ["proposed", "approved", "submitted"])
def test_plan_only_stages_are_recordable_but_never_admitted(phase):
    step = make_step()
    step = replace(
        step,
        approved=step.approved if phase != "proposed" else None,
        submitted=step.submitted if phase == "submitted" else None,
        outcome=None,
        after=None,
    )
    assert step.audit_status()["latest_stage"] == phase
    assert step.audit_status()["training_evidence_structurally_eligible"] is False
    assert step.audit_status()["train_admitted"] is False
    with pytest.raises(AutonomyContractError, match="two actual endpoints"):
        step.observed_motion()


@pytest.mark.parametrize("status", ["failed", "timed_out", "unknown"])
def test_failure_and_unknown_feedback_remain_recordable_not_success(status):
    step = make_step(status=status)
    assert step.audit_status()["latest_stage"] == status
    assert step.audit_status()["training_evidence_structurally_eligible"] is False
    partial = replace(
        step, after=None, outcome=replace(step.outcome, after_observation_sha256=None)
    )
    assert partial.audit_status()["observed_success_recorded"] is False


def test_unknown_metric_does_not_get_invented_from_actual_motion():
    step = make_step(target=PlannedNavigationTarget("translate"))
    assert step.teacher_target.translation_m is None
    assert step.observed_motion()["translation_m"] == [2, 1, -1]
    assert step.audit_status()["training_evidence_structurally_eligible"] is False


def test_stop_keeps_endpoints_but_is_not_wait_complete_or_rtl():
    step = make_step(target=PlannedNavigationTarget("stop"))
    assert step.teacher_target.kind == "stop"
    assert step.teacher_target.translation_m is None
    assert step.audit_status()["mission_complete_verified"] is False
    # Even a caller-declared successful STOP must not erase measured motion.
    assert step.observed_motion()["translation_m"] == [2, 1, -1]


@pytest.mark.parametrize("kind", ["wait", "hover", "complete", "rtl", "land"])
def test_other_control_semantics_cannot_be_navigation_stop(kind):
    raw = make_step().to_mapping()
    raw["teacher_target"]["kind"] = kind
    with pytest.raises(AutonomyContractError):
        trajectory.ExecutedTrajectoryStep.from_mapping(raw)


@pytest.mark.parametrize("frame", ["world", "body_flu", "ned_cm", "NED", "unknown"])
def test_unknown_axes_are_rejected(frame):
    with pytest.raises(AutonomyContractError):
        replace(make_step().before.pose, world_frame=frame)


@pytest.mark.parametrize("convention", ["xyzw", "passive_world_to_body_wxyz", "body_flu_to_enu"])
def test_quaternion_convention_cannot_be_relabelled(convention):
    with pytest.raises(AutonomyContractError):
        replace(make_step().before.pose, quaternion_convention=convention)


@pytest.mark.parametrize("origin", ["planned_pose", "gold", "executed", "unknown"])
def test_planned_or_unknown_pose_is_not_an_actual_endpoint(origin):
    with pytest.raises(AutonomyContractError, match="planned pose"):
        replace(make_step().before.pose, origin=origin)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_finite_strict_metric_and_time_checks(value):
    with pytest.raises(AutonomyContractError):
        replace(make_step().before.pose, position_m=(value, 0, 0))
    with pytest.raises(AutonomyContractError):
        replace(make_step().proposed, at_s=value)
    with pytest.raises(AutonomyContractError):
        replace(make_step().before.pose, quaternion_wxyz=(value, 0, 0, 0))


@pytest.mark.parametrize("q", [(0, 0, 0, 0), (2, 0, 0, 0), (1, 1, 0, 0)])
def test_nonunit_quaternion_not_silently_normalized(q):
    with pytest.raises(AutonomyContractError, match="unit length"):
        replace(make_step().before.pose, quaternion_wxyz=q)


@pytest.mark.parametrize(
    "changes",
    [
        {"approved": None},
        {"submitted": None},
        {"before": None},
        {"after": None},
    ],
)
def test_observed_success_requires_full_stage_chain_and_endpoints(changes):
    with pytest.raises(AutonomyContractError):
        replace(make_step(), **changes)


def test_changed_approval_is_explicitly_not_supported_as_original_proposal():
    step = make_step()
    changed = canonical_sha256(PlannedNavigationTarget("translate", (1, 0, 0)).to_mapping())
    with pytest.raises(AutonomyContractError, match="teacher action identity"):
        replace(step, approved=replace(step.approved, action_sha256=changed))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: replace(s.approved, phase="proposed"),
        lambda s: replace(s.approved, at_s=0.9),
        lambda s: replace(s.approved, clock_id="wall-clock"),
        lambda s: replace(s.approved, action_sha256="b" * 64),
    ],
)
def test_stage_identity_chronology_and_kind_gates(mutation):
    step = make_step()
    with pytest.raises(AutonomyContractError):
        replace(step, approved=mutation(step))


@pytest.mark.parametrize(
    "field,value",
    [
        ("command_id", "22345678-1234-4234-8234-123456789abc"),
        ("action_sha256", "b" * 64),
        ("clock_id", "source-boot-ms"),
        ("at_s", 2.9),
        ("before_observation_sha256", "b" * 64),
        ("after_observation_sha256", "b" * 64),
    ],
)
def test_outcome_exactly_binds_command_target_clock_and_endpoints(field, value):
    step = make_step()
    with pytest.raises(AutonomyContractError):
        replace(step, outcome=replace(step.outcome, **{field: value}))


@pytest.mark.parametrize("command", [400, "400", "cmd-1", "00000000-0000-0000-0000-000000000000"])
def test_mavlink_enum_or_missing_transaction_id_is_not_unique_command(command):
    with pytest.raises(AutonomyContractError):
        replace(make_step().submitted, command_id=command)


@pytest.mark.parametrize(
    "which,changes",
    [
        ("before", {"at_s": 1.1}),
        ("after", {"at_s": 2.9}),
        ("after", {"at_s": 6.0}),
        ("after", {"sequence": 0}),
        ("after", {"frame_id": "frame-0"}),
        ("after", {"clock_id": "other-clock"}),
    ],
)
def test_endpoint_temporal_or_identity_mismatch_rejected(which, changes):
    step = make_step()
    with pytest.raises(AutonomyContractError):
        changed = replace(getattr(step, which), **changes)
        remap_endpoints(step, **{which: changed})


@pytest.mark.parametrize(
    "changes",
    [
        {"world_origin_id": "different-local-origin"},
        {"origin": "measured_observation"},
        {"at_s": 4.2},
        {"source": binding(source_id="other-vehicle-pose")},
    ],
)
def test_actual_pose_origin_and_rgb_alignment_are_not_assumed(changes):
    step = make_step()
    with pytest.raises(AutonomyContractError):
        after = replace(step.after, pose=replace(step.after.pose, **changes))
        remap_endpoints(step, after=after)


def test_pose_future_even_inside_skew_is_not_input_and_pose_before_submission_is_not_after():
    step = make_step()
    before = replace(step.before, at_s=1.0, pose=replace(step.before.pose, at_s=1.05))
    with pytest.raises(AutonomyContractError, match="future"):
        remap_endpoints(step, before=before)
    after = replace(step.after, at_s=3.01, pose=replace(step.after.pose, at_s=2.99))
    with pytest.raises(AutonomyContractError, match="ordered"):
        remap_endpoints(step, after=after)


@pytest.mark.parametrize(
    "path", ["../pose.json", "/pose.json", "C:/pose.json", "a\\b", "a//b", "./a"]
)
def test_source_path_scope_is_canonical_and_relative(path):
    with pytest.raises(AutonomyContractError):
        replace(binding(), path=path)


@pytest.mark.parametrize("pointer", ["not/slashed", "/bad~", "/bad~2"])
def test_invalid_json_pointers_rejected(pointer):
    with pytest.raises(AutonomyContractError):
        replace(binding(), json_pointer=pointer)


def test_json_and_raw_binary_binding_scopes_are_distinct():
    assert binding(pointer="/a~1b/c~0d").json_pointer == "/a~1b/c~0d"
    with pytest.raises(AutonomyContractError):
        replace(make_step().proposed, source=replace(binding(), json_pointer=None))
    with pytest.raises(AutonomyContractError):
        replace(make_step().before, image_source=binding("image.png", "/pixel"))


@pytest.mark.parametrize("source", [None, binding("frames.json", None)])
def test_frame_metadata_requires_its_own_json_source_binding(source):
    with pytest.raises((AutonomyContractError, TypeError)):
        replace(make_step().before, source=source)


def test_frame_metadata_source_is_required_in_strict_json_schema():
    raw = make_step().to_mapping()
    del raw["before"]["source"]
    with pytest.raises(AutonomyContractError, match="missing"):
        trajectory.ExecutedTrajectoryStep.from_mapping(raw)


def test_frame_receipt_source_identity_is_in_execution_endpoint_hash():
    step = make_step()
    before = replace(step.before, source=replace(step.before.source, sha256="b" * 64))
    assert before.canonical_hash != step.before.canonical_hash
    with pytest.raises(AutonomyContractError, match="endpoint identity"):
        replace(step, before=before)


def test_frame_projection_excludes_self_source_and_keeps_all_camera_metadata():
    before = make_step().before
    projection = before.to_mapping()
    source = projection.pop("source")
    assert source["path"] == "frames.json"
    assert source["json_pointer"] == "/frames/0"
    assert set(projection) == {"frame_id", "sequence", "at_s", "clock_id", "image_source", "pose"}
    assert projection["image_source"]["path"] == "images/0.png"
    assert projection["pose"]["source"]["path"] == "records.json"
    # A frame record embeds upstream identities, never the hash of frames.json itself.
    assert "frames.json" not in json.dumps(projection)


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "z" * 64, True])
def test_source_hash_is_strict_but_not_authority(bad):
    with pytest.raises(AutonomyContractError):
        replace(binding(), sha256=bad)
    assert make_step().audit_status()["provenance_verified"] is False


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"n":NaN}',
        '{"n":Infinity}',
        "{",
        b"\xff",
    ],
)
def test_strict_json_rejects_duplicate_nonfinite_invalid_and_truncated(payload):
    with pytest.raises(AutonomyContractError):
        trajectory.ExecutedTrajectoryStep.from_json(payload)


@pytest.mark.parametrize("where", [None, "teacher_target", "proposed", "before", "pose", "source"])
def test_extra_fields_and_gold_injection_rejected_at_each_layer(where):
    raw = make_step().to_mapping()
    target = (
        raw
        if where is None
        else (
            raw["before"]["pose"]
            if where == "pose"
            else raw["proposed"]["source"]
            if where == "source"
            else raw[where]
        )
    )
    target["gold_future_pose"] = [9, 9, 9]
    with pytest.raises(AutonomyContractError):
        trajectory.ExecutedTrajectoryStep.from_mapping(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("time_basis", "wall_clock"),
        ("contract", "planned-supervision"),
    ],
)
def test_version_and_time_basis_are_explicit(field, value):
    raw = make_step().to_mapping()
    raw[field] = value
    with pytest.raises(AutonomyContractError):
        trajectory.ExecutedTrajectoryStep.from_mapping(raw)


def test_bounded_json_and_missing_fields():
    with pytest.raises(AutonomyContractError):
        trajectory.ExecutedTrajectoryStep.from_json(b" " * (trajectory.MAX_JSON_BYTES + 1))
    raw = make_step().to_mapping()
    del raw["before"]["pose"]["quaternion_wxyz"]
    with pytest.raises(AutonomyContractError):
        trajectory.ExecutedTrajectoryStep.from_mapping(raw)


def test_ned_enu_vectors_and_quaternions_change_world_axes_only():
    assert trajectory.convert_world_vector((1, 2, 3), "ned", "enu") == (2, 1, -3)
    q = trajectory.convert_world_quaternion((1, 0, 0, 0), "ned", "enu")
    assert q == pytest.approx((0, sqrt(0.5), sqrt(0.5), 0))
    assert trajectory.convert_world_quaternion(q, "enu", "ned") == pytest.approx((-1, 0, 0, 0))
    # Quaternion sign equivalence must not be confused with source-byte equality.
    step = make_step()
    after = observation(1, 4.0, (1, 2, 1), q, "enu")
    motion = remap_endpoints(step, after=after).observed_motion()
    assert motion["translation_m"] == pytest.approx([2, 1, -1])
    assert motion["yaw_delta_rad"] == pytest.approx(0)


@pytest.mark.parametrize(
    "q,world_delta",
    [
        ((cos(pi / 4), 0, 0, sin(pi / 4)), (-2, 3, 1)),
        ((cos(pi / 4), sin(pi / 4), 0, 0), (3, -1, 2)),
        ((cos(pi / 4), 0, sin(pi / 4), 0), (1, 2, -3)),
    ],
)
def test_full_attitude_not_yaw_only_maps_actual_motion_to_before_frd(q, world_delta):
    step = make_step(
        before=observation(0, 0.5, quaternion=q), after=observation(1, 4, world_delta, q)
    )
    assert step.observed_motion()["translation_m"] == pytest.approx([3, 2, 1])
    if abs(q[2]) > 0:
        assert step.observed_motion()["yaw_delta_rad"] is None


def test_wrapped_yaw_heading_feedback_and_sign_equivalence():
    def q(yaw):
        return (cos(yaw / 2), 0, 0, sin(yaw / 2))

    step = make_step(
        before=observation(0, 0.5, quaternion=q(pi - 0.1)),
        after=observation(1, 4, quaternion=q(-pi + 0.1)),
    )
    assert step.observed_motion()["yaw_delta_rad"] == pytest.approx(0.2)
    unchanged = make_step(after=observation(1, 4, quaternion=(-1, 0, 0, 0)))
    assert unchanged.observed_motion()["yaw_delta_rad"] == pytest.approx(0)


def test_gazebo_conversion_composes_world_left_and_body_right_rotation():
    position, q = trajectory.gazebo_flu_enu_to_frd_ned((1, 2, 3), (0, 0, 0, 1))
    assert position == (2, 1, -3)
    # Raw FLU/ENU identity points forward East, right South, down down.
    assert q == pytest.approx((-sqrt(0.5), 0, 0, -sqrt(0.5)))
    step = make_step(
        before=observation(0, 0.5, quaternion=q), after=observation(1, 4, (-2, 3, 1), q)
    )
    assert step.observed_motion()["translation_m"] == pytest.approx([3, 2, 1])


def test_import_is_quiet_stdlib_and_does_not_create_actor_or_io(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path.insert(0, {str(root / 'src')!r}); "
        "from multidetect.autonomy import onboard_executed_trajectory as m; "
        "assert not {'torch','transformers','peft','PIL','numpy','pymavlink'} & set(sys.modules); "
        "assert not hasattr(m.ExecutedTrajectoryStep, 'to_actor'); "
        "print(m.CONTRACT)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == trajectory.CONTRACT
    assert list(tmp_path.iterdir()) == []


def test_stored_admission_flag_cannot_be_injected_or_trusted():
    raw = copy.deepcopy(make_step().to_mapping())
    raw["train_admitted"] = True
    with pytest.raises(AutonomyContractError):
        trajectory.ExecutedTrajectoryStep.from_mapping(raw)
    # Dataclass serialization does not include any automatically generated gold.
    assert "actual_endpoint" not in json.dumps(make_step().teacher_target.to_mapping())
