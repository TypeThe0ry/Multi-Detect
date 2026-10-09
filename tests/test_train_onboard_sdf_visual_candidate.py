from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import train_onboard_sdf_visual_candidate as trainer


def dataset(tmp_path: Path, monkeypatch) -> tuple[Path, dict]:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(trainer, "ROOT", source)
    base = tmp_path / "renders"
    base.mkdir()
    frames = []
    for split in ("train", "val", "test"):
        world = source / f"{split}.sdf"
        world.write_text(f"<world name='{split}'/>", encoding="utf-8")
        frame = {
            "split": split,
            "world_path": world.name,
            "world_sha256": trainer.sha256(world),
            "source_hashes": {world.name: trainer.sha256(world)},
        }
        for kind in ("image", "label", "mask"):
            suffix = {"image": ".png", "label": ".txt", "mask": ".png"}[kind]
            path = base / f"{split}-{kind}{suffix}"
            path.write_bytes(f"unit test identity fixture {split} {kind}".encode())
            frame[f"{kind}_path"] = path.name
            frame[f"{kind}_sha256"] = trainer.sha256(path)
        frames.append(frame)
    yaml = base / "dataset.yaml"
    yaml.write_text("names: {0: search_target}\n", encoding="utf-8")
    document = {
        "contract": trainer.DATASET_CONTRACT,
        "frames": frames,
        "frame_count": len(frames),
        "names": ["search_target"],
        "training_ready": False,
        "gazebo_capture": False,
        "dataset_yaml": yaml.name,
        "dataset_yaml_sha256": trainer.sha256(yaml),
    }
    path = base / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document


def rewrite(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


def developer_parent(tmp_path):
    parent = tmp_path / "best.pt"
    parent.write_bytes(b"test checkpoint identity, not a real model")
    report = tmp_path / "visual-training-report.json"
    document = {
        "contract": "multidetect.onboard-sdf-visual-candidate-development-v1",
        "names": {"0": "search_target"},
        "checkpoint": {"path": str(parent), "sha256": trainer.sha256(parent)},
        **{
            key: False
            for key in (
                "training_ready",
                "gazebo_capture",
                "candidate_promoted",
                "original_detector_changed",
                "onboard_policy_reference_changed",
                "final_release_allowed",
            )
        },
    }
    rewrite(report, document)
    return parent, report, document


def test_incremental_parent_is_bound_to_report_and_checkpoint(tmp_path):
    parent, report, _ = developer_parent(tmp_path)
    digest, binding = trainer.resolve_parent_binding(parent, report, trainer.sha256(report))
    assert digest == trainer.sha256(parent)
    assert binding == {"path": str(report), "sha256": trainer.sha256(report)}


def test_byte_identical_parent_can_be_relocated_without_rewriting_producer_report(tmp_path):
    parent, report, _ = developer_parent(tmp_path)
    moved = tmp_path / "verified-copy.pt"
    moved.write_bytes(parent.read_bytes())
    digest, _ = trainer.resolve_parent_binding(moved, report, trainer.sha256(report))
    assert digest == trainer.sha256(parent)


@pytest.mark.parametrize("missing", ["report", "hash"])
def test_partial_parent_binding_is_rejected(tmp_path, missing):
    parent, report, _ = developer_parent(tmp_path)
    with pytest.raises(ValueError, match="supplied together"):
        trainer.resolve_parent_binding(
            parent,
            None if missing == "report" else report,
            None if missing == "hash" else trainer.sha256(report),
        )


def test_unbound_development_parent_does_not_replace_default_pin(tmp_path):
    parent, _, _ = developer_parent(tmp_path)
    with pytest.raises(ValueError, match="hash-pinned COCO"):
        trainer.resolve_parent_binding(parent)


@pytest.mark.parametrize("tamper", ["report", "checkpoint", "path", "names", "promoted"])
def test_incremental_parent_identity_and_claim_tampering_is_rejected(tmp_path, tamper):
    parent, report, document = developer_parent(tmp_path)
    expected = trainer.sha256(report)
    if tamper == "report":
        report.write_text("{}")
    elif tamper == "checkpoint":
        parent.write_bytes(b"different weights")
    else:
        if tamper == "path":
            document["checkpoint"]["path"] = None
        elif tamper == "names":
            document["names"] = {"0": "car"}
        else:
            document["candidate_promoted"] = True
        rewrite(report, document)
        expected = trainer.sha256(report)
    with pytest.raises(ValueError):
        trainer.resolve_parent_binding(parent, report, expected)


def test_verified_offline_dataset_keeps_developer_boundary(tmp_path, monkeypatch):
    manifest, expected = dataset(tmp_path, monkeypatch)
    actual, yaml = trainer.verify_dataset(manifest)
    assert actual == expected
    assert yaml.name == "dataset.yaml"
    assert actual["training_ready"] is False


def test_modified_pixels_rejected(tmp_path, monkeypatch):
    manifest, document = dataset(tmp_path, monkeypatch)
    (manifest.parent / document["frames"][0]["image_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity mismatch"):
        trainer.verify_dataset(manifest)


def test_shared_source_world_rejected(tmp_path, monkeypatch):
    manifest, document = dataset(tmp_path, monkeypatch)
    for key in ("world_path", "world_sha256"):
        document["frames"][1][key] = document["frames"][0][key]
    document["frames"][1]["source_hashes"] = document["frames"][0]["source_hashes"]
    rewrite(manifest, document)
    with pytest.raises(ValueError, match="independent source worlds"):
        trainer.verify_dataset(manifest)


def test_missing_test_split_rejected(tmp_path, monkeypatch):
    manifest, document = dataset(tmp_path, monkeypatch)
    document["frames"].pop()
    document["frame_count"] -= 1
    rewrite(manifest, document)
    with pytest.raises(ValueError, match="must all contain frames"):
        trainer.verify_dataset(manifest)


def test_outside_path_rejected_even_when_bytes_match(tmp_path, monkeypatch):
    manifest, document = dataset(tmp_path, monkeypatch)
    outside = tmp_path / "outside.fixture"
    outside.write_bytes(b"outside")
    document["frames"][0]["mask_path"] = "../outside.fixture"
    document["frames"][0]["mask_sha256"] = trainer.sha256(outside)
    rewrite(manifest, document)
    with pytest.raises(ValueError, match="remain inside"):
        trainer.verify_dataset(manifest)


@pytest.mark.parametrize(
    "field,value", [("training_ready", True), ("gazebo_capture", True), ("names", ["car"])]
)
def test_false_capture_or_implicit_class_claim_rejected(tmp_path, monkeypatch, field, value):
    manifest, document = dataset(tmp_path, monkeypatch)
    document[field] = value
    rewrite(manifest, document)
    with pytest.raises(ValueError):
        trainer.verify_dataset(manifest)


def test_experiment_flag_required_before_loading_training_dependencies():
    with pytest.raises(ValueError, match="development-experiment"):
        trainer.main(
            ["--manifest", "missing.json", "--parent", "missing.pt", "--output-directory", "unused"]
        )


def test_closed_inputs_ignore_misleading_yaml_and_unregistered_files(tmp_path, monkeypatch):
    manifest, document = dataset(tmp_path, monkeypatch)
    source_yaml = manifest.parent / "dataset.yaml"
    source_yaml.write_text("path: /unrelated\ntrain: test\nval: train\ntest: train\n")
    document["dataset_yaml_sha256"] = trainer.sha256(source_yaml)
    rewrite(manifest, document)
    (manifest.parent / "unregistered.png").write_bytes(b"must not enter training")
    verified, _ = trainer.verify_dataset(manifest)
    staged = tmp_path / "closed-inputs"
    yaml = trainer.stage_training_inputs(manifest, verified, staged)
    actual = json.loads(yaml.read_text())
    assert actual["path"] == str(staged.resolve())
    assert actual["train"] == "images/train"
    assert actual["val"] == "images/val"
    assert actual["test"] == "images/test"
    assert actual["names"] == {"0": "search_target"}
    assert len(list((staged / "images").rglob("*.png"))) == 3
    assert not list(staged.rglob("unregistered.png"))
    assert trainer.verify_staged_inputs(verified, staged) == yaml


def test_finalization_rejects_extra_staged_image(tmp_path, monkeypatch):
    manifest, document = dataset(tmp_path, monkeypatch)
    staged = tmp_path / "closed-inputs"
    trainer.stage_training_inputs(manifest, document, staged)
    (staged / "images/test/unregistered.png").write_bytes(b"unregistered")
    with pytest.raises(ValueError, match="extra or missing"):
        trainer.verify_staged_inputs(document, staged)


def test_finalization_rejects_changed_staged_configuration(tmp_path, monkeypatch):
    manifest, document = dataset(tmp_path, monkeypatch)
    staged = tmp_path / "closed-inputs"
    yaml = trainer.stage_training_inputs(manifest, document, staged)
    config = json.loads(yaml.read_text())
    config["test"] = "images/train"
    yaml.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="configuration changed"):
        trainer.verify_staged_inputs(document, staged)


def test_finalization_reuses_original_byte_identity_binding(tmp_path):
    expected = {"manifest": {"sha256": "original"}, "parent": {"sha256": "parent"}}
    first = trainer.bind_training_invocation(tmp_path, expected, create=True)
    assert trainer.bind_training_invocation(tmp_path, expected, create=False) == first
    with pytest.raises(FileExistsError):
        trainer.bind_training_invocation(tmp_path, expected, create=True)


def test_old_unbound_training_cannot_be_finalized_as_new_provenance(tmp_path):
    with pytest.raises(ValueError, match="initial training invocation"):
        trainer.bind_training_invocation(tmp_path, {}, create=False)


@pytest.mark.parametrize("key", ["manifest", "parent", "parent_training_report", "configuration"])
def test_finalization_rejects_same_path_replaced_sources_or_configuration(tmp_path, key):
    original = {
        "manifest": {"path": "same.json", "sha256": "old-dataset"},
        "parent": {"path": "same.pt", "sha256": "old-parent"},
        "parent_training_report": {"path": "same-report.json", "sha256": "old-report"},
        "configuration": {"epochs": 30},
    }
    trainer.bind_training_invocation(tmp_path, original, create=True)
    changed = {**original, key: {**original[key], "sha256": "changed-source"}}
    with pytest.raises(ValueError, match="cannot relabel old weights"):
        trainer.bind_training_invocation(tmp_path, changed, create=False)


def test_developer_checkpoint_actual_names_cannot_be_report_only_alias():
    trainer.verify_parent_names({0: "search_target"}, developer_parent=True)
    trainer.verify_parent_names({0: "person", 1: "car"}, developer_parent=False)
    with pytest.raises(ValueError, match="actual parent checkpoint"):
        trainer.verify_parent_names({0: "vehicle"}, developer_parent=True)


def test_existing_onnx_requires_its_original_checkpoint_receipt(tmp_path):
    checkpoint, exported = tmp_path / "best.pt", tmp_path / "best.onnx"
    checkpoint.write_bytes(b"checkpoint identity fixture")
    exported.write_bytes(b"raw export identity fixture")
    with pytest.raises(ValueError, match="refusing reuse"):
        trainer.bind_export_receipt(checkpoint, exported, create=False)
    bound = trainer.bind_export_receipt(checkpoint, exported, create=True)
    assert trainer.bind_export_receipt(checkpoint, exported, create=False) == bound


@pytest.mark.parametrize("changed", ["checkpoint", "onnx"])
def test_old_export_cannot_be_sealed_for_different_weight_bytes(tmp_path, changed):
    checkpoint, exported = tmp_path / "best.pt", tmp_path / "best.onnx"
    checkpoint.write_bytes(b"checkpoint identity fixture")
    exported.write_bytes(b"raw export identity fixture")
    trainer.bind_export_receipt(checkpoint, exported, create=True)
    (checkpoint if changed == "checkpoint" else exported).write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="checkpoint-bound export"):
        trainer.bind_export_receipt(checkpoint, exported, create=False)
