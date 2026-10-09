from __future__ import annotations

import json
import math
import shutil
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import render_onboard_sdf_blender_far_curriculum as renderer
from scripts import train_onboard_sdf_visual_candidate as trainer
from scripts.evaluate_onboard_visual_curriculum import verify_geometry_provenance

ROOT = Path(__file__).resolve().parents[1]


def _inputs(root: Path = ROOT):
    worlds = {
        split: renderer.base.load_world(root, relative)
        for split, relative in renderer.base.WORLD_PATHS.items()
    }
    targets = {
        key: renderer.curriculum.load_target(root, relative)
        for key, relative in renderer.curriculum.TARGET_PATHS.items()
    }
    return worlds, targets


def test_near_far_conditions_and_geometry_do_not_collapse_to_the_same_bucket() -> None:
    worlds, targets = _inputs()
    plans = [
        renderer.plan_sample(ROOT, worlds["train"], "train", 20261011, i, targets)
        for i in range(36)
    ]
    assert Counter(p.camera["distance_regime"] for p in plans) == {
        "near_9_to_30m": 12,
        "far_30_to_50m": 24,
    }
    for geometry in renderer.curriculum.SPLIT_GEOMETRIES["train"]:
        chosen = [p for p in plans if p.target_model["geometry_key"] == geometry]
        assert Counter(p.camera["distance_regime"] for p in chosen) == {
            "near_9_to_30m": 4,
            "far_30_to_50m": 8,
        }
        for distance in ("near_9_to_30m", "far_30_to_50m"):
            assert {
                p.planned_condition for p in chosen if p.camera["distance_regime"] == distance
            } == set(renderer.curriculum.CONDITIONS)
    assert {p.camera["framing_regime"] for p in plans} == {"center", "intermediate", "edge"}
    assert {p.lighting["regime"] for p in plans} == {"weak", "strong"}


def test_actual_camera_pose_has_low_target_and_optical_angles_and_diverse_framing() -> None:
    worlds, targets = _inputs()
    for index in range(36):
        plan = renderer.plan_sample(ROOT, worlds["train"], "train", 20261011, index, targets)
        camera = plan.camera
        location, center, look_at = camera["location"], camera["target_center"], camera["look_at"]
        distance = math.hypot(location[0] - center[0], location[1] - center[1])
        elevation = math.degrees(math.atan2(location[2] - center[2], distance))
        optical = math.degrees(
            math.atan2(
                location[2] - look_at[2],
                math.hypot(location[0] - look_at[0], location[1] - look_at[1]),
            )
        )
        assert camera["horizontal_distance_m"] == pytest.approx(distance)
        assert camera["target_center_elevation_deg"] == pytest.approx(elevation)
        assert camera["optical_axis_depression_deg"] == pytest.approx(optical)
        assert 3.0 - 1e-9 <= elevation <= 12.0 + 1e-9
        assert 3.0 - 1e-9 <= optical <= 12.0 + 1e-9
        if camera["distance_regime"] == "near_9_to_30m":
            assert 9.0 <= distance <= 30.0
        else:
            assert 30.0 <= distance <= 50.0
        offset = abs(camera["planned_horizontal_framing_half_extent"])
        low, high = {"center": (0.0, 0.12), "intermediate": (0.18, 0.5), "edge": (0.58, 0.88)}[
            camera["framing_regime"]
        ]
        assert low <= offset <= high
        if plan.lighting["regime"] == "weak":
            assert 0.08 <= plan.world.sun_intensity <= 0.8
        else:
            assert 0.6 <= plan.world.sun_intensity <= 2.2
        assert plan.lighting["blender_sun_energy"] == pytest.approx(plan.world.sun_intensity * 2.5)
        assert plan.lighting["world_background"] == plan.world.background


def test_plan_is_reproducible_and_new_entry_identity_reaches_actual_scene() -> None:
    worlds, targets = _inputs()
    first = renderer.plan_sample(ROOT, worlds["test"], "test", 20261011, 4, targets)
    assert first == renderer.plan_sample(ROOT, worlds["test"], "test", 20261011, 4, targets)
    changed = renderer.plan_sample(ROOT, worlds["test"], "test", 20261012, 4, targets)
    assert first.sample_seed_sha256 != changed.sample_seed_sha256
    assert first.world.primitives != changed.world.primitives
    for path in renderer.SOURCE_SCRIPTS:
        assert dict(first.world.sources)[path] == renderer.base.sha256(ROOT / path)
    assert first.camera["sample_seed_sha256"] == first.sample_seed_sha256
    assert first.target_model["geometry_key"] == "utility_van"
    assert first.world.relative_path == renderer.base.WORLD_PATHS["test"]


def test_physical_screen_is_recomputed_for_the_new_camera_ray() -> None:
    worlds, targets = _inputs()
    for index in (1, 2, 5, 6):
        plan = renderer.plan_sample(ROOT, worlds["train"], "train", 20261011, index, targets)
        screen = next(p for p in plan.world.primitives if plan.roles[p.name] == "planned_occluder")
        camera = plan.camera
        center = plan.world.target_center
        toward = (math.cos(camera["azimuth_rad"]), math.sin(camera["azimuth_rad"]))
        delta = (screen.matrix[0][3] - center[0], screen.matrix[1][3] - center[1])
        fraction = (
            sum(a * b for a, b in zip(delta, toward, strict=True)) / camera["horizontal_distance_m"]
        )
        assert 0.12 <= fraction <= 0.2
        assert screen.matrix[2][3] == pytest.approx(screen.dimensions[2] / 2)
        assert screen.dimensions[2] > plan.target_model["world_bounds_m"][1][2]
        assert plan.camera["target_present"] is True
    absent = renderer.plan_sample(ROOT, worlds["train"], "train", 20261011, 3, targets)
    assert absent.camera["target_present"] is False
    assert "planned_occluder" not in absent.roles.values()
    assert all(
        p["hidden_for_absent"]
        for p in renderer.curriculum._primitive_inventory(absent)
        if p["target"]
    )


def _mock_blender(monkeypatch) -> None:
    """Contract fixture only; no actual Blender acceptance is claimed here."""
    monkeypatch.setitem(
        sys.modules,
        "bpy",
        SimpleNamespace(
            app=SimpleNamespace(version=(4, 5, 3), version_string="4.5.3-contract-fixture")
        ),
    )
    monkeypatch.setitem(sys.modules, "mathutils", SimpleNamespace())

    def render(_bpy, _mathutils, plan, args, image, raw_mask, raw_amodal):
        total = args.width * args.height
        amodal = b"\xff\xff" + bytes(total - 2)
        if not plan.camera["target_present"]:
            visible = amodal = bytes(total)
        elif plan.planned_condition == "full_occlusion":
            visible = bytes(total)
        elif plan.planned_condition == "partial_occlusion":
            visible = b"\xff" + bytes(total - 1)
        else:
            visible = amodal
        image.write_bytes(b"fixture-only-rgb-" + plan.sample_seed_sha256.encode())
        raw_mask.write_bytes(renderer.base._mask_png(visible, args.width, args.height))
        raw_amodal.write_bytes(renderer.base._mask_png(amodal, args.width, args.height))
        return visible, amodal, [list(row) for row in renderer.base.IDENTITY]

    monkeypatch.setattr(renderer.curriculum, "_render_sample", render)


def test_manifest_reuses_actual_contract_and_measured_labels_not_camera_plan(
    tmp_path, monkeypatch
) -> None:
    _mock_blender(monkeypatch)
    output = tmp_path / "new-far-corpus"
    args = renderer.parse_arguments(
        [
            "--output-dir",
            str(output),
            "--samples-per-split",
            "12",
            "--width",
            "16",
            "--height",
            "16",
            "--seed",
            "20261011",
        ]
    )
    result = renderer.render_dataset(args)
    assert result["frame_count"] == 36
    assert result["curriculum"]["distance_regime_counts"] == {
        "near_9_to_30m": 12,
        "far_30_to_50m": 24,
    }
    verified, yaml = trainer.verify_dataset(output / "manifest.json")
    assert verified == json.loads(json.dumps(result))
    assert yaml == output / "dataset.yaml"
    assert len(verify_geometry_provenance(verified)) == 4
    for frame in result["frames"]:
        for path in renderer.SOURCE_SCRIPTS:
            assert path in frame["source_hashes"]
        if frame["planned_condition"] in {"full_occlusion", "absent"}:
            assert frame["visible_bbox_xyxy_px"] is None
            assert (output / frame["label_path"]).read_text() == ""
        for kind in ("image", "label", "mask", "mask_render", "amodal_mask", "amodal_render"):
            assert renderer.base.sha256(output / frame[f"{kind}_path"]) == frame[f"{kind}_sha256"]
        assert (
            frame["actual_amodal_image_area_fraction"]
            == frame["curriculum"]["amodal_target_pixels"] / 256
        )
    for flag in (
        "training_ready",
        "gazebo_capture",
        "flight",
        "promotion",
        "hardware_acceptance",
        "hil_acceptance",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert result[flag] is False


def test_plan_category_cannot_fabricate_rendered_occlusion(tmp_path, monkeypatch) -> None:
    _mock_blender(monkeypatch)
    fixture = renderer.curriculum._render_sample

    def all_unoccluded(*arguments):
        _visible, amodal, matrix = fixture(*arguments)
        return amodal, amodal, matrix

    monkeypatch.setattr(renderer.curriculum, "_render_sample", all_unoccluded)
    args = renderer.parse_arguments(
        [
            "--output-dir",
            str(tmp_path / "unoccluded"),
            "--samples-per-split",
            "4",
            "--width",
            "16",
            "--height",
            "16",
        ]
    )
    result = renderer.render_dataset(args)
    assert result["curriculum"]["planned_condition_counts"]["full_occlusion"] == 3
    assert result["curriculum"]["measured_condition_counts"] == {"clear": 9, "absent": 3}


def test_existing_output_is_not_overwritten(tmp_path) -> None:
    args = renderer.parse_arguments(["--output-dir", str(tmp_path)])
    original = tmp_path / "user.txt"
    original.write_text("preserve")
    with pytest.raises(renderer.base.SdfRenderError, match="refusing overwrite"):
        renderer.render_dataset(args)
    assert original.read_text() == "preserve"


def _copy_sources(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for path in [
        *renderer.base.WORLD_PATHS.values(),
        *renderer.curriculum.TARGET_PATHS.values(),
        *renderer.SOURCE_SCRIPTS,
    ]:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    return root


def test_imported_renderer_must_match_declared_repo_root_source(tmp_path, monkeypatch) -> None:
    _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)
    helper = root / renderer.curriculum.RENDERER_PATH
    helper.write_bytes(helper.read_bytes() + b"\n<!-- different bytes -->\n")
    output = tmp_path / "rejected"
    args = renderer.parse_arguments(["--repo-root", str(root), "--output-dir", str(output)])
    with pytest.raises(renderer.base.SdfRenderError, match="imported renderer source differs"):
        renderer.render_dataset(args)
    assert not output.exists()


def test_changed_new_entry_prevents_manifest_sealing(tmp_path, monkeypatch) -> None:
    _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)
    fixture = renderer.curriculum._render_sample

    def change(*arguments):
        result = fixture(*arguments)
        source = root / renderer.RENDERER_PATH
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(renderer.curriculum, "_render_sample", change)
    args = renderer.parse_arguments(
        [
            "--repo-root",
            str(root),
            "--output-dir",
            str(tmp_path / "incomplete"),
            "--samples-per-split",
            "1",
            "--width",
            "16",
            "--height",
            "16",
        ]
    )
    with pytest.raises(renderer.base.SdfRenderError, match="source changed"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()


@pytest.mark.parametrize("kind", ["world", "target"])
def test_source_mutation_during_parse_cannot_bind_old_primitives_to_new_bytes(
    tmp_path, monkeypatch, kind
) -> None:
    _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)
    if kind == "world":
        original = renderer.base.load_world

        def mutate_world(task_root, relative):
            parsed = original(task_root, relative)
            path = task_root / relative
            path.write_bytes(path.read_bytes() + b"\n<!-- changed after parse -->\n")
            return parsed

        monkeypatch.setattr(renderer.base, "load_world", mutate_world)
    else:
        original = renderer.curriculum.load_target

        def mutate_target(task_root, relative):
            parsed = original(task_root, relative)
            path = task_root / relative
            path.write_bytes(path.read_bytes() + b"\n<!-- changed after parse -->\n")
            return parsed

        monkeypatch.setattr(renderer.curriculum, "load_target", mutate_target)
    output = tmp_path / "unsealed"
    args = renderer.parse_arguments(["--repo-root", str(root), "--output-dir", str(output)])
    with pytest.raises(renderer.base.SdfRenderError, match="source changed"):
        renderer.render_dataset(args)
    assert not output.exists()


def test_imported_helper_copy_is_rechecked_at_sealing(tmp_path, monkeypatch) -> None:
    _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)
    imported_helper = tmp_path / "imported-curriculum.py"
    imported_helper.write_bytes((ROOT / renderer.curriculum.RENDERER_PATH).read_bytes())
    monkeypatch.setattr(renderer.curriculum, "__file__", str(imported_helper))
    fixture = renderer.curriculum._render_sample

    def mutate_imported(*arguments):
        result = fixture(*arguments)
        imported_helper.write_bytes(imported_helper.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(renderer.curriculum, "_render_sample", mutate_imported)
    args = renderer.parse_arguments(
        [
            "--repo-root",
            str(root),
            "--output-dir",
            str(tmp_path / "imported-drift"),
            "--samples-per-split",
            "1",
            "--width",
            "16",
            "--height",
            "16",
        ]
    )
    with pytest.raises(renderer.base.SdfRenderError, match="imported renderer source differs"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()


def test_inconsistent_mask_cannot_be_sealed_as_supervision(tmp_path, monkeypatch) -> None:
    _mock_blender(monkeypatch)
    fixture = renderer.curriculum._render_sample

    def inconsistent(*arguments):
        visible, amodal, matrix = fixture(*arguments)
        return visible, bytes(len(amodal)), matrix

    monkeypatch.setattr(renderer.curriculum, "_render_sample", inconsistent)
    args = renderer.parse_arguments(
        [
            "--output-dir",
            str(tmp_path / "bad-mask"),
            "--samples-per-split",
            "1",
            "--width",
            "16",
            "--height",
            "16",
        ]
    )
    with pytest.raises(renderer.base.SdfRenderError, match="inconsistent rendered masks"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()


@pytest.mark.parametrize(
    "extra", [["--samples-per-split", "0"], ["--seed", "-1"], ["--width", "0"]]
)
def test_invalid_cli_values_rejected(extra) -> None:
    with pytest.raises(SystemExit):
        renderer.parse_arguments(["--output-dir", "unused", *extra])
