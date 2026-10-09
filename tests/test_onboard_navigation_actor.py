"""CPU-only shared actor projection, immutable RGB and fail-closed boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from multidetect.autonomy import onboard_navigation_actor as actor_module
from multidetect.autonomy.onboard_navigation_actor import (
    ACTOR_CONTRACT,
    ACTOR_SYSTEM_PROMPT,
    NavigationActorFrame,
    NavigationActorInput,
    serialize_navigation_actor_messages,
)
from multidetect.autonomy.onboard_navigation_supervision import (
    LABEL_PROVENANCE,
    SUPERVISION_CONTRACT,
    NavigationSourceValidationSession,
    NavigationSupervisionBudget,
    NavigationSupervisionSample,
)
from multidetect.autonomy.serialization import AutonomyContractError


def _encoded(color=(20, 40, 60), *, mode="RGB", format="PNG", size=(4, 3)):
    stream = BytesIO()
    Image.new(mode, size, color).save(stream, format=format)
    return stream.getvalue()


def _frame(sequence=0, *, color=(20, 40, 60), encoding="png", payload=None, **changes):
    if payload is None:
        payload = bytes(color) * 12 if encoding == "rgb8" else _encoded(color)
    values = dict(
        sequence=sequence,
        image_bytes=payload,
        byte_count=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        width=4,
        height=3,
        encoding=encoding,
    )
    return NavigationActorFrame.from_observation(**(values | changes))


def _actor(frames=None, **changes):
    frames = (_frame(),) if frames is None else frames
    values = dict(prompt_original="  Follow the building.\n", current_sequence=0, frames=frames)
    return NavigationActorInput.from_observation(**(values | changes))


def _messages_binding(messages):
    return [
        {
            "role": message["role"],
            "content": [
                dict(item)
                if item["type"] == "text"
                else {
                    "type": "image",
                    "mode": item["image"].mode,
                    "size": item["image"].size,
                    "pixels": item["image"].tobytes(),
                }
                for item in message["content"]
            ],
        }
        for message in messages
    ]


@pytest.fixture
def source_sample(tmp_path):
    """Real bounded source files, not a mocked source-validation checkbox."""
    prompt = "  Follow the building without changing this prompt.\n"
    actions = [
        {"imageid": 0, "pos": [0, 0, 0], "yaw": 0, "type": "go straight", "value": 3},
        {"imageid": 1, "pos": [3, 0, 0], "yaw": 0, "type": "stop", "value": 0},
    ]
    annotation = tmp_path / "annotation.json"
    annotation.write_text(json.dumps([{"gpt_instruction": prompt}]), encoding="utf-8")
    pose = tmp_path / "pose.jsonl"
    pose.write_text(
        "\n".join(json.dumps({"action": action}) for action in actions)
        + '\n{"aim_landmark":{"offline_only":"secret-gold-goal"}}\n',
        encoding="utf-8",
    )

    def binding(path):
        payload = path.read_bytes()
        return {
            "path": path.relative_to(tmp_path).as_posix(),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    metadata = {}
    for name in ("conversion", "license", "unit", "alignment"):
        path = tmp_path / (name + ".txt")
        path.write_text("Bound " + name, encoding="utf-8")
        metadata[name] = binding(path)
    frames = []
    for sequence, color in enumerate(((70, 80, 90), (100, 110, 120))):
        path = tmp_path / f"frame-{sequence}.png"
        path.write_bytes(_encoded(color))
        frames.append(
            {
                "frame_id": "opaque-frame-" + str(sequence),
                "episode_id": "offline-episode-secret",
                "scene_id": "offline-scene-secret",
                "step_index": sequence,
                **binding(path),
                "width": 4,
                "height": 3,
                "encoding": "png",
                "source_clock_id": None,
                "source_stamp": None,
                "stamp_unit": None,
            }
        )
    mapping = {
        "schema_version": 1,
        "contract": SUPERVISION_CONTRACT,
        "sample_id": "offline-sample-secret",
        "episode_id": "offline-episode-secret",
        "scene_id": "offline-scene-secret",
        "step_index": 1,
        "source": {
            "repository_id": "IPEC-COMMUNITY/OpenFly",
            "revision": "fixed-revision",
            "original_split": "train",
            "annotation_record_id": "0",
            "annotation": binding(annotation),
            "pose": binding(pose),
            "conversion": metadata["conversion"],
            "license": metadata["license"],
            "review_receipt": None,
            "unit_verification": metadata["unit"],
            "alignment_verification": metadata["alignment"],
        },
        "prompt": {
            "text_original": prompt,
            "utf8_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "source_record_sha256": binding(annotation)["sha256"],
            "source_pointer": {
                "format": "json", "record_index": None, "json_pointer": "/0/gpt_instruction"
            },
        },
        "frames": frames,
        "pose": {
            "position_xyz": [3, 0, 0],
            "yaw": 0,
            "coordinate_frame": "source_xy_z_up_ccw_yaw",
            "length_unit": "m",
            "yaw_unit": "rad",
            "origin": "planned_pose",
            "source_record_sha256": binding(pose)["sha256"],
        },
        "planned_action": {
            "raw": actions[1],
            "raw_record_sha256": binding(pose)["sha256"],
            "kind": "stop",
            "translation_m": None,
            "yaw_delta_rad": None,
            "unit_verification_sha256": None,
            "raw_source_pointer": {
                "format": "json_sequence", "record_index": 1, "json_pointer": "/action"
            },
        },
        "alignment": {
            "observation_step": 1,
            "action_step": 1,
            "next_step": None,
            "evidence_sha256": metadata["alignment"]["sha256"],
            "dt_s": None,
        },
        "partition": "train",
        "label_provenance": LABEL_PROVENANCE,
    }
    return NavigationSupervisionSample.from_mapping(mapping)


def test_training_and_observation_factories_share_exact_text_and_rgb(source_sample, tmp_path):
    training = NavigationActorInput.from_supervision_sample(source_sample, tmp_path)
    observable = NavigationActorInput.from_observation(
        prompt_original=source_sample.prompt.text_original,
        current_sequence=source_sample.step_index,
        frames=tuple(
            NavigationActorFrame.from_observation(
                sequence=frame.step_index,
                image_bytes=(tmp_path / frame.path).read_bytes(),
                byte_count=frame.bytes,
                sha256=frame.sha256,
                width=frame.width,
                height=frame.height,
                encoding=frame.encoding,
            )
            for frame in source_sample.frames
        ),
    )
    actual = serialize_navigation_actor_messages(training)
    assert _messages_binding(actual) == _messages_binding(
        serialize_navigation_actor_messages(observable)
    )
    assert training.identity_sha256 == observable.identity_sha256
    assert actual[1]["content"][0]["text"] == source_sample.prompt.text_original
    images = [item["image"] for item in actual[1]["content"] if item["type"] == "image"]
    assert [image.getpixel((0, 0)) for image in images] == [(70, 80, 90), (100, 110, 120)]
    text = "\n".join(
        item["text"] for message in actual for item in message["content"] if item["type"] == "text"
    )
    for forbidden in ("offline-sample-secret", "offline-scene-secret", "offline-episode-secret",
                      "opaque-frame-", "secret-gold-goal", "imageid", "source_xy_z_up_ccw_yaw"):
        assert forbidden not in text
    assert [message["role"] for message in actual] == ["system", "user"]


def test_source_factory_shared_session_requires_callers_final_gate(source_sample, tmp_path):
    with NavigationSourceValidationSession(tmp_path) as session:
        session.prime((source_sample,))
        actor = NavigationActorInput.from_supervision_sample(
            source_sample, tmp_path, session=session
        )
        assert not session.verified
        assert actor.frames[-1].sequence == source_sample.step_index
    assert session.verified


def test_original_source_changes_cannot_be_projected(source_sample, tmp_path):
    (tmp_path / source_sample.frames[-1].path).write_bytes(_encoded((1, 2, 3)))
    with pytest.raises(AutonomyContractError):
        NavigationActorInput.from_supervision_sample(source_sample, tmp_path)


def test_source_validation_session_budget_cannot_bypass_actor_budget(source_sample, tmp_path):
    actor_budget = replace(NavigationSupervisionBudget(), max_source_record_bytes=100)
    with NavigationSourceValidationSession(tmp_path) as session:
        session.prime((source_sample,))
        with pytest.raises(AutonomyContractError, match="session budget differs"):
            NavigationActorInput.from_supervision_sample(
                source_sample, tmp_path, budget=actor_budget, session=session
            )
    with pytest.raises(TypeError, match="NavigationSourceValidationSession"):
        NavigationActorInput.from_supervision_sample(source_sample, tmp_path, session=object())


def test_pending_session_source_change_fails_final_validation(source_sample, tmp_path):
    with pytest.raises(AutonomyContractError):
        with NavigationSourceValidationSession(tmp_path) as session:
            session.prime((source_sample,))
            NavigationActorInput.from_supervision_sample(source_sample, tmp_path, session=session)
            (tmp_path / source_sample.source.annotation.path).write_text(
                "tampered", encoding="utf-8"
            )
    assert not session.verified


@pytest.mark.parametrize("encoding", ["rgb8", "png", "jpeg"])
def test_supported_rgb_encodings(encoding):
    payload = _encoded(format="JPEG") if encoding == "jpeg" else None
    messages = serialize_navigation_actor_messages(
        _actor((_frame(encoding=encoding, payload=payload),))
    )
    image = messages[1]["content"][-1]["image"]
    assert image.mode == "RGB" and image.size == (4, 3)


def test_mutating_returned_pil_cannot_change_future_calls_or_identity():
    actor = _actor()
    identity = actor.identity_sha256
    first = serialize_navigation_actor_messages(actor)[1]["content"][-1]["image"]
    first.putpixel((0, 0), (1, 2, 3))
    second = serialize_navigation_actor_messages(actor)[1]["content"][-1]["image"]
    assert first is not second and second.getpixel((0, 0)) == (20, 40, 60)
    assert actor.identity_sha256 == identity
    with pytest.raises(FrozenInstanceError):
        actor.current_sequence = 8


def test_container_text_metadata_is_not_an_actor_input_channel():
    metadata = PngInfo()
    metadata.add_text("Comment", "offline-gold-pose-and-ID-secret")
    stream = BytesIO()
    Image.new("RGB", (4, 3), (20, 40, 60)).save(stream, format="PNG", pnginfo=metadata)
    actor = _actor((_frame(payload=stream.getvalue()),))
    image = serialize_navigation_actor_messages(actor)[1]["content"][-1]["image"]
    assert image.info == {} and len(image.getexif()) == 0
    assert actor.identity_sha256 == _actor().identity_sha256


def test_actor_identity_excludes_outer_sequence_and_source_encoding():
    original = _actor()
    shifted = _actor((_frame(30, encoding="rgb8"),), current_sequence=30)
    assert original.identity_sha256 == shifted.identity_sha256
    assert _messages_binding(serialize_navigation_actor_messages(original)) == _messages_binding(
        serialize_navigation_actor_messages(shifted)
    )
    assert set(original.to_identity_mapping()) == {
        "actor_contract", "system_prompt", "prompt_original", "ordered_rgb_pixel_bindings"
    }


def test_actor_identity_binds_prompt_order_pixels_version_and_system(monkeypatch):
    frames = (_frame(0), _frame(1, color=(90, 80, 70)))
    actor = _actor(frames, current_sequence=1)
    original = actor.identity_sha256
    changed_prompt = _actor(frames, current_sequence=1, prompt_original="different")
    assert changed_prompt.identity_sha256 != original
    swapped = (_frame(0, color=(90, 80, 70)), _frame(1))
    assert _actor(swapped, current_sequence=1).identity_sha256 != original
    monkeypatch.setattr(actor_module, "ACTOR_CONTRACT", ACTOR_CONTRACT + "-next")
    assert actor.identity_sha256 != original
    monkeypatch.setattr(actor_module, "ACTOR_CONTRACT", ACTOR_CONTRACT)
    monkeypatch.setattr(actor_module, "ACTOR_SYSTEM_PROMPT", ACTOR_SYSTEM_PROMPT + " Changed.")
    assert actor.identity_sha256 != original


@pytest.mark.parametrize("changes", [
    {"sequence": True}, {"width": True}, {"height": False}, {"byte_count": True},
    {"byte_count": 1}, {"sha256": "0" * 64}, {"sha256": "A" * 64},
    {"image_bytes": bytearray(b"bad")}, {"encoding": "unknown"}, {"sequence": -1},
])
def test_frame_rejects_unknown_formats_bad_binding_bool_numbers_and_mutability(changes):
    with pytest.raises((AutonomyContractError, TypeError)):
        _frame(**changes)


@pytest.mark.parametrize("frames,current", [
    ((), 0), ([_frame()], 0), ((_frame(0), _frame(1), _frame(2)), 2),
    ((_frame(1), _frame(0)), 1), ((_frame(0), _frame(0)), 0),
    ((_frame(2),), 1), ((_frame(0),), 1), ((object(),), 0),
])
def test_actor_rejects_mutable_overfull_reordered_duplicate_future_or_not_current(frames, current):
    with pytest.raises((AutonomyContractError, TypeError)):
        _actor(frames, current_sequence=current)


@pytest.mark.parametrize("changes", [
    {"prompt_original": ""}, {"prompt_original": " \n"}, {"prompt_original": False},
    {"current_sequence": True}, {"current_sequence": -1}, {"budget": {}},
])
def test_actor_requires_exact_prompt_current_sequence_and_typed_budget(changes):
    with pytest.raises((AutonomyContractError, TypeError)):
        _actor(**changes)


@pytest.mark.parametrize("budget", [
    replace(NavigationSupervisionBudget(), max_image_bytes=1),
    replace(NavigationSupervisionBudget(), max_total_image_bytes=1),
    replace(NavigationSupervisionBudget(), max_image_side=3),
    replace(NavigationSupervisionBudget(), max_image_pixels=11),
    replace(NavigationSupervisionBudget(), max_total_pixels=11),
    replace(NavigationSupervisionBudget(), max_prompt_bytes=1),
])
def test_per_image_total_dimensions_pixels_and_prompt_budgets_fail_closed(budget):
    with pytest.raises(AutonomyContractError):
        _actor(budget=budget)


@pytest.mark.parametrize("frame", [
    _frame(width=5),
    _frame(payload=_encoded(mode="RGBA", color=(1, 2, 3, 255))),
    _frame(payload=_encoded(format="JPEG")),
    _frame(payload=b"not-a-PNG"),
])
def test_declared_image_metadata_does_not_override_actual_format_mode_or_dimensions(frame):
    with pytest.raises(AutonomyContractError):
        _actor((frame,))


def test_raw_rgb_exact_length_and_unsupported_metadata_kwargs():
    with pytest.raises(AutonomyContractError):
        _frame(encoding="rgb8", width=5)
    with pytest.raises(TypeError):
        NavigationActorInput.from_observation(
            prompt_original="prompt", current_sequence=0, frames=(_frame(),), gold_target={}
        )
    with pytest.raises(TypeError):
        serialize_navigation_actor_messages({"prompt": "prompt", "pose": [1, 2, 3]})


def test_system_prompt_clarifies_mutual_exclusion_unknown_metrics_without_probe_answers():
    assert "mutually exclusively" in ACTOR_SYSTEM_PROMPT
    assert "yaw_delta_rad must be null, never zero" in ACTOR_SYSTEM_PROMPT
    assert "translation_m must be null" in ACTOR_SYSTEM_PROMPT
    assert "must both be null" in ACTOR_SYSTEM_PROMPT
    assert "Unknown metric quantities stay null" in ACTOR_SYSTEM_PROMPT
    assert "not landing, arrival or task completion" in ACTOR_SYSTEM_PROMPT
    assert "[3" not in ACTOR_SYSTEM_PROMPT
    assert "[1.0" not in ACTOR_SYSTEM_PROMPT


def test_import_is_stdlib_only_without_pillow_scripts_or_model_runtime():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, " + repr(str(root / "src")) + "); "
        "import multidetect.autonomy.onboard_navigation_actor; "
        "assert not any(x.split('.')[0] in "
        "{'torch','transformers','safetensors','peft','numpy','PIL','scripts'} "
        "for x in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
