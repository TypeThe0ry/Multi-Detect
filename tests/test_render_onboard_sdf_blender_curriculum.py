from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import render_onboard_sdf_blender_curriculum as renderer
from scripts import train_onboard_sdf_visual_candidate as trainer

ROOT = Path(__file__).resolve().parents[1]


def _inputs(root: Path = ROOT):
    worlds = {
        split: renderer.base.load_world(root, relative)
        for split, relative in renderer.base.WORLD_PATHS.items()
    }
    targets = {
        key: renderer.load_target(root, relative) for key, relative in renderer.TARGET_PATHS.items()
    }
    return worlds, targets


def test_repository_geometries_are_distinct_and_test_is_held_out() -> None:
    _, targets = _inputs()
    fingerprints = {key: renderer.geometry_sha256(value) for key, value in targets.items()}
    assert len(set(fingerprints.values())) == 4
    assert renderer.SPLIT_GEOMETRIES["train"] == renderer.SPLIT_GEOMETRIES["val"]
    assert renderer.SPLIT_GEOMETRIES["test"] == ("utility_van",)
    for value in targets.values():
        assert len(value) >= 8
        assert all(primitive.target for primitive in value)


def test_geometry_fingerprint_does_not_treat_material_or_name_as_new_geometry() -> None:
    from dataclasses import replace

    _, targets = _inputs()
    original = targets["original"]
    changed = tuple(
        replace(p, name=p.name + "different", diffuse=(1.0, 0.0, 0.0, 1.0)) for p in original
    )
    assert renderer.geometry_sha256(original) == renderer.geometry_sha256(changed)


def test_seeded_plans_include_actual_entities_lighting_and_source_identity() -> None:
    worlds, targets = _inputs()
    plans = [
        renderer.plan_sample(ROOT, worlds["train"], "train", 71, i, targets) for i in range(12)
    ]
    assert plans[0] == renderer.plan_sample(ROOT, worlds["train"], "train", 71, 0, targets)
    assert plans[0] != renderer.plan_sample(ROOT, worlds["train"], "train", 72, 0, targets)
    assert plans[0] != renderer.plan_sample(ROOT, worlds["val"], "val", 71, 0, targets)
    assert [p.planned_condition for p in plans[:4]] == list(renderer.CONDITIONS)
    assert {p.target_model["geometry_key"] for p in plans} == {
        "original",
        "rover_long",
        "cargo_cart",
    }
    for plan in plans:
        source_hashes = dict(plan.world.sources)
        assert source_hashes[renderer.BASE_RENDERER_PATH] == renderer.base.sha256(
            ROOT / renderer.BASE_RENDERER_PATH
        )
        assert source_hashes[renderer.RENDERER_PATH] == renderer.base.sha256(
            ROOT / renderer.RENDERER_PATH
        )
        assert source_hashes[plan.target_model["path"]] == plan.target_model["sha256"]
        assert plan.camera["target_present"] == (plan.planned_condition != "absent")
        assert set(plan.roles.values()) >= {
            "source_world_background",
            "target",
            "background_facility",
            "non_target_distractor",
        }
        assert len(renderer._primitive_inventory(plan)) == len(plan.world.primitives)
        assert sum(p.name.startswith("curriculum/distractor_") for p in plan.world.primitives) == 5
        assert plan.target_model["world_bounds_m"][0][2] == pytest.approx(0.1)
        if plan.planned_condition in {"partial_occlusion", "full_occlusion"}:
            screen = next(
                p for p in plan.world.primitives if plan.roles[p.name] == "planned_occluder"
            )
            assert screen.geometry == "box"
            assert screen.matrix[2][3] == pytest.approx(screen.dimensions[2] / 2)
        else:
            assert "planned_occluder" not in plan.roles.values()
    assert len({p.lighting["sdf_intensity"] for p in plans}) == len(plans)
    assert len({p.camera["location"][0] for p in plans}) == len(plans)


@pytest.mark.parametrize(
    "visible,amodal,present,condition,fraction",
    [
        (b"\xff\xff\0", b"\xff\xff\0", True, "clear", 1.0),
        (b"\xff\0\0", b"\xff\xff\0", True, "partially_occluded", 0.5),
        (b"\0\0\0", b"\xff\xff\0", True, "fully_occluded", 0.0),
        (b"\0\0\0", b"\0\0\0", False, "absent", None),
        (b"\0\0\0", b"\0\0\0", True, "out_of_frame", None),
        (b"\0\0\xff", b"\xff\xff\0", True, "inconsistent_mask", 0.0),
        (b"\xff\0\0", b"\xff\0\0", False, "inconsistent_absent_mask", 1.0),
    ],
)
def test_visibility_is_measured_not_inferred_from_planned_condition(
    visible: bytes, amodal: bytes, present: bool, condition: str, fraction: float | None
) -> None:
    measured = renderer.measure_visibility(visible, amodal, present)
    assert measured["measured_condition"] == condition
    assert measured["visible_fraction"] == fraction
    assert measured["visible_target_pixels"] == sum(bool(value) for value in visible)
    assert measured["amodal_target_pixels"] == sum(bool(value) for value in amodal)


def test_mask_dimension_mismatch_is_rejected() -> None:
    with pytest.raises(renderer.base.SdfRenderError, match="dimensions mismatch"):
        renderer.measure_visibility(b"\0", b"\0\0", True)


@pytest.mark.parametrize("render_fails", [False, True])
def test_amodal_pass_hides_only_other_geometry_and_restores_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, render_fails: bool
) -> None:
    class Object(SimpleNamespace):
        def __getitem__(self, key):
            return self.target if key == "sdf_target" else None

    objects = [
        Object(target=True, hide_render=False, data=SimpleNamespace(materials=["target-rgb"])),
        Object(target=False, hide_render=False, data=SimpleNamespace(materials=["wall-rgb"])),
        Object(target=True, hide_render=True, data=SimpleNamespace(materials=["absent-rgb"])),
    ]
    background_input = SimpleNamespace(default_value=(0.5, 0.6, 0.7, 1))
    background = SimpleNamespace(inputs={"Color": background_input})
    scene = SimpleNamespace(
        world=SimpleNamespace(node_tree=SimpleNamespace(nodes={"Background": background})),
        render=SimpleNamespace(image_settings=SimpleNamespace(color_mode="RGB"), filepath=""),
    )
    removed = []

    def render(**_):
        assert [obj.hide_render for obj in objects] == [False, True, True]
        assert background_input.default_value == (0, 0, 0, 1)
        if render_fails:
            raise RuntimeError("fixture render failed")

    def fill(pixels):
        for index in range(len(pixels)):
            pixels[index] = 1.0 if index < 4 else 0.0

    image = SimpleNamespace(size=(2, 2), pixels=SimpleNamespace(foreach_get=fill))
    bpy = SimpleNamespace(
        ops=SimpleNamespace(render=SimpleNamespace(render=render)),
        data=SimpleNamespace(
            images=SimpleNamespace(load=lambda *_args, **_kwargs: image, remove=removed.append),
            materials=SimpleNamespace(remove=removed.append),
        ),
    )
    monkeypatch.setattr(renderer.base, "_material", lambda *_args: "white-emission-fixture")
    if render_fails:
        with pytest.raises(RuntimeError, match="fixture render failed"):
            renderer._render_amodal_mask(bpy, scene, objects, tmp_path / "amodal.png", 2, 2)
    else:
        mask = renderer._render_amodal_mask(bpy, scene, objects, tmp_path / "amodal.png", 2, 2)
        assert mask == bytes((0, 0, 255, 0))
        assert image in removed
    assert [obj.hide_render for obj in objects] == [False, False, True]
    assert [obj.data.materials for obj in objects] == [["target-rgb"], ["wall-rgb"], ["absent-rgb"]]
    assert background_input.default_value == (0.5, 0.6, 0.7, 1)
    assert "white-emission-fixture" in removed


def _mock_blender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract fixture only; this does not assert that Blender rendered correctly."""
    monkeypatch.setitem(
        sys.modules,
        "bpy",
        SimpleNamespace(
            app=SimpleNamespace(version=(4, 5, 3), version_string="4.5.3-test-fixture")
        ),
    )
    monkeypatch.setitem(sys.modules, "mathutils", SimpleNamespace())

    def sample(_bpy, _mathutils, plan, args, image, raw_mask, raw_amodal):
        count = args.width * args.height
        reference = b"\xff\xff" + bytes(count - 2)
        if not plan.camera["target_present"]:
            visible = reference = bytes(count)
        elif plan.planned_condition == "partial_occlusion":
            visible = b"\xff" + bytes(count - 1)
        elif plan.planned_condition == "full_occlusion":
            visible = bytes(count)
        else:
            visible = reference
        image.write_bytes(b"mock-rgb-" + plan.sample_seed_sha256.encode())
        raw_mask.write_bytes(renderer.base._mask_png(visible, args.width, args.height))
        raw_amodal.write_bytes(renderer.base._mask_png(reference, args.width, args.height))
        return visible, reference, [list(row) for row in renderer.base.IDENTITY]

    monkeypatch.setattr(renderer, "_render_sample", sample)


def test_complete_manifest_is_compatible_with_existing_trainer_and_binds_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_blender(monkeypatch)
    output = tmp_path / "curriculum"
    args = renderer.parse_arguments(
        ["--output-dir", str(output), "--samples-per-split", "4", "--width", "16", "--height", "16"]
    )
    result = renderer.render_dataset(args)
    assert result["frame_count"] == 12
    assert result["names"] == ["search_target"]
    assert result["curriculum"]["planned_condition_counts"] == dict.fromkeys(renderer.CONDITIONS, 3)
    assert result["curriculum"]["measured_condition_counts"] == {
        "clear": 3,
        "partially_occluded": 3,
        "fully_occluded": 3,
        "absent": 3,
    }
    assert result["curriculum"]["test_geometry_disjoint_from_train_and_val"] is True
    document, data = trainer.verify_dataset(output / "manifest.json")
    assert document == json.loads(json.dumps(result))
    assert data == output / "dataset.yaml"
    for claim in (
        "training_ready",
        "gazebo_capture",
        "flight",
        "promotion",
        "final_release_allowed",
    ):
        assert result[claim] is False
    for frame in result["frames"]:
        assert frame["offline_truth_only"] is True
        assert frame["target_model"]["path"] in frame["source_hashes"]
        assert (
            frame["curriculum"]["visible_fraction"]
            == frame["visibility_measurement"]["visible_fraction"]
        )
        for kind in ("image", "label", "mask", "mask_render", "amodal_mask", "amodal_render"):
            assert renderer.base.sha256(output / frame[f"{kind}_path"]) == frame[f"{kind}_sha256"]
        if frame["planned_condition"] in {"absent", "full_occlusion"}:
            assert frame["visible_bbox_xyxy_px"] is None
            assert (output / frame["label_path"]).read_text() == ""


def test_existing_output_is_preserved_before_blender_import(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    user = output / "keep.txt"
    user.write_text("user work")
    args = renderer.parse_arguments(["--output-dir", str(output)])
    with pytest.raises(renderer.base.SdfRenderError, match="refusing overwrite"):
        renderer.render_dataset(args)
    assert user.read_text() == "user work"


def _copy_sources(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    for relative in [
        *renderer.base.WORLD_PATHS.values(),
        *renderer.TARGET_PATHS.values(),
        renderer.BASE_RENDERER_PATH,
        renderer.RENDERER_PATH,
    ]:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return root


def test_duplicate_geometry_cannot_be_called_independent_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)
    original = root / renderer.TARGET_PATHS["original"]
    duplicate = root / renderer.TARGET_PATHS["utility_van"]
    duplicate.write_bytes(original.read_bytes() + b"\n<!-- changed file hash only -->\n")
    args = renderer.parse_arguments(
        ["--repo-root", str(root), "--output-dir", str(tmp_path / "bad")]
    )
    with pytest.raises(renderer.base.SdfRenderError, match="geometry must be hash-disjoint"):
        renderer.render_dataset(args)
    assert not args.output_dir.exists()


def test_changed_target_source_prevents_manifest_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)
    fixture = renderer._render_sample

    def mutate(*args):
        value = fixture(*args)
        source = root / renderer.TARGET_PATHS["cargo_cart"]
        source.write_bytes(source.read_bytes() + b"\n")
        return value

    monkeypatch.setattr(renderer, "_render_sample", mutate)
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
    assert not (args.output_dir / "dataset.yaml").exists()


def test_actual_measurements_do_not_copy_plan_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_blender(monkeypatch)
    fixture = renderer._render_sample

    def unoccluded(*args):
        visible, amodal, matrix = fixture(*args)
        return amodal, amodal, matrix

    monkeypatch.setattr(renderer, "_render_sample", unoccluded)
    args = renderer.parse_arguments(
        [
            "--output-dir",
            str(tmp_path / "unexpected"),
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


@pytest.mark.parametrize("split,index", [("unknown", 0), ("train", -1)])
def test_invalid_plan_identity_is_rejected(split: str, index: int) -> None:
    worlds, targets = _inputs()
    with pytest.raises(renderer.base.SdfRenderError, match="invalid split"):
        renderer.plan_sample(ROOT, worlds["train"], split, 1, index, targets)
