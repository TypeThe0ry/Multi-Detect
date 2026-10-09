#!/usr/bin/env python3
"""Offline multi-view clip suite, not physics, sensor capture or flight evidence.

    blender --background --factory-startup --python this_script.py -- \
        --output-dir NEW_DIRECTORY --clips-per-split 3 --frames-per-clip 16

Reuses the frozen clip planner and curriculum's three rendered passes. Each
clip has a fixed scene, geometry, materials, lighting and ground-supported
screen, with continuous target/camera translation. Offline scene coordinates
and masks must never be supplied to a detector or runtime task policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import render_onboard_sdf_blender_clips as legacy  # noqa: E402

curriculum, base = legacy.curriculum, legacy.base
RENDERER_PATH = "scripts/render_onboard_sdf_blender_clip_suite.py"
SOURCE_SCRIPTS = (*legacy.SOURCE_SCRIPTS, RENDERER_PATH)
SUITE_CONTRACT = "multidetect.offline-sdf-visual-clip-suite-v1"


@dataclass(frozen=True)
class ViewProfile:
    key: str
    distance_range_m: tuple[float, float]
    nominal_slant_distance_m: float
    camera_azimuth_relative_to_target_rad: float
    view_elevation_rad: float
    horizontal_framing_fraction: float
    sun_azimuth_relative_to_camera_rad: float
    sun_elevation_rad: float
    sun_intensity: float
    sun_color: tuple[float, ...]
    background: tuple[float, ...]


PROFILES = (
    ViewProfile(
        "near",
        (21.0, 27.0),
        24.0,
        math.radians(20),
        math.radians(10),
        0.0,
        -0.6,
        math.radians(30),
        0.65,
        (1.0, 0.88, 0.75, 1.0),
        (0.52, 0.58, 0.66, 1.0),
    ),
    ViewProfile(
        "medium",
        (30.0, 38.0),
        34.0,
        math.radians(65),
        math.radians(15),
        0.30,
        1.2,
        math.radians(65),
        1.25,
        (0.90, 0.96, 1.0, 1.0),
        (0.45, 0.62, 0.80, 1.0),
    ),
    ViewProfile(
        "far",
        (40.0, 50.0),
        45.0,
        math.radians(110),
        math.radians(22),
        -0.44,
        -1.7,
        math.radians(20),
        0.45,
        (0.85, 0.89, 1.0, 1.0),
        (0.35, 0.42, 0.53, 1.0),
    ),
)


def profile_for(index: int) -> ViewProfile:
    if index < 0:
        raise base.SdfRenderError("invalid suite clip index")
    # Further groups rotate the geometry/profile pairing. Validation also uses
    # a different geometry offset from training. Target color never uses profile.
    return PROFILES[(index + index // len(PROFILES)) % len(PROFILES)]


def plan_clip(
    root: Path,
    world: base.World,
    split: str,
    seed: int,
    index: int,
    frames_per_clip: int,
    fps: float,
    targets: dict[str, tuple[base.Primitive, ...]],
) -> legacy.ClipPlan:
    profile = profile_for(index)
    source_index = index + (1 if split == "val" else 0)
    original = legacy.plan_clip(
        root, world, split, seed, source_index, frames_per_clip, fps, targets
    )
    first = original.frames[0]
    center = tuple(first.camera["look_at"])
    target_start = first.world.target_center
    lower, upper = first.target_model["world_bounds_m"]
    extent = math.hypot(upper[0] - lower[0], upper[1] - lower[1])
    placement = first.target_model["placement_matrix"]
    target_yaw = math.atan2(placement[1][0], placement[0][0])
    azimuth = target_yaw + profile.camera_azimuth_relative_to_target_rad
    toward, right = (math.cos(azimuth), math.sin(azimuth)), (-math.sin(azimuth), math.cos(azimuth))
    horizontal_distance = profile.nominal_slant_distance_m * math.cos(profile.view_elevation_rad)
    height = profile.nominal_slant_distance_m * math.sin(profile.view_elevation_rad)
    framing_offset = (
        profile.horizontal_framing_fraction * horizontal_distance * math.tan(math.pi / 6)
    )
    screen_fraction = 0.22
    screen_height = upper[2] + height * screen_fraction + 1.5
    screen = curriculum._primitive(
        "clip-suite/fixed_construction_screen",
        "box",
        (extent * 1.15, 0.4, screen_height),
        (
            center[0] + toward[0] * horizontal_distance * screen_fraction,
            center[1] + toward[1] * horizontal_distance * screen_fraction,
            screen_height / 2,
        ),
        (0.35, 0.42, 0.48, 1.0),
        azimuth + math.pi / 2,
    )
    static = tuple(
        p
        for p in first.world.primitives
        if not p.target and first.roles[p.name] != "fixed_physical_occluder"
    ) + (screen,)
    moving = tuple(p for p in first.world.primitives if p.target)
    roles = {p.name: first.roles[p.name] for p in static if p.name != screen.name}
    roles.update({p.name: "target" for p in moving})
    roles[screen.name] = "fixed_physical_occluder"
    sources = {
        **dict(first.world.sources),
        RENDERER_PATH: base.sha256(base._source(root, RENDERER_PATH)),
    }
    sun_azimuth = azimuth + profile.sun_azimuth_relative_to_camera_rad
    sun_direction = (
        math.cos(sun_azimuth) * math.cos(profile.sun_elevation_rad),
        math.sin(sun_azimuth) * math.cos(profile.sun_elevation_rad),
        -math.sin(profile.sun_elevation_rad),
    )
    lighting = {
        "sun_direction": sun_direction,
        "sun_color": profile.sun_color,
        "sdf_intensity": profile.sun_intensity,
        "blender_sun_energy": profile.sun_intensity * 2.5,
        "world_background": profile.background,
        "world_strength": 0.35,
    }
    frames, phases = [], []
    for frame_index in range(frames_per_clip):
        fraction = frame_index / (frames_per_clip - 1)
        travel, camera_shift = (fraction * 2 - 1) * extent * 1.4, (fraction - 0.5) * 1.2
        target_center = (center[0] + right[0] * travel, center[1] + right[1] * travel, center[2])
        shift = curriculum._matrix(*(target_center[k] - target_start[k] for k in range(3)))
        moved = tuple(replace(p, matrix=base.multiply(shift, p.matrix)) for p in moving)
        location = (
            center[0] + toward[0] * horizontal_distance + right[0] * camera_shift,
            center[1] + toward[1] * horizontal_distance + right[1] * camera_shift,
            center[2] + height,
        )
        slant_distance = math.dist(location, target_center)
        if not profile.distance_range_m[0] <= slant_distance <= profile.distance_range_m[1]:
            raise base.SdfRenderError(
                "actual camera-target range exceeds declared suite distance band"
            )
        if fraction < 0.15:
            phase, condition = "visible_before", "clear"
        elif fraction < 0.42:
            phase, condition = "entering_occlusion", "partial_occlusion"
        elif fraction <= 0.58:
            phase, condition = "occlusion_window", "full_occlusion"
        elif fraction <= 0.85:
            phase, condition = "leaving_occlusion", "partial_occlusion"
        else:
            phase, condition = "visible_after", "clear"
        frame_seed = hashlib.sha256(
            f"{first.camera['clip_seed_sha256']}|suite|{profile.key}|{frame_index}".encode()
        ).hexdigest()
        camera = {
            **first.camera,
            "index": frame_index,
            "location": list(location),
            "look_at": [
                center[0] + right[0] * framing_offset,
                center[1] + right[1] * framing_offset,
                center[2],
            ],
            "target_center": list(target_center),
            "target_present": True,
            "horizontal_distance_m": math.dist(location[:2], target_center[:2]),
            "slant_distance_m": slant_distance,
            "azimuth_rad": azimuth,
            "height_above_target_center_m": height,
            "look_at_horizontal_jitter_m": framing_offset,
            "look_at_vertical_jitter_m": 0.0,
            "sample_seed_sha256": frame_seed,
            "suite_profile": asdict(profile),
        }
        frames.append(
            replace(
                first,
                world=replace(
                    first.world,
                    primitives=static + moved,
                    target_center=target_center,
                    sun_direction=sun_direction,
                    sun_color=profile.sun_color,
                    sun_intensity=profile.sun_intensity,
                    background=profile.background,
                    sources=tuple(sorted(sources.items())),
                ),
                camera=camera,
                roles=roles,
                lighting=lighting,
                planned_condition=condition,
                sample_seed_sha256=frame_seed,
                target_model={
                    **first.target_model,
                    "placement_matrix": base.multiply(shift, placement),
                    "world_bounds_m": curriculum._bounds(moved),
                },
            )
        )
        phases.append(phase)
    return legacy.ClipPlan(
        f"{split}-suite-{profile.key}-{index:05d}", split, fps, tuple(frames), tuple(phases), ()
    )


def _verify_sources(root: Path, sources: dict[str, str], imported: dict[str, Path]) -> None:
    for relative, digest in sources.items():
        if base.sha256(base._source(root, relative)) != digest:
            raise base.SdfRenderError(f"source changed: {relative}; no suite manifest sealed")
    for relative, path in imported.items():
        if base.sha256(path) != sources[relative]:
            raise base.SdfRenderError(
                f"imported renderer source differs: {relative}; no suite manifest sealed"
            )


def _record_frame(clip, index, plan, args, paths, visible, amodal, matrix):
    if len(visible) != args.width * args.height or len(amodal) != len(visible):
        raise base.SdfRenderError("rendered suite mask dimensions mismatch")
    measurement = curriculum.measure_visibility(visible, amodal, plan.camera["target_present"])
    if measurement["measured_condition"].startswith("inconsistent"):
        raise base.SdfRenderError("inconsistent suite mask; no suite manifest sealed")
    box = base.visible_bbox(visible, args.width, args.height)
    paths["mask"].write_bytes(base._mask_png(visible, args.width, args.height))
    paths["amodal_mask"].write_bytes(base._mask_png(amodal, args.width, args.height))
    paths["label"].write_text(base.yolo_label(box, args.width, args.height), encoding="utf-8")
    frame = {
        "frame_id": f"{clip.clip_id}-frame-{index:05d}",
        "clip_id": clip.clip_id,
        "frame_index": index,
        "captured_at_s": index / clip.fps,
        "dt": 0.0 if index == 0 else 1.0 / clip.fps,
        "timestamp_semantics": legacy.TIME_SEMANTICS,
        "split": clip.split,
        "width": args.width,
        "height": args.height,
        "world_path": plan.world.relative_path,
        "world_sha256": dict(plan.world.sources)[plan.world.relative_path],
        "source_hashes": dict(plan.world.sources),
        "camera": {**plan.camera, "matrix_world": matrix},
        "target_model": plan.target_model,
        "lighting": plan.lighting,
        "scene_primitives": curriculum._primitive_inventory(plan),
        "sample_seed_sha256": plan.sample_seed_sha256,
        "suite_profile": plan.camera["suite_profile"],
        "planned_phase": clip.phases[index],
        "planned_condition": plan.planned_condition,
        "offline_disappearance_event": False,
        "target_present": True,
        "target_visible": box is not None,
        "visible_bbox_xyxy_px": box,
        "visible_target_pixels": measurement["visible_target_pixels"],
        "visibility_measurement": measurement,
        "offline_truth_only": True,
    }
    for kind, path in paths.items():
        frame[f"{kind}_path"] = path.relative_to(args.output_dir.resolve()).as_posix()
        frame[f"{kind}_sha256"] = base.sha256(path)
    frame["rgb_payload_sha256"] = frame["image_sha256"]
    frame["rgb_payload_hash_semantics"] = "encoded_rgb_png_file_bytes"
    return frame


def _record_clip(clip, frames):
    distances = [frame["camera"]["slant_distance_m"] for frame in frames]
    return {
        "clip_id": clip.clip_id,
        "split": clip.split,
        "frame_count": len(frames),
        "frame_ids": [frame["frame_id"] for frame in frames],
        "fps": clip.fps,
        "captured_at_start_s": 0.0,
        "captured_at_end_s": (len(frames) - 1) / clip.fps,
        "timestamp_semantics": legacy.TIME_SEMANTICS,
        "dt_semantics": "0_for_first_frame_else_1_over_fps",
        "world_path": frames[0]["world_path"],
        "world_sha256": frames[0]["world_sha256"],
        "source_hashes": frames[0]["source_hashes"],
        "suite_profile": frames[0]["suite_profile"],
        "actual_slant_distance_range_m": [min(distances), max(distances)],
        "target_model": {
            key: value
            for key, value in frames[0]["target_model"].items()
            if key not in {"placement_matrix", "world_bounds_m"}
        },
        "lighting": frames[0]["lighting"],
        "planned_phases": list(clip.phases),
        "offline_disappearance_frame_indices": [],
        "offline_truth_only": True,
        "measured_condition_counts": dict(
            Counter(frame["visibility_measurement"]["measured_condition"] for frame in frames)
        ),
    }


def render_dataset(args: argparse.Namespace) -> dict[str, Any]:
    output, root = args.output_dir.resolve(), args.repo_root.resolve()
    if output.exists():
        raise base.SdfRenderError("output-dir already exists; refusing overwrite")
    if args.clips_per_split < 3:
        raise base.SdfRenderError("suite requires at least three clips per split")
    # Snapshot before parsing, then verify again before rendering and sealing.
    paths = (*base.WORLD_PATHS.values(), *curriculum.TARGET_PATHS.values(), *SOURCE_SCRIPTS)
    sources = {relative: base.sha256(base._source(root, relative)) for relative in paths}
    imported = {
        curriculum.BASE_RENDERER_PATH: Path(base.__file__),
        curriculum.RENDERER_PATH: Path(curriculum.__file__),
        legacy.RENDERER_PATH: Path(legacy.__file__),
        RENDERER_PATH: Path(__file__),
    }
    worlds = {split: base.load_world(root, path) for split, path in base.WORLD_PATHS.items()}
    targets = {
        key: curriculum.load_target(root, path) for key, path in curriculum.TARGET_PATHS.items()
    }
    geometry_hashes = {key: curriculum.geometry_sha256(value) for key, value in targets.items()}
    if {geometry_hashes[key] for key in curriculum.SPLIT_GEOMETRIES["test"]} & {
        geometry_hashes[key]
        for split in ("train", "val")
        for key in curriculum.SPLIT_GEOMETRIES[split]
    }:
        raise base.SdfRenderError("test target geometry must be hash-disjoint from train and val")
    _verify_sources(root, sources, imported)
    import bpy  # type: ignore[import-not-found]
    import mathutils  # type: ignore[import-not-found]

    if bpy.app.version[:2] != (4, 5):
        raise base.SdfRenderError(f"requires Blender 4.5 API, got {bpy.app.version_string}")
    output.mkdir(parents=True, exist_ok=False)
    for group in ("images", "labels", "masks", "mask-renders", "amodal-masks", "amodal-renders"):
        for split in base.WORLD_PATHS:
            (output / group / split).mkdir(parents=True)
    all_frames, records = [], []
    for split, world in worlds.items():
        for index in range(args.clips_per_split):
            clip = plan_clip(
                root, world, split, args.seed, index, args.frames_per_clip, args.fps, targets
            )
            frames = []
            for frame_index, plan in enumerate(clip.frames):
                frame_id = f"{clip.clip_id}-frame-{frame_index:05d}"
                paths = legacy._frame_paths(output, split, frame_id)
                visible, amodal, matrix = curriculum._render_sample(
                    bpy,
                    mathutils,
                    plan,
                    args,
                    paths["image"],
                    paths["mask_render"],
                    paths["amodal_render"],
                )
                frames.append(
                    _record_frame(clip, frame_index, plan, args, paths, visible, amodal, matrix)
                )
            all_frames.extend(frames)
            records.append(_record_clip(clip, frames))
            print(f"{clip.clip_id}: {len(frames)} offline suite frames", flush=True)
    _verify_sources(root, sources, imported)
    yaml_path = output / "dataset.yaml"
    yaml_path.write_text(
        f"path: {json.dumps(output.as_posix())}\n"
        "train: images/train\nval: images/val\ntest: images/test\n"
        "names: ['search_target']\n",
        encoding="utf-8",
    )
    manifest = {
        "contract": legacy.CONTRACT,
        "clip_contract": legacy.CLIP_CONTRACT,
        "suite_contract": SUITE_CONTRACT,
        "purpose": "offline_synthetic_developer_visual_experiment",
        "names": ["search_target"],
        "dataset_yaml": "dataset.yaml",
        "dataset_yaml_sha256": base.sha256(yaml_path),
        "frame_count": len(all_frames),
        "frames": all_frames,
        "clips": records,
        "clip_count": len(records),
        "seed": args.seed,
        "split_unit": "source_world_and_complete_clip",
        "split_worlds": base.WORLD_PATHS,
        "split_geometries": curriculum.SPLIT_GEOMETRIES,
        "target_geometry_sha256": geometry_hashes,
        "test_geometry_disjoint_from_train_and_val": True,
        "source_hashes": sources,
        "profiles": [asdict(profile) for profile in PROFILES],
        "profile_design": (
            "joint_distance_angle_light_framing_conditions_not_single_factor_causal_test"
        ),
        "profile_geometry_assignment": (
            "rotated_groups_validation_geometry_offset_target_color_independent"
        ),
        "test_usage": "new_pixels_new_seed_development_test_not_untouched_blind_benchmark",
        "timestamp_semantics": legacy.TIME_SEMANTICS,
        "runtime_replay_input_allowlist": ["image_path", "image_sha256", "dt"],
        "offline_truth_only": True,
        "renderer": {
            "engine": args.engine,
            "blender_version": bpy.app.version_string,
            "script_sha256": sources[RENDERER_PATH],
            "base_script_sha256": sources[curriculum.BASE_RENDERER_PATH],
            "curriculum_script_sha256": sources[curriculum.RENDERER_PATH],
            "clip_script_sha256": sources[legacy.RENDERER_PATH],
            "render_samples": args.render_samples,
            "threads": args.threads,
            "cycles_device": "CPU" if args.engine == "CYCLES" else None,
        },
        "planned_condition_is_not_measured_evidence": True,
        "mask_semantics": "same_camera_3d_emission_visible_pass_retains_all_occluders",
        "amodal_semantics": "same_camera_hide_non_target_geometry",
        "mask_row_order": "top_to_bottom",
        "bbox_source": "visible_mask_pixels_exclusive_xyxy",
        "material_import": "opaque_diffuse_emissive_approximation_transparency_not_reproduced",
        "projection_bbox_used": False,
        "billboard_images_used": False,
        **dict.fromkeys(
            (
                "training_ready",
                "gazebo_capture",
                "flight",
                "promotion",
                "hardware_acceptance",
                "hil_acceptance",
                "outdoor_gnss_evidence",
                "final_release_allowed",
                "flight_action_labels_created",
            ),
            False,
        ),
        "limitations": [
            "offline_continuous_translation_not_physics_or_gazebo_sensor_capture",
            "declared_rendered_sampling_time_not_measured_runtime_timestamps",
            "planned_visibility_requires_actual_visible_and_amodal_mask_measurement",
            "primitive_prototypes_not_general_object_recognition",
            "coastal_utility_van_previously_used_for_development_diagnostics_not_blind_test",
            "no_flight_action_supervision_or_closed_loop_flight_or_hardware_acceptance",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return manifest


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--clips-per-split", type=int, default=3)
    parser.add_argument("--frames-per-clip", type=int, default=16)
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--engine", choices=("EEVEE", "CYCLES"), default="EEVEE")
    parser.add_argument("--render-samples", type=int, default=16)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)
    if not 3 <= args.clips_per_split <= 10000:
        parser.error("clips-per-split must be in [3, 10000] to cover near/medium/far")
    if not 12 <= args.frames_per_clip <= 10000:
        parser.error("frames-per-clip must be in [12, 10000]")
    if not math.isfinite(args.fps) or not 0 < args.fps <= 240:
        parser.error("fps must be finite and in (0, 240]")
    if not 16 <= args.width <= 4096 or not 16 <= args.height <= 4096:
        parser.error("width/height must be in [16, 4096]")
    if not 1 <= args.render_samples <= 4096 or not 0 <= args.seed < 2**32:
        parser.error("invalid render-samples or seed")
    if not 1 <= args.threads <= 1024:
        parser.error("threads must be in [1, 1024]")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        result = render_dataset(args)
    except (base.SdfRenderError, OSError, ET.ParseError, ImportError) as exc:
        print(f"SDF Blender clip suite failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "suite_contract": SUITE_CONTRACT,
                "frame_count": result["frame_count"],
                "clip_count": result["clip_count"],
                "training_ready": False,
                "output_dir": str(args.output_dir.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    blender_args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    raise SystemExit(main(blender_args))
