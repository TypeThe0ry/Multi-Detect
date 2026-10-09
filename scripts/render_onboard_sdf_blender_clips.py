#!/usr/bin/env python3
"""Continuous offline SDF/Blender clips; no sensor clock, physics or flight labels.

Run with Blender 4.5::

    blender --background --factory-startup --python this_script.py -- \
        --output-dir NEW_DIRECTORY --clips-per-split 3 --frames-per-clip 24 --fps 8

The frozen curriculum supplies primitive construction and its RGB, visible-mask
and amodal-mask passes. Geometry and materials stay fixed within each clip;
only the target and camera translate. Scene coordinates and masks are offline
truth, not inputs for a subsequent detector/tracker replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import render_onboard_sdf_blender_curriculum as curriculum  # noqa: E402

base = curriculum.base
CONTRACT = base.CONTRACT
CLIP_CONTRACT = "multidetect.offline-sdf-visual-clips-v1"
RENDERER_PATH = "scripts/render_onboard_sdf_blender_clips.py"
SOURCE_SCRIPTS = (curriculum.BASE_RENDERER_PATH, curriculum.RENDERER_PATH, RENDERER_PATH)
TIME_SEMANTICS = "declared_rendered_clip_sample_time_not_sensor_clock"


@dataclass(frozen=True)
class ClipPlan:
    clip_id: str
    split: str
    fps: float
    frames: tuple[curriculum.SamplePlan, ...]
    phases: tuple[str, ...]
    disappearance_frames: tuple[int, ...]


def plan_clip(
    root: Path,
    world: base.World,
    split: str,
    seed: int,
    index: int,
    frames_per_clip: int,
    fps: float,
    targets: dict[str, tuple[base.Primitive, ...]],
) -> ClipPlan:
    if split not in curriculum.SPLIT_GEOMETRIES or index < 0:
        raise base.SdfRenderError("invalid split or clip index")
    if frames_per_clip < 12 or not math.isfinite(fps) or fps <= 0:
        raise base.SdfRenderError("clips require at least 12 frames and finite positive fps")
    # Multiples of four choose a clear curriculum plan while cycling all three
    # training geometries. No new random appearance is sampled per frame.
    fixed = curriculum.plan_sample(root, world, split, seed, index * 4, targets)
    sources = dict(fixed.world.sources)
    sources[RENDERER_PATH] = base.sha256(base._source(root, RENDERER_PATH))
    lower, upper = fixed.target_model["world_bounds_m"]
    center = fixed.world.target_center
    extent = math.hypot(upper[0] - lower[0], upper[1] - lower[1])
    azimuth = fixed.camera["azimuth_rad"]
    toward = (math.cos(azimuth), math.sin(azimuth))
    right = (-toward[1], toward[0])
    distance = max(18.0, extent * 6.0)
    height = max(3.5, upper[2] - lower[2] + 2.0)
    screen_distance = distance * 0.18
    screen_height = upper[2] + height * 0.18 + 1.5
    screen = curriculum._primitive(
        "clips/fixed_construction_screen",
        "box",
        (extent * 1.4, 0.4, screen_height),
        (
            center[0] + toward[0] * screen_distance,
            center[1] + toward[1] * screen_distance,
            screen_height / 2,
        ),
        (0.35, 0.42, 0.48, 1.0),
        azimuth + math.pi / 2,
    )
    static = tuple(p for p in fixed.world.primitives if not p.target) + (screen,)
    moving = tuple(p for p in fixed.world.primitives if p.target)
    roles = {**fixed.roles, screen.name: "fixed_physical_occluder"}
    clip_id = f"{split}-clip-{index:05d}"
    # A minority of clips include explicitly artificial entity removal AFTER
    # the main physical occlusion. Hidden targets stay hidden in all three passes.
    hidden = (frames_per_clip - 3, frames_per_clip - 2) if index % 5 == 4 else ()
    frames, phases = [], []
    for frame_index in range(frames_per_clip):
        fraction = frame_index / (frames_per_clip - 1)
        travel = (fraction * 2.0 - 1.0) * extent * 1.9
        shift = curriculum._matrix(right[0] * travel, right[1] * travel, 0.0)
        moved = tuple(replace(p, matrix=base.multiply(shift, p.matrix)) for p in moving)
        target_center = (center[0] + right[0] * travel, center[1] + right[1] * travel, center[2])
        camera_shift = (fraction - 0.5) * 1.2
        present = frame_index not in hidden
        if not present:
            phase, condition = "explicit_offline_disappearance", "absent"
        elif fraction < 0.15:
            phase, condition = "visible_before", "clear"
        elif fraction < 0.42:
            phase, condition = "entering_occlusion", "partial_occlusion"
        elif fraction <= 0.58:
            phase, condition = "occlusion_window", "full_occlusion"
        elif fraction <= 0.85:
            phase, condition = "leaving_occlusion", "partial_occlusion"
        else:
            phase, condition = "visible_after", "clear"
        phases.append(phase)
        frame_seed = hashlib.sha256(
            f"{fixed.sample_seed_sha256}|clip-frame|{frame_index}".encode()
        ).hexdigest()
        camera = {
            **fixed.camera,
            "index": frame_index,
            "location": [
                center[0] + toward[0] * distance + right[0] * camera_shift,
                center[1] + toward[1] * distance + right[1] * camera_shift,
                center[2] + height,
            ],
            "look_at": list(center),
            "target_center": list(target_center),
            "horizontal_distance_m": math.hypot(distance, travel - camera_shift),
            "height_above_target_center_m": height,
            "look_at_horizontal_jitter_m": 0.0,
            "look_at_vertical_jitter_m": 0.0,
            "target_present": present,
            "clip_seed_sha256": fixed.sample_seed_sha256,
            "sample_seed_sha256": frame_seed,
        }
        frames.append(
            replace(
                fixed,
                world=replace(
                    fixed.world,
                    primitives=static + moved,
                    target_center=target_center,
                    sources=tuple(sorted(sources.items())),
                ),
                camera=camera,
                roles=roles,
                target_model={
                    **fixed.target_model,
                    "placement_matrix": base.multiply(
                        shift, fixed.target_model["placement_matrix"]
                    ),
                    "world_bounds_m": curriculum._bounds(moved),
                },
                planned_condition=condition,
                sample_seed_sha256=frame_seed,
            )
        )
    return ClipPlan(clip_id, split, fps, tuple(frames), tuple(phases), hidden)


def _frame_paths(output: Path, split: str, frame_id: str) -> dict[str, Path]:
    return {
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


def render_dataset(args: argparse.Namespace) -> dict[str, Any]:
    output, root = args.output_dir.resolve(), args.repo_root.resolve()
    if output.exists():
        raise base.SdfRenderError("output-dir already exists; refusing overwrite")
    worlds = {split: base.load_world(root, path) for split, path in base.WORLD_PATHS.items()}
    targets = {
        key: curriculum.load_target(root, path) for key, path in curriculum.TARGET_PATHS.items()
    }
    geometry_hashes = {key: curriculum.geometry_sha256(value) for key, value in targets.items()}
    test_geometry = {geometry_hashes[key] for key in curriculum.SPLIT_GEOMETRIES["test"]}
    development_geometry = {
        geometry_hashes[key]
        for split in ("train", "val")
        for key in curriculum.SPLIT_GEOMETRIES[split]
    }
    if test_geometry & development_geometry:
        raise base.SdfRenderError("test target geometry must be hash-disjoint from train and val")
    all_sources = {
        relative: digest for world in worlds.values() for relative, digest in world.sources
    }
    for relative in (*curriculum.TARGET_PATHS.values(), *SOURCE_SCRIPTS):
        all_sources[relative] = base.sha256(base._source(root, relative))
    # Imported renderer code must match the declared source copy as well.
    imported_sources = {
        SOURCE_SCRIPTS[0]: Path(base.__file__),
        SOURCE_SCRIPTS[1]: Path(curriculum.__file__),
        RENDERER_PATH: Path(__file__),
    }
    for relative, path in imported_sources.items():
        if base.sha256(path) != all_sources[relative]:
            raise base.SdfRenderError(
                f"imported renderer source differs from repo-root: {relative}"
            )
    import bpy  # type: ignore[import-not-found]
    import mathutils  # type: ignore[import-not-found]

    if bpy.app.version[:2] != (4, 5):
        raise base.SdfRenderError(f"requires Blender 4.5 API, got {bpy.app.version_string}")
    output.mkdir(parents=True, exist_ok=False)
    for group in ("images", "labels", "masks", "mask-renders", "amodal-masks", "amodal-renders"):
        for split in base.WORLD_PATHS:
            (output / group / split).mkdir(parents=True)
    frames, clips = [], []
    for split, world in worlds.items():
        for index in range(args.clips_per_split):
            clip = plan_clip(
                root, world, split, args.seed, index, args.frames_per_clip, args.fps, targets
            )
            clip_frames = []
            for frame_index, plan in enumerate(clip.frames):
                frame_id = f"{clip.clip_id}-frame-{frame_index:05d}"
                paths = _frame_paths(output, split, frame_id)
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
                    raise base.SdfRenderError("rendered clip mask dimensions mismatch")
                measurement = curriculum.measure_visibility(
                    visible, amodal, plan.camera["target_present"]
                )
                if measurement["measured_condition"].startswith("inconsistent"):
                    raise base.SdfRenderError("inconsistent rendered clip mask; no manifest sealed")
                box = base.visible_bbox(visible, args.width, args.height)
                paths["mask"].write_bytes(base._mask_png(visible, args.width, args.height))
                paths["amodal_mask"].write_bytes(base._mask_png(amodal, args.width, args.height))
                paths["label"].write_text(
                    base.yolo_label(box, args.width, args.height), encoding="utf-8"
                )
                frame = {
                    "frame_id": frame_id,
                    "clip_id": clip.clip_id,
                    "frame_index": frame_index,
                    "captured_at_s": frame_index / clip.fps,
                    "dt": 0.0 if frame_index == 0 else 1.0 / clip.fps,
                    "timestamp_semantics": TIME_SEMANTICS,
                    "split": split,
                    "width": args.width,
                    "height": args.height,
                    "world_path": world.relative_path,
                    "world_sha256": dict(world.sources)[world.relative_path],
                    "source_hashes": dict(plan.world.sources),
                    "camera": {**plan.camera, "matrix_world": matrix},
                    "target_model": plan.target_model,
                    "lighting": plan.lighting,
                    "scene_primitives": curriculum._primitive_inventory(plan),
                    "sample_seed_sha256": plan.sample_seed_sha256,
                    "planned_phase": clip.phases[frame_index],
                    "planned_condition": plan.planned_condition,
                    "offline_disappearance_event": frame_index in clip.disappearance_frames,
                    "target_present": plan.camera["target_present"],
                    "target_visible": box is not None,
                    "visible_bbox_xyxy_px": box,
                    "visible_target_pixels": measurement["visible_target_pixels"],
                    "visibility_measurement": measurement,
                    "offline_truth_only": True,
                }
                for kind, path in paths.items():
                    frame[f"{kind}_path"] = path.relative_to(output).as_posix()
                    frame[f"{kind}_sha256"] = base.sha256(path)
                frame["rgb_payload_sha256"] = frame["image_sha256"]
                frame["rgb_payload_hash_semantics"] = "encoded_rgb_png_file_bytes"
                clip_frames.append(frame)
                frames.append(frame)
            clips.append(
                {
                    "clip_id": clip.clip_id,
                    "split": split,
                    "frame_count": len(clip_frames),
                    "frame_ids": [frame["frame_id"] for frame in clip_frames],
                    "fps": clip.fps,
                    "captured_at_start_s": 0.0,
                    "captured_at_end_s": (len(clip_frames) - 1) / clip.fps,
                    "timestamp_semantics": TIME_SEMANTICS,
                    "dt_semantics": "0_for_first_frame_else_1_over_fps",
                    "world_path": world.relative_path,
                    "world_sha256": dict(world.sources)[world.relative_path],
                    "source_hashes": dict(clip.frames[0].world.sources),
                    "target_model": {
                        key: value
                        for key, value in clip.frames[0].target_model.items()
                        if key not in {"placement_matrix", "world_bounds_m"}
                    },
                    "lighting": clip.frames[0].lighting,
                    "planned_phases": list(clip.phases),
                    "offline_disappearance_frame_indices": list(clip.disappearance_frames),
                    "measured_condition_counts": dict(
                        Counter(
                            frame["visibility_measurement"]["measured_condition"]
                            for frame in clip_frames
                        )
                    ),
                    "offline_truth_only": True,
                }
            )
            print(f"{clip.clip_id}: {len(clip_frames)} offline rendered frames", flush=True)
    for relative, digest in all_sources.items():
        if base.sha256(base._source(root, relative)) != digest:
            raise base.SdfRenderError(
                f"source changed during render: {relative}; no manifest sealed"
            )
    for relative, path in imported_sources.items():
        if base.sha256(path) != all_sources[relative]:
            raise base.SdfRenderError(
                f"imported renderer source changed during render: {relative}; no manifest sealed"
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
        "clips": clips,
        "clip_count": len(clips),
        "clip_contract": CLIP_CONTRACT,
        "seed": args.seed,
        "split_unit": "source_world_and_complete_clip",
        "split_worlds": base.WORLD_PATHS,
        "split_geometries": curriculum.SPLIT_GEOMETRIES,
        "target_geometry_sha256": geometry_hashes,
        "test_geometry_disjoint_from_train_and_val": True,
        "source_hashes": all_sources,
        "timestamp_semantics": TIME_SEMANTICS,
        "runtime_replay_input_allowlist": ["image_path", "image_sha256", "dt"],
        "offline_truth_fields": [
            "mask",
            "amodal_mask",
            "label",
            "camera",
            "scene_primitives",
            "target_model",
            "target_present",
            "target_visible",
            "visibility_measurement",
            "planned_phase",
        ],
        "offline_truth_only": True,
        "renderer": {
            "engine": args.engine,
            "blender_version": bpy.app.version_string,
            "script_sha256": all_sources[RENDERER_PATH],
            "base_script_sha256": all_sources[curriculum.BASE_RENDERER_PATH],
            "curriculum_script_sha256": all_sources[curriculum.RENDERER_PATH],
            "render_samples": args.render_samples,
            "threads": args.threads,
            "cycles_device": "CPU" if args.engine == "CYCLES" else None,
        },
        "planned_condition_is_not_measured_evidence": True,
        "mask_semantics": "same_camera_3d_emission_visible_pass_retains_all_occluders",
        "amodal_semantics": "same_camera_hide_non_target_geometry_keep_absent_target_hidden",
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
        "flight_action_labels_created": False,
        "limitations": [
            "offline_continuous_translation_not_physics_or_gazebo_sensor_capture",
            "declared_rendered_sampling_time_not_measured_runtime_timestamps",
            "planned_visibility_phases_require_rendered_mask_measurement",
            "minority_entity_removal_events_are_explicit_offline_artificial_disappearance",
            "primitive_based_prototype_geometries_not_general_object_recognition",
            "no_closed_loop_flight_or_runtime_policy_supervision",
            "no_sensor_noise_or_domain_transfer_acceptance",
            "bbox_and_class_supervision_not_independently_reviewed",
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
    parser.add_argument("--frames-per-clip", type=int, default=24)
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--engine", choices=("EEVEE", "CYCLES"), default="EEVEE")
    parser.add_argument("--render-samples", type=int, default=16)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)
    if not 1 <= args.clips_per_split <= 10000:
        parser.error("clips-per-split must be in [1, 10000]")
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
        print(f"SDF Blender clips failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "contract": CONTRACT,
                "clip_contract": CLIP_CONTRACT,
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
