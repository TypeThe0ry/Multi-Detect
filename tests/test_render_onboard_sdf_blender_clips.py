from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import render_onboard_sdf_blender_clips as renderer
from scripts import train_onboard_sdf_visual_candidate as trainer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def inputs():
    worlds = {
        split: renderer.base.load_world(ROOT, path)
        for split, path in renderer.base.WORLD_PATHS.items()
    }
    targets = {
        key: renderer.curriculum.load_target(ROOT, path)
        for key, path in renderer.curriculum.TARGET_PATHS.items()
    }
    return worlds, targets


def _plan(inputs, split="train", index=0, frames=12, seed=19):
    worlds, targets = inputs
    return renderer.plan_clip(ROOT, worlds[split], split, seed, index, frames, 8.0, targets)


def test_seeded_clip_preserves_appearance_and_has_continuous_translation(inputs) -> None:
    clip = _plan(inputs)
    assert clip == _plan(inputs)
    assert clip != _plan(inputs, seed=20)
    static = [tuple(p for p in f.world.primitives if not p.target) for f in clip.frames]
    assert all(value == static[0] for value in static)
    assert all(frame.lighting == clip.frames[0].lighting for frame in clip.frames)
    target_appearance = [
        tuple(
            (p.name, p.geometry, p.dimensions, p.diffuse, p.emissive)
            for p in f.world.primitives
            if p.target
        )
        for f in clip.frames
    ]
    assert all(value == target_appearance[0] for value in target_appearance)
    centers = [frame.world.target_center for frame in clip.frames]
    cameras = [frame.camera["location"] for frame in clip.frames]
    for positions in (centers, cameras):
        steps = [
            tuple(b[k] - a[k] for k in range(3))
            for a, b in zip(positions, positions[1:], strict=False)
        ]
        assert math.dist(steps[0], (0, 0, 0)) > 0
        for step in steps:
            assert step == pytest.approx(steps[0])
    assert all(frame.camera["look_at"] == clip.frames[0].camera["look_at"] for frame in clip.frames)
    assert clip.phases[0] == "visible_before"
    assert clip.phases[-1] == "visible_after"
    assert "occlusion_window" in clip.phases
    assert not clip.disappearance_frames


def _screen_blocks_point(camera, point, screen, azimuth):
    toward = (math.cos(azimuth), math.sin(azimuth))
    right = (-toward[1], toward[0])
    center = tuple(screen.matrix[k][3] for k in range(3))

    def dot(v, axis):
        return sum(v[k] * axis[k] for k in range(2))

    denominator = dot(tuple(point[k] - camera[k] for k in range(3)), toward)
    fraction = dot(tuple(center[k] - camera[k] for k in range(3)), toward) / denominator
    intersection = tuple(camera[k] + fraction * (point[k] - camera[k]) for k in range(3))
    return (
        0 < fraction < 1
        and abs(dot(tuple(intersection[k] - center[k] for k in range(3)), right))
        <= screen.dimensions[0] / 2
        and 0 <= intersection[2] <= screen.dimensions[2]
    )


@pytest.mark.parametrize("split,index", [("train", 0), ("train", 1), ("val", 2), ("test", 0)])
def test_fixed_opaque_screen_intercepts_target_rays_not_a_drawn_box(inputs, split, index) -> None:
    """Analytic scene-plan check, not evidence of an actual Blender mask render."""
    clip = _plan(inputs, split, index)
    blocked = []
    for plan in clip.frames:
        screen = next(
            p for p in plan.world.primitives if p.name == "clips/fixed_construction_screen"
        )
        assert plan.roles[screen.name] == "fixed_physical_occluder"
        assert screen.geometry == "box"
        assert screen.matrix[2][3] == screen.dimensions[2] / 2
        corners = [
            point for p in plan.world.primitives if p.target for point in renderer.base._corners(p)
        ]
        hits = sum(
            _screen_blocks_point(plan.camera["location"], point, screen, plan.camera["azimuth_rad"])
            for point in corners
        )
        blocked.append((hits, len(corners)))
    assert blocked[0][0] == blocked[-1][0] == 0
    assert any(hits == count for hits, count in blocked)
    assert any(0 < hits < count for hits, count in blocked)


def test_complete_clips_have_explicit_disjoint_geometry_split(inputs) -> None:
    plans = {
        split: [_plan(inputs, split, i) for i in range(6)] for split in ("train", "val", "test")
    }
    for split in ("train", "val"):
        assert {clip.frames[0].target_model["geometry_key"] for clip in plans[split]} == {
            "original",
            "rover_long",
            "cargo_cart",
        }
    assert {clip.frames[0].target_model["geometry_key"] for clip in plans["test"]} == {
        "utility_van"
    }
    for split, clips in plans.items():
        for clip in clips:
            assert clip.split == split
            assert all(
                frame.camera["split_seed_sha256"] == clip.frames[0].camera["split_seed_sha256"]
                for frame in clip.frames
            )
            assert len({frame.target_model["geometry_sha256"] for frame in clip.frames}) == 1


def test_artificial_disappearance_is_minority_explicit_and_all_passes_share_state(inputs) -> None:
    clips = [_plan(inputs, index=i) for i in range(5)]
    assert sum(bool(clip.disappearance_frames) for clip in clips) == 1
    last = clips[-1]
    assert last.disappearance_frames == (9, 10)
    for index, frame in enumerate(last.frames):
        assert frame.camera["target_present"] == (index not in last.disappearance_frames)
        assert (frame.planned_condition == "absent") == (index in (9, 10))
        inventory = renderer.curriculum._primitive_inventory(frame)
        assert all(
            item["hidden_for_absent"] == (item["target"] and index in (9, 10)) for item in inventory
        )
    assert last.frames[-1].camera["target_present"] is True


def _mock_blender(monkeypatch):
    """Only manifest/adapter contract checks, not real rendering acceptance."""
    monkeypatch.setitem(
        sys.modules,
        "bpy",
        SimpleNamespace(app=SimpleNamespace(version=(4, 5, 3), version_string="4.5.3-fixture")),
    )
    monkeypatch.setitem(sys.modules, "mathutils", SimpleNamespace())

    def sample(_bpy, _mathutils, plan, args, image, raw_mask, raw_amodal):
        count = args.width * args.height
        amodal = b"\xff\xff" + bytes(count - 2)
        if not plan.camera["target_present"]:
            visible = amodal = bytes(count)
        elif plan.planned_condition == "full_occlusion":
            visible = bytes(count)
        elif plan.planned_condition == "partial_occlusion":
            visible = b"\xff" + bytes(count - 1)
        else:
            visible = amodal
        image.write_bytes(b"rgb-fixture-" + plan.sample_seed_sha256.encode())
        raw_mask.write_bytes(renderer.base._mask_png(visible, args.width, args.height))
        raw_amodal.write_bytes(renderer.base._mask_png(amodal, args.width, args.height))
        return visible, amodal, [list(row) for row in renderer.base.IDENTITY]

    monkeypatch.setattr(renderer.curriculum, "_render_sample", sample)
    return sample


def _args(tmp_path, *extras):
    return renderer.parse_arguments(
        [
            "--output-dir",
            str(tmp_path / "clips"),
            "--clips-per-split",
            "1",
            "--frames-per-clip",
            "12",
            "--fps",
            "7.5",
            "--width",
            "16",
            "--height",
            "16",
            *extras,
        ]
    )


def test_flat_v1_manifest_and_complete_clip_inventory_bind_pixels_time_and_offline_truth(
    tmp_path, monkeypatch
) -> None:
    _mock_blender(monkeypatch)
    args = _args(tmp_path, "--clips-per-split", "5")
    result = renderer.render_dataset(args)
    assert result["contract"] == renderer.base.CONTRACT
    assert result["frame_count"] == 180
    assert result["clip_count"] == 15
    assert result["runtime_replay_input_allowlist"] == ["image_path", "image_sha256", "dt"]
    loaded, _ = trainer.verify_dataset(args.output_dir / "manifest.json")
    assert loaded == json.loads(json.dumps(result))
    lookup = {frame["frame_id"]: frame for frame in result["frames"]}
    assert len(lookup) == result["frame_count"]
    for clip in result["clips"]:
        assert clip["timestamp_semantics"] == renderer.TIME_SEMANTICS
        assert clip["offline_truth_only"] is True
        assert len(clip["frame_ids"]) == clip["frame_count"] == 12
        for index, frame_id in enumerate(clip["frame_ids"]):
            frame = lookup[frame_id]
            assert frame["clip_id"] == clip["clip_id"]
            assert frame["split"] == clip["split"]
            assert frame["frame_index"] == index
            assert frame["captured_at_s"] == pytest.approx(index / clip["fps"])
            assert frame["dt"] == (0.0 if index == 0 else 1 / clip["fps"])
            assert frame["offline_truth_only"] is True
            assert frame["rgb_payload_sha256"] == frame["image_sha256"]
            assert set(renderer.SOURCE_SCRIPTS) <= set(frame["source_hashes"])
            for kind in ("image", "label", "mask", "mask_render", "amodal_mask", "amodal_render"):
                assert (
                    renderer.base.sha256(args.output_dir / frame[f"{kind}_path"])
                    == (frame[f"{kind}_sha256"])
                )
            if frame["offline_disappearance_event"]:
                assert frame["visibility_measurement"]["measured_condition"] == "absent"
                assert frame["visibility_measurement"]["amodal_target_pixels"] == 0
            if not frame["target_visible"]:
                assert (args.output_dir / frame["label_path"]).read_text() == ""
    for claim in (
        "training_ready",
        "gazebo_capture",
        "flight",
        "promotion",
        "hardware_acceptance",
        "hil_acceptance",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "flight_action_labels_created",
    ):
        assert result[claim] is False
    assert not any(
        "task_label" in key or "flight_label" in key for frame in lookup.values() for key in frame
    )


def test_planned_full_occlusion_is_not_used_as_measured_visibility(tmp_path, monkeypatch) -> None:
    fixture = _mock_blender(monkeypatch)

    def unobstructed(*args):
        _, amodal, matrix = fixture(*args)
        return amodal, amodal, matrix

    monkeypatch.setattr(renderer.curriculum, "_render_sample", unobstructed)
    result = renderer.render_dataset(_args(tmp_path))
    assert any(frame["planned_condition"] == "full_occlusion" for frame in result["frames"])
    assert all(clip["measured_condition_counts"] == {"clear": 12} for clip in result["clips"])


def _copy_sources(tmp_path):
    root = tmp_path / "repository"
    for relative in (
        *renderer.base.WORLD_PATHS.values(),
        *renderer.curriculum.TARGET_PATHS.values(),
        *renderer.SOURCE_SCRIPTS,
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return root


@pytest.mark.parametrize(
    "relative", [*renderer.SOURCE_SCRIPTS, renderer.curriculum.TARGET_PATHS["cargo_cart"]]
)
def test_every_source_including_new_script_is_rechecked_before_sealing(
    tmp_path, monkeypatch, relative
) -> None:
    fixture = _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)

    def mutate(*args):
        value = fixture(*args)
        source = root / relative
        source.write_bytes(source.read_bytes() + b"\n")
        return value

    monkeypatch.setattr(renderer.curriculum, "_render_sample", mutate)
    args = _args(tmp_path, "--repo-root", str(root))
    with pytest.raises(renderer.base.SdfRenderError, match="source changed"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()
    assert not (args.output_dir / "dataset.yaml").exists()


def test_imported_script_provenance_cannot_be_replaced_with_a_different_copy(tmp_path) -> None:
    root = _copy_sources(tmp_path)
    source = root / renderer.RENDERER_PATH
    source.write_bytes(source.read_bytes() + b"\n")
    args = _args(tmp_path, "--repo-root", str(root))
    with pytest.raises(renderer.base.SdfRenderError, match="imported renderer source differs"):
        renderer.render_dataset(args)
    assert not args.output_dir.exists()


@pytest.mark.parametrize("module", [renderer, renderer.base, renderer.curriculum])
def test_imported_runtime_source_is_rechecked_even_with_separate_repo_root(
    tmp_path, monkeypatch, module
) -> None:
    fixture = _mock_blender(monkeypatch)
    runtime_source = tmp_path / "imported.py"
    shutil.copyfile(module.__file__, runtime_source)
    monkeypatch.setattr(module, "__file__", str(runtime_source))

    def mutate(*args):
        value = fixture(*args)
        runtime_source.write_bytes(runtime_source.read_bytes() + b"\n")
        return value

    monkeypatch.setattr(renderer.curriculum, "_render_sample", mutate)
    args = _args(tmp_path)
    with pytest.raises(renderer.base.SdfRenderError, match="imported renderer source changed"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()
    assert not (args.output_dir / "dataset.yaml").exists()


def test_test_geometry_duplicates_are_rejected_before_blender_import(tmp_path) -> None:
    root = _copy_sources(tmp_path)
    original = root / renderer.curriculum.TARGET_PATHS["original"]
    test = root / renderer.curriculum.TARGET_PATHS["utility_van"]
    test.write_bytes(original.read_bytes() + b"\n<!-- file hash differs, geometry does not -->\n")
    args = _args(tmp_path, "--repo-root", str(root))
    with pytest.raises(renderer.base.SdfRenderError, match="geometry must be hash-disjoint"):
        renderer.render_dataset(args)
    assert not args.output_dir.exists()


@pytest.mark.parametrize("bad_mask", ["dimensions", "outside_amodal", "absent_has_pixels"])
def test_inconsistent_masks_fail_closed(tmp_path, monkeypatch, bad_mask) -> None:
    fixture = _mock_blender(monkeypatch)

    def inconsistent(*args):
        visible, amodal, matrix = fixture(*args)
        if bad_mask == "dimensions":
            visible = b"\0"
        elif bad_mask == "outside_amodal":
            visible = bytes(len(amodal) - 1) + b"\xff"
        else:
            args[2].camera["target_present"] = False
        return visible, amodal, matrix

    monkeypatch.setattr(renderer.curriculum, "_render_sample", inconsistent)
    args = _args(tmp_path)
    with pytest.raises(renderer.base.SdfRenderError, match="dimensions mismatch|inconsistent"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()


def test_existing_output_is_never_overwritten(tmp_path) -> None:
    args = _args(tmp_path)
    args.output_dir.mkdir()
    sentinel = args.output_dir / "user.txt"
    sentinel.write_text("keep")
    with pytest.raises(renderer.base.SdfRenderError, match="refusing overwrite"):
        renderer.render_dataset(args)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize(
    "option,value",
    [
        ("--clips-per-split", "0"),
        ("--frames-per-clip", "11"),
        ("--fps", "0"),
        ("--fps", "nan"),
        ("--fps", "inf"),
        ("--width", "15"),
        ("--threads", "0"),
    ],
)
def test_invalid_cli_parameters_fail_before_render(tmp_path, option, value) -> None:
    with pytest.raises(SystemExit):
        _args(tmp_path, option, value)


@pytest.mark.parametrize(
    "split,index,frames,fps",
    [
        ("other", 0, 12, 8.0),
        ("train", -1, 12, 8.0),
        ("train", 0, 11, 8.0),
        ("train", 0, 12, float("nan")),
    ],
)
def test_invalid_plan_parameters_fail_closed(inputs, split, index, frames, fps) -> None:
    worlds, targets = inputs
    with pytest.raises(renderer.base.SdfRenderError):
        renderer.plan_clip(ROOT, worlds["train"], split, 1, index, frames, fps, targets)
