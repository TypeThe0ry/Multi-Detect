from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.domain import BoundingBox
from multidetect.evaluation import GroundTruthFrame, LabeledBox, PredictionFrame
from scripts.evaluate_onboard_visual_curriculum import (
    measured_condition,
    summarize_groups,
    verify_geometry_provenance,
    verify_model_contract,
    verify_source_names,
)
from scripts.render_onboard_sdf_blender_curriculum import TARGET_PATHS, geometry_sha256, load_target
from scripts.train_onboard_sdf_visual_candidate import sha256

ROOT = Path(__file__).resolve().parents[1]


def _geometry_document():
    frames = []
    for split, key in (("train", "original"), ("val", "cargo_cart"), ("test", "utility_van")):
        path = TARGET_PATHS[key]
        digest = sha256(ROOT / path)
        geometry = geometry_sha256(load_target(ROOT, path))
        frames.append(
            {
                "split": split,
                "curriculum": {
                    "target_model_path": path,
                    "target_model_sha256": digest,
                    "target_geometry_sha256": geometry,
                },
                "target_model": {"path": path, "sha256": digest, "geometry_sha256": geometry},
                "source_hashes": {path: digest},
            }
        )
    return {"frames": frames}


def test_geometry_holdout_is_recomputed_from_bound_target_sdf():
    document = _geometry_document()
    result = verify_geometry_provenance(document)
    for frame in document["frames"]:
        assert result[frame["target_model"]["path"]] == frame["target_model"]


@pytest.mark.parametrize("tamper", ["fingerprint", "path", "hash", "model", "missing-source"])
def test_target_geometry_cannot_be_faked_by_manifest_metadata(tamper):
    document = _geometry_document()
    target = document["frames"][-1]
    if tamper == "fingerprint":
        target["curriculum"]["target_geometry_sha256"] = "0" * 64
        target["target_model"]["geometry_sha256"] = "0" * 64
    elif tamper == "path":
        target["curriculum"]["target_model_path"] = TARGET_PATHS["original"]
    elif tamper == "hash":
        target["curriculum"]["target_model_sha256"] = "0" * 64
        target["target_model"]["sha256"] = "0" * 64
        target["source_hashes"][target["target_model"]["path"]] = "0" * 64
    elif tamper == "model":
        target["target_model"]["path"] = TARGET_PATHS["original"]
    else:
        target["source_hashes"].clear()
    with pytest.raises(ValueError):
        verify_geometry_provenance(document)


def test_same_model_cannot_be_declared_unseen_geometry():
    document = _geometry_document()
    same = deepcopy(document["frames"][0])
    same["split"] = "test"
    document["frames"][-1] = same
    with pytest.raises(ValueError, match="overlaps train or validation"):
        verify_geometry_provenance(document)


def test_duplicate_geometry_at_new_source_path_cannot_be_declared_held_out(tmp_path, monkeypatch):
    from scripts import evaluate_onboard_visual_curriculum as evaluator

    document = _geometry_document()
    # Copy the exact training primitives to another SDF path. A comment changes
    # the source SHA but does not create an unseen geometry.
    duplicate = tmp_path / "duplicate.sdf"
    duplicate.write_bytes((ROOT / TARGET_PATHS["original"]).read_bytes() + b"\n<!-- copy -->\n")
    source_root = tmp_path
    for frame in document["frames"]:
        path = frame["target_model"]["path"]
        destination = source_root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / path).read_bytes())
    model = {
        "path": duplicate.name,
        "sha256": sha256(duplicate),
        "geometry_sha256": geometry_sha256(load_target(tmp_path, duplicate.name)),
    }
    document["frames"][-1] = {
        "split": "test",
        "curriculum": {
            "target_model_path": model["path"],
            "target_model_sha256": model["sha256"],
            "target_geometry_sha256": model["geometry_sha256"],
        },
        "target_model": model,
        "source_hashes": {model["path"]: model["sha256"]},
    }
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="identity overlaps despite different model paths"):
        verify_geometry_provenance(document)


@pytest.mark.parametrize(
    "raw",
    ["{0: 'person'}", "{0:'vehicle'}", "{1:'search_target'}", "", "[]", "{False:'search_target'}"],
)
def test_runtime_names_do_not_silently_relabel_a_different_single_class(raw):
    with pytest.raises(ValueError):
        verify_source_names(raw)


def test_runtime_names_match_original_trained_class_identity():
    verify_source_names("{0: 'search_target'}")


@pytest.mark.parametrize(
    "shapes,metadata,external,valid",
    [
        ([[1, 5, 8400]], ["{0:'search_target'}"], False, True),
        ([], ["{0:'search_target'}"], False, False),
        ([[1, 5, 8400], [1, 5, 8400]], ["{0:'search_target'}"], False, False),
        ([[1, 84, 8400]], ["{0:'search_target'}"], False, False),
        ([[1, 5, 0]], ["{0:'search_target'}"], False, False),
        ([[1, 5, 8400]], ["{0:'vehicle'}"], False, False),
        ([[1, 5, 8400]], ["{0:'search_target'}", "{0:'search_target'}"], False, False),
        ([[1, 5, 8400]], ["{0:'search_target'}"], True, False),
    ],
)
def test_actual_onnx_contract_rejects_ambiguous_or_unbound_outputs(
    monkeypatch, tmp_path, shapes, metadata, external, valid
):
    graph = SimpleNamespace(
        graph=SimpleNamespace(
            initializer=[SimpleNamespace(data_location=1 if external else 0)],
            output=[
                SimpleNamespace(
                    type=SimpleNamespace(
                        tensor_type=SimpleNamespace(
                            shape=SimpleNamespace(dim=[SimpleNamespace(dim_value=v) for v in shape])
                        )
                    )
                )
                for shape in shapes
            ],
        ),
        metadata_props=[SimpleNamespace(key="names", value=names) for names in metadata],
    )
    monkeypatch.setitem(
        sys.modules,
        "onnx",
        SimpleNamespace(
            load_model=lambda *_args, **_kwargs: graph, TensorProto=SimpleNamespace(EXTERNAL=1)
        ),
    )
    if valid:
        verify_model_contract(tmp_path / "model.onnx")
    else:
        with pytest.raises(ValueError):
            verify_model_contract(tmp_path / "model.onnx")


@pytest.mark.parametrize(
    "present,visible,amodal,expected",
    [
        (False, 0, 0, "absent"),
        (True, 0, 0, "out_of_frame"),
        (True, 0, 100, "fully_occluded"),
        (True, 40, 100, "partially_occluded"),
        (True, 99, 100, "partially_occluded"),
        (True, 100, 100, "clear"),
    ],
)
def test_grouping_uses_actual_pixels_not_planned_condition(present, visible, amodal, expected):
    assert measured_condition(present, visible, amodal) == expected


@pytest.mark.parametrize(
    "present,visible,amodal",
    [
        (False, 1, 0),
        (False, 0, 1),
        (True, 1, 0),
        (True, 110, 100),
        (True, -1, 100),
        (True, False, 100),
        (1, 0, 0),
    ],
)
def test_impossible_visibility_is_rejected(present, visible, amodal):
    with pytest.raises(ValueError):
        measured_condition(present, visible, amodal)


def test_occlusion_misses_and_negative_false_alarms_are_separate_groups():
    box = LabeledBox("search_target", BoundingBox(0.1, 0.1, 0.3, 0.3), 0.8)
    truth = [GroundTruthFrame("visible", (box,)), GroundTruthFrame("occluded", ())]
    predictions = [PredictionFrame("visible", (), 1), PredictionFrame("occluded", (box,), 1)]
    report = summarize_groups(
        truth, predictions, {"partially_occluded": [0], "fully_occluded": [1]}, 0.25
    )
    assert report["overall"]["overall"]["false_positives"] == 1
    assert report["overall"]["overall"]["false_negatives"] == 1
    groups = report["by_measured_condition"]
    assert groups["partially_occluded"]["overall"]["false_negatives"] == 1
    assert groups["fully_occluded"]["overall"]["false_positives"] == 1
    assert groups["fully_occluded"]["overall"]["false_negatives"] == 0
