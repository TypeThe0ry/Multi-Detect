from __future__ import annotations

import io
import json
import re
import shutil
from copy import deepcopy

import pytest

from scripts import build_onboard_vlm_grounding_dataset as builder


def _json(path, document):
    path.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")


@pytest.fixture
def source(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    root = tmp_path / "repository"
    root.mkdir()
    for relative in builder.SOURCE_PATHS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(builder.ROOT / relative, path)
    for module in (builder, builder.shadow, builder.audit, builder.trainer, builder.evaluator):
        monkeypatch.setattr(module, "ROOT", root)
    base = root / "source"
    base.mkdir()
    frames, clips = [], []
    for group, (split, clip_id) in enumerate(
        (
            ("train", "training-scene"),
            ("val", "validation-scene"),
            ("test", "testing-scene"),
            ("train", "hidden-training-scene"),
        )
    ):
        world = root / f"world-{split}.sdf"
        world.write_text(f"<sdf><world name='{split}'/></sdf>")
        target = root / f"target-{'unseen' if split == 'test' else 'seen'}.sdf"
        target.write_text(
            "<sdf><model name='target'><link name='body'><visual name='body'>"
            f"<geometry><box><size>{2 if split == 'test' else 1} 1 1</size>"
            "</box></geometry></visual></link></model></sdf>"
        )
        target_path = target.relative_to(root).as_posix()
        model = {
            "path": target_path,
            "sha256": builder.trainer.sha256(target),
            "geometry_sha256": builder.shadow.curriculum.geometry_sha256(
                builder.shadow.curriculum.load_target(root, target_path)
            ),
        }
        sources = {world.name: builder.trainer.sha256(world), target_path: model["sha256"]}
        ids = []
        for index in range(5):
            frame_id = f"{clip_id}-frame-{index:03d}"
            ids.append(frame_id)
            mask = np.zeros((48, 64), np.uint8)
            amodal = np.zeros_like(mask)
            amodal[4:20, 8:24] = 255
            if index == 0:
                mask[4:20, 8:24] = 255  # Qualified 256-pixel reference.
            elif index == 2:
                mask[4:12, 8:24] = 255  # 128 pixels, fraction .5.
            elif index == 3:
                mask[4:11, 8:17] = 255  # 63 pixels: ambiguous, not negative.
            elif index == 4:
                amodal[4:44, 8:48] = 255
                mask[4:12, 8:24] = 255  # 128 pixels but fraction .08: ambiguous.
            if group == 3:
                mask[:] = 0  # Cannot borrow an amodal or another clip's reference.
            visible = int(np.count_nonzero(mask))
            total = int(np.count_nonzero(amodal))
            bbox = builder.shadow.renderer.visible_bbox(mask.tobytes(), 64, 48)
            frame = {
                "frame_id": frame_id,
                "clip_id": clip_id,
                "frame_index": index,
                "split": split,
                "width": 64,
                "height": 48,
                "world_path": world.name,
                "world_sha256": sources[world.name],
                "source_hashes": deepcopy(sources),
                "target_model": deepcopy(model),
                "captured_at_s": index / 8,
                "dt": 0 if index == 0 else 1 / 8,
                "timestamp_semantics": builder.shadow.CLOCK_SEMANTICS,
                "target_present": True,
                "target_visible": visible > 0,
                "visible_target_pixels": visible,
                "visible_bbox_xyxy_px": None if bbox is None else list(bbox),
                "planned_condition": "PLAN_SECRET_MUST_NOT_BE_MODEL_INPUT",
                "visibility_measurement": {
                    "amodal_target_pixels": total,
                    "visible_amodal_overlap_pixels": visible,
                    "visible_fraction": visible / total,
                    "measured_condition": "fully_occluded"
                    if visible == 0
                    else ("clear" if visible == total else "partially_occluded"),
                },
            }
            for kind in ("image", "label", "mask", "amodal_mask"):
                path = base / f"{frame_id}-{kind}{'.txt' if kind == 'label' else '.png'}"
                if kind == "label":
                    path.write_text(builder.shadow.renderer.yolo_label(bbox, 64, 48))
                else:
                    pixels = mask if kind == "mask" else amodal
                    if kind == "image":
                        pixels = np.full((48, 64, 3), 20 + group * 30 + index, np.uint8)
                        pixels[mask != 0] = (15, 110, 220)
                    assert cv2.imwrite(str(path), pixels)
                frame[f"{kind}_path"] = path.name
                frame[f"{kind}_sha256"] = builder.trainer.sha256(path)
            frame["rgb_payload_sha256"] = frame["image_sha256"]
            frames.append(frame)
        clips.append(
            {
                "clip_id": clip_id,
                "split": split,
                "frame_count": 5,
                "frame_ids": ids,
                "fps": 8,
                "captured_at_start_s": 0,
                "captured_at_end_s": 0.5,
                "timestamp_semantics": builder.shadow.CLOCK_SEMANTICS,
                "world_path": world.name,
                "world_sha256": sources[world.name],
                "source_hashes": sources,
            }
        )
    yaml = base / "dataset.yaml"
    _json(yaml, {"names": {"0": "search_target"}})
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
    _json(manifest, document)
    return root, manifest, document


def _build(source):
    root, manifest, _ = source
    result = builder.build_grounding_dataset(
        manifest, builder.trainer.sha256(manifest), root / "draft"
    )
    return result, root / "draft" / "manifest.pending-review.json"


def _receipt(document, manifest):
    return {
        "schema_version": 1,
        "contract": builder.REVIEW_CONTRACT,
        "dataset_sha256": builder.trainer.sha256(manifest),
        "dataset_bytes": manifest.stat().st_size,
        "reviewer": "independent-fixture-reviewer",
        "reviewed_sample_ids": sorted(sample["sample_id"] for sample in document["samples"]),
        "approved_for_development_training": True,
        **{key: True for key in builder.REVIEW_CHECKS},
    }


def test_draft_preserves_splits_excludes_ambiguity_and_skips_hidden_reference(source):
    result, manifest = _build(source)
    assert builder.validate_grounding_dataset(manifest, builder.trainer.sha256(manifest)) == result
    assert result["split_samples"] == {"train": 2, "val": 2, "test": 2}
    assert result["split_clip_counts"] == {"train": 1, "val": 1, "test": 1}
    assert result["sample_count"] == 6 and result["ambiguous_excluded_count"] == 6
    assert result["skipped_clip_count"] == 1
    assert all(result[key] is False for key in builder.FALSE_FLAGS)
    assert result["review_status"] == "pending_independent_fact_review"
    assert not (manifest.parent / "manifest.json").exists()
    for sample in result["samples"]:
        assert sample["split"] == sample["provenance"]["original_split"]
        assert re.fullmatch(r"[0-9a-f]{32}", sample["sample_id"])
        assert re.fullmatch(r"[0-9a-f]{32}", sample["model_input"]["target_reference_id"])
        assert sample["assistant_target"]["current_target_visibility"] in {
            "visible",
            "not_confirmed",
        }
        assert sample["assistant_target"]["gps"] == sample["assistant_target"]["ned"] == "unknown"
        assert sample["assistant_target"]["aircraft_motion"] == "unknown"
        assert [image["role"] for image in sample["model_input"]["images"]] == [
            "reference",
            "current",
        ]


def test_reference_is_exact_half_open_rgb_crop_not_mask_or_amodal(source):
    from PIL import Image

    result, manifest = _build(source)
    original = source[2]["frames"][0]
    image = result["samples"][0]["model_input"]["images"][0]
    assert (image["width"], image["height"]) == (16, 16)
    with Image.open(source[1].parent / original["image_path"]) as full:
        crop = full.crop(tuple(original["visible_bbox_xyxy_px"]))
        with Image.open(manifest.parent / image["path"]) as actual:
            assert actual.mode == "RGB" and actual.size == crop.size
            assert actual.tobytes() == crop.tobytes()
    assert result["samples"][0]["provenance"]["reference"]["visible_target_pixels"] == 256


def test_model_allowlist_hides_metadata_paths_hashes_split_and_masks(source):
    from PIL import Image

    result, manifest = _build(source)
    sample = result["samples"][0]
    sample["provenance"]["secret"] = "TRUTH_SECRET"
    supplied = builder.model_sample_input(sample, manifest.parent)
    assert set(supplied) == {"prompt", "target_reference_id", "images", "context"}
    text = json.dumps(
        {
            **supplied,
            "images": [
                {key: value for key, value in image.items() if key != "image_bytes"}
                for image in supplied["images"]
            ],
        }
    )
    for secret in (
        "TRUTH_SECRET",
        "PLAN_SECRET",
        "source_clip",
        "training-scene",
        "mask",
        "geometry",
        "sha256",
        "condition",
        "split",
        "amodal",
    ):
        assert secret not in text
    assert all(set(image) == {"image_id", "role", "image_bytes"} for image in supplied["images"])
    for image in supplied["images"]:
        with Image.open(io.BytesIO(image["image_bytes"])) as actual:
            assert actual.mode == "RGB"


@pytest.mark.parametrize(
    "pixels,fraction,expected",
    [
        (0, None, "not_confirmed"),
        (127, 0.99, None),
        (128, 0.099, None),
        (128, 0.10, "visible"),
        (1, 0.001, None),
        (1000, 0.09, None),
    ],
)
def test_preregistered_visibility_policy_not_model_tuned(pixels, fraction, expected):
    assert (
        builder._label(
            {
                "visible_target_pixels": pixels,
                "visibility_measurement": {"visible_fraction": fraction},
            }
        )
        == expected
    )


@pytest.mark.parametrize(
    "pixels,fraction,box,expected",
    [
        (255, 1, [0, 0, 16, 16], False),
        (256, 0.249, [0, 0, 16, 16], False),
        (256, 0.25, [0, 0, 16, 16], True),
        (256, 1, [0, 0, 7, 40], False),
        (256, 1, [0, 0, 64, 48], False),
        (256, 1, None, False),
    ],
)
def test_preregistered_useful_reference_crop(pixels, fraction, box, expected):
    assert (
        builder._reference_ok(
            {
                "visible_target_pixels": pixels,
                "visible_bbox_xyxy_px": box,
                "visibility_measurement": {"visible_fraction": fraction},
            }
        )
        == expected
    )


def test_no_future_or_cross_clip_reference_loan(source):
    result, _ = _build(source)
    for sample in result["samples"]:
        reference, current = sample["provenance"]["reference"], sample["provenance"]["current"]
        assert reference["clip_id"] == current["clip_id"]
        assert reference["captured_at_s"] < current["captured_at_s"]
    skipped = next(clip for clip in result["clip_inventory"] if clip["skip_reason"])
    assert skipped["source_clip_id"] == "hidden-training-scene"
    assert skipped["reference_frame_id"] is None and skipped["included_frame_ids"] == []


def test_existing_output_wrong_pin_and_three_image_history_fail_before_writes(source):
    root, manifest, _ = source
    with pytest.raises(ValueError, match="identity"):
        builder.build_grounding_dataset(manifest, "0" * 64, root / "draft")
    assert not (root / "draft").exists()
    with pytest.raises(ValueError, match="three-image"):
        builder.build_grounding_dataset(
            manifest, builder.trainer.sha256(manifest), root / "draft", include_two_frame=True
        )
    (root / "draft").mkdir()
    with pytest.raises(FileExistsError):
        _build(source)


@pytest.mark.parametrize("mutation", ["time", "clock", "cross_split", "bbox", "count", "label"])
def test_bad_original_clock_split_visible_count_bbox_or_label_fails_closed(source, mutation):
    root, manifest, document = source
    if mutation == "time":
        document["frames"][1]["captured_at_s"] = 0
    elif mutation == "clock":
        document["frames"][1]["timestamp_semantics"] = "pretended_sensor_clock"
    elif mutation == "cross_split":
        document["frames"][1]["split"] = "test"
    elif mutation == "bbox":
        document["frames"][0]["visible_bbox_xyxy_px"][2] += 1
    elif mutation == "count":
        document["frames"][0]["visible_target_pixels"] += 1
    else:
        frame = document["frames"][0]
        path = manifest.parent / frame["label_path"]
        path.write_text("0 0.5 0.5 0.2 0.2\n")
        frame["label_sha256"] = builder.trainer.sha256(path)
    _json(manifest, document)
    with pytest.raises(ValueError):
        _build(source)
    assert not (root / "draft").exists()


@pytest.mark.parametrize("asset", ["image", "mask", "amodal_mask", "label"])
def test_original_source_hash_change_rejected_after_draft(source, asset):
    _result, pending = _build(source)
    frame = source[2]["frames"][0]
    path = source[1].parent / frame[f"{asset}_path"]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="identity"):
        builder.validate_grounding_dataset(pending)


@pytest.mark.parametrize("field", ["split", "target", "prompt", "policy", "crop", "extra"])
def test_persisted_supervision_model_input_policy_or_crop_tampering_rejected(source, field):
    document, pending = _build(source)
    if field == "split":
        document["samples"][0]["split"] = "test"
    elif field == "target":
        document["samples"][0]["assistant_target"]["gps"] = "1,2"
    elif field == "prompt":
        document["samples"][0]["model_input"]["prompt"] = "fly"
    elif field == "policy":
        document["label_policy"]["positive_min_visible_pixels"] = 1
    elif field == "extra":
        document["samples"][0]["model_input"]["condition"] = "truth"
    else:
        image = document["samples"][0]["model_input"]["images"][0]
        path = pending.parent / image["path"]
        path.write_bytes(path.read_bytes() + b"changed")
    _json(pending, document)
    with pytest.raises(ValueError):
        builder.validate_grounding_dataset(pending)


def test_optimizer_requires_exact_independent_review_not_self_attestation(source):
    document, pending = _build(source)
    with pytest.raises(ValueError, match="review receipt"):
        builder.validate_grounding_dataset(pending, require_review=True)
    receipt_path = pending.parent / "independent-review.json"
    receipt = _receipt(document, pending)
    _json(receipt_path, receipt)
    assert (
        builder.validate_grounding_dataset(
            pending, review_receipt=receipt_path, require_review=True
        )
        == document
    )
    assert document["training_ready"] is False


@pytest.mark.parametrize("mutation", ["self", "missing_samples", "false", "wrong_hash", "extra"])
def test_review_receipt_fail_closed(source, mutation):
    document, pending = _build(source)
    receipt = _receipt(document, pending)
    if mutation == "self":
        receipt["reviewer"] = builder.BUILDER_ID
    elif mutation == "missing_samples":
        receipt["reviewed_sample_ids"].pop()
    elif mutation == "false":
        receipt["visible_mask_labels_checked"] = False
    elif mutation == "wrong_hash":
        receipt["dataset_sha256"] = "0" * 64
    else:
        receipt["flight_success"] = True
    path = pending.parent / "review.json"
    _json(path, receipt)
    with pytest.raises(ValueError, match="review"):
        builder.validate_grounding_dataset(pending, review_receipt=path, require_review=True)


def test_parent_bytes_and_producer_sources_bound_without_claiming_weight_transfer(source):
    root, manifest, _ = source
    parent = root / "parent.fixture"
    parent.write_bytes(b"fixture-not-trained-weights")
    reference = {"path": parent.name, "sha256": builder.trainer.sha256(parent)}
    document = builder.build_grounding_dataset(
        manifest, builder.trainer.sha256(manifest), root / "draft", parent_models=(reference,)
    )
    assert document["parent_models"] == [{**reference, "bytes": parent.stat().st_size}]
    pending = root / "draft" / "manifest.pending-review.json"
    parent.write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity"):
        builder.validate_grounding_dataset(pending)


def test_builder_source_drift_rejected(source):
    _document, pending = _build(source)
    path = source[0] / "scripts/build_onboard_vlm_grounding_dataset.py"
    path.write_bytes(path.read_bytes() + b"\n# source drift\n")
    with pytest.raises(ValueError, match="changed"):
        builder.validate_grounding_dataset(pending)
