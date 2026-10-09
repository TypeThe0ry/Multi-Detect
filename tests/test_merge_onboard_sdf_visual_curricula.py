from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import merge_onboard_sdf_visual_curricula as merger


def _write(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    paths = ["scripts/merge_onboard_sdf_visual_curricula.py", *merger.SOURCE_MODULES]
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(merger.ROOT / relative, path)
    monkeypatch.setattr(merger, "ROOT", root)
    monkeypatch.setattr(merger.trainer, "ROOT", root)
    monkeypatch.setattr(merger.evaluator, "ROOT", root)
    return root


def _png(path: Path, pixels) -> str:
    import cv2

    assert cv2.imwrite(str(path), pixels)
    return merger.trainer.sha256(path)


def _dataset(root: Path, key: str, *, clip: bool = False, offset: int = 0) -> tuple[Path, dict]:
    import numpy as np

    base = root / "artifacts" / key
    base.mkdir(parents=True)
    frames, clips = [], []
    for split_index, split in enumerate(merger.SPLITS):
        world = root / "worlds" / key / f"{split}.sdf"
        world.parent.mkdir(parents=True, exist_ok=True)
        world.write_text(f"<sdf><world name='{key}-{split}'/></sdf>")
        target = root / "models" / f"{'unseen' if split == 'test' else 'seen'}.sdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "<sdf><model name='unit-shape'><link name='body'><visual name='shape'>"
            f"<geometry><box><size>{2 if split == 'test' else 1} 1 1</size></box></geometry>"
            "</visual></link></model></sdf>"
        )
        relative_target = target.relative_to(root).as_posix()
        model = {
            "path": relative_target,
            "sha256": merger.trainer.sha256(target),
            "geometry_sha256": merger.curriculum.geometry_sha256(
                merger.curriculum.load_target(root, relative_target)
            ),
        }
        clip_frames = []
        for index in range(2 if clip else 1):
            identifier = f"{split}-{index}"
            mask = np.zeros((8, 10), dtype=np.uint8)
            mask[2:4, 3:5] = 255
            bbox = merger.renderer.visible_bbox(mask.reshape(-1).tobytes(), 10, 8)
            measured = {
                "measured_condition": "clear",
                "visible_target_pixels": 4,
                "amodal_target_pixels": 4,
                "visible_amodal_overlap_pixels": 4,
                "visible_pixels_outside_amodal": 0,
                "occluded_amodal_pixels": 0,
                "visible_fraction": 1.0,
                "visible_pixel_count_ratio": 1.0,
                "amodal_reference_in_frame": True,
            }
            frame = {
                "frame_id": identifier,
                "split": split,
                "width": 10,
                "height": 8,
                "world_path": world.relative_to(root).as_posix(),
                "world_sha256": merger.trainer.sha256(world),
                "source_hashes": {
                    world.relative_to(root).as_posix(): merger.trainer.sha256(world),
                    relative_target: model["sha256"],
                },
                "target_model": deepcopy(model),
                "target_present": True,
                "target_visible": True,
                "visible_target_pixels": 4,
                "visible_bbox_xyxy_px": list(bbox),
                "planned_condition": "clear",
                "offline_truth_only": True,
            }
            for kind in merger.FILE_KINDS:
                path = base / f"{identifier}-{kind}{'.txt' if kind == 'label' else '.png'}"
                if kind == "label":
                    path.write_text(merger.renderer.yolo_label(bbox, 10, 8))
                elif kind == "image":
                    _png(path, np.full((8, 10, 3), 20 + offset + split_index * 5 + index, np.uint8))
                else:
                    _png(path, mask)
                frame[f"{kind}_path"] = path.name
                frame[f"{kind}_sha256"] = merger.trainer.sha256(path)
            frame["rgb_payload_sha256"] = frame["image_sha256"]
            if clip:
                frame.update(
                    {
                        "clip_id": f"{split}-clip",
                        "frame_index": index,
                        "captured_at_s": index / 8,
                        "dt": 0 if index == 0 else 1 / 8,
                        "visibility_measurement": measured,
                    }
                )
            else:
                frame["curriculum"] = {
                    "target_model_path": model["path"],
                    "target_model_sha256": model["sha256"],
                    "target_geometry_sha256": model["geometry_sha256"],
                    "planned_condition": "clear",
                    **measured,
                }
            frames.append(frame)
            clip_frames.append(identifier)
        if clip:
            clips.append(
                {
                    "clip_id": f"{split}-clip",
                    "split": split,
                    "frame_count": 2,
                    "frame_ids": clip_frames,
                    "fps": 8,
                    "captured_at_start_s": 0,
                    "captured_at_end_s": 1 / 8,
                }
            )
    yaml = base / "dataset.yaml"
    _write(yaml, {"names": {"0": "search_target"}})
    document = {
        "contract": merger.trainer.DATASET_CONTRACT,
        "names": ["search_target"],
        "training_ready": False,
        "gazebo_capture": False,
        "dataset_yaml": yaml.name,
        "dataset_yaml_sha256": merger.trainer.sha256(yaml),
        "frame_count": len(frames),
        "frames": frames,
    }
    if clip:
        document["clips"] = clips
    manifest = base / "manifest.json"
    _write(manifest, document)
    return manifest, document


def _change_payload(manifest: Path, document: dict, frame: dict, kind: str, payload: bytes) -> None:
    path = manifest.parent / frame[f"{kind}_path"]
    path.write_bytes(payload)
    frame[f"{kind}_sha256"] = merger.trainer.sha256(path)
    if kind == "image":
        frame["rgb_payload_sha256"] = frame["image_sha256"]
    _write(manifest, document)


def test_independent_copy_preserves_splits_masks_geometry_and_old_trainer_contract(repository):
    one, _ = _dataset(repository, "static")
    two, _ = _dataset(repository, "clips", clip=True, offset=50)
    before = {path: merger.trainer.sha256(path) for path in (one, two)}
    output = repository / "merged"
    result = merger.merge_datasets([one, two], output)
    verified, _ = merger.trainer.verify_dataset(output / "manifest.json")
    assert verified == result
    assert result["split_frames"] == {"train": 3, "val": 3, "test": 3}
    assert len({frame["frame_id"] for frame in result["frames"]}) == 9
    assert len(result["target_geometries"]) == 2
    assert result["test_geometry_disjoint_from_train_and_val"] is True
    assert all(result[key] is False for key in merger.FALSE_FLAGS)
    for frame in result["frames"]:
        provenance = frame["merge_provenance"]
        assert provenance["original_split"] == frame["split"]
        assert "curriculum" in frame
        for kind in merger.FILE_KINDS:
            copied = output / frame[f"{kind}_path"]
            source = repository / provenance["manifest"]["path"]
            original = source.parent / f"{provenance['frame_id']}-{kind}{copied.suffix}"
            assert copied.read_bytes() == original.read_bytes()
            assert copied.resolve() != original.resolve()
    assert before == {path: merger.trainer.sha256(path) for path in (one, two)}


@pytest.mark.parametrize("claim", ["names", "gazebo_capture", "candidate_promoted", "flight"])
def test_class_alias_or_false_capture_and_promotion_rejected_before_output(repository, claim):
    manifest, document = _dataset(repository, "input")
    document[claim] = ["car"] if claim == "names" else True
    _write(manifest, document)
    output = repository / "merged"
    with pytest.raises(ValueError):
        merger.merge_datasets([manifest], output)
    assert not output.exists()


@pytest.mark.parametrize("tamper", ["label", "bbox", "fraction", "count", "condition", "visible"])
def test_actual_mask_supervision_not_planned_visibility_defines_labels(repository, tamper):
    manifest, document = _dataset(repository, "input")
    frame = document["frames"][0]
    if tamper == "label":
        _change_payload(manifest, document, frame, "label", b"0 0.8 0.8 0.1 0.1\n")
    elif tamper == "bbox":
        frame["visible_bbox_xyxy_px"] = [0, 0, 2, 2]
    elif tamper == "fraction":
        frame["curriculum"]["visible_fraction"] = 0.2
    elif tamper == "count":
        frame["visible_target_pixels"] = 1
    elif tamper == "condition":
        frame["curriculum"]["measured_condition"] = "fully_occluded"
    else:
        frame["target_visible"] = False
    _write(manifest, document)
    output = repository / "merged"
    with pytest.raises(ValueError):
        merger.merge_datasets([manifest], output)
    assert not output.exists()


@pytest.mark.parametrize("mask_kind", ["mask", "amodal_mask"])
def test_nonbinary_masks_are_rejected(repository, mask_kind):
    import cv2

    manifest, document = _dataset(repository, "input")
    frame = document["frames"][0]
    path = manifest.parent / frame[f"{mask_kind}_path"]
    mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    mask[2, 3] = 128
    _png(path, mask)
    frame[f"{mask_kind}_sha256"] = merger.trainer.sha256(path)
    _write(manifest, document)
    with pytest.raises(ValueError, match="binary"):
        merger.merge_datasets([manifest], repository / "merged")


def test_visible_pixels_outside_amodal_are_rejected(repository):
    import cv2

    manifest, document = _dataset(repository, "input")
    frame = document["frames"][0]
    path = manifest.parent / frame["amodal_mask_path"]
    mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    mask[2, 3], mask[6, 6] = 0, 255
    _png(path, mask)
    frame["amodal_mask_sha256"] = merger.trainer.sha256(path)
    _write(manifest, document)
    with pytest.raises(ValueError, match="outside amodal"):
        merger.merge_datasets([manifest], repository / "merged")


@pytest.mark.parametrize("condition", ["absent", "fully_occluded", "out_of_frame"])
def test_invisible_actual_masks_have_empty_labels_not_planned_boxes(repository, condition):
    import numpy as np

    manifest, document = _dataset(repository, "input")
    frame = document["frames"][0]
    amodal = 4 if condition == "fully_occluded" else 0
    for kind in ("mask", "amodal_mask"):
        if kind == "amodal_mask" and amodal:
            continue
        path = manifest.parent / frame[f"{kind}_path"]
        _png(path, np.zeros((8, 10), dtype=np.uint8))
        frame[f"{kind}_sha256"] = merger.trainer.sha256(path)
    _change_payload(manifest, document, frame, "label", b"")
    frame.update(
        target_present=condition != "absent",
        target_visible=False,
        visible_target_pixels=0,
        visible_bbox_xyxy_px=None,
    )
    frame["curriculum"].update(
        measured_condition=condition,
        visible_target_pixels=0,
        amodal_target_pixels=amodal,
        visible_amodal_overlap_pixels=0,
        visible_pixels_outside_amodal=0,
        occluded_amodal_pixels=amodal,
        visible_fraction=0.0 if amodal else None,
        visible_pixel_count_ratio=0.0 if amodal else None,
        amodal_reference_in_frame=bool(amodal),
    )
    # The original planned condition remains clear; only measured pixels
    # determine the actual supervision and its summary bucket.
    _write(manifest, document)
    output = repository / "merged"
    result = merger.merge_datasets([manifest], output)
    assert result["measured_condition_counts"]["train"] == {condition: 1}
    copied = result["frames"][0]
    assert (output / copied["label_path"]).read_bytes() == b""
    assert copied["visible_bbox_xyxy_px"] is None
    assert copied["curriculum"]["planned_condition"] == "clear"


@pytest.mark.parametrize("leak", ["rgb", "world_path", "world_content"])
def test_cross_manifest_split_contamination_is_rejected(repository, leak):
    one, first = _dataset(repository, "first")
    two, second = _dataset(repository, "second", offset=50)
    train, validation = first["frames"][0], second["frames"][1]
    if leak == "rgb":
        _change_payload(
            two, second, validation, "image", (one.parent / train["image_path"]).read_bytes()
        )
    elif leak == "world_path":
        validation["world_path"] = train["world_path"]
        validation["world_sha256"] = train["world_sha256"]
        validation["source_hashes"][train["world_path"]] = train["world_sha256"]
    else:
        target = repository / validation["world_path"]
        target.write_bytes((repository / train["world_path"]).read_bytes())
        validation["world_sha256"] = merger.trainer.sha256(target)
        validation["source_hashes"][validation["world_path"]] = validation["world_sha256"]
    _write(two, second)
    # These are independently valid old-trainer manifests. Only the merged
    # global audit can discover the newly introduced inter-corpus collision.
    merger.trainer.verify_dataset(one)
    merger.trainer.verify_dataset(two)
    output = repository / "merged"
    with pytest.raises(ValueError, match="cross"):
        merger.merge_datasets([one, two], output)
    assert not output.exists()


@pytest.mark.parametrize("tamper", ["fingerprint", "model_path", "copied_seen_geometry"])
def test_actual_heldout_geometry_not_declared_path_or_fingerprint(repository, tamper):
    manifest, document = _dataset(repository, "input")
    test, train = document["frames"][2], document["frames"][0]
    if tamper == "fingerprint":
        test["target_model"]["geometry_sha256"] = "f" * 64
        test["curriculum"]["target_geometry_sha256"] = "f" * 64
    elif tamper == "model_path":
        test["curriculum"]["target_model_path"] = train["target_model"]["path"]
    else:
        copied = repository / "models" / "different-filename.sdf"
        copied.write_bytes((repository / train["target_model"]["path"]).read_bytes())
        digest = merger.trainer.sha256(copied)
        relative = copied.relative_to(repository).as_posix()
        test["target_model"] = {
            "path": relative,
            "sha256": digest,
            "geometry_sha256": train["target_model"]["geometry_sha256"],
        }
        test["curriculum"].update(
            target_model_path=relative,
            target_model_sha256=digest,
            target_geometry_sha256=train["target_model"]["geometry_sha256"],
        )
        test["source_hashes"][relative] = digest
    _write(manifest, document)
    with pytest.raises(ValueError, match="geometry|identity"):
        merger.merge_datasets([manifest], repository / "merged")


def test_clip_frame_order_is_not_silently_changed(repository):
    manifest, document = _dataset(repository, "input", clip=True)
    document["clips"][0]["frame_ids"].reverse()
    _write(manifest, document)
    with pytest.raises(ValueError, match="persisted frame order"):
        merger.merge_datasets([manifest], repository / "merged")


@pytest.mark.parametrize("kind", ["manifest", "imported_helper", "copied_amodal"])
def test_parse_and_copy_races_do_not_seal_manifest(repository, monkeypatch, kind):
    manifest, _ = _dataset(repository, "input")
    output = repository / "merged"
    if kind in {"manifest", "imported_helper"}:
        original = merger.trainer.verify_dataset

        def mutate(path):
            result = original(path)
            victim = (
                manifest
                if kind == "manifest"
                else repository / "scripts/render_onboard_sdf_blender_curriculum.py"
            )
            victim.write_bytes(victim.read_bytes() + b"\n")
            return result

        monkeypatch.setattr(merger.trainer, "verify_dataset", mutate)
    else:
        original = merger._audit_pixels
        calls = 0

        def mutate(frame, base, cv2):
            nonlocal calls
            result = original(frame, base, cv2)
            if base == output:
                calls += 1
                if calls == 1:
                    path = base / frame["amodal_mask_path"]
                    path.write_bytes(path.read_bytes() + b"drift")
            return result

        monkeypatch.setattr(merger, "_audit_pixels", mutate)
    with pytest.raises(ValueError, match="source changed"):
        merger.merge_datasets([manifest], output)
    assert not (output / "manifest.json").exists()


def test_sealing_rechecks_actual_imported_helper_copy(repository, monkeypatch):
    manifest, _ = _dataset(repository, "input")
    imported = repository / "other-imported-helper.py"
    shutil.copyfile(Path(merger.curriculum.__file__), imported)
    monkeypatch.setattr(merger.curriculum, "__file__", str(imported))
    original = merger.trainer.verify_dataset

    def mutate(path):
        result = original(path)
        imported.write_bytes(imported.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(merger.trainer, "verify_dataset", mutate)
    with pytest.raises(ValueError, match="source changed"):
        merger.merge_datasets([manifest], repository / "merged")
    assert not (repository / "merged" / "manifest.json").exists()


def test_initial_imported_helper_mismatch_rejected_before_output(repository, monkeypatch):
    manifest, _ = _dataset(repository, "input")
    imported = repository / "different-imported-helper.py"
    imported.write_text("not the bound helper source")
    monkeypatch.setattr(merger.curriculum, "__file__", str(imported))
    with pytest.raises(ValueError, match="bound source identity"):
        merger.merge_datasets([manifest], repository / "merged")
    assert not (repository / "merged").exists()


def test_copy_failure_retains_sources_but_never_publishes_manifest(repository, monkeypatch):
    manifest, _ = _dataset(repository, "input")
    digest = merger.trainer.sha256(manifest)
    original = merger.shutil.copyfile
    copied = 0

    def fail(source, destination):
        nonlocal copied
        copied += 1
        if copied == 3:
            raise OSError("synthetic disk failure")
        return original(source, destination)

    monkeypatch.setattr(merger.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="disk failure"):
        merger.merge_datasets([manifest], repository / "merged")
    assert merger.trainer.sha256(manifest) == digest
    assert not (repository / "merged" / "manifest.json").exists()


def test_validator_success_cannot_publish_subsequently_tampered_pending_manifest(
    repository, monkeypatch
):
    manifest, _ = _dataset(repository, "input")
    output = repository / "merged"
    original = merger.trainer.verify_dataset
    validated_pending = []

    def tamper_after_success(path):
        result = original(path)
        if path.name == "manifest.pending.json":
            validated_pending.append(path)
            # This mutation happens after a real, successful old-trainer
            # validation, not instead of validation or before it.
            path.write_text('{"tampered":true}', encoding="utf-8")
        return result

    monkeypatch.setattr(merger.trainer, "verify_dataset", tamper_after_success)
    with pytest.raises(ValueError, match="source changed|pending manifest bytes changed"):
        merger.merge_datasets([manifest], output)
    assert len(validated_pending) == 1
    assert validated_pending[0].is_file()
    assert not (output / "manifest.json").exists()


def test_duplicate_manifest_and_existing_destination_rejected(repository):
    manifest, _ = _dataset(repository, "input")
    output = repository / "merged"
    with pytest.raises(ValueError, match="duplicate source"):
        merger.merge_datasets([manifest, manifest], output)
    output.mkdir()
    marker = output / "user-data.txt"
    marker.write_text("preserve")
    with pytest.raises(FileExistsError):
        merger.merge_datasets([manifest], output)
    assert marker.read_text() == "preserve"


def test_duplicate_json_keys_rejected_before_output(repository):
    manifest, _ = _dataset(repository, "input")
    manifest.write_text('{"contract":"one","contract":"two"}')
    with pytest.raises(ValueError, match="duplicate manifest key"):
        merger.merge_datasets([manifest], repository / "merged")


def test_direct_cli_help_is_lightweight():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(Path(merger.__file__)), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--manifest" in result.stdout
    assert "--output-directory" in result.stdout
