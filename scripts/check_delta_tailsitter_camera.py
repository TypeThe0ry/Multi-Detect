#!/usr/bin/env python3
"""Offline trimesh ray check for delta tailsitter camera self-occlusion."""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = ROOT / "simulation/gazebo/models"


def _pose_matrix(trimesh: Any, text: str | None):
    values = [float(value) for value in (text or "0 0 0 0 0 0").split()]
    matrix = trimesh.transformations.euler_matrix(*values[3:6], axes="sxyz")
    matrix[:3, 3] = values[:3]
    return matrix


def _translation(trimesh: Any, xyz: tuple[float, float, float]):
    matrix = trimesh.transformations.identity_matrix()
    matrix[:3, 3] = xyz
    return matrix


def _mesh_parts(trimesh: Any, variant: str) -> list[Any]:
    base_dir = MODEL_ROOT / "delta_tailsitter"
    root = ET.parse(base_dir / "model.sdf").getroot().find("model")
    if root is None:
        raise ValueError("delta_tailsitter model.sdf has no model element")
    meshes: list[Any] = []
    for link in root.findall("link"):
        link_name = str(link.get("name", ""))
        link_pose = _pose_matrix(trimesh, link.findtext("pose"))
        if link_pose is None:
            continue
        for visual in link.findall("visual"):
            uri = visual.findtext("geometry/mesh/uri")
            if not uri or not uri.startswith("model://delta_tailsitter/meshes/"):
                continue
            mesh_path = base_dir / uri.removeprefix("model://delta_tailsitter/")
            part = trimesh.load_mesh(mesh_path, process=False)
            part.apply_transform(link_pose @ _pose_matrix(trimesh, visual.findtext("pose")))
            meshes.append(part)

    if variant == "gimbal":
        wrapper = ET.parse(MODEL_ROOT / "delta_tailsitter_gimbal_cam/model.sdf").getroot().find("model")
        if wrapper is None:
            raise ValueError("gimbal wrapper has no model element")
        centre = (0.046, 0.0, 0.388)
        for link in wrapper.findall("link"):
            if link.get("name") == "camera_link":
                continue
            for visual in link.findall("visual"):
                box = visual.find("geometry/box")
                if box is None:
                    continue
                part = trimesh.creation.box(extents=[float(x) for x in box.findtext("size", "").split()])
                offset = _pose_matrix(trimesh, visual.findtext("pose"))
                offset[:3, 3] += centre
                part.apply_transform(offset)
                meshes.append(part)
    if not meshes:
        raise ValueError("no assembled visual meshes were loaded")
    return meshes


def _sensor_geometry(variant: str) -> tuple[list[float], list[float], float, int, int]:
    path = MODEL_ROOT / f"delta_tailsitter_{'gimbal_cam' if variant == 'gimbal' else 'mono_cam'}/model.sdf"
    root = ET.parse(path).getroot().find("model")
    if root is None:
        raise ValueError(f"{path} has no model element")
    sensor = root.find("link[@name='camera_link']/sensor[@name='camera']")
    if sensor is None:
        raise ValueError(f"{path} has no RGB sensor")
    pose = [float(value) for value in sensor.findtext("pose", "0 0 0 0 0 0").split()]
    fov = float(sensor.findtext("camera/horizontal_fov", "1.74"))
    width = int(sensor.findtext("camera/image/width", "1280"))
    height = int(sensor.findtext("camera/image/height", "960"))
    if variant == "fixed":
        link = root.find("link[@name='camera_link']")
        assert link is not None
        link_pose = [float(value) for value in link.findtext("pose", "0 0 0 0 0 0").split()]
        return link_pose, pose, fov, width, height
    pitch_joint = root.find("joint[@name='c11_pitch_joint']")
    pitch_pose = [float(value) for value in (pitch_joint.findtext("pose") if pitch_joint is not None else "0 0 0 0 0 0").split()]
    return pitch_pose, pose, fov, width, height


def _camera_body_mesh(trimesh: Any, pitch_joint_pose: list[float], pitch_deg: float):
    wrapper = ET.parse(MODEL_ROOT / "delta_tailsitter_gimbal_cam/model.sdf").getroot().find("model")
    assert wrapper is not None
    visual = wrapper.find("link[@name='camera_link']/visual[@name='camera_body']")
    assert visual is not None
    size = [float(value) for value in visual.findtext("geometry/box/size", "").split()]
    camera_body = trimesh.creation.box(extents=size)
    centre = _translation(trimesh, (0.046, 0.0, 0.388))
    joint = _pose_matrix(trimesh, " ".join(str(x) for x in pitch_joint_pose))
    pitch = trimesh.transformations.euler_matrix(0.0, math.radians(pitch_deg), 0.0, axes="sxyz")
    camera_body.apply_transform(centre @ joint @ pitch @ _pose_matrix(trimesh, visual.findtext("pose")))
    return camera_body


def _camera_pose(trimesh: Any, variant: str, sensor_link_pose: list[float], sensor_pose: list[float], pitch_deg: float):
    pitch_matrix = trimesh.transformations.euler_matrix(0.0, math.radians(pitch_deg), 0.0, axes="sxyz")
    if variant == "fixed":
        link_matrix = _pose_matrix(trimesh, " ".join(str(x) for x in sensor_link_pose))
        sensor_matrix = _pose_matrix(trimesh, " ".join(str(x) for x in sensor_pose))
        transform = link_matrix @ sensor_matrix
    else:
        c11 = _translation(trimesh, (0.046, 0.0, 0.388))
        joint_offset = _translation(trimesh, tuple(sensor_link_pose[:3]))
        sensor_offset = _translation(trimesh, tuple(sensor_pose[:3]))
        sensor_rotation = trimesh.transformations.euler_matrix(*sensor_pose[3:6], axes="sxyz")
        transform = c11 @ joint_offset @ pitch_matrix @ sensor_offset @ sensor_rotation
    return transform[:3, 3], transform[:3, :3]


def _occlusion_fraction(
    mesh: Any,
    origin: Any,
    rotation: Any,
    *,
    horizontal_fov: float,
    width: int,
    height: int,
    grid_width: int,
    grid_height: int,
) -> float:
    import numpy as np

    tan_h = math.tan(horizontal_fov / 2.0)
    vertical_fov = 2.0 * math.atan(tan_h * height / width)
    xs = np.linspace(-tan_h, tan_h, grid_width)
    ys = np.linspace(math.tan(vertical_fov / 2.0), -math.tan(vertical_fov / 2.0), grid_height)
    xx, yy = np.meshgrid(xs, ys)
    local = np.stack((np.ones_like(xx), xx, yy), axis=-1).reshape(-1, 3)
    local /= np.linalg.norm(local, axis=1, keepdims=True)
    directions = local @ rotation.T
    origins = np.broadcast_to(origin, directions.shape)
    hits = mesh.ray.intersects_any(origins, directions)
    return float(np.count_nonzero(hits) / hits.size)


def check_variant(
    variant: str,
    *,
    grid_width: int = 96,
    grid_height: int = 72,
    pitch_step_deg: float = 10.0,
) -> dict[str, Any]:
    try:
        import trimesh
    except ImportError as exc:
        raise RuntimeError("trimesh is required; use the existing geometry environment") from exc
    parts = _mesh_parts(trimesh, variant)
    mesh = trimesh.util.concatenate(parts)
    link_pose, sensor_pose, fov, width, height = _sensor_geometry(variant)
    pitches = [0.0] if variant == "fixed" else [float(value) for value in range(-90, 1, int(pitch_step_deg))]
    if variant == "gimbal" and pitches[-1] != 0.0:
        pitches.append(0.0)
    samples = []
    for pitch in pitches:
        origin, rotation = _camera_pose(trimesh, variant, link_pose, sensor_pose, pitch)
        ray_mesh = trimesh.util.concatenate(
            [mesh, _camera_body_mesh(trimesh, link_pose, pitch)]
            if variant == "gimbal"
            else [mesh]
        )
        fraction = _occlusion_fraction(
            ray_mesh,
            origin,
            rotation,
            horizontal_fov=fov,
            width=width,
            height=height,
            grid_width=grid_width,
            grid_height=grid_height,
        )
        samples.append({"pitch_deg": pitch, "self_occlusion_fraction": fraction})
    maximum = max(sample["self_occlusion_fraction"] for sample in samples)
    return {
        "variant": variant,
        "mesh_face_count": int(len(mesh.faces)),
        "ray_grid": [grid_width, grid_height],
        "samples": samples,
        "maximum_self_occlusion_fraction": maximum,
        "threshold_fraction": 0.05,
        "pass": maximum < 0.05,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("fixed", "gimbal", "both"), default="both")
    parser.add_argument("--grid-width", type=int, default=96)
    parser.add_argument("--grid-height", type=int, default=72)
    parser.add_argument("--pitch-step-deg", type=float, default=10.0)
    args = parser.parse_args(argv)
    variants = ("fixed", "gimbal") if args.variant == "both" else (args.variant,)
    results = [
        check_variant(
            variant,
            grid_width=args.grid_width,
            grid_height=args.grid_height,
            pitch_step_deg=args.pitch_step_deg,
        )
        for variant in variants
    ]
    print(json.dumps({"results": results, "pass": all(item["pass"] for item in results)}, indent=2))
    return 0 if all(item["pass"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
