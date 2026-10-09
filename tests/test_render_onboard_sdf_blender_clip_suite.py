from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import render_onboard_sdf_blender_clip_suite as renderer
from scripts import replay_onboard_visual_clips as replay
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


def _plan(inputs, split="test", index=0, frames=16, seed=20261012):
    worlds, targets = inputs
    return renderer.plan_clip(ROOT, worlds[split], split, seed, index, frames, 8.0, targets)


@pytest.mark.parametrize(
    "index,band,bounds",
    [
        (0, "near", (21, 27)),
        (1, "medium", (30, 38)),
        (2, "far", (40, 50)),
    ],
)
def test_three_test_clips_cover_actual_distance_angle_light_and_edge_view(
    inputs, index, band, bounds
):
    clip = _plan(inputs, index=index)
    assert clip.split == "test"
    assert len(clip.frames) == 16
    assert clip.frames[0].camera["suite_profile"]["key"] == band
    assert clip.frames[0].target_model["geometry_key"] == "utility_van"
    for frame in clip.frames:
        assert frame.world.relative_path == renderer.base.WORLD_PATHS["test"]
        assert frame.camera["target_present"] is True
        distance = math.dist(frame.camera["location"], frame.world.target_center)
        assert bounds[0] <= distance <= bounds[1]
        assert distance == frame.camera["slant_distance_m"]
        assert renderer.RENDERER_PATH in dict(frame.world.sources)
    if band != "near":
        assert clip.frames[0].camera["look_at_horizontal_jitter_m"] != 0


def test_seeded_suite_preserves_static_geometry_materials_light_and_continuity(inputs):
    clip = _plan(inputs)
    assert clip == _plan(inputs)
    assert clip != _plan(inputs, seed=20261013)
    assert clip.phases[0] == "visible_before" and clip.phases[-1] == "visible_after"
    assert "occlusion_window" in clip.phases
    assert not clip.disappearance_frames
    static = [tuple(p for p in frame.world.primitives if not p.target) for frame in clip.frames]
    assert all(value == static[0] for value in static)
    assert all(frame.lighting == clip.frames[0].lighting for frame in clip.frames)
    appearance = [
        tuple(
            (p.geometry, p.dimensions, p.diffuse, p.emissive)
            for p in frame.world.primitives
            if p.target
        )
        for frame in clip.frames
    ]
    assert all(value == appearance[0] for value in appearance)
    for positions in (
        [frame.world.target_center for frame in clip.frames],
        [frame.camera["location"] for frame in clip.frames],
    ):
        steps = [
            tuple(b[k] - a[k] for k in range(3))
            for a, b in zip(positions, positions[1:], strict=False)
        ]
        assert math.dist(steps[0], (0, 0, 0)) > 0
        for step in steps:
            assert step == pytest.approx(steps[0])


def _blocks(camera, point, screen, azimuth):
    toward, right = (math.cos(azimuth), math.sin(azimuth)), (-math.sin(azimuth), math.cos(azimuth))
    center = tuple(screen.matrix[k][3] for k in range(3))

    def dot(vector, axis):
        return sum(vector[k] * axis[k] for k in range(2))

    fraction = dot(tuple(center[k] - camera[k] for k in range(3)), toward) / dot(
        tuple(point[k] - camera[k] for k in range(3)), toward
    )
    hit = tuple(camera[k] + fraction * (point[k] - camera[k]) for k in range(3))
    return (
        0 < fraction < 1
        and 0 <= hit[2] <= screen.dimensions[2]
        and abs(dot(tuple(hit[k] - center[k] for k in range(3)), right)) <= screen.dimensions[0] / 2
    )


@pytest.mark.parametrize(
    "seed,index", [(20261012, i) for i in range(3)] + [(20261013, i) for i in range(3)]
)
def test_fixed_ground_supported_screen_physically_blocks_rays_then_target_reappears(
    inputs, seed, index
):
    """Scene-plan geometry check, not an assertion that Blender actually rendered it."""
    clip = _plan(inputs, index=index, seed=seed)
    counts = []
    for frame in clip.frames:
        screens = [
            p for p in frame.world.primitives if frame.roles[p.name] == "fixed_physical_occluder"
        ]
        assert len(screens) == 1
        screen = screens[0]
        assert screen.geometry == "box"
        assert screen.matrix[2][3] == screen.dimensions[2] / 2
        corners = [
            point for p in frame.world.primitives if p.target for point in renderer.base._corners(p)
        ]
        count = sum(
            _blocks(frame.camera["location"], point, screen, frame.camera["azimuth_rad"])
            for point in corners
        )
        counts.append((count, len(corners)))
    assert counts[0][0] == counts[-1][0] == 0
    assert any(count == total for count, total in counts)
    assert any(0 < count < total for count, total in counts)


def test_profiles_are_not_bound_to_target_geometry_or_material_color(inputs, monkeypatch):
    clips = [_plan(inputs, split, i) for split in ("train", "val") for i in range(3)]
    mappings = {}
    for clip in clips:
        mappings.setdefault(clip.frames[0].target_model["geometry_key"], set()).add(
            clip.frames[0].camera["suite_profile"]["key"]
        )
    assert set(mappings) == {"original", "rover_long", "cargo_cart"}
    assert all(len(profiles) >= 2 for profiles in mappings.values())
    # The nine-clip curriculum fully crosses each geometry with all profiles.
    mappings = {}
    for i in range(9):
        first = _plan(inputs, "train", i).frames[0]
        mappings.setdefault(first.target_model["geometry_key"], set()).add(
            first.camera["suite_profile"]["key"]
        )
    assert all(value == {"near", "medium", "far"} for value in mappings.values())
    baseline = _plan(inputs)
    monkeypatch.setattr(renderer, "PROFILES", renderer.PROFILES[1:] + renderer.PROFILES[:1])
    changed = _plan(inputs)
    assert (
        baseline.frames[0].target_model["geometry_sha256"]
        == changed.frames[0].target_model["geometry_sha256"]
    )
    assert [p.diffuse for p in baseline.frames[0].world.primitives if p.target] == [
        p.diffuse for p in changed.frames[0].world.primitives if p.target
    ]
    assert baseline.frames[0].lighting != changed.frames[0].lighting


def test_suite_reuses_legacy_plan_without_mutating_it_or_preserving_artificial_hide(inputs):
    worlds, targets = inputs
    before = renderer.legacy.plan_clip(ROOT, worlds["test"], "test", 20261012, 4, 16, 8, targets)
    suite = _plan(inputs, index=4)
    after = renderer.legacy.plan_clip(ROOT, worlds["test"], "test", 20261012, 4, 16, 8, targets)
    assert before == after
    assert before.disappearance_frames
    assert not suite.disappearance_frames
    assert all(frame.camera["target_present"] for frame in suite.frames)


def _mock_blender(monkeypatch):
    """Contract fixture only: no real Blender rendering or model execution."""
    monkeypatch.setitem(
        sys.modules,
        "bpy",
        SimpleNamespace(app=SimpleNamespace(version=(4, 5, 3), version_string="4.5.3-fixture")),
    )
    monkeypatch.setitem(sys.modules, "mathutils", SimpleNamespace())

    def sample(_bpy, _mathutils, plan, args, image, raw_mask, raw_amodal):
        count = args.width * args.height
        amodal = b"\xff\xff" + bytes(count - 2)
        if plan.planned_condition == "full_occlusion":
            visible = bytes(count)
        elif plan.planned_condition == "partial_occlusion":
            visible = b"\xff" + bytes(count - 1)
        else:
            visible = amodal
        seed = hashlib.sha256(plan.sample_seed_sha256.encode()).digest()
        pixels = (seed * ((count + len(seed) - 1) // len(seed)))[:count]
        image.write_bytes(renderer.base._mask_png(pixels, args.width, args.height))
        raw_mask.write_bytes(renderer.base._mask_png(visible, args.width, args.height))
        raw_amodal.write_bytes(renderer.base._mask_png(amodal, args.width, args.height))
        return visible, amodal, [list(row) for row in renderer.base.IDENTITY]

    monkeypatch.setattr(renderer.curriculum, "_render_sample", sample)
    return sample


def _args(tmp_path, *extra):
    return renderer.parse_arguments(
        ["--output-dir", str(tmp_path / "suite"), "--width", "16", "--height", "16", *extra]
    )


def test_144_frame_suite_is_flat_dataset_and_replay_compatible_with_auditable_sources(
    tmp_path, monkeypatch
):
    _mock_blender(monkeypatch)
    old_hashes = {
        path: renderer.base.sha256(ROOT / path) for path in renderer.legacy.SOURCE_SCRIPTS
    }
    args = _args(tmp_path)
    document = renderer.render_dataset(args)
    assert document["frame_count"] == 144 and document["clip_count"] == 9
    assert document["seed"] == 20261012
    assert document["profile_design"] == (
        "joint_distance_angle_light_framing_conditions_not_single_factor_causal_test"
    )
    assert document["renderer"]["script_sha256"] == renderer.base.sha256(
        ROOT / renderer.RENDERER_PATH
    )
    assert (
        document["test_usage"]
        == "new_pixels_new_seed_development_test_not_untouched_blind_benchmark"
    )
    validated, _ = trainer.verify_dataset(args.output_dir / "manifest.json")
    assert validated == json.loads(json.dumps(document))
    groups = replay.validate_clips(validated)
    assert len(groups) == 9
    assert {clip["suite_profile"]["key"] for clip, _ in groups if clip["split"] == "test"} == {
        "near",
        "medium",
        "far",
    }
    assert document["runtime_replay_input_allowlist"] == ["image_path", "image_sha256", "dt"]
    for clip, frames in groups:
        assert clip["offline_truth_only"] is True
        assert len(frames) == 16
        assert clip["timestamp_semantics"] == renderer.legacy.TIME_SEMANTICS
        assert set(renderer.SOURCE_SCRIPTS) <= set(clip["source_hashes"])
        for index, frame in enumerate(frames):
            assert frame["frame_index"] == index
            assert frame["captured_at_s"] == index / 8
            assert frame["dt"] == (0 if index == 0 else 1 / 8)
            assert frame["offline_disappearance_event"] is False
            assert frame["offline_truth_only"] is True
            assert frame["rgb_payload_sha256"] == frame["image_sha256"]
            assert set(renderer.SOURCE_SCRIPTS) <= set(frame["source_hashes"])
            for kind in ("image", "label", "mask", "mask_render", "amodal_mask", "amodal_render"):
                assert (
                    renderer.base.sha256(args.output_dir / frame[f"{kind}_path"])
                    == frame[f"{kind}_sha256"]
                )
        if clip["split"] == "test":
            assert clip["target_model"]["geometry_key"] == "utility_van"
    assert old_hashes == {path: renderer.base.sha256(ROOT / path) for path in old_hashes}
    for flag in (
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
        assert document[flag] is False


def test_suite_does_not_copy_planned_occlusion_into_measurement(tmp_path, monkeypatch):
    fixture = _mock_blender(monkeypatch)

    def unoccluded(*args):
        _, amodal, matrix = fixture(*args)
        return amodal, amodal, matrix

    monkeypatch.setattr(renderer.curriculum, "_render_sample", unoccluded)
    document = renderer.render_dataset(_args(tmp_path))
    assert any(frame["planned_condition"] == "full_occlusion" for frame in document["frames"])
    assert all(clip["measured_condition_counts"] == {"clear": 16} for clip in document["clips"])


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
    "relative", [*renderer.SOURCE_SCRIPTS, renderer.curriculum.TARGET_PATHS["utility_van"]]
)
def test_source_changes_including_entry_prevent_manifest_sealing(tmp_path, monkeypatch, relative):
    fixture = _mock_blender(monkeypatch)
    root = _copy_sources(tmp_path)

    def mutate(*args):
        result = fixture(*args)
        source = root / relative
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(renderer.curriculum, "_render_sample", mutate)
    args = _args(tmp_path, "--repo-root", str(root))
    with pytest.raises(renderer.base.SdfRenderError, match="source changed"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()
    assert not (args.output_dir / "dataset.yaml").exists()


def test_source_change_during_parse_is_detected_before_blender_import(tmp_path, monkeypatch):
    root = _copy_sources(tmp_path)
    loader = renderer.curriculum.load_target

    def mutate(root, path):
        result = loader(root, path)
        source = root / path
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(renderer.curriculum, "load_target", mutate)
    args = _args(tmp_path, "--repo-root", str(root))
    with pytest.raises(renderer.base.SdfRenderError, match="source changed"):
        renderer.render_dataset(args)
    assert not args.output_dir.exists()


def test_imported_runtime_source_identity_cannot_be_forged(tmp_path):
    root = _copy_sources(tmp_path)
    source = root / renderer.RENDERER_PATH
    source.write_bytes(source.read_bytes() + b"\n")
    args = _args(tmp_path, "--repo-root", str(root))
    with pytest.raises(renderer.base.SdfRenderError, match="imported renderer source differs"):
        renderer.render_dataset(args)
    assert not args.output_dir.exists()


def test_duplicate_test_geometry_is_not_an_unseen_shape(tmp_path):
    root = _copy_sources(tmp_path)
    (root / renderer.curriculum.TARGET_PATHS["utility_van"]).write_bytes(
        (root / renderer.curriculum.TARGET_PATHS["original"]).read_bytes()
    )
    args = _args(tmp_path, "--repo-root", str(root))
    with pytest.raises(renderer.base.SdfRenderError, match="hash-disjoint"):
        renderer.render_dataset(args)
    assert not args.output_dir.exists()


@pytest.mark.parametrize("mask_error", ["dimensions", "outside_amodal"])
def test_inconsistent_masks_fail_closed(tmp_path, monkeypatch, mask_error):
    fixture = _mock_blender(monkeypatch)

    def inconsistent(*args):
        _, amodal, matrix = fixture(*args)
        visible = b"\0" if mask_error == "dimensions" else bytes(len(amodal) - 1) + b"\xff"
        return visible, amodal, matrix

    monkeypatch.setattr(renderer.curriculum, "_render_sample", inconsistent)
    args = _args(tmp_path)
    with pytest.raises(renderer.base.SdfRenderError, match="dimensions mismatch|inconsistent"):
        renderer.render_dataset(args)
    assert not (args.output_dir / "manifest.json").exists()


def test_existing_output_is_preserved(tmp_path):
    args = _args(tmp_path)
    args.output_dir.mkdir()
    sentinel = args.output_dir / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(renderer.base.SdfRenderError, match="refusing overwrite"):
        renderer.render_dataset(args)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize(
    "option,value",
    [
        ("--clips-per-split", "2"),
        ("--frames-per-clip", "11"),
        ("--fps", "0"),
        ("--fps", "nan"),
        ("--fps", "inf"),
        ("--threads", "0"),
        ("--width", "15"),
    ],
)
def test_invalid_cli_is_rejected_before_render(tmp_path, option, value):
    with pytest.raises(SystemExit):
        _args(tmp_path, option, value)
