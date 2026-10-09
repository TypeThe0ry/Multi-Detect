"""Receive/data fixtures, not independent fact review or model/flight evidence."""

from __future__ import annotations

import io
import json
from collections import Counter
from copy import deepcopy

import pytest
from PIL import Image

from scripts import build_onboard_vlm_grounding_increment_dataset as builder


def _put(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return builder.base._digest(payload)


def _png(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """Stub ONLY the prior independent review and SDF geometry extraction.

    Actual PNG bytes, masks, bbox/YOLO labels, crop pixels and all SHA checks
    still go through the production source helpers. This is not a real receipt.
    """
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.trainer, "ROOT", root)
    monkeypatch.setattr(builder.evaluator, "verify_geometry_provenance", lambda d: {})
    source = root / "source"
    frames = []
    categories = ["visible"] * 4 + ["fully_occluded"] * 3 + ["absent"] * 3 + ["ambiguous"]
    for index, category in enumerate([*categories, "val", "test"]):
        split = category if category in {"val", "test"} else "train"
        kind = "visible" if split != "train" else category
        image = Image.new("RGB", (64, 48), (index * 11, 17, 71))
        mask = Image.new("L", (64, 48))
        amodal = Image.new("L", (64, 48))
        bbox = None
        if kind != "absent":
            amodal.paste(255, (5, 5, 25, 25))
        if kind in {"visible", "ambiguous"}:
            bbox = (5, 5, 25 if kind == "visible" else 10, 25)
            mask.paste(255, bbox)
            image.paste((211, 77, 39), bbox)
        pixels = 400 if kind == "visible" else 100 if kind == "ambiguous" else 0
        amodal_pixels = 0 if kind == "absent" else 400
        condition = (
            "partially_occluded"
            if kind == "ambiguous"
            else ("clear" if kind == "visible" else kind)
        )
        world_path = f"worlds/{split}.sdf"
        world_sha = _put(root / world_path, f"{split} world fixture".encode())
        model_path = f"models/{'test' if split == 'test' else 'original'}.sdf"
        model_sha = _put(root / model_path, model_path.encode())
        geometry = builder.base._digest(model_path.encode())
        frame = {
            "frame_id": f"{split}-{index}",
            "split": split,
            "width": 64,
            "height": 48,
            "world_path": world_path,
            "world_sha256": world_sha,
            "source_hashes": {world_path: world_sha, model_path: model_sha},
            "target_model": {"path": model_path, "sha256": model_sha, "geometry_sha256": geometry},
            "target_visible": pixels > 0,
            "target_present": kind != "absent",
            "visible_target_pixels": pixels,
            "visible_bbox_xyxy_px": None if bbox is None else list(bbox),
            "curriculum": {
                "amodal_target_pixels": amodal_pixels,
                "visible_fraction": None if not amodal_pixels else pixels / 400,
                "visible_amodal_overlap_pixels": pixels,
                "measured_condition": condition,
            },
            "scene_primitives": [
                {
                    "name": "target/link/body",
                    "target": True,
                    "geometry": "box",
                    "dimensions_m": [2, 1, 1],
                },
                {
                    "name": "target/link/wheel",
                    "target": True,
                    "geometry": "cylinder",
                    "dimensions_m": [0.2, 0.1],
                },
                {
                    "name": "distractor/link/visual",
                    "target": False,
                    "geometry": "sphere",
                    "dimensions_m": [0.4],
                },
            ],
        }
        frame["visibility_measurement"] = {
            "visible_fraction": frame["curriculum"]["visible_fraction"],
        }
        for prefix, payload, suffix in (
            ("image", _png(image), "png"),
            ("mask", _png(mask), "png"),
            ("amodal_mask", _png(amodal), "png"),
            ("label", builder.evaluator.yolo_label(bbox, 64, 48).encode(), "txt"),
        ):
            relative = f"{prefix}/{split}-{index}.{suffix}"
            frame[f"{prefix}_path"] = relative
            frame[f"{prefix}_sha256"] = _put(source / relative, payload)
        frames.append(frame)
    source_document = {
        "contract": builder.trainer.DATASET_CONTRACT,
        "frame_count": len(frames),
        "frames": frames,
        "names": ["search_target"],
        "training_ready": False,
        "gazebo_capture": False,
        "dataset_yaml": "dataset.yaml",
    }
    source_document["dataset_yaml_sha256"] = _put(source / "dataset.yaml", b"fixture yaml")
    manifest = source / "manifest.json"
    source_sha = _put(manifest, json.dumps(source_document).encode())
    old_directory = root / "old"
    old_samples = []
    for frame in (frames[0], frames[-2], frames[-1]):
        payload = (source / frame["image_path"]).read_bytes()
        identifier = builder.base._digest(frame["frame_id"].encode())[:32]
        image_record = builder.base._image_record(identifier, "current", payload, 64, 48)
        ref = deepcopy(image_record)
        ref.update(
            image_id=builder.base._digest((identifier + "ref").encode())[:32],
            path=f"images/{identifier}-reference.png",
            role="reference",
        )
        for record in (ref, image_record):
            _put(old_directory / record["path"], payload)
        model_input = {
            "prompt": builder.base.PROMPT,
            "target_reference_id": identifier,
            "images": [ref, image_record],
            "context": deepcopy(builder.base.UNKNOWN_CONTEXT),
        }
        target = {
            "target_reference_id": identifier,
            "current_target_visibility": "visible",
            **builder.base.UNKNOWN_CONTEXT,
        }
        old_samples.append(
            {
                "sample_id": identifier,
                "split": frame["split"],
                "model_input": model_input,
                "assistant_target": target,
                "model_input_sha256": builder.canonical_sha256(model_input),
                "assistant_target_sha256": builder.canonical_sha256(target),
                "provenance": {
                    "current": {"image_sha256": frame["image_sha256"]},
                    "world": {"sha256": frame["world_sha256"]},
                    "target_geometry": {
                        "geometry_sha256": frame["target_model"]["geometry_sha256"]
                    },
                },
            }
        )
    old = {"samples": old_samples, "parent_models": [], "model_input_allowlist": ["RGB"]}
    old_manifest = old_directory / "manifest.pending-review.json"
    old_sha = _put(old_manifest, json.dumps(old).encode())
    review = old_directory / "review-receipt.json"
    _put(review, b"prior review stub - never used as a real training receipt")
    monkeypatch.setattr(
        builder.base, "validate_grounding_dataset", lambda *args, **kwargs: deepcopy(old)
    )
    adapter = root / "parent/adapter.safetensors"
    adapter_sha = _put(adapter, b"fixture adapter bytes - not a model")
    kwargs = {
        "source_manifest": manifest,
        "expected_source_sha256": source_sha,
        "base_manifest": old_manifest,
        "expected_base_sha256": old_sha,
        "base_review_receipt": review,
        "parent_adapter": adapter,
        "expected_parent_adapter_sha256": adapter_sha,
        "max_new_train_samples": 6,
        "seed": 1234,
    }
    return root, source_document, old, kwargs


def test_real_masks_crops_balanced_increment_and_exact_original_samples(corpus):
    root, _, old, kwargs = corpus
    manifest = builder.build_increment_dataset(root / "new", **kwargs)
    document = builder.validate_grounding_increment_dataset(manifest)
    assert document["samples"][:3] == old["samples"]
    assert document["split_samples"] == {"train": 7, "val": 1, "test": 1}
    assert document["sample_count"] == 9
    new = document["samples"][3:]
    assert Counter(s["provenance"]["selection_bucket"] for s in new) == {
        "visible": 2,
        "fully_occluded": 2,
        "absent": 2,
    }
    assert len(document["selection"]["exclusions"]["old_train_current_rgb"]) == 1
    assert len(document["selection"]["exclusions"]["ambiguous"]) == 1
    for sample in new:
        assert sample["split"] == "train"
        assert set(sample) == builder.base.SAMPLE_FIELDS
        assert all(len(image["image_id"]) == 32 for image in sample["model_input"]["images"])
        assert sample["model_input"]["context"] == builder.base.UNKNOWN_CONTEXT
        provenance = sample["provenance"]
        assert provenance["current"]["original_split"] == "train"
        assert provenance["reference"]["original_split"] == "train"
        assert provenance["current"]["world_sha256"] == provenance["reference"]["world_sha256"]
        assert (
            provenance["current"]["target_model"]["geometry_sha256"]
            == provenance["reference"]["target_model"]["geometry_sha256"]
        )
        image = sample["model_input"]["images"][0]
        with Image.open(root / "new" / image["path"]) as crop:
            assert crop.size == (20, 20)  # Half-open bbox: no extra +1.
        if provenance["selection_bucket"] == "visible":
            assert provenance["reference"]["image_sha256"] == provenance["current"]["image_sha256"]
            assert provenance["reference_pairing_semantics"].startswith("same_frame")
        else:
            assert provenance["current"]["visible_target_pixels"] == 0
            assert provenance["negative_scene_check"]["second_exact_geometry_entity_found"] is False
        assert provenance["semantic_identity_confirmed"] is False
    assert all(document[flag] is False for flag in builder.base.FALSE_FLAGS)
    assert not (root / "new/review-receipt.json").exists()
    with pytest.raises(ValueError, match="independent increment fact review"):
        builder.validate_grounding_increment_dataset(manifest, require_review=True)


@pytest.mark.parametrize("change", ["label", "model_input", "holdout", "selection", "flag"])
def test_mutated_manifest_rejected(corpus, change):
    root, _, _, kwargs = corpus
    manifest = builder.build_increment_dataset(root / "new", **kwargs)
    document = json.loads(manifest.read_bytes())
    if change == "label":
        document["samples"][-1]["assistant_target"]["current_target_visibility"] = "visible"
    elif change == "model_input":
        document["samples"][-1]["model_input"]["context"]["gps"] = [1, 2, 3]
    elif change == "holdout":
        document["samples"][1]["model_input"]["prompt"] = "modified holdout prompt"
    elif change == "selection":
        document["seed"] += 1
    else:
        document["training_ready"] = True
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="changed"):
        builder.validate_grounding_increment_dataset(manifest)


@pytest.mark.parametrize("kind", ["RGB", "mask", "parent_adapter", "old_manifest", "old_image"])
def test_actual_byte_mutation_rejected(corpus, kind):
    root, source, old, kwargs = corpus
    manifest = builder.build_increment_dataset(root / "new", **kwargs)
    paths = {
        "RGB": kwargs["source_manifest"].parent / source["frames"][1]["image_path"],
        "mask": kwargs["source_manifest"].parent / source["frames"][1]["mask_path"],
        "parent_adapter": kwargs["parent_adapter"],
        "old_manifest": kwargs["base_manifest"],
        "old_image": kwargs["base_manifest"].parent
        / old["samples"][0]["model_input"]["images"][0]["path"],
    }
    paths[kind].write_bytes(b"changed")
    with pytest.raises((ValueError, OSError)):
        builder.validate_grounding_increment_dataset(manifest)


@pytest.mark.parametrize("maximum", [True, 0, 2, 129, 3.0, "6"])
def test_bad_maximum_rejected_without_output(corpus, maximum):
    root, _, _, kwargs = corpus
    kwargs["max_new_train_samples"] = maximum
    with pytest.raises(ValueError, match="max_new_train_samples"):
        builder.build_increment_dataset(root / "new", **kwargs)
    assert not (root / "new").exists()


def test_no_overwrite_and_deterministic_content(corpus):
    root, _, _, kwargs = corpus
    a = builder.build_increment_dataset(root / "new-a", **kwargs)
    b = builder.build_increment_dataset(root / "new-b", **kwargs)
    da, db = json.loads(a.read_bytes()), json.loads(b.read_bytes())
    assert da["samples"] == db["samples"]
    with pytest.raises(FileExistsError):
        builder.build_increment_dataset(root / "new-a", **kwargs)


@pytest.mark.parametrize("kind", ["RGB", "world", "test_geometry"])
def test_old_holdout_contamination_rejected(corpus, kind):
    _, source, old, _ = corpus
    held = old["samples"][-1]
    if kind == "RGB":
        source["frames"][1]["image_sha256"] = held["provenance"]["current"]["image_sha256"]
    elif kind == "world":
        source["frames"][1]["world_sha256"] = held["provenance"]["world"]["sha256"]
    else:
        source["frames"][1]["target_model"]["geometry_sha256"] = held["provenance"][
            "target_geometry"
        ]["geometry_sha256"]
    with pytest.raises(ValueError, match="overlaps"):
        builder._choose_frames(source, old, 6, 1234)


def test_zero_mask_without_reference_and_second_geometry_fail_closed(corpus):
    _, source, old, _ = corpus
    negative = deepcopy(source["frames"][4])
    second = deepcopy(negative["scene_primitives"][:2])
    for primitive in second:
        primitive["name"] = primitive["name"].replace("target/", "second/")
        primitive["target"] = False
    negative["scene_primitives"].extend(second)
    with pytest.raises(ValueError, match="another complete target geometry"):
        builder._negative_scene(negative)
    for frame in source["frames"]:
        if frame["split"] == "train":
            frame["visible_target_pixels"] = 0
            frame["visible_bbox_xyxy_px"] = None
    with pytest.raises(ValueError, match="insufficient"):
        builder._choose_frames(source, old, 6, 1234)


def test_trainer_compatible_exact_two_image_and_no_truth_in_model_input(corpus):
    root, _, _, kwargs = corpus
    manifest = builder.build_increment_dataset(root / "new", **kwargs)
    document = json.loads(manifest.read_bytes())
    for sample in document["samples"][3:]:
        visible = builder.base.model_sample_input(sample, manifest.parent)
        assert set(visible) == {"prompt", "target_reference_id", "images", "context"}
        assert len(visible["images"]) == 2
        assert "mask" not in str(visible.keys())


@pytest.mark.parametrize("self_review", [True, False])
def test_review_requires_exact_external_bytes_and_checks(corpus, self_review):
    root, _, _, kwargs = corpus
    manifest = builder.build_increment_dataset(root / "new", **kwargs)
    raw = manifest.read_bytes()
    document = json.loads(raw)
    receipt = {
        "contract": builder.REVIEW_CONTRACT,
        "reviewer": builder.BUILDER_ID if self_review else "independent-unit-fixture",
        "dataset_sha256": builder.base._digest(raw),
        "dataset_bytes": len(raw),
        "reviewed_sample_ids": sorted(s["sample_id"] for s in document["samples"]),
        "approved_for_development_training": True,
        **{key: True for key in builder.REVIEW_CHECKS},
    }
    path = root / "unit-review.json"
    path.write_text(json.dumps(receipt))
    if self_review:
        with pytest.raises(ValueError, match="independent increment review"):
            builder.validate_grounding_increment_dataset(
                manifest, review_receipt=path, require_review=True
            )
    else:
        assert (
            builder.validate_grounding_increment_dataset(
                manifest, review_receipt=path, require_review=True
            )
            == document
        )
        receipt["heldout_inputs_unchanged"] = False
        path.write_text(json.dumps(receipt))
        with pytest.raises(ValueError, match="independent increment review"):
            builder.validate_grounding_increment_dataset(
                manifest, review_receipt=path, require_review=True
            )
