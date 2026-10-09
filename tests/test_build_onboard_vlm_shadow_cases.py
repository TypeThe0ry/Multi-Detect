from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import build_onboard_vlm_shadow_cases as builder

ISSUED = "2026-10-07T06:00:00+00:00"


def _write(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")


def _png(path: Path, pixels) -> str:
    import cv2

    assert cv2.imwrite(str(path), pixels)
    return builder.trainer.sha256(path)


@pytest.fixture
def source(tmp_path, monkeypatch):
    import numpy as np

    root = tmp_path / "repository"
    root.mkdir()
    for relative in builder.SOURCE_PATHS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(builder.ROOT / relative, path)
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.trainer, "ROOT", root)
    monkeypatch.setattr(builder.evaluator, "ROOT", root)
    base = root / "artifacts" / "source"
    base.mkdir(parents=True)
    frames, clips = [], []
    specifications = (
        ("train", "train-suite-near-00000", 16),
        ("train", "train-suite-far-00002", 16),
        ("val", "val-clip", 2),
        ("test", "test-clip", 2),
    )
    for clip_index, (split, clip_id, count) in enumerate(specifications):
        world = root / "worlds" / f"{split}.sdf"
        world.parent.mkdir(exist_ok=True)
        world.write_text(f"<sdf><world name='{split}'/></sdf>")
        target = root / "models" / f"{'unseen' if split == 'test' else 'seen'}.sdf"
        target.parent.mkdir(exist_ok=True)
        target.write_text(
            "<sdf><model name='probe'><link name='body'><visual name='body'>"
            f"<geometry><box><size>{2 if split == 'test' else 1} 1 1</size></box></geometry>"
            "</visual></link></model></sdf>"
        )
        target_path = target.relative_to(root).as_posix()
        target_sha = builder.trainer.sha256(target)
        model = {
            "path": target_path,
            "sha256": target_sha,
            "geometry_sha256": builder.curriculum.geometry_sha256(
                builder.curriculum.load_target(root, target_path)
            ),
        }
        world_path = world.relative_to(root).as_posix()
        source_hashes = {world_path: builder.trainer.sha256(world), target_path: target_sha}
        clip_ids = []
        for index in range(count):
            identifier = f"{clip_id}-frame-{index:05d}"
            clip_ids.append(identifier)
            mask = np.zeros((8, 10), dtype=np.uint8)
            amodal = np.zeros((8, 10), dtype=np.uint8)
            amodal[2:4, 3:5] = 255
            if index not in (6, 7, 8, 9):
                mask[2:4, 3:5] = 255
            visible = int(np.count_nonzero(mask))
            bbox = builder.renderer.visible_bbox(mask.reshape(-1).tobytes(), 10, 8)
            frame = {
                "frame_id": identifier,
                "clip_id": clip_id,
                "frame_index": index,
                "split": split,
                "width": 10,
                "height": 8,
                "world_path": world_path,
                "world_sha256": source_hashes[world_path],
                "source_hashes": deepcopy(source_hashes),
                "target_model": deepcopy(model),
                "captured_at_s": index / 8,
                "dt": 0 if index == 0 else 1 / 8,
                "timestamp_semantics": builder.CLOCK_SEMANTICS,
                "target_present": True,
                "target_visible": bool(visible),
                "visible_target_pixels": visible,
                "visible_bbox_xyxy_px": None if bbox is None else list(bbox),
                "planned_condition": "arbitrary_plan_not_observation",
                "visibility_measurement": {
                    "amodal_target_pixels": 4,
                    "visible_amodal_overlap_pixels": visible,
                    "visible_fraction": visible / 4,
                    "measured_condition": "clear" if visible else "fully_occluded",
                },
            }
            for kind in ("image", "label", "mask", "amodal_mask"):
                path = base / f"{identifier}-{kind}{'.txt' if kind == 'label' else '.png'}"
                if kind == "label":
                    path.write_text(builder.renderer.yolo_label(bbox, 10, 8))
                elif kind == "image":
                    _png(path, np.full((8, 10, 3), 10 + clip_index * 30 + index, np.uint8))
                else:
                    _png(path, mask if kind == "mask" else amodal)
                frame[f"{kind}_path"] = path.name
                frame[f"{kind}_sha256"] = builder.trainer.sha256(path)
            frame["rgb_payload_sha256"] = frame["image_sha256"]
            frames.append(frame)
        clips.append(
            {
                "clip_id": clip_id,
                "split": split,
                "frame_count": count,
                "frame_ids": clip_ids,
                "fps": 8,
                "captured_at_start_s": 0,
                "captured_at_end_s": (count - 1) / 8,
                "timestamp_semantics": builder.CLOCK_SEMANTICS,
                "world_path": world_path,
                "world_sha256": source_hashes[world_path],
                "source_hashes": source_hashes,
            }
        )
    yaml = base / "dataset.yaml"
    _write(yaml, {"names": {"0": "search_target"}})
    document = {
        "contract": builder.trainer.DATASET_CONTRACT,
        "names": ["search_target"],
        "training_ready": False,
        "gazebo_capture": False,
        "dataset_yaml": yaml.name,
        "dataset_yaml_sha256": builder.trainer.sha256(yaml),
        "frames": frames,
        "frame_count": len(frames),
        "clips": clips,
    }
    manifest = base / "manifest.json"
    _write(manifest, document)
    return root, manifest, document


def _build(source, **kwargs) -> tuple[dict, Path]:
    root, manifest, _document = source
    output = root / "shadow"
    result = builder.build_shadow_cases(
        manifest,
        builder.trainer.sha256(manifest),
        output,
        prompt_issued_at=ISSUED,
        **kwargs,
    )
    return result, output


def _change(source, index: int, kind: str, pixels) -> None:
    _root, manifest, document = source
    frame = document["frames"][index]
    path = manifest.parent / frame[f"{kind}_path"]
    _png(path, pixels)
    frame[f"{kind}_sha256"] = builder.trainer.sha256(path)
    if kind == "image":
        frame["rgb_payload_sha256"] = frame["image_sha256"]
    _write(manifest, document)


def test_eight_cases_copy_exact_rgb_and_preserve_original_binding(source):
    root, manifest, original = source
    before = builder.trainer.sha256(manifest)
    result, output = _build(source)
    sealed = output / "manifest.json"
    assert builder.validate_shadow_cases(sealed, builder.trainer.sha256(sealed)) == result
    assert result["case_count"] == 8
    assert result["unique_rgb_count"] == 7
    assert result["source_split"] == "train"
    assert result["aircraft_context"] is None
    assert result["prompt_issued_at_semantics"] == "builder_host_utc_not_sensor_utc"
    assert all(result[key] is False for key in builder.FALSE_FLAGS)
    originals = {frame["frame_id"]: frame for frame in original["frames"]}
    for case in result["cases"]:
        assert case["prompt_issued_at"] == ISSUED
        assert case["context_missing_fields"] == list(builder.MISSING_FIELDS)
        assert case["evaluation_metadata"]["gold_flight_action"] is None
        for frame in case["frames"]:
            raw = originals[frame["frame_id"]]
            original_image = manifest.parent / raw["image_path"]
            copied = root / frame["path"]
            assert copied != original_image
            assert copied.read_bytes() == original_image.read_bytes()
            assert frame["sha256"] == raw["image_sha256"]
            assert frame["split"] == raw["split"] == "train"
            assert frame["captured_at_s"] == raw["captured_at_s"]
            assert frame["source_clip_id"] == raw["clip_id"]
            assert "offline-rendered" in frame["clock_domain"]
    assert builder.trainer.sha256(manifest) == before


def test_real_comparison_pairs_have_same_rgb_or_prompt_as_declared(source):
    result, _ = _build(source)
    cases = result["cases"]
    assert cases[0]["frames"] == cases[1]["frames"] == cases[2]["frames"] == cases[3]["frames"]
    assert len({case["prompt"] for case in cases[:4]}) == 4
    assert cases[4]["prompt"] == cases[5]["prompt"] == cases[6]["prompt"]
    assert cases[4]["frames"][-1] == cases[5]["frames"][-1]
    assert len(cases[4]["frames"]) == 1 and len(cases[5]["frames"]) == 4
    assert len(cases[6]["frames"]) == 6
    assert (
        cases[4]["evaluation_metadata"]["source_provenance"][-1]["measured_condition"]
        == "fully_occluded"
    )
    assert cases[6]["evaluation_metadata"]["source_provenance"][-1]["measured_condition"] == "clear"


def test_offline_truth_and_provenance_are_excluded_from_model_allowlist(source):
    result, _ = _build(source)
    for case in result["cases"]:
        supplied = builder.model_case_input(case)
        assert set(supplied) == {"prompt", "frames", "context_missing_fields"}
        serialized = json.dumps(supplied)
        for forbidden in (
            "mask",
            "label",
            "world",
            "measured_condition",
            "target_geometry",
            "gold_",
        ):
            assert forbidden not in serialized
        assert all(set(frame) == builder.FRAME_FIELDS for frame in supplied["frames"])
        assert "planned_condition" not in json.dumps(case["evaluation_metadata"])


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e999", '{"x":1,"x":2}'])
def test_strict_json_rejects_nonfinite_and_duplicate_keys(bad):
    payload = bad if bad.startswith("{") else '{"value":' + bad + "}"
    with pytest.raises(ValueError):
        builder._strict_json(payload.encode())


def test_manifest_byte_pin_and_existing_output_fail_before_any_write(source):
    root, manifest, _ = source
    with pytest.raises(ValueError, match="identity"):
        builder.build_shadow_cases(manifest, "0" * 64, root / "shadow")
    assert not (root / "shadow").exists()
    (root / "shadow").mkdir()
    with pytest.raises(FileExistsError):
        _build(source)


@pytest.mark.parametrize("selection", [(8, 0), (0, 0), (-1,), (True,), tuple(range(9)), (90,), ()])
def test_invalid_frame_selection_refused_before_output(source, selection):
    plans = (
        replace(builder.DEFAULT_CASE_PLANS[0], frame_indices=selection),
        *builder.DEFAULT_CASE_PLANS[1:],
    )
    with pytest.raises(ValueError, match="frame"):
        _build(source, plans=plans)
    assert not (source[0] / "shadow").exists()


@pytest.mark.parametrize("split", ["val", "test"])
def test_original_holdout_cannot_be_used_to_design_development_cases(source, split):
    plans = (
        replace(builder.DEFAULT_CASE_PLANS[0], clip_id=f"{split}-clip"),
        *builder.DEFAULT_CASE_PLANS[1:],
    )
    with pytest.raises(ValueError, match="only original train"):
        _build(source, plans=plans)
    assert not (source[0] / "shadow").exists()


@pytest.mark.parametrize("tamper", ["clock", "time", "split", "model", "clip_inventory"])
def test_bad_original_scope_or_timing_refused_before_output(source, tamper):
    _root, manifest, document = source
    if tamper == "clock":
        document["frames"][3]["timestamp_semantics"] = "sensor_utc"
    elif tamper == "time":
        document["frames"][3]["captured_at_s"] = 0
    elif tamper == "split":
        document["frames"][3]["split"] = "test"
    elif tamper == "model":
        document["frames"][3]["target_model"]["sha256"] = "0" * 64
    else:
        document["clips"][0]["world_sha256"] = "0" * 64
    _write(manifest, document)
    with pytest.raises(ValueError):
        _build(source)
    assert not (source[0] / "shadow").exists()


@pytest.mark.parametrize("mode", ["gray", "rgba", "size", "uint16"])
def test_actual_png_must_be_rgb8_and_declared_dimensions(source, mode):
    import numpy as np

    shapes = {"gray": (8, 10), "rgba": (8, 10, 4), "size": (9, 10, 3), "uint16": (8, 10, 3)}
    pixels = np.full(
        shapes[mode],
        500 if mode == "uint16" else 200,
        dtype=np.uint16 if mode == "uint16" else np.uint8,
    )
    _change(source, 0, "image", pixels)
    with pytest.raises(ValueError, match="RGB"):
        _build(source)
    assert not (source[0] / "shadow").exists()


@pytest.mark.parametrize("tamper", ["label", "visible", "condition", "pixels"])
def test_measured_masks_not_plan_or_self_report_bind_evaluation_metadata(source, tamper):
    _root, manifest, document = source
    frame = document["frames"][0]
    if tamper == "label":
        path = manifest.parent / frame["label_path"]
        path.write_text("0 0.9 0.9 0.1 0.1\n")
        frame["label_sha256"] = builder.trainer.sha256(path)
    elif tamper == "visible":
        frame["target_visible"] = False
    elif tamper == "condition":
        frame["visibility_measurement"]["measured_condition"] = "fully_occluded"
    else:
        frame["visible_target_pixels"] = 0
    _write(manifest, document)
    with pytest.raises(ValueError):
        _build(source)
    assert not (source[0] / "shadow").exists()


@pytest.mark.parametrize(
    "field", ["split", "clock_domain", "source_clip_id", "captured_at_s", "sha256", "mask"]
)
def test_saved_case_binding_or_truth_injection_rejected(source, field):
    result, output = _build(source)
    frame = result["cases"][0]["frames"][0]
    frame[field] = 100 if field == "captured_at_s" else "tamper"
    _write(output / "manifest.json", result)
    with pytest.raises(ValueError):
        builder.validate_shadow_cases(output / "manifest.json")


def test_saved_case_cross_clip_history_rejected_even_when_each_frame_is_bound(source):
    result, output = _build(source)
    result["cases"][5]["frames"].append(result["cases"][7]["frames"][0])
    result["cases"][5]["frames"][-1]["captured_at_s"] = 2
    result["cases"][5]["evaluation_metadata"]["source_provenance"].append(
        result["cases"][7]["evaluation_metadata"]["source_provenance"][0]
    )
    _write(output / "manifest.json", result)
    with pytest.raises(ValueError):
        builder.validate_shadow_cases(output / "manifest.json")


def test_successful_validator_then_pending_tamper_cannot_publish(source, monkeypatch):
    original = builder.validate_shadow_cases

    def tamper(path, expected_sha256=None):
        result = original(path, expected_sha256)
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(builder, "validate_shadow_cases", tamper)
    with pytest.raises(ValueError, match="identity changed"):
        _build(source)
    assert not (source[0] / "shadow" / "manifest.json").exists()


@pytest.mark.parametrize("kind", ["manifest", "rgb", "helper", "world"])
def test_post_validation_source_drift_cannot_publish(source, monkeypatch, kind):
    root, manifest, document = source
    original = builder.validate_shadow_cases
    target = {
        "manifest": manifest,
        "rgb": manifest.parent / document["frames"][0]["image_path"],
        "helper": root / "scripts/replay_onboard_visual_clips.py",
        "world": root / document["frames"][0]["world_path"],
    }[kind]

    def tamper(path, expected_sha256=None):
        result = original(path, expected_sha256)
        target.write_bytes(target.read_bytes() + b" ")
        return result

    monkeypatch.setattr(builder, "validate_shadow_cases", tamper)
    with pytest.raises(ValueError, match="identity changed"):
        _build(source)
    assert not (root / "shadow" / "manifest.json").exists()


def test_imported_helper_identity_must_match_canonical_copy(source):
    root, _manifest, _document = source
    path = root / "scripts/replay_onboard_visual_clips.py"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="identity changed"):
        _build(source)
    assert not (root / "shadow").exists()


@pytest.mark.parametrize("issued", ["2026-10-07T06:00:00", "2026-10-07T06:00:00+08:00"])
def test_prompt_host_time_is_explicit_utc_not_sensor_time(source, issued):
    root, manifest, _ = source
    with pytest.raises(ValueError, match="host UTC"):
        builder.build_shadow_cases(
            manifest, builder.trainer.sha256(manifest), root / "shadow", prompt_issued_at=issued
        )
    assert not (root / "shadow").exists()
