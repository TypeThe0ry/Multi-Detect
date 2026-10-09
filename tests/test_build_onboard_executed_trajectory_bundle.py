from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from multidetect.autonomy.serialization import AutonomyContractError, canonical_sha256
from scripts import build_onboard_executed_trajectory_bundle as bundle


def binding(path, *, pointer=None):
    raw = path.read_bytes()
    return {
        "path": path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_id": "fixture-source",
        "json_pointer": pointer,
    }


def manifest(tmp_path):
    target = {
        "schema_version": 1,
        "contract": "multidetect.onboard-planned-navigation-target-v1",
        "kind": "translate",
        "translation_frame": "body_frd",
        "yaw_convention": "clockwise_right_positive",
        "translation_m": [1.0, 2.0, -1.0],
        "yaw_delta_rad": None,
    }
    stage = {
        "phase": "proposed",
        "at_s": 2.0,
        "clock_id": "fixture-clock",
        "action_sha256": canonical_sha256(target),
        "command_id": None,
    }
    source = tmp_path / "events.json"
    source.write_text(json.dumps({"proposed": stage}), encoding="utf-8")
    step = {
        "schema_version": 1,
        "contract": "multidetect.onboard-executed-trajectory-step-v1",
        "time_basis": "observer_monotonic",
        "step_id": "fixture-step",
        "episode_id": "fixture-episode",
        "vehicle_id": "fixture-x500",
        "clock_id": "fixture-clock",
        "context_sha256": "b" * 64,
        "actor_input_sha256": "c" * 64,
        "teacher_target": target,
        "proposed": {**stage, "source": binding(source, pointer="/proposed")},
        "approved": None,
        "submitted": None,
        "outcome": None,
        "before": None,
        "after": None,
        "max_pose_skew_s": 0.05,
    }
    return {"schema_version": 1, "contract": bundle.CONTRACT, "steps": [step]}


def completed_manifest(tmp_path):
    value = manifest(tmp_path)
    step = value["steps"][0]
    command = "12345678-1234-4234-8234-123456789abc"
    events = {"proposed": {key: item for key, item in step["proposed"].items() if key != "source"}}
    for phase, at_s in (("approved", 3.0), ("submitted", 4.0)):
        events[phase] = {
            **events["proposed"],
            "phase": phase,
            "at_s": at_s,
            "command_id": command if phase == "submitted" else None,
        }
    for name, at_s, position in (
        ("before", 1.0, [0.0, 0.0, -5.0]),
        ("after", 5.0, [0.5, 1.0, -5.5]),
    ):
        events[name + "_pose"] = {
            "position_m": position,
            "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            "world_frame": "ned",
            "world_origin_id": "fixture-world",
            "quaternion_convention": "active_body_frd_to_world_wxyz",
            "origin": "simulated_observation",
            "at_s": at_s,
            "clock_id": "fixture-clock",
        }
    event_path = tmp_path / "events.json"
    event_path.write_text(json.dumps(events), encoding="utf-8")
    for phase in ("proposed", "approved", "submitted"):
        step[phase] = {**events[phase], "source": binding(event_path, pointer="/" + phase)}
    frames = {}
    for sequence, name in enumerate(("before", "after")):
        image = tmp_path / (name + "-fixture.bin")
        image.write_bytes(b"fixture-not-real-camera-" + name.encode())
        pose = {
            **events[name + "_pose"],
            "source": binding(event_path, pointer="/" + name + "_pose"),
        }
        frames[name] = {
            "frame_id": "fixture-" + name,
            "sequence": sequence,
            "at_s": pose["at_s"],
            "clock_id": "fixture-clock",
            "image_source": binding(image),
            "pose": pose,
        }
    frame_path = tmp_path / "frames.json"
    frame_path.write_text(json.dumps(frames), encoding="utf-8")
    for name in ("before", "after"):
        step[name] = {**frames[name], "source": binding(frame_path, pointer="/" + name)}
    outcome = {
        "status": "observed_success",
        "at_s": 6.0,
        "clock_id": "fixture-clock",
        "action_sha256": step["proposed"]["action_sha256"],
        "command_id": command,
        "before_observation_sha256": canonical_sha256(step["before"]),
        "after_observation_sha256": canonical_sha256(step["after"]),
    }
    outcome_path = tmp_path / "outcome.json"
    outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
    step["outcome"] = {**outcome, "source": binding(outcome_path, pointer="")}
    return value


@pytest.mark.parametrize("pointer,expected", [("", {"a/b": {"~x": [7]}}), ("/a~1b/~0x/0", 7)])
def test_pointer_original_projection(pointer, expected):
    assert bundle.json_pointer({"a/b": {"~x": [7]}}, pointer) == expected


@pytest.mark.parametrize(
    "pointer",
    ["a", "/~2", "/a~", "/missing", "/rows/01", "/rows/-", "/rows/2", "/rows/0/nope", "/" * 33],
)
def test_pointer_rejects_ambiguous_or_absent_record(pointer):
    with pytest.raises(AutonomyContractError):
        bundle.json_pointer({"rows": [3]}, pointer)


def test_source_sha_is_not_authority_and_proposal_is_not_execution(tmp_path):
    normalized, audit = bundle.verify_manifest(manifest(tmp_path), artifact_root=tmp_path)
    assert len(normalized["steps"]) == 1
    assert audit["source_integrity_and_projections_verified"] is True
    assert audit["verified_record_projection_count"] == 1
    assert audit["steps"][0]["observed_motion"] is None
    for name in (
        "train_admitted",
        "training_ready",
        "execution_authority",
        "candidate_promoted",
        "source_producer_authenticated",
        "runtime_Actor_created",
        "pixel_content_decoded",
    ):
        assert audit[name] is False


def test_integer_source_time_roundtrips_without_silent_float_coercion(tmp_path):
    value = manifest(tmp_path)
    value["steps"][0]["proposed"]["at_s"] = 2
    path = tmp_path / "events.json"
    stage = {key: item for key, item in value["steps"][0]["proposed"].items() if key != "source"}
    path.write_text(json.dumps({"proposed": stage}), encoding="utf-8")
    value["steps"][0]["proposed"]["source"] = binding(path, pointer="/proposed")
    emitted, audit = bundle.verify_manifest(value, artifact_root=tmp_path)
    assert type(emitted["steps"][0]["proposed"]["at_s"]) is int
    assert audit["manifest_canonical_sha256"] == audit["normalized_canonical_sha256"]
    bundle.verify_manifest(emitted, artifact_root=tmp_path)
    value["steps"][0]["proposed"]["at_s"] = 999
    assert emitted["steps"][0]["proposed"]["at_s"] == 2


def test_three_dimensional_feedback_is_not_the_teacher_command(tmp_path):
    normalized, audit = bundle.verify_manifest(completed_manifest(tmp_path), artifact_root=tmp_path)
    assert audit["steps"][0]["audit"]["training_evidence_structurally_eligible"] is True
    assert audit["steps"][0]["observed_motion"]["translation_m"] == [0.5, 1.0, -0.5]
    assert normalized["steps"][0]["teacher_target"]["translation_m"] == [1.0, 2.0, -1.0]
    assert audit["verified_record_projection_count"] == 8
    assert audit["verified_unique_file_count"] == 5
    assert audit["train_admitted"] is False
    assert audit["source_producer_authenticated"] is False


def test_frame_metadata_cannot_be_fabricated_even_if_endpoint_hash_is_updated(tmp_path):
    value = completed_manifest(tmp_path)
    step = value["steps"][0]
    step["before"]["frame_id"] = "fabricated-frame-id"
    step["outcome"]["before_observation_sha256"] = canonical_sha256(step["before"])
    with pytest.raises(AutonomyContractError, match="projection differs"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_complete_frame_bytes_are_checked_not_just_pose(tmp_path):
    value = completed_manifest(tmp_path)
    path = tmp_path / "before-fixture.bin"
    path.write_bytes(path.read_bytes().replace(b"before", b"broken"))
    with pytest.raises(AutonomyContractError, match="bytes/SHA differ"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_renamed_step_cannot_replay_one_submitted_command(tmp_path):
    value = completed_manifest(tmp_path)
    replay = deepcopy(value["steps"][0])
    replay["step_id"] = "different-step-same-execution"
    value["steps"].append(replay)
    with pytest.raises(AutonomyContractError, match="Duplicate submitted command"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_episode_cannot_switch_vehicle_domain(tmp_path):
    value = manifest(tmp_path)
    replay = deepcopy(value["steps"][0])
    replay["step_id"] = "new-step"
    replay["vehicle_id"] = "another-vehicle"
    value["steps"].append(replay)
    with pytest.raises(AutonomyContractError, match="Episode vehicle or clock"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_stage_projection_cannot_claim_a_different_original_action(tmp_path):
    value = manifest(tmp_path)
    value["steps"][0]["proposed"]["at_s"] = 3.0
    with pytest.raises(AutonomyContractError, match="projection differs"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_file_same_length_tamper_rejected(tmp_path):
    value = manifest(tmp_path)
    path = tmp_path / "events.json"
    path.write_bytes(path.read_bytes().replace(b"2.0", b"3.0"))
    with pytest.raises(AutonomyContractError, match="bytes/SHA differ"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_original_duplicate_keys_rejected_even_with_matching_file_hash(tmp_path):
    value = manifest(tmp_path)
    path = tmp_path / "events.json"
    path.write_text('{"proposed":{},"proposed":{}}', encoding="utf-8")
    value["steps"][0]["proposed"]["source"] = binding(path, pointer="/proposed")
    with pytest.raises(AutonomyContractError, match="duplicate JSON"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_original_nonfinite_number_rejected(tmp_path):
    value = manifest(tmp_path)
    path = tmp_path / "events.json"
    path.write_text('{"proposed":{},"untrusted":NaN}', encoding="utf-8")
    value["steps"][0]["proposed"]["source"] = binding(path, pointer="/proposed")
    with pytest.raises(AutonomyContractError, match="non-finite"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_path_traversal_and_external_source_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "source.json"
    outside.write_text("{}", encoding="utf-8")
    verifier = bundle.SourceVerifier(root)
    for path in ("../source.json", "..\\source.json", outside.as_posix()):
        value = binding(outside)
        value["path"] = path
        with pytest.raises(AutonomyContractError):
            verifier.verify_file(value)


def test_source_close_rechecks_bytes(tmp_path):
    path = tmp_path / "source.json"
    path.write_text("{}", encoding="utf-8")
    verifier = bundle.SourceVerifier(tmp_path)
    verifier.verify_file(binding(path))
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="changed during"):
        verifier.close()


def test_conflicting_declarations_not_cached_as_pass(tmp_path):
    path = tmp_path / "source.json"
    path.write_text("{}", encoding="utf-8")
    verifier = bundle.SourceVerifier(tmp_path)
    value = binding(path)
    verifier.verify_file(value)
    value["sha256"] = "a" * 64
    with pytest.raises(AutonomyContractError, match="Conflicting"):
        verifier.verify_file(value)


def test_step_ids_are_unique(tmp_path):
    value = manifest(tmp_path)
    value["steps"].append(deepcopy(value["steps"][0]))
    with pytest.raises(AutonomyContractError, match="Duplicate step_id"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_raw_sha_cannot_be_uppercase(tmp_path):
    value = manifest(tmp_path)
    value["steps"][0]["proposed"]["source"]["sha256"] = "A" * 64
    with pytest.raises(ValueError):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_manifest_header_not_legacy_planned_supervision(tmp_path):
    value = manifest(tmp_path)
    value["contract"] = "multidetect.onboard-navigation-supervision-v1"
    with pytest.raises(AutonomyContractError, match="Unsupported manifest contract"):
        bundle.verify_manifest(value, artifact_root=tmp_path)


def test_bundle_output_create_only_and_bound(tmp_path):
    value = manifest(tmp_path)
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "new-bundle"
    audit = bundle.build_bundle(source, sha, artifact_root=tmp_path, output_directory=output)
    assert audit["train_admitted"] is False
    assert (
        audit["normalized_file"]["sha256"]
        == hashlib.sha256((output / "steps.json").read_bytes()).hexdigest()
    )
    before = (output / "source-audit.json").read_bytes()
    with pytest.raises(FileExistsError):
        bundle.build_bundle(source, sha, artifact_root=tmp_path, output_directory=output)
    assert (output / "source-audit.json").read_bytes() == before


def test_changed_manifest_is_denied_before_output(tmp_path):
    value = manifest(tmp_path)
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    output = tmp_path / "must-not-exist"
    with pytest.raises(AutonomyContractError, match="Manifest SHA differs"):
        bundle.build_bundle(source, "0" * 64, artifact_root=tmp_path, output_directory=output)
    assert not output.exists()


@pytest.mark.parametrize("filename", ["steps.json", "source-audit.json"])
def test_tampered_output_cannot_be_bound_as_a_success(tmp_path, monkeypatch, filename):
    value = manifest(tmp_path)
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    original = bundle._write_new

    def corrupt(path, document):
        result = original(path, document)
        if path.name == filename:
            path.write_text('{"changed":true}', encoding="utf-8")
        return result

    monkeypatch.setattr(bundle, "_write_new", corrupt)
    with pytest.raises(AutonomyContractError, match="output differs"):
        bundle.build_bundle(source, sha, artifact_root=tmp_path, output_directory=tmp_path / "new")


def test_main_failure_does_not_write_output(tmp_path, capsys):
    source = tmp_path / "manifest.json"
    source.write_text("{}", encoding="utf-8")
    output = tmp_path / "must-not-exist"
    status = bundle.main(
        [
            "--manifest",
            str(source),
            "--manifest-sha256",
            "0" * 64,
            "--artifact-root",
            str(tmp_path),
            "--output-directory",
            str(output),
        ]
    )
    assert status == 1
    assert json.loads(capsys.readouterr().err)["train_admitted"] is False
    assert not output.exists()
