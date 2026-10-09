#!/usr/bin/env python3
"""Mixed near/far, low-view-angle offline 3-D visual development dataset.

Run in Blender 4.5, using a new output directory::

    blender --background --factory-startup --python this_script.py -- \
        --output-dir NEW_DIRECTORY --train-count 384 --val-count 96 --test-count 96

The frozen curriculum owns SDF geometry and all three rendering passes. This
entry only resamples camera/framing/light and repositions physical screens.
Planned coverage never creates labels: visible pixels still define supervision.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import render_onboard_sdf_blender_curriculum as curriculum  # noqa: E402

base = curriculum.base
CONTRACT = base.CONTRACT
CURRICULUM = "multidetect.offline-sdf-low-angle-far-visual-curriculum-v1"
RENDERER_PATH = "scripts/render_onboard_sdf_blender_far_curriculum.py"
SOURCE_SCRIPTS = (curriculum.BASE_RENDERER_PATH, curriculum.RENDERER_PATH, RENDERER_PATH)


def plan_sample(
    root: Path,
    world: base.World,
    split: str,
    seed: int,
    index: int,
    targets: dict[str, tuple[base.Primitive, ...]],
    *,
    width: int = 640,
    height: int = 480,
) -> curriculum.SamplePlan:
    if split not in curriculum.SPLIT_GEOMETRIES or index < 0 or min(width, height) <= 0:
        raise base.SdfRenderError("invalid far-curriculum sample identity or image dimensions")
    script_hash = base.sha256(base._source(root, RENDERER_PATH))
    identity = hashlib.sha256(
        f"{CURRICULUM}|{script_hash}|{seed}|{split}|{index}|{width}|{height}".encode()
    ).hexdigest()
    # The entry's source identity seeds the inherited geometry/material plan,
    # rather than merely annotating an old deterministic sample with a new name.
    inherited_seed = int(identity[:8], 16)
    fixed = curriculum.plan_sample(root, world, split, inherited_seed, index, targets)
    identity = hashlib.sha256(f"{identity}|{fixed.sample_seed_sha256}".encode()).hexdigest()
    rng = random.Random(int(identity, 16))
    # This nine-frame cycle is independent of the 3-geometry/4-condition cycles.
    # Near residues rotate through all geometries. Every 36 frames cover every
    # geometry/condition at near distance as well as far distance.
    near = index % 9 in {0, 4, 8}
    distance = rng.uniform(9.0, 30.0) if near else rng.uniform(30.0, 50.0)
    elevation = math.radians(rng.uniform(3.0, 12.0))
    camera_height = distance * math.tan(elevation)
    azimuth = fixed.camera["azimuth_rad"]
    toward = (math.cos(azimuth), math.sin(azimuth))
    right = (-toward[1], toward[0])
    up = (-toward[0] * math.sin(elevation), -toward[1] * math.sin(elevation), math.cos(elevation))
    center = fixed.world.target_center
    framing_mode = ("center", "intermediate", "edge")[(index // 4) % 3]
    max_offset = {"center": 0.12, "intermediate": 0.5, "edge": 0.88}[framing_mode]
    min_offset = {"center": 0.0, "intermediate": 0.18, "edge": 0.58}[framing_mode]
    horizontal_offset = rng.uniform(min_offset, max_offset) * rng.choice((-1.0, 1.0))
    vertical_offset = rng.uniform(-0.08, 0.08)
    half_width = distance * math.tan(fixed.camera["horizontal_fov_rad"] / 2.0)
    half_height = half_width * height / width
    look_shift_h = horizontal_offset * half_width
    look_shift_v = vertical_offset * half_height
    location = [
        center[0] + toward[0] * distance,
        center[1] + toward[1] * distance,
        center[2] + camera_height,
    ]
    look_at = [
        center[i] + (right[i] * look_shift_h if i < 2 else 0.0) + up[i] * look_shift_v
        for i in range(3)
    ]
    optical_distance = math.hypot(location[0] - look_at[0], location[1] - look_at[1])
    optical_elevation = math.atan2(location[2] - look_at[2], optical_distance)
    optical_elevation = min(math.radians(12.0), max(math.radians(3.0), optical_elevation))
    look_at[2] = location[2] - optical_distance * math.tan(optical_elevation)
    actual_vertical_shift = sum((look_at[i] - center[i]) * up[i] for i in range(3))
    camera = {
        **fixed.camera,
        "sample_seed_sha256": identity,
        "split_seed_sha256": hashlib.sha256(
            f"{CURRICULUM}|{script_hash}|{seed}|{split}".encode()
        ).hexdigest(),
        "location": location,
        "look_at": look_at,
        "horizontal_distance_m": distance,
        "height_above_target_center_m": camera_height,
        "target_center_elevation_deg": math.degrees(elevation),
        "optical_axis_depression_deg": math.degrees(optical_elevation),
        "distance_regime": "near_9_to_30m" if near else "far_30_to_50m",
        "framing_regime": framing_mode,
        "planned_horizontal_framing_half_extent": horizontal_offset,
        "planned_vertical_framing_half_extent": vertical_offset,
        "look_at_horizontal_jitter_m": look_shift_h,
        "look_at_vertical_jitter_m": actual_vertical_shift,
    }
    primitives = []
    for primitive in fixed.world.primitives:
        if fixed.roles[primitive.name] != "planned_occluder":
            primitives.append(primitive)
            continue
        # Ground-supported screen, reconstructed for the NEW target-camera ray.
        # Its dimensions do not presume successful occlusion; masks measure it.
        lower, upper = fixed.target_model["world_bounds_m"]
        extent = math.hypot(upper[0] - lower[0], upper[1] - lower[1])
        fraction = rng.uniform(0.12, 0.2)
        screen_width = extent * (1.0 - fraction) * 1.35
        side_offset = 0.0
        if fixed.planned_condition == "partial_occlusion":
            screen_width *= rng.uniform(0.35, 0.55)
            side_offset = extent * rng.uniform(0.12, 0.26)
        screen_height = upper[2] + camera_height * fraction + 1.2
        position = (
            center[0] + toward[0] * distance * fraction + right[0] * side_offset,
            center[1] + toward[1] * distance * fraction + right[1] * side_offset,
            screen_height / 2,
        )
        primitives.append(
            replace(
                primitive,
                dimensions=(screen_width, 0.4, screen_height),
                matrix=curriculum._matrix(*position, azimuth + math.pi / 2),
            )
        )
    weak_light = rng.random() < 0.5
    intensity = rng.uniform(0.08, 0.8) if weak_light else rng.uniform(0.6, 2.2)
    sun_azimuth = rng.uniform(-math.pi, math.pi)
    sun_elevation = math.radians(rng.uniform(8.0, 32.0) if weak_light else rng.uniform(25.0, 75.0))
    sun_direction = (
        math.cos(sun_azimuth) * math.cos(sun_elevation),
        math.sin(sun_azimuth) * math.cos(sun_elevation),
        -math.sin(sun_elevation),
    )
    background_scale = rng.uniform(0.1, 0.45) if weak_light else rng.uniform(0.85, 1.15)
    background = tuple(min(1.0, c * background_scale) for c in fixed.world.background[:3]) + (1.0,)
    sources = {**dict(fixed.world.sources), RENDERER_PATH: script_hash}
    lighting = {
        **fixed.lighting,
        "regime": "weak" if weak_light else "strong",
        "sun_direction": sun_direction,
        "sdf_intensity": intensity,
        "blender_sun_energy": intensity * 2.5,
        "world_background": background,
        "background_color_scale": background_scale,
    }
    return replace(
        fixed,
        world=replace(
            fixed.world,
            primitives=tuple(primitives),
            sun_direction=sun_direction,
            sun_intensity=intensity,
            background=background,
            sources=tuple(sorted(sources.items())),
        ),
        camera=camera,
        lighting=lighting,
        sample_seed_sha256=identity,
    )


def _paths(output: Path, split: str, frame_id: str) -> dict[str, Path]:
    groups = (
        ("image", "images", "png"),
        ("label", "labels", "txt"),
        ("mask", "masks", "png"),
        ("mask_render", "mask-renders", "png"),
        ("amodal_mask", "amodal-masks", "png"),
        ("amodal_render", "amodal-renders", "png"),
    )
    return {kind: output / group / split / f"{frame_id}.{suffix}" for kind, group, suffix in groups}


def _verify_sources(root: Path, sources: dict[str, str], imported: dict[str, Path]) -> None:
    for relative, digest in sources.items():
        if base.sha256(base._source(root, relative)) != digest:
            raise base.SdfRenderError(f"source changed: {relative}; no manifest sealed")
    for relative, imported_path in imported.items():
        if base.sha256(imported_path) != sources[relative]:
            raise base.SdfRenderError(
                f"imported renderer source differs from repo-root: {relative}; no manifest sealed"
            )


def render_dataset(args: Any) -> dict[str, Any]:
    output, root = args.output_dir.resolve(), args.repo_root.resolve()
    if output.exists():
        raise base.SdfRenderError("output-dir already exists; refusing overwrite")
    # Snapshot BEFORE parsing: primitives must never acquire a later version's
    # hash merely because a source changed between parsing and inventorying.
    all_sources = {
        relative: base.sha256(base._source(root, relative))
        for relative in (
            *base.WORLD_PATHS.values(),
            *curriculum.TARGET_PATHS.values(),
            *SOURCE_SCRIPTS,
        )
    }
    imported = dict(
        zip(
            SOURCE_SCRIPTS,
            (Path(base.__file__), Path(curriculum.__file__), Path(__file__)),
            strict=True,
        )
    )
    _verify_sources(root, all_sources, imported)
    worlds = {split: base.load_world(root, path) for split, path in base.WORLD_PATHS.items()}
    targets = {
        key: curriculum.load_target(root, path) for key, path in curriculum.TARGET_PATHS.items()
    }
    for world in worlds.values():
        for relative, digest in world.sources:
            if all_sources.get(relative) != digest:
                raise base.SdfRenderError(
                    f"source changed during world parsing: {relative}; no manifest sealed"
                )
    _verify_sources(root, all_sources, imported)
    geometry_hashes = {key: curriculum.geometry_sha256(value) for key, value in targets.items()}
    if {geometry_hashes[key] for key in curriculum.SPLIT_GEOMETRIES["test"]} & {
        geometry_hashes[key]
        for split in ("train", "val")
        for key in curriculum.SPLIT_GEOMETRIES[split]
    }:
        raise base.SdfRenderError("test target geometry must be hash-disjoint from train and val")
    import bpy  # type: ignore[import-not-found]
    import mathutils  # type: ignore[import-not-found]

    if bpy.app.version[:2] != (4, 5):
        raise base.SdfRenderError(f"requires Blender 4.5 API, got {bpy.app.version_string}")
    output.mkdir(parents=True, exist_ok=False)
    for group in ("images", "labels", "masks", "mask-renders", "amodal-masks", "amodal-renders"):
        for split in worlds:
            (output / group / split).mkdir(parents=True)
    frames = []
    for split, world in worlds.items():
        for index in range(getattr(args, f"{split}_count")):
            plan = plan_sample(
                root, world, split, args.seed, index, targets, width=args.width, height=args.height
            )
            if any(all_sources.get(path) != digest for path, digest in plan.world.sources):
                raise base.SdfRenderError(
                    "source changed while creating sample plan; no manifest sealed"
                )
            frame_id = f"{split}-{index:05d}"
            paths = _paths(output, split, frame_id)
            visible, amodal, matrix = curriculum._render_sample(
                bpy,
                mathutils,
                plan,
                args,
                paths["image"],
                paths["mask_render"],
                paths["amodal_render"],
            )
            if len(visible) != args.width * args.height or len(amodal) != len(visible):
                raise base.SdfRenderError("rendered far-curriculum mask dimensions mismatch")
            measurement = curriculum.measure_visibility(
                visible, amodal, plan.camera["target_present"]
            )
            if measurement["measured_condition"].startswith("inconsistent"):
                raise base.SdfRenderError("inconsistent rendered masks; no manifest sealed")
            box = base.visible_bbox(visible, args.width, args.height)
            amodal_box = base.visible_bbox(amodal, args.width, args.height)
            paths["mask"].write_bytes(base._mask_png(visible, args.width, args.height))
            paths["amodal_mask"].write_bytes(base._mask_png(amodal, args.width, args.height))
            paths["label"].write_text(
                base.yolo_label(box, args.width, args.height), encoding="utf-8"
            )
            frame = {
                "frame_id": frame_id,
                "split": split,
                "width": args.width,
                "height": args.height,
                "world_path": world.relative_path,
                "world_sha256": dict(world.sources)[world.relative_path],
                "source_hashes": dict(plan.world.sources),
                "camera": {**plan.camera, "matrix_world": matrix},
                "visible_bbox_xyxy_px": box,
                "amodal_bbox_xyxy_px": amodal_box,
                "target_visible": box is not None,
                "target_present": plan.camera["target_present"],
                "target_model": plan.target_model,
                "lighting": plan.lighting,
                "sample_seed_sha256": plan.sample_seed_sha256,
                "planned_condition": plan.planned_condition,
                "visibility_measurement": measurement,
                "curriculum": {
                    "target_model_path": plan.target_model["path"],
                    "target_model_sha256": plan.target_model["sha256"],
                    "target_geometry_sha256": plan.target_model["geometry_sha256"],
                    "planned_condition": plan.planned_condition,
                    **measurement,
                },
                "visible_target_pixels": measurement["visible_target_pixels"],
                "actual_amodal_image_area_fraction": measurement["amodal_target_pixels"]
                / (args.width * args.height),
                "scene_primitives": curriculum._primitive_inventory(plan),
                "offline_truth_only": True,
            }
            for kind, path in paths.items():
                frame[f"{kind}_path"] = path.relative_to(output).as_posix()
                frame[f"{kind}_sha256"] = base.sha256(path)
            frames.append(frame)
            print(
                f"{frame_id}: {plan.camera['distance_regime']}, "
                f"planned={plan.planned_condition}, measured={measurement['measured_condition']}",
                flush=True,
            )
    _verify_sources(root, all_sources, imported)
    yaml_path = output / "dataset.yaml"
    yaml_path.write_text(
        f"path: {json.dumps(output.as_posix())}\n"
        "train: images/train\nval: images/val\ntest: images/test\nnames: ['search_target']\n",
        encoding="utf-8",
    )
    manifest = {
        "contract": CONTRACT,
        "purpose": "offline_synthetic_developer_visual_experiment",
        "names": ["search_target"],
        "dataset_yaml": "dataset.yaml",
        "dataset_yaml_sha256": base.sha256(yaml_path),
        "frame_count": len(frames),
        "frames": frames,
        "seed": args.seed,
        "split_unit": "source_world",
        "split_worlds": base.WORLD_PATHS,
        "source_hashes": all_sources,
        "renderer": {
            "engine": args.engine,
            "blender_version": bpy.app.version_string,
            "script_sha256": all_sources[RENDERER_PATH],
            "source_scripts": {path: all_sources[path] for path in SOURCE_SCRIPTS},
            "render_samples": args.render_samples,
            "threads": args.threads,
            "cycles_device": "CPU" if args.engine == "CYCLES" else None,
        },
        "curriculum": {
            "contract": CURRICULUM,
            "split_geometries": curriculum.SPLIT_GEOMETRIES,
            "target_geometry_sha256": geometry_hashes,
            "test_geometry_disjoint_from_train_and_val": True,
            "distance_sampling": "near frame residues 0,4,8 of 9; 1/3 near over 36 frames",
            "target_center_elevation_deg_range": [3.0, 12.0],
            "distance_regime_counts": dict(
                Counter(frame["camera"]["distance_regime"] for frame in frames)
            ),
            "framing_regime_counts": dict(
                Counter(frame["camera"]["framing_regime"] for frame in frames)
            ),
            "lighting_regime_counts": dict(
                Counter(frame["lighting"]["regime"] for frame in frames)
            ),
            "planned_condition_counts": dict(
                Counter(frame["planned_condition"] for frame in frames)
            ),
            "measured_condition_counts": dict(
                Counter(frame["visibility_measurement"]["measured_condition"] for frame in frames)
            ),
            "planned_condition_is_not_measured_evidence": True,
            "amodal_semantics": (
                "same_target_camera_hide_all_non_target_geometry_keep_absent_target_hidden"
            ),
            "visibility_fraction_semantics": (
                "visible_amodal_pixel_intersection_divided_by_amodal_pixel_count"
            ),
            "offline_truth_only": True,
        },
        "mask_semantics": (
            "second_3d_emission_render_same_camera_all_occluders_retained_threshold_0.5"
        ),
        "mask_row_order": "top_to_bottom",
        "bbox_source": "visible_mask_pixels_exclusive_xyxy",
        "material_import": "opaque_diffuse_emissive_approximation_transparency_not_reproduced",
        "projection_bbox_used": False,
        "billboard_images_used": False,
        "training_ready": False,
        "gazebo_capture": False,
        "flight": False,
        "promotion": False,
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
        "limitations": [
            "low_poly_static_offline_geometry_not_Gazebo_sensor_capture",
            "camera_sampling_uses_known_offline_target_center",
            "sampled_distance_and_framing_are_plans_not_observed_detection_success",
            "weak_and_strong_light_are_render_parameters_not_calibrated_sensor_noise",
            "no_task_policy_or_flight_action_training",
            "no_hardware_or_domain_transfer_acceptance",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return manifest


def parse_arguments(argv: list[str] | None = None) -> Any:
    return base.parse_arguments(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        result = render_dataset(args)
    except (base.SdfRenderError, OSError, ET.ParseError, ImportError) as exc:
        print(f"SDF Blender far curriculum failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "contract": CONTRACT,
                "curriculum": CURRICULUM,
                "frame_count": result["frame_count"],
                "output_dir": str(args.output_dir.resolve()),
                "training_ready": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    blender_args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    raise SystemExit(main(blender_args))
