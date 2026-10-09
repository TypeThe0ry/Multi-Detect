#!/usr/bin/env python3
"""Offline 3-D visual curriculum; not Gazebo/PX4 or flight evidence.

Run with Blender 4.5::

    blender --background --factory-startup --python this_script.py -- \
        --output-dir NEW_DIRECTORY --samples-per-split 4

The original SDF renderer remains hash-stable. This entry reuses its geometry,
RGB and visible-mask passes, adds independently rendered amodal masks, and
measures occlusion rather than treating a planned occlusion as an observation.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import xml.etree.ElementTree as ET
from array import array
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import render_onboard_sdf_blender_dataset as base  # noqa: E402

CONTRACT = base.CONTRACT
CURRICULUM = "multidetect.offline-sdf-visual-curriculum-v2"
RENDERER_PATH = "scripts/render_onboard_sdf_blender_curriculum.py"
BASE_RENDERER_PATH = "scripts/render_onboard_sdf_blender_dataset.py"
TARGET_PATHS = {
    "original": base.TARGET_PATH,
    "rover_long": "simulation/gazebo/models/multidetect_search_rover_long_v1/model.sdf",
    "cargo_cart": "simulation/gazebo/models/multidetect_search_cargo_cart_v1/model.sdf",
    "utility_van": "simulation/gazebo/models/multidetect_search_utility_van_v1/model.sdf",
}
SPLIT_GEOMETRIES = {
    "train": ("original", "rover_long", "cargo_cart"),
    "val": ("original", "rover_long", "cargo_cart"),
    "test": ("utility_van",),
}
# These locations are beside the actual world buildings/markers, rather than
# the isolated target include used by the first experiment. Coordinates are
# offline sampling inputs, never observations supplied to a flight policy.
ANCHORS = {
    "train": ((70.0, 10.0), (125.0, -17.0), (215.0, 10.0)),
    "val": ((43.0, 70.0), (112.0, 26.0), (84.0, 29.0)),
    "test": ((30.0, 52.0), (108.0, 28.0), (-108.0, -29.0)),
}
CONDITIONS = ("clear", "partial_occlusion", "full_occlusion", "absent")


@dataclass(frozen=True)
class SamplePlan:
    world: base.World
    camera: dict[str, Any]
    roles: dict[str, str]
    target_model: dict[str, Any]
    lighting: dict[str, Any]
    planned_condition: str
    sample_seed_sha256: str


def _matrix(x: float, y: float, z: float, yaw: float = 0.0) -> base.Matrix:
    c, s = math.cos(yaw), math.sin(yaw)
    return ((c, -s, 0.0, x), (s, c, 0.0, y), (0.0, 0.0, 1.0, z), (0.0, 0.0, 0.0, 1.0))


def _bounds(primitives: tuple[base.Primitive, ...]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    points = [point for primitive in primitives for point in base._corners(primitive)]
    if not points:
        raise base.SdfRenderError("target model has no visual primitives")
    return tuple(min(p[i] for p in points) for i in range(3)), tuple(
        max(p[i] for p in points) for i in range(3)
    )


def load_target(root: Path, relative: str) -> tuple[base.Primitive, ...]:
    model = ET.parse(base._source(root, relative)).getroot().find("model")
    if model is None:
        raise base.SdfRenderError("target SDF lacks model")
    result = tuple(base._model_primitives(model, base.IDENTITY, True, "curriculum_target"))
    _bounds(result)
    return result


def geometry_sha256(primitives: tuple[base.Primitive, ...]) -> str:
    """Geometry identity is independent of SDF names, comments and materials."""
    value = [
        {"geometry": p.geometry, "dimensions": p.dimensions, "matrix": p.matrix} for p in primitives
    ]
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _color(rng: random.Random) -> tuple[float, ...]:
    # Color is independent of identity and split; no fixed red/green answer.
    return (*(rng.uniform(0.12, 0.82) for _ in range(3)), 1.0)


def _primitive(
    name: str,
    geometry: str,
    dimensions: tuple[float, ...],
    position: tuple[float, ...],
    color: tuple[float, ...],
    yaw: float = 0.0,
) -> base.Primitive:
    return base.Primitive(
        name=name,
        geometry=geometry,
        dimensions=dimensions,
        matrix=_matrix(*position, yaw),
        diffuse=color,
        emissive=(0.0, 0.0, 0.0, 1.0),
        target=False,
    )


def plan_sample(
    root: Path,
    world: base.World,
    split: str,
    seed: int,
    index: int,
    targets: dict[str, tuple[base.Primitive, ...]],
) -> SamplePlan:
    if split not in SPLIT_GEOMETRIES or index < 0:
        raise base.SdfRenderError("invalid split or sample index")
    target_key = SPLIT_GEOMETRIES[split][index % len(SPLIT_GEOMETRIES[split])]
    target_path = TARGET_PATHS[target_key]
    sources = dict(world.sources)
    sources[target_path] = base.sha256(base._source(root, target_path))
    for relative in (BASE_RENDERER_PATH, RENDERER_PATH):
        sources[relative] = base.sha256(base._source(root, relative))
    identity = hashlib.sha256(
        f"{seed}|{split}|{index}|{sources[world.relative_path]}|{sources[target_path]}".encode()
    ).hexdigest()
    rng = random.Random(int(identity, 16))
    condition = CONDITIONS[index % len(CONDITIONS)]
    anchor = ANCHORS[split][index % len(ANCHORS[split])]
    x, y = anchor[0] + rng.uniform(-2.0, 2.0), anchor[1] + rng.uniform(-2.0, 2.0)
    yaw = rng.uniform(-math.pi, math.pi)
    original_target = targets[target_key]
    lower, upper = _bounds(original_target)
    center = tuple((a + b) / 2 for a, b in zip(lower, upper, strict=True))
    placement = base.multiply(_matrix(x, y, 0.1, yaw), _matrix(-center[0], -center[1], -lower[2]))
    target_color = _color(rng)
    moved = tuple(
        replace(
            primitive,
            matrix=base.multiply(placement, primitive.matrix),
            diffuse=tuple(
                min(0.95, max(0.03, target_color[c] * rng.uniform(0.7, 1.2))) for c in range(3)
            )
            + (1.0,),
        )
        for primitive in original_target
    )
    lower, upper = _bounds(moved)
    target_center = tuple((a + b) / 2 for a, b in zip(lower, upper, strict=True))
    azimuth = rng.uniform(-math.pi, math.pi)
    distance = rng.uniform(9.0, 30.0)
    height = rng.uniform(3.0, 13.0)
    right = (-math.sin(azimuth), math.cos(azimuth))
    jitter = rng.uniform(-0.12, 0.12) * distance
    vertical_jitter = rng.uniform(-0.05, 0.05) * distance
    camera = {
        "index": index,
        "split_seed_sha256": hashlib.sha256(f"{seed}|{split}".encode()).hexdigest(),
        "sample_seed_sha256": identity,
        "location": [
            x + distance * math.cos(azimuth),
            y + distance * math.sin(azimuth),
            target_center[2] + height,
        ],
        "look_at": [
            target_center[0] + right[0] * jitter,
            target_center[1] + right[1] * jitter,
            target_center[2] + vertical_jitter,
        ],
        "target_center": list(target_center),
        "horizontal_distance_m": distance,
        "height_above_target_center_m": height,
        "azimuth_rad": azimuth,
        "look_at_horizontal_jitter_m": jitter,
        "look_at_vertical_jitter_m": vertical_jitter,
        "horizontal_fov_rad": math.radians(60.0),
        "target_present": condition != "absent",
    }
    primitives = [
        replace(
            primitive,
            diffuse=tuple(
                min(1.0, max(0.02, component * rng.uniform(0.8, 1.2)))
                for component in primitive.diffuse[:3]
            )
            + (1.0,),
        )
        for primitive in world.primitives
        if not primitive.target
    ]
    roles = {primitive.name: "source_world_background" for primitive in primitives}
    primitives.extend(moved)
    roles.update({primitive.name: "target" for primitive in moved})

    def add(primitive: base.Primitive, role: str) -> None:
        primitives.append(primitive)
        roles[primitive.name] = role

    add(
        _primitive("curriculum/lot", "box", (22.0, 20.0, 0.04), (x, y, 0.07), _color(rng), yaw),
        "background_lot",
    )
    # A true 3-D facility behind the target adds depth and non-uniform context.
    behind = (x - math.cos(azimuth) * 12.0, y - math.sin(azimuth) * 12.0)
    add(
        _primitive("curriculum/shed", "box", (6.0, 4.0, 3.0), (*behind, 1.5), _color(rng), yaw),
        "background_facility",
    )
    add(
        _primitive(
            "curriculum/shed_roof", "box", (6.5, 4.5, 0.25), (*behind, 3.15), _color(rng), yaw
        ),
        "background_facility",
    )
    for number in range(5):
        angle = azimuth + math.pi + rng.uniform(-1.4, 1.4)
        radius = rng.uniform(4.0, 10.0)
        px, py = x + radius * math.cos(angle), y + radius * math.sin(angle)
        shape = ("box", "cylinder", "sphere")[number % 3]
        if shape == "box":
            size = (rng.uniform(1.0, 2.5), rng.uniform(0.8, 2.0), rng.uniform(0.7, 2.3))
            z = size[2] / 2 + 0.1
        elif shape == "cylinder":
            size = (rng.uniform(0.5, 1.0), rng.uniform(1.0, 2.6))
            z = size[1] / 2 + 0.1
        else:
            size = (rng.uniform(0.6, 1.4),)
            z = size[0] + 0.1
        add(
            _primitive(
                f"curriculum/distractor_{number}",
                shape,
                size,
                (px, py, z),
                _color(rng),
                rng.uniform(-math.pi, math.pi),
            ),
            "non_target_distractor",
        )
    if condition in {"partial_occlusion", "full_occlusion"}:
        # Upright, ground-supported construction screen. Its geometry is aimed
        # to occlude, but only the two rendered masks establish actual coverage.
        fraction = rng.uniform(0.12, 0.2)
        extent = math.hypot(upper[0] - lower[0], upper[1] - lower[1])
        width = extent * (1.0 - fraction) * 1.35
        offset = 0.0
        if condition == "partial_occlusion":
            width *= rng.uniform(0.35, 0.55)
            offset = extent * rng.uniform(0.12, 0.26)
        screen_height = upper[2] + height * fraction + 1.2
        position = (
            x + math.cos(azimuth) * distance * fraction + right[0] * offset,
            y + math.sin(azimuth) * distance * fraction + right[1] * offset,
            screen_height / 2,
        )
        add(
            _primitive(
                "curriculum/construction_screen",
                "box",
                (width, 0.4, screen_height),
                position,
                _color(rng),
                azimuth + math.pi / 2,
            ),
            "planned_occluder",
        )
    sun_azimuth = rng.uniform(-math.pi, math.pi)
    sun_elevation = rng.uniform(math.radians(18), math.radians(75))
    sun_direction = (
        math.cos(sun_azimuth) * math.cos(sun_elevation),
        math.sin(sun_azimuth) * math.cos(sun_elevation),
        -math.sin(sun_elevation),
    )
    sun_color = (rng.uniform(0.8, 1.0), rng.uniform(0.8, 1.0), rng.uniform(0.72, 1.0), 1.0)
    sun_intensity = rng.uniform(0.5, 1.6)
    background = (rng.uniform(0.3, 0.65), rng.uniform(0.4, 0.75), rng.uniform(0.55, 0.9), 1.0)
    scene = replace(
        world,
        primitives=tuple(primitives),
        target_center=target_center,
        sun_direction=sun_direction,
        sun_color=sun_color,
        sun_intensity=sun_intensity,
        background=background,
        sources=tuple(sorted(sources.items())),
    )
    return SamplePlan(
        world=scene,
        camera=camera,
        roles=roles,
        target_model={
            "geometry_key": target_key,
            "path": target_path,
            "sha256": sources[target_path],
            "geometry_sha256": geometry_sha256(original_target),
            "placement_matrix": placement,
            "local_bounds_m": _bounds(original_target),
            "world_bounds_m": (lower, upper),
        },
        lighting={
            "sun_direction": sun_direction,
            "sun_color": sun_color,
            "sdf_intensity": sun_intensity,
            "blender_sun_energy": sun_intensity * 2.5,
            "world_background": background,
            "world_strength": 0.35,
        },
        planned_condition=condition,
        sample_seed_sha256=identity,
    )


def measure_visibility(visible: bytes, amodal: bytes, target_present: bool) -> dict[str, Any]:
    if len(visible) != len(amodal):
        raise base.SdfRenderError("visible/amodal mask dimensions mismatch")
    visible_count = sum(bool(v) for v in visible)
    amodal_count = sum(bool(a) for a in amodal)
    overlap = sum(bool(v and a) for v, a in zip(visible, amodal, strict=True))
    outside = visible_count - overlap
    if not target_present:
        condition = "absent" if visible_count == amodal_count == 0 else "inconsistent_absent_mask"
    elif amodal_count == 0:
        condition = "out_of_frame" if visible_count == 0 else "inconsistent_mask"
    elif outside:
        condition = "inconsistent_mask"
    elif not visible_count:
        condition = "fully_occluded"
    elif overlap < amodal_count:
        condition = "partially_occluded"
    else:
        condition = "clear"
    return {
        "measured_condition": condition,
        "visible_target_pixels": visible_count,
        "amodal_target_pixels": amodal_count,
        "visible_amodal_overlap_pixels": overlap,
        "visible_pixels_outside_amodal": outside,
        "occluded_amodal_pixels": amodal_count - overlap,
        "visible_fraction": overlap / amodal_count if amodal_count else None,
        "visible_pixel_count_ratio": visible_count / amodal_count if amodal_count else None,
        "amodal_reference_in_frame": amodal_count > 0,
    }


def _render_amodal_mask(
    bpy: Any, scene: Any, objects: list[Any], path: Path, width: int, height: int
) -> bytes:
    materials = [list(obj.data.materials) for obj in objects]
    hidden = [obj.hide_render for obj in objects]
    background = scene.world.node_tree.nodes.get("Background")
    background_color = tuple(background.inputs["Color"].default_value)
    white = base._material(bpy, "amodal_target_mask", (1, 1, 1, 1), (0, 0, 0, 1), True)
    try:
        for obj in objects:
            obj.hide_render = bool(not obj["sdf_target"] or obj.hide_render)
            obj.data.materials.clear()
            obj.data.materials.append(white)
        background.inputs["Color"].default_value = (0, 0, 0, 1)
        scene.render.image_settings.color_mode = "BW"
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(str(path), check_existing=False)
        try:
            if tuple(image.size) != (width, height):
                raise base.SdfRenderError("saved amodal render dimensions mismatch")
            pixels = array("f", [0.0]) * (width * height * 4)
            image.pixels.foreach_get(pixels)
            return base.mask_from_rgba(pixels, width, height)
        finally:
            bpy.data.images.remove(image)
    finally:
        for obj, original, was_hidden in zip(objects, materials, hidden, strict=True):
            obj.hide_render = was_hidden
            obj.data.materials.clear()
            for material in original:
                obj.data.materials.append(material)
        background.inputs["Color"].default_value = background_color
        bpy.data.materials.remove(white)


def _render_sample(
    bpy: Any,
    mathutils: Any,
    plan: SamplePlan,
    args: Any,
    image: Path,
    raw_mask: Path,
    raw_amodal: Path,
) -> tuple[bytes, bytes, list[list[float]]]:
    scene, objects = base._build_blender_scene(bpy, mathutils, plan.world, args)
    visible, matrix = base._render_frame(
        bpy, mathutils, scene, objects, plan.camera, image, raw_mask, args.width, args.height
    )
    amodal = _render_amodal_mask(bpy, scene, objects, raw_amodal, args.width, args.height)
    return visible, amodal, matrix


def _primitive_inventory(plan: SamplePlan) -> list[dict[str, Any]]:
    return [
        {
            "name": p.name,
            "role": plan.roles[p.name],
            "geometry": p.geometry,
            "dimensions_m": p.dimensions,
            "matrix_world": p.matrix,
            "diffuse": p.diffuse,
            "emissive": p.emissive,
            "target": p.target,
            "hidden_for_absent": p.target and not plan.camera["target_present"],
        }
        for p in plan.world.primitives
    ]


def render_dataset(args: Any) -> dict[str, Any]:
    output, root = args.output_dir.resolve(), args.repo_root.resolve()
    if output.exists():
        raise base.SdfRenderError("output-dir already exists; refusing overwrite")
    worlds = {split: base.load_world(root, path) for split, path in base.WORLD_PATHS.items()}
    targets = {key: load_target(root, path) for key, path in TARGET_PATHS.items()}
    target_hashes = {
        key: base.sha256(base._source(root, path)) for key, path in TARGET_PATHS.items()
    }
    geometry_hashes = {key: geometry_sha256(primitives) for key, primitives in targets.items()}
    if set(geometry_hashes[key] for key in SPLIT_GEOMETRIES["test"]) & set(
        geometry_hashes[key] for split in ("train", "val") for key in SPLIT_GEOMETRIES[split]
    ):
        raise base.SdfRenderError("test target geometry must be hash-disjoint from train and val")
    all_sources = {
        relative: digest for world in worlds.values() for relative, digest in world.sources
    }
    all_sources.update({path: target_hashes[key] for key, path in TARGET_PATHS.items()})
    for relative in (BASE_RENDERER_PATH, RENDERER_PATH):
        all_sources[relative] = base.sha256(base._source(root, relative))
    import bpy  # type: ignore[import-not-found]
    import mathutils  # type: ignore[import-not-found]

    if bpy.app.version[:2] != (4, 5):
        raise base.SdfRenderError(f"requires Blender 4.5 API, got {bpy.app.version_string}")
    output.mkdir(parents=True, exist_ok=False)
    for group in ("images", "labels", "masks", "mask-renders", "amodal-masks", "amodal-renders"):
        for split in base.WORLD_PATHS:
            (output / group / split).mkdir(parents=True)
    frames = []
    for split, world in worlds.items():
        for index in range(getattr(args, f"{split}_count")):
            plan = plan_sample(root, world, split, args.seed, index, targets)
            frame_id = f"{split}-{index:05d}"
            paths = {
                kind: output / group / split / f"{frame_id}.{suffix}"
                for kind, group, suffix in (
                    ("image", "images", "png"),
                    ("label", "labels", "txt"),
                    ("mask", "masks", "png"),
                    ("mask_render", "mask-renders", "png"),
                    ("amodal_mask", "amodal-masks", "png"),
                    ("amodal_render", "amodal-renders", "png"),
                )
            }
            visible, amodal, matrix = _render_sample(
                bpy,
                mathutils,
                plan,
                args,
                paths["image"],
                paths["mask_render"],
                paths["amodal_render"],
            )
            box = base.visible_bbox(visible, args.width, args.height)
            paths["mask"].write_bytes(base._mask_png(visible, args.width, args.height))
            paths["amodal_mask"].write_bytes(base._mask_png(amodal, args.width, args.height))
            paths["label"].write_text(
                base.yolo_label(box, args.width, args.height), encoding="utf-8"
            )
            observation = measure_visibility(visible, amodal, plan.camera["target_present"])
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
                "target_visible": box is not None,
                "target_present": plan.camera["target_present"],
                "target_model": plan.target_model,
                "lighting": plan.lighting,
                "sample_seed_sha256": plan.sample_seed_sha256,
                "planned_condition": plan.planned_condition,
                "visibility_measurement": observation,
                "curriculum": {
                    "target_model_path": plan.target_model["path"],
                    "target_model_sha256": plan.target_model["sha256"],
                    "target_geometry_sha256": plan.target_model["geometry_sha256"],
                    "planned_condition": plan.planned_condition,
                    **observation,
                },
                "visible_target_pixels": observation["visible_target_pixels"],
                "scene_primitives": _primitive_inventory(plan),
                "offline_truth_only": True,
            }
            for kind, path in paths.items():
                frame[f"{kind}_path"] = path.relative_to(output).as_posix()
                frame[f"{kind}_sha256"] = base.sha256(path)
            frames.append(frame)
            print(
                f"{frame_id}: planned={plan.planned_condition}, "
                f"measured={observation['measured_condition']}, "
                f"visible_fraction={observation['visible_fraction']}",
                flush=True,
            )
    for relative, digest in all_sources.items():
        if base.sha256(base._source(root, relative)) != digest:
            raise base.SdfRenderError(
                f"source changed during render: {relative}; no manifest sealed"
            )
    yaml_path = output / "dataset.yaml"
    yaml_path.write_text(
        f"path: {json.dumps(output.as_posix())}\n"
        "train: images/train\nval: images/val\ntest: images/test\n"
        "names: ['search_target']\n",
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
            "base_script_sha256": all_sources[BASE_RENDERER_PATH],
            "render_samples": args.render_samples,
            "threads": args.threads,
            "cycles_device": "CPU" if args.engine == "CYCLES" else None,
        },
        "curriculum": {
            "contract": CURRICULUM,
            "split_geometries": SPLIT_GEOMETRIES,
            "target_model_sha256": target_hashes,
            "target_geometry_sha256": geometry_hashes,
            "test_geometry_disjoint_from_train_and_val": True,
            "planned_condition_counts": dict(
                Counter(frame["planned_condition"] for frame in frames)
            ),
            "measured_condition_counts": dict(
                Counter(frame["visibility_measurement"]["measured_condition"] for frame in frames)
            ),
            "planned_condition_is_not_measured_evidence": True,
            "non_target_distractors_are_3d": True,
            "amodal_semantics": (
                "same_target_camera_hide_all_non_target_geometry_keep_absent_target_hidden"
            ),
            "visibility_fraction_semantics": (
                "visible_amodal_pixel_intersection_divided_by_amodal_pixel_count"
            ),
            "mask_inconsistency_handling": (
                "any_visible_pixel_outside_amodal_is_recorded_as_inconsistent_mask"
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
            "four_primitive_based_prototype_geometries_not_general_object_recognition",
            "three_static_worlds_procedurally_augmented_offline",
            "camera_sampling_uses_known_offline_target_center",
            "planned_occlusion_may_differ_from_mask_measurement",
            "no_dynamic_sensor_noise_or_domain_transfer_acceptance",
            "no_closed_loop_flight_or_runtime_policy_supervision",
            "bbox_and_class_supervision_not_independently_reviewed",
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
        print(f"SDF Blender curriculum failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "contract": CONTRACT,
                "curriculum": CURRICULUM,
                "frame_count": result["frame_count"],
                "training_ready": False,
                "output_dir": str(args.output_dir.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    blender_args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    raise SystemExit(main(blender_args))
