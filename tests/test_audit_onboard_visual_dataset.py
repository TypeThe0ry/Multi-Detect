from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scripts import audit_onboard_visual_dataset as auditor
from scripts import evaluate_onboard_visual_curriculum as curriculum
from scripts import train_onboard_sdf_visual_candidate as trainer
from scripts.render_onboard_sdf_blender_curriculum import geometry_sha256, load_target
from scripts.render_onboard_sdf_blender_dataset import visible_bbox, yolo_label

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def rewrite(manifest: Path, document: dict) -> None:
    manifest.write_text(json.dumps(document), encoding="utf-8")


def dataset(tmp_path: Path, monkeypatch) -> tuple[Path, dict, Path]:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(trainer, "ROOT", source)
    monkeypatch.setattr(curriculum, "ROOT", source)
    base = tmp_path / "renders"
    base.mkdir()
    producer = source / "producer.py"
    producer.write_text("# synthetic test producer identity\n", encoding="utf-8")
    sources = {producer.name: trainer.sha256(producer)}
    frames = []
    for index, (split, box, angle) in enumerate((
        ("train", (10, 10, 20, 65), 8.0),
        ("val", (30, 15, 45, 71), 11.0),
        ("test", (45, 20, 61, 40), 4.0),
    )):
        world = source / f"{split}-world.sdf"
        world.write_text(f"<sdf><world name='{split}'/></sdf>", encoding="utf-8")
        target = source / f"{split}-model.sdf"
        target.write_text(
            "<sdf><model name='target'><link name='body'><visual name='body'>"
            f"<geometry><box><size>{index + 2} 1 1</size></box></geometry>"
            "</visual></link></model></sdf>", encoding="utf-8",
        )
        digest = trainer.sha256(target)
        geometry = geometry_sha256(load_target(source, target.name))
        world_digest = trainer.sha256(world)
        frame = {
            "frame_id": f"{split}-00000",
            "split": split,
            "width": 96,
            "height": 80,
            "world_path": world.name,
            "world_sha256": world_digest,
            "source_hashes": {world.name: world_digest, target.name: digest},
            "target_model": {
                "path": target.name, "sha256": digest, "geometry_sha256": geometry,
            },
            "camera": {
                "location": [64.0, 77.0, 2.0 + 100 * math.tan(math.radians(angle))],
                "target_center": [4.0, -3.0, 2.0],
                "horizontal_distance_m": 999.0,
                "height_above_target_center_m": 999.0,
                "look_at": [999.0, 999.0, 999.0],
            },
            "curriculum": {
                "target_model_path": target.name,
                "target_model_sha256": digest,
                "target_geometry_sha256": geometry,
                "planned_condition": "fully_occluded",
            },
        }
        image = base / f"{split}-image.png"
        assert cv2.imwrite(str(image), np.full((80, 96, 3), index + 20, dtype=np.uint8))
        frame["image_path"] = image.name
        frame["image_sha256"] = trainer.sha256(image)
        frames.append(frame)
        set_visibility(base, frame, box)
        sources.update(frame["source_hashes"])
    yaml = base / "dataset.yaml"
    yaml.write_text("names: ['search_target']\n", encoding="utf-8")
    document = {
        "contract": trainer.DATASET_CONTRACT,
        "names": ["search_target"],
        "frame_count": len(frames),
        "frames": frames,
        "source_hashes": sources,
        "dataset_yaml": yaml.name,
        "dataset_yaml_sha256": trainer.sha256(yaml),
        **{key: False for key in auditor.FALSE_FLAGS},
    }
    manifest = base / "manifest.json"
    rewrite(manifest, document)
    return manifest, document, source


def set_visibility(
    base: Path, frame: dict, box: tuple[int, int, int, int] | None,
    *, amodal_box: tuple[int, int, int, int] | None = None, present: bool = True,
) -> None:
    masks = []
    for prefix, bounds in (("mask", box), ("amodal_mask", amodal_box or box)):
        mask = np.zeros((80, 96), dtype=np.uint8)
        if bounds is not None:
            x1, y1, x2, y2 = bounds
            mask[y1:y2, x1:x2] = 255
        masks.append(mask)
        path = base / f"{frame['split']}-{prefix}.png"
        assert cv2.imwrite(str(path), mask)
        frame[f"{prefix}_path"] = path.name
        frame[f"{prefix}_sha256"] = trainer.sha256(path)
    visible, amodal = (int((mask != 0).sum()) for mask in masks)
    actual_box = visible_bbox(masks[0].tobytes(), 96, 80)
    label = base / f"{frame['split']}-label.txt"
    label.write_text(yolo_label(actual_box, 96, 80), encoding="utf-8")
    frame["label_path"] = label.name
    frame["label_sha256"] = trainer.sha256(label)
    frame["visible_bbox_xyxy_px"] = None if actual_box is None else list(actual_box)
    frame["visible_target_pixels"] = visible
    frame["target_present"] = present
    frame["target_visible"] = visible > 0
    frame["curriculum"].update({
        "amodal_target_pixels": amodal,
        "visible_fraction": None if not amodal else visible / amodal,
        "visible_amodal_overlap_pixels": int(((masks[0] != 0) & (masks[1] != 0)).sum()),
        "measured_condition": curriculum.measured_condition(present, visible, amodal),
    })


def test_tiny_actual_png_audit_reports_every_split_and_actual_camera(tmp_path, monkeypatch):
    manifest, document, _ = dataset(tmp_path, monkeypatch)
    before = trainer.sha256(manifest)
    output = tmp_path / "new" / "audit.json"
    result = auditor.audit(manifest, output)
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert trainer.sha256(manifest) == before
    assert result["dataset"]["sha256"] == before
    assert result["frame_count"] == 3
    assert result["audit_passed"] is True
    assert result["input_hashes_verified_before_and_after"] is True
    assert result["test_geometry_disjoint_from_train_and_val"] is True
    for flag in (*auditor.FALSE_FLAGS, "model_inference_performed", "training_performed"):
        assert result[flag] is False
    for split in auditor.SPLITS:
        summary = result["splits"][split]
        assert summary["frame_count"] == 1
        assert summary["visible_frame_count"] == 1
        assert summary["measured_condition_counts"] == {"clear": 1}
        assert summary["planned_condition_counts"] == {"fully_occluded": 1}
        assert len(summary["geometries"]) == 1
        assert summary["camera_all_frames"]["actual_horizontal_distance_m"]["min"] == 100
    train = result["splits"]["train"]
    assert train["visible_low_view_le_10_deg_count"] == 1
    assert train["visible_bbox_height_le_55_px_count"] == 1
    assert train["visible_low_view_and_small_bbox_count"] == 1
    assert train["bbox_visible_frames"]["height_px"]["min"] == 55
    assert result["splits"]["val"]["visible_bbox_height_le_55_px_count"] == 0
    assert result["splits"]["val"]["visible_low_view_le_10_deg_count"] == 0
    frame = result["frames"][0]
    assert frame["camera"]["actual_downward_elevation_deg"] == pytest.approx(8)
    assert frame["bbox"]["center_x_px"] == 15
    assert frame["bbox"]["center_y_px"] == 37.5
    assert frame["rgb_shape_hwc"] == [80, 96, 3]
    assert frame["visible_target_pixels"] == document["frames"][0]["visible_target_pixels"]


def test_fully_occluded_and_absent_are_not_small_visible_targets(tmp_path, monkeypatch):
    manifest, document, _ = dataset(tmp_path, monkeypatch)
    set_visibility(manifest.parent, document["frames"][1], None, amodal_box=(10, 10, 20, 30))
    set_visibility(manifest.parent, document["frames"][2], None, present=False)
    rewrite(manifest, document)
    result = auditor.audit(manifest, tmp_path / "audit.json")
    for split, condition in (("val", "fully_occluded"), ("test", "absent")):
        summary = result["splits"][split]
        assert summary["measured_condition_counts"] == {condition: 1}
        assert summary["visible_frame_count"] == 0
        assert summary["visible_low_view_le_10_deg_count"] == 0
        assert summary["visible_bbox_height_le_55_px_count"] == 0
        assert summary["bbox_visible_frames"]["height_px"] == {
            "count": 0, "min": None, "median": None, "max": None,
        }


def test_zero_visible_training_split_rejects_without_report(tmp_path, monkeypatch):
    manifest, document, _ = dataset(tmp_path, monkeypatch)
    set_visibility(manifest.parent, document["frames"][0], None, present=False)
    rewrite(manifest, document)
    output = tmp_path / "audit.json"
    with pytest.raises(ValueError, match="training split has zero visible targets"):
        auditor.audit(manifest, output)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["label", "mask", "amodal_mask", "rgb", "channels", "dtype"])
def test_actual_asset_corruption_rejects_even_if_hashes_are_resealed(tmp_path, monkeypatch, kind):
    manifest, document, _ = dataset(tmp_path, monkeypatch)
    frame = document["frames"][0]
    if kind == "label":
        key = "label"
        (manifest.parent / frame["label_path"]).write_text("0 0.5 0.5 0.8 0.8\n")
    elif kind in ("mask", "amodal_mask"):
        key = kind
        mask = cv2.imread(str(manifest.parent / frame[f"{key}_path"]), cv2.IMREAD_GRAYSCALE)
        mask[10, 10] = 0 if kind == "mask" else 128
        assert cv2.imwrite(str(manifest.parent / frame[f"{key}_path"]), mask)
    else:
        key = "image"
        shape = (10, 12, 3) if kind == "rgb" else (80, 96, 4 if kind == "channels" else 3)
        dtype = np.uint16 if kind == "dtype" else np.uint8
        assert cv2.imwrite(
            str(manifest.parent / frame["image_path"]), np.full(shape, 20, dtype=dtype)
        )
    frame[f"{key}_sha256"] = trainer.sha256(manifest.parent / frame[f"{key}_path"])
    rewrite(manifest, document)
    output = tmp_path / "audit.json"
    with pytest.raises(ValueError):
        auditor.audit(manifest, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "tamper", ["world", "duplicate-world", "geometry", "duplicate-geometry", "frame-id"]
)
def test_independence_and_identities_are_verified_not_self_declared(tmp_path, monkeypatch, tamper):
    manifest, document, source = dataset(tmp_path, monkeypatch)
    train, _, test = document["frames"]
    if tamper == "world":
        test["world_path"] = train["world_path"]
        test["world_sha256"] = train["world_sha256"]
        test["source_hashes"][train["world_path"]] = train["world_sha256"]
    elif tamper == "duplicate-world":
        path = source / test["world_path"]
        path.write_bytes((source / train["world_path"]).read_bytes())
        digest = trainer.sha256(path)
        test["world_sha256"] = digest
        test["source_hashes"][path.name] = digest
        document["source_hashes"][path.name] = digest
    elif tamper == "geometry":
        test["target_model"]["geometry_sha256"] = "0" * 64
        test["curriculum"]["target_geometry_sha256"] = "0" * 64
    elif tamper == "duplicate-geometry":
        path = source / test["target_model"]["path"]
        path.write_bytes((source / train["target_model"]["path"]).read_bytes() + b"<!-- copy -->")
        digest = trainer.sha256(path)
        geometry = train["target_model"]["geometry_sha256"]
        test["target_model"].update(sha256=digest, geometry_sha256=geometry)
        test["curriculum"].update(target_model_sha256=digest, target_geometry_sha256=geometry)
        test["source_hashes"][path.name] = digest
        document["source_hashes"][path.name] = digest
    else:
        test["frame_id"] = train["frame_id"]
    rewrite(manifest, document)
    with pytest.raises(ValueError):
        auditor.audit(manifest, tmp_path / "audit.json")


@pytest.mark.parametrize("change", ["manifest", "producer", "image", "runtime"])
def test_input_and_helper_source_drift_during_audit_fail_closed(tmp_path, monkeypatch, change):
    manifest, document, source = dataset(tmp_path, monkeypatch)
    if change == "runtime":
        runtime = tmp_path / "runtime.py"
        runtime.write_text("before\n")
        monkeypatch.setattr(auditor, "ROOT", tmp_path)
        monkeypatch.setattr(auditor, "SOURCE_PATHS", (runtime.name,))
    original = auditor._camera_measurements
    changed = False

    def mutate_once(frame):
        nonlocal changed
        if not changed:
            changed = True
            if change == "manifest":
                manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            elif change == "producer":
                (source / "producer.py").write_text("changed producer\n")
            elif change == "image":
                path = manifest.parent / document["frames"][0]["image_path"]
                path.write_bytes(b"changed RGB pixels")
            else:
                runtime.write_text("changed helper\n")
        return original(frame)

    monkeypatch.setattr(auditor, "_camera_measurements", mutate_once)
    output = tmp_path / "audit.json"
    with pytest.raises(ValueError):
        auditor.audit(manifest, output)
    assert not output.exists()


@pytest.mark.parametrize("bad_json", [
    '{"contract":"a","contract":"b"}',
    '{"number":NaN}',
    '{"number":Infinity}',
    '{"number":1e999}',
])
def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(tmp_path, bad_json):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(bad_json, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate|nonfinite"):
        auditor.audit(manifest, tmp_path / "audit.json")


def test_report_refuses_overwrite_before_reading_inputs(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("retain original evidence\n")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        auditor.audit(tmp_path / "missing.json", output)
    assert output.read_text() == "retain original evidence\n"


def test_existing_report_created_during_audit_is_not_overwritten(tmp_path, monkeypatch):
    manifest, _, _ = dataset(tmp_path, monkeypatch)
    output = tmp_path / "audit.json"
    original = auditor._camera_measurements

    def create_output(frame):
        output.write_text("other writer's evidence\n")
        return original(frame)

    monkeypatch.setattr(auditor, "_camera_measurements", create_output)
    with pytest.raises(FileExistsError):
        auditor.audit(manifest, output)
    assert output.read_text() == "other writer's evidence\n"


@pytest.mark.parametrize("coordinates", [[0, 0, True], [0, 0, 0], [0, 0], [0, 0, "1"]])
def test_invalid_camera_coordinates_reject(coordinates):
    with pytest.raises(ValueError, match="finite|nonzero"):
        auditor._camera_measurements({
            "camera": {"location": coordinates, "target_center": [0, 0, 0]},
        })


def test_command_line_entry_uses_no_model_or_training_arguments(tmp_path, monkeypatch, capsys):
    manifest, _, _ = dataset(tmp_path, monkeypatch)
    output = tmp_path / "audit.json"
    assert auditor.main(["--manifest", str(manifest), "--output", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["audit_passed"] is True
    assert summary["frame_count"] == 3
