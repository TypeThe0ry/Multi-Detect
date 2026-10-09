"""Native source pointers, bounded reuse, and truth-free actor serialization."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest
from PIL import Image

from multidetect.autonomy import onboard_navigation_supervision as module
from multidetect.autonomy.onboard_navigation_supervision import (
    NavigationRecordPointer,
    NavigationSourceFile,
    NavigationSourceValidationSession,
    NavigationSupervisionBudget,
    NavigationSupervisionSample,
    serialize_navigation_supervision_messages,
    serialize_navigation_supervision_target,
    validate_navigation_episode_samples,
)
from multidetect.autonomy.serialization import AutonomyContractError, canonical_json


def _binding(path, root):
    raw = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.fixture
def sample_mapping(tmp_path):
    prompt = "  Fly past the building and observe the route.\n"
    annotation = tmp_path / "original-train.json"
    annotation.write_text(json.dumps([{"gpt_instruction": prompt}]), encoding="utf-8")
    actions = [
        {"imageid": 0, "pos": [0, 0, 0], "yaw": 0, "type": "go straight", "value": 3},
        {"imageid": 1, "pos": [3, 0, 0], "yaw": 0, "type": "stop", "value": 0},
    ]
    pose = tmp_path / "original-pose.jsonl"
    pose.write_text(
        "\n".join(json.dumps({"action": action}, indent=2) for action in actions)
        + '\n{"aim_landmark": {"offline_only": "future goal secret"}}\n',
        encoding="utf-8",
    )
    metadata = {}
    for name in ("conversion", "license", "unit", "alignment"):
        path = tmp_path / f"{name}.txt"
        path.write_text(f"Source-bound {name}; not review approval", encoding="utf-8")
        metadata[name] = _binding(path, tmp_path)
    image = tmp_path / "source-0.png"
    Image.new("RGB", (4, 3), (70, 80, 90)).save(image)
    frame = {
        "frame_id": "opaque-current-0",
        "episode_id": "episode-alpha",
        "scene_id": "scene-a",
        "step_index": 0,
        **_binding(image, tmp_path),
        "width": 4,
        "height": 3,
        "encoding": "png",
        "source_clock_id": None,
        "source_stamp": None,
        "stamp_unit": None,
    }
    return {
        "schema_version": 1,
        "contract": module.SUPERVISION_CONTRACT,
        "sample_id": "sample-alpha-0",
        "episode_id": "episode-alpha",
        "scene_id": "scene-a",
        "step_index": 0,
        "source": {
            "repository_id": "IPEC-COMMUNITY/OpenFly",
            "revision": "pinned-source-revision",
            "original_split": "train",
            "annotation_record_id": "0",
            "annotation": _binding(annotation, tmp_path),
            "pose": _binding(pose, tmp_path),
            "conversion": metadata["conversion"],
            "license": metadata["license"],
            "review_receipt": None,
            "unit_verification": metadata["unit"],
            "alignment_verification": metadata["alignment"],
        },
        "prompt": {
            "text_original": prompt,
            "utf8_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "source_record_sha256": _binding(annotation, tmp_path)["sha256"],
            "source_pointer": {
                "format": "json",
                "record_index": None,
                "json_pointer": "/0/gpt_instruction",
            },
        },
        "frames": [frame],
        "pose": {
            "position_xyz": [0, 0, 0],
            "yaw": 0,
            "coordinate_frame": "source_xy_z_up_ccw_yaw",
            "length_unit": "m",
            "yaw_unit": "rad",
            "origin": "planned_pose",
            "source_record_sha256": _binding(pose, tmp_path)["sha256"],
        },
        "planned_action": {
            "raw": actions[0],
            "raw_record_sha256": _binding(pose, tmp_path)["sha256"],
            "kind": "translate",
            "translation_m": [3, 0, 0],
            "yaw_delta_rad": None,
            "unit_verification_sha256": metadata["unit"]["sha256"],
            "raw_source_pointer": {
                "format": "json_sequence",
                "record_index": 0,
                "json_pointer": "/action",
            },
        },
        "alignment": {
            "observation_step": 0,
            "action_step": 0,
            "next_step": 1,
            "evidence_sha256": metadata["alignment"]["sha256"],
            "dt_s": None,
        },
        "partition": "train",
        "label_provenance": module.LABEL_PROVENANCE,
    }


def _sample(mapping):
    return NavigationSupervisionSample.from_mapping(copy.deepcopy(mapping))


def _second(mapping, root):
    result = copy.deepcopy(mapping)
    result.update(sample_id="sample-alpha-1", step_index=1)
    raw = {"imageid": 1, "pos": [3, 0, 0], "yaw": 0, "type": "stop", "value": 0}
    result["planned_action"].update(
        raw=raw,
        kind="stop",
        translation_m=None,
        unit_verification_sha256=None,
        raw_source_pointer={
            "format": "json_sequence",
            "record_index": 1,
            "json_pointer": "/action",
        },
    )
    result["pose"]["position_xyz"] = [3, 0, 0]
    image = root / "source-1.png"
    Image.new("RGB", (4, 3), (100, 110, 120)).save(image)
    current = copy.deepcopy(result["frames"][0])
    current.update(frame_id="opaque-current-1", step_index=1, **_binding(image, root))
    result["frames"].append(current)
    result["alignment"].update(observation_step=1, action_step=1, next_step=None)
    return result


def test_native_json_sequence_pretty_records_and_json_array_pointer(tmp_path, sample_mapping):
    sample = _sample(sample_mapping)
    assert sample.to_mapping() == sample_mapping
    assert sample.validate_sources(tmp_path)[0].getpixel((0, 0)) == (70, 80, 90)
    assert sample.training_ready is False
    assert sample.source.review_receipt is None
    assert sample.alignment.dt_s is None


@pytest.mark.parametrize(
    "format,payload,index,pointer,expected",
    [
        ("json", b'{"a/b":{"~key":[9,10]}}', None, "/a~1b/~0key/1", 10),
        ("json", b'[{"p":"first"},{"p":"second"}]', None, "/1/p", "second"),
        ("json_sequence", b'{"a":1}\n{\n "a":2\n}\n', 1, "/a", 2),
        ("jsonl", b'{"a":1}\n{"a":2}\n', 1, "/a", 2),
    ],
)
def test_explicit_source_pointer_formats(format, payload, index, pointer, expected):
    assert NavigationRecordPointer(format, index, pointer).extract(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}\n',
        b'{"a":NaN}\n',
        b'{"a":1e999}\n',
        b'{"a":1}{"a":2}',
        b'{"a":1}\nnot-json',
        b'{"a":1}\n{"b":',
    ],
)
def test_sequence_rejects_ambiguous_nonfinite_or_incomplete_source(payload):
    with pytest.raises(AutonomyContractError):
        NavigationRecordPointer("json_sequence", 0, "/a").extract(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'[{"a":1},]',
        b'[{"a":1}]garbage',
        b'[{"a":1},{"a":NaN}]',
        b'[{"a":1}{"a":2}]',
        b'[{"a":1}',
    ],
)
def test_full_source_parse_checks_unrequested_records_and_tail(payload):
    with pytest.raises(AutonomyContractError):
        NavigationRecordPointer("json", None, "/0/a").extract(payload)


def test_prompt_cannot_be_changed_and_rehashed(tmp_path, sample_mapping):
    sample_mapping["prompt"]["text_original"] = "Invented improved instruction"
    sample_mapping["prompt"]["utf8_sha256"] = hashlib.sha256(
        sample_mapping["prompt"]["text_original"].encode()
    ).hexdigest()
    with pytest.raises(AutonomyContractError, match="Prompt differs"):
        _sample(sample_mapping).validate_sources(tmp_path)


def test_raw_action_cannot_be_relabelled_and_rehashed_metadata(tmp_path, sample_mapping):
    sample_mapping["planned_action"]["raw"]["value"] = 9
    with pytest.raises(AutonomyContractError, match="raw planned action differs"):
        _sample(sample_mapping).validate_sources(tmp_path)


def test_pose_metadata_must_match_original_action_observation(tmp_path, sample_mapping):
    sample_mapping["pose"]["position_xyz"] = [99, 0, 0]
    with pytest.raises(AutonomyContractError, match="source pose differs"):
        _sample(sample_mapping).validate_sources(tmp_path)


def test_actor_only_exact_prompt_and_current_latest_past_images(tmp_path, sample_mapping):
    sample = _sample(_second(sample_mapping, tmp_path))
    messages = serialize_navigation_supervision_messages(sample, tmp_path)
    text = "\n".join(
        block["text"]
        for message in messages
        for block in message["content"]
        if block["type"] == "text"
    )
    assert messages[1]["content"][0]["text"] == sample.prompt.text_original
    assert "future goal secret" not in text
    for offline_only in ("episode-alpha", "scene-a", "original-pose", "imageid", "go straight"):
        assert offline_only not in text
    images = [block["image"] for block in messages[1]["content"] if block["type"] == "image"]
    assert [image.getpixel((0, 0)) for image in images] == [(70, 80, 90), (100, 110, 120)]
    target = json.loads(serialize_navigation_supervision_target(sample, tmp_path))
    assert target["kind"] == "stop"
    assert target["translation_m"] is None
    assert target["yaw_delta_rad"] is None
    assert not set(target) & {"land", "completed", "speed", "duration", "confidence"}


@pytest.mark.parametrize("translation", [[3, 0, 0], [0, 3, 0], [0, -3, 0], [0, 0, -3], [0, 0, 3]])
def test_independent_body_frd_targets_preserve_all_signs(tmp_path, sample_mapping, translation):
    # Coordinate conversion itself belongs to the source builder, not this serializer.
    sample_mapping["planned_action"]["translation_m"] = translation
    target = json.loads(serialize_navigation_supervision_target(_sample(sample_mapping), tmp_path))
    assert target["translation_frame"] == "body_frd"
    assert target["translation_m"] == translation
    assert target["yaw_convention"] == "clockwise_right_positive"


@pytest.mark.parametrize("yaw", [-0.5235987755982988, 0.5235987755982988])
def test_right_positive_yaw_targets_preserve_sign(tmp_path, sample_mapping, yaw):
    sample_mapping["planned_action"].update(kind="turn_yaw", translation_m=None, yaw_delta_rad=yaw)
    target = json.loads(serialize_navigation_supervision_target(_sample(sample_mapping), tmp_path))
    assert target["yaw_delta_rad"] == yaw
    assert target["translation_m"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m["frames"][0].update(step_index=1),
        lambda m: m["frames"][0].update(scene_id="other-scene"),
        lambda m: m["alignment"].update(action_step=1),
        lambda m: m["alignment"].update(next_step=2),
        lambda m: m["planned_action"]["raw_source_pointer"].update(record_index=1),
        lambda m: m["pose"].update(length_unit=None),
        lambda m: m["pose"].update(yaw_unit=None),
        lambda m: m["source"].update(unit_verification=None),
        lambda m: m["planned_action"].update(kind="land"),
        lambda m: m["frames"][0].update(source_stamp=100),
        lambda m: m["source"].update(original_split="unseen_eval"),
        lambda m: m.update(truth={"target": "secret"}),
    ],
)
def test_future_misalignment_unknown_units_and_unsupported_fields_reject(sample_mapping, mutation):
    mutation(sample_mapping)
    with pytest.raises(AutonomyContractError):
        _sample(sample_mapping)


def test_unknown_metric_quantities_remain_null(tmp_path, sample_mapping):
    sample_mapping["pose"].update(length_unit=None, yaw_unit=None, coordinate_frame=None)
    sample_mapping["source"]["unit_verification"] = None
    sample_mapping["planned_action"].update(translation_m=None, unit_verification_sha256=None)
    target = json.loads(serialize_navigation_supervision_target(_sample(sample_mapping), tmp_path))
    assert target["translation_m"] is None


def test_episode_validation_keeps_whole_scene_and_exact_source_order(tmp_path, sample_mapping):
    first, second = _sample(sample_mapping), _sample(_second(sample_mapping, tmp_path))
    assert validate_navigation_episode_samples((first, second)) == (first, second)
    with pytest.raises(AutonomyContractError, match="step 0"):
        validate_navigation_episode_samples((second, first))
    with pytest.raises(AutonomyContractError, match="scene cannot cross"):
        validate_navigation_episode_samples((first, replace(second, partition="dev-test")))


def test_incomplete_episode_and_stop_then_next_are_not_full_episode_evidence(
    tmp_path, sample_mapping
):
    first, second = _sample(sample_mapping), _sample(_second(sample_mapping, tmp_path))
    with pytest.raises(AutonomyContractError, match="terminal STOP"):
        validate_navigation_episode_samples((first,))
    with pytest.raises(AutonomyContractError, match="subsequent steps"):
        validate_navigation_episode_samples(
            (first, second, replace(second, sample_id="not-a-real-step-after-stop"))
        )


@pytest.mark.parametrize("source_field", ["pose", "annotation", "conversion", "license"])
def test_same_episode_id_cannot_splice_different_original_files(
    tmp_path, sample_mapping, source_field
):
    first, second = _sample(sample_mapping), _sample(_second(sample_mapping, tmp_path))
    source_file = getattr(second.source, source_field)
    alias = tmp_path / f"different-original-{source_field}.dat"
    alias.write_bytes((tmp_path / source_file.path).read_bytes())
    changed = replace(
        second,
        source=replace(
            second.source,
            **{source_field: NavigationSourceFile.from_mapping(_binding(alias, tmp_path))},
        ),
    )
    # Both individual records still have valid byte/pointer bindings. They cannot
    # be stitched into one original episode merely by reusing an episode ID.
    first.validate_sources(tmp_path)
    changed.validate_sources(tmp_path)
    with pytest.raises(AutonomyContractError, match="full-source identity"):
        validate_navigation_episode_samples((first, changed))


def test_original_annotation_record_identity_cannot_change_mid_episode(tmp_path, sample_mapping):
    first, second = _sample(sample_mapping), _sample(_second(sample_mapping, tmp_path))
    changed = replace(second, source=replace(second.source, annotation_record_id="other-record"))
    with pytest.raises(AutonomyContractError, match="full-source identity"):
        validate_navigation_episode_samples((first, changed))


def test_shared_session_hashes_sources_twice_and_parses_once(tmp_path, sample_mapping, monkeypatch):
    samples = (_sample(sample_mapping), _sample(_second(sample_mapping, tmp_path)))
    verified, parsed = [], []
    original_verify, original_extract = NavigationSourceFile.verify, module._extract_pointers

    def verify(bound, root, *, maximum_bytes):
        verified.append(bound.path)
        return original_verify(bound, root, maximum_bytes=maximum_bytes)

    def extract(stream, pointers, budget):
        parsed.append(tuple(pointers))
        return original_extract(stream, pointers, budget)

    monkeypatch.setattr(NavigationSourceFile, "verify", verify)
    monkeypatch.setattr(module, "_extract_pointers", extract)
    session = NavigationSourceValidationSession(tmp_path)
    with session:
        session.prime(samples)
        for sample in samples:
            serialize_navigation_supervision_messages(sample, tmp_path, session=session)
            serialize_navigation_supervision_target(sample, tmp_path, session=session)
        assert not session.verified
    assert session.verified
    assert len(parsed) == 2  # One annotation array, one pretty-JSON pose sequence.
    assert set(verified) == {item.name for item in tmp_path.iterdir()}
    assert all(verified.count(path) == 2 for path in set(verified))
    with pytest.raises(AutonomyContractError, match="active context"):
        session.validate_sample(samples[0])


def test_large_array_is_streamed_not_whole_file_read(tmp_path, sample_mapping, monkeypatch):
    path = tmp_path / "original-train.json"
    prompt = sample_mapping["prompt"]["text_original"]
    records = [{"gpt_instruction": prompt, "padding": "x" * 2048} for _ in range(2500)]
    path.write_text(json.dumps(records), encoding="utf-8")
    bound = _binding(path, tmp_path)
    sample_mapping["source"]["annotation"] = bound
    sample_mapping["prompt"]["source_record_sha256"] = bound["sha256"]
    sample_mapping["prompt"]["source_pointer"]["json_pointer"] = "/2499/gpt_instruction"
    original_read = NavigationSourceFile.read

    def bounded_read(source, root, *, maximum_bytes):
        assert source.path != path.name, "Large original annotation must never use whole-file read"
        return original_read(source, root, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(NavigationSourceFile, "read", bounded_read)
    budget = NavigationSupervisionBudget(max_source_record_bytes=4096)
    assert _sample(sample_mapping).validate_sources(tmp_path, budget=budget)[0].size == (4, 3)


def test_source_change_after_priming_fails_and_never_sets_verified(tmp_path, sample_mapping):
    sample = _sample(sample_mapping)
    session = NavigationSourceValidationSession(tmp_path)
    with pytest.raises(AutonomyContractError):
        with session:
            session.prime((sample,))
            path = tmp_path / sample.source.annotation.path
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b"building", b"BUILDING"))
    assert not session.verified


def test_change_between_validation_calls_is_detected(tmp_path, sample_mapping):
    sample = _sample(sample_mapping)
    with pytest.raises(AutonomyContractError, match="source changed"):
        with NavigationSourceValidationSession(tmp_path) as session:
            session.prime((sample,))
            session.validate_sample(sample)
            path = tmp_path / sample.source.conversion.path
            raw = path.read_bytes()
            path.write_bytes(raw.swapcase())
            session.validate_sample(sample)


def test_explicit_source_path_must_be_root_contained(sample_mapping):
    sample_mapping["source"]["pose"]["path"] = "../unrelated-pose.json"
    with pytest.raises(AutonomyContractError, match="traverse"):
        _sample(sample_mapping)


def test_record_pointer_cache_and_file_budgets_enforced(tmp_path, sample_mapping):
    sample = _sample(sample_mapping)
    for budget in (
        NavigationSupervisionBudget(max_source_file_bytes=8),
        NavigationSupervisionBudget(max_source_record_bytes=8),
        NavigationSupervisionBudget(max_cached_pointer_bytes=8),
        NavigationSupervisionBudget(max_source_files=1),
    ):
        with pytest.raises(AutonomyContractError):
            sample.validate_sources(tmp_path, budget=budget)


def test_duplicate_sample_wire_key_is_rejected(sample_mapping):
    raw = canonical_json(sample_mapping)
    raw = raw[:-1] + ',"schema_version":1}'
    with pytest.raises(AutonomyContractError, match="duplicate"):
        NavigationSupervisionSample.from_json(raw)
