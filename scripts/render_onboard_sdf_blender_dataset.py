#!/usr/bin/env python3
"""Render existing SDF primitives as an offline Blender visual experiment.

Run with Blender 4.5, not a normal Python interpreter::

    blender --background --factory-startup --python this_script.py -- \
        --output-dir NEW_DIRECTORY --samples-per-split 1

RGB and visible-instance masks are separate 3-D renders of the same geometry
and camera. Bounding boxes come only from the rendered mask pixels, never
from a projected 3-D box. This is not Gazebo/PX4 capture or flight evidence.
SDF materials are approximated as opaque diffuse/emissive surfaces in both
passes; transparent water, physics and sensor noise are not reproduced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-sdf-blender-visual-dataset-v1"
TARGET_URI = "model://multidetect_search_target_v1"
TARGET_PATH = "simulation/gazebo/models/multidetect_search_target_v1/model.sdf"
WORLD_PATHS = {
    "train": "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf",
    "val": "simulation/gazebo/worlds/river_rescue_vtol_001.sdf",
    "test": "simulation/gazebo/worlds/coastal_patrol_fixed_wing_001.sdf",
}
Matrix = tuple[tuple[float, ...], ...]
IDENTITY: Matrix = tuple(tuple(float(i == j) for j in range(4)) for i in range(4))


class SdfRenderError(ValueError):
    """Unsupported or inconsistent input: do not guess SDF semantics."""


@dataclass(frozen=True)
class Primitive:
    name: str
    geometry: str
    dimensions: tuple[float, ...]
    matrix: Matrix
    diffuse: tuple[float, ...]
    emissive: tuple[float, ...]
    target: bool


@dataclass(frozen=True)
class World:
    relative_path: str
    primitives: tuple[Primitive, ...]
    target_center: tuple[float, ...]
    background: tuple[float, ...]
    sun_direction: tuple[float, ...]
    sun_color: tuple[float, ...]
    sun_intensity: float
    sources: tuple[tuple[str, str], ...]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _numbers(text: str | None, count: int, description: str) -> tuple[float, ...]:
    try:
        values = tuple(float(value) for value in (text or "").split())
    except ValueError as exc:
        raise SdfRenderError(f"invalid {description}") from exc
    if len(values) != count or not all(math.isfinite(value) for value in values):
        raise SdfRenderError(f"{description} requires {count} finite values")
    return values


def multiply(left: Matrix, right: Matrix) -> Matrix:
    return tuple(
        tuple(sum(left[i][k] * right[k][j] for k in range(4)) for j in range(4)) for i in range(4)
    )


def transform_point(matrix: Matrix, point: Sequence[float]) -> tuple[float, ...]:
    return tuple(sum(matrix[i][j] * point[j] for j in range(3)) + matrix[i][3] for i in range(3))


def pose_matrix(element: ET.Element) -> Matrix:
    pose = element.find("pose")
    if pose is None:
        return IDENTITY
    if pose.get("relative_to") or pose.get("rotation_format", "euler_rpy") != "euler_rpy":
        raise SdfRenderError("pose relative_to / non-Euler rotation is unsupported")
    if pose.get("degrees", "false").lower() not in {"false", "0"}:
        raise SdfRenderError("degree-format pose is unsupported")
    x, y, z, roll, pitch, yaw = _numbers(pose.text, 6, "pose")
    cr, sr, cp, sp, cy, sy = (
        math.cos(roll),
        math.sin(roll),
        math.cos(pitch),
        math.sin(pitch),
        math.cos(yaw),
        math.sin(yaw),
    )
    # SDF implicit parent-frame transform: Rz(yaw) Ry(pitch) Rx(roll).
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, x),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, y),
        (-sp, cp * sr, cp * cr, z),
        (0.0, 0.0, 0.0, 1.0),
    )


def _source(root: Path, relative: str) -> Path:
    candidate = root / relative
    if not candidate.is_file() or candidate.is_symlink():
        raise SdfRenderError(f"missing or linked SDF source: {relative}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise SdfRenderError(f"source escapes repository: {relative}")
    return candidate


def _geometry(visual: ET.Element) -> tuple[str, tuple[float, ...], Matrix]:
    node = visual.find("geometry")
    if node is None or len(node) != 1:
        raise SdfRenderError("visual needs exactly one geometry")
    shape = node[0]
    if shape.tag == "box":
        dimensions = _numbers(shape.findtext("size"), 3, "box size")
    elif shape.tag == "cylinder":
        dimensions = (
            *_numbers(shape.findtext("radius"), 1, "cylinder radius"),
            *_numbers(shape.findtext("length"), 1, "cylinder length"),
        )
    elif shape.tag == "sphere":
        dimensions = _numbers(shape.findtext("radius"), 1, "sphere radius")
    elif shape.tag == "plane":
        dimensions = _numbers(shape.findtext("size"), 2, "plane size")
    else:
        raise SdfRenderError(f"unsupported geometry: {shape.tag}")
    if any(value <= 0.0 for value in dimensions):
        raise SdfRenderError("geometry dimensions must be positive")
    orientation = IDENTITY
    if shape.tag == "plane":
        normal = _numbers(shape.findtext("normal"), 3, "plane normal")
        length = math.sqrt(sum(value * value for value in normal))
        if length < 1e-12:
            raise SdfRenderError("plane normal is zero")
        normal = tuple(value / length for value in normal)
        tangent = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
        dot = sum(a * b for a, b in zip(normal, tangent, strict=True))
        tangent = tuple(tangent[i] - dot * normal[i] for i in range(3))
        length = math.sqrt(sum(value * value for value in tangent))
        tangent = tuple(value / length for value in tangent)
        bitangent = (
            normal[1] * tangent[2] - normal[2] * tangent[1],
            normal[2] * tangent[0] - normal[0] * tangent[2],
            normal[0] * tangent[1] - normal[1] * tangent[0],
        )
        orientation = tuple((tangent[i], bitangent[i], normal[i], 0.0) for i in range(3)) + (
            (0.0, 0.0, 0.0, 1.0),
        )
    return shape.tag, dimensions, orientation


def _color(parent: ET.Element, name: str, default: str) -> tuple[float, ...]:
    values = _numbers(parent.findtext(name, default), 4, name)
    if any(value < 0.0 or value > 1.0 for value in values):
        raise SdfRenderError(f"{name} must be in [0, 1]")
    return values


def _model_primitives(
    model: ET.Element, parent: Matrix, target: bool, name: str
) -> list[Primitive]:
    if any(model.find(tag) is not None for tag in ("model", "include", "frame")):
        raise SdfRenderError("nested models/includes/named frames are unsupported")
    matrix = multiply(parent, pose_matrix(model))
    result = []
    for link in model.findall("link"):
        link_matrix = multiply(matrix, pose_matrix(link))
        for visual in link.findall("visual"):
            geometry, dimensions, orientation = _geometry(visual)
            visual_matrix = multiply(multiply(link_matrix, pose_matrix(visual)), orientation)
            material = visual.find("material")
            if material is None:
                material = ET.Element("material")
            if material.find("script") is not None or material.find("pbr") is not None:
                raise SdfRenderError("scripted/PBR SDF materials are unsupported")
            result.append(
                Primitive(
                    name=f"{name}/{link.get('name', '')}/{visual.get('name', '')}",
                    geometry=geometry,
                    dimensions=dimensions,
                    matrix=visual_matrix,
                    diffuse=_color(material, "diffuse", "0.5 0.5 0.5 1"),
                    emissive=_color(material, "emissive", "0 0 0 1"),
                    target=target,
                )
            )
    return result


def _corners(primitive: Primitive) -> list[tuple[float, ...]]:
    if primitive.geometry == "box":
        extents = tuple(value / 2 for value in primitive.dimensions)
    elif primitive.geometry == "cylinder":
        radius, length = primitive.dimensions
        extents = (radius, radius, length / 2)
    elif primitive.geometry == "sphere":
        extents = primitive.dimensions * 3
    else:
        extents = (*[value / 2 for value in primitive.dimensions], 0.0)
    return [
        transform_point(primitive.matrix, (x, y, z))
        for x in (-extents[0], extents[0])
        for y in (-extents[1], extents[1])
        for z in (-extents[2], extents[2])
    ]


def load_world(root: Path, relative_path: str) -> World:
    path = _source(root, relative_path)
    world = ET.parse(path).getroot().find("world")
    if world is None or world.find("frame") is not None:
        raise SdfRenderError("requires a world without named frames")
    sources = {relative_path: sha256(path)}
    primitives = []
    for model in world.findall("model"):
        primitives.extend(_model_primitives(model, IDENTITY, False, model.get("name", "")))
    includes = world.findall("include")
    if len(includes) != 1 or includes[0].findtext("uri") != TARGET_URI:
        raise SdfRenderError("world requires exactly one local neutral-target include")
    include = includes[0]
    if include.find("placement_frame") is not None:
        raise SdfRenderError("include placement_frame is unsupported")
    target_path = _source(root, TARGET_PATH)
    sources[TARGET_PATH] = sha256(target_path)
    target_model = ET.parse(target_path).getroot().find("model")
    if target_model is None:
        raise SdfRenderError("target SDF lacks model")
    primitives.extend(
        _model_primitives(
            target_model, pose_matrix(include), True, include.findtext("name", "search_target")
        )
    )
    target_points = [
        point for primitive in primitives if primitive.target for point in _corners(primitive)
    ]
    if not target_points:
        raise SdfRenderError("target has no visual primitives")
    center = tuple(
        (min(point[i] for point in target_points) + max(point[i] for point in target_points)) / 2
        for i in range(3)
    )
    lights = world.findall("light")
    if len(lights) != 1 or lights[0].get("type") != "directional":
        raise SdfRenderError("requires exactly one directional light")
    light = lights[0]
    direction = _numbers(light.findtext("direction"), 3, "sun direction")
    light_matrix = pose_matrix(light)
    direction = tuple(sum(light_matrix[i][j] * direction[j] for j in range(3)) for i in range(3))
    if sum(value * value for value in direction) < 1e-12:
        raise SdfRenderError("sun direction is zero")
    scene = world.find("scene")
    if scene is None:
        scene = ET.Element("scene")
    intensity = _numbers(light.findtext("intensity", "1"), 1, "sun intensity")[0]
    if intensity <= 0.0:
        raise SdfRenderError("sun intensity must be positive")
    return World(
        relative_path=relative_path,
        primitives=tuple(primitives),
        target_center=center,
        background=_color(scene, "background", "0.6 0.75 0.9 1"),
        sun_direction=direction,
        sun_color=_color(light, "diffuse", "1 1 1 1"),
        sun_intensity=intensity,
        sources=tuple(sorted(sources.items())),
    )


def camera_samples(
    world: World,
    split: str,
    seed: int,
    count: int,
    *,
    propeller_angle_rad: float = 0.0,
    propeller_spin_rad_s: float = 0.0,
    sample_period_s: float = 1.0 / 30.0,
    gimbal_angle_rad: Sequence[float] = (0.0, 0.0, 0.0),
    camera_pose: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Create camera samples with explicit, replayable actuator state."""
    values = (float(propeller_angle_rad), float(propeller_spin_rad_s), float(sample_period_s))
    if not all(math.isfinite(value) for value in values) or sample_period_s <= 0.0:
        raise SdfRenderError(
            "propeller inputs and sample period must be finite; period must be positive"
        )
    if len(gimbal_angle_rad) != 3 or not all(
        math.isfinite(float(value)) for value in gimbal_angle_rad
    ):
        raise SdfRenderError("gimbal_angle_rad must contain three finite values")
    pose_location: list[float] | None = None
    pose_look_at: list[float] | None = None
    pose_rotation_matrix: list[list[float]] | None = None
    if camera_pose is not None:
        center = camera_pose.get("center_ned", camera_pose.get("center"))
        rotation = camera_pose.get("optical_to_world")
        if (
            not isinstance(center, (list, tuple))
            or len(center) != 3
            or not all(math.isfinite(float(value)) for value in center)
            or not isinstance(rotation, (list, tuple))
            or len(rotation) != 9
            or not all(math.isfinite(float(value)) for value in rotation)
        ):
            raise SdfRenderError("camera_pose requires finite center_ned and optical_to_world")
        # camera_pose is expressed in NED, while the loaded SDF scene is ENU.
        north, east, down = (float(value) for value in center)
        pose_location = [east, north, -down]
        # optical_to_world maps optical axes into NED; convert the first
        # optical axis (+X/forward) into Blender's ENU world.
        forward_n, forward_e, forward_d = (
            float(rotation[0]), float(rotation[3]), float(rotation[6])
        )
        optical_to_ned = [
            [float(rotation[row * 3 + column]) for column in range(3)]
            for row in range(3)
        ]

        def ned_to_enu(vector: Sequence[float]) -> tuple[float, float, float]:
            return float(vector[1]), float(vector[0]), -float(vector[2])

        optical_columns = [
            tuple(optical_to_ned[row][column] for row in range(3)) for column in range(3)
        ]
        blender_camera_axes = [
            ned_to_enu(optical_columns[1]),
            ned_to_enu(tuple(-value for value in optical_columns[2])),
            ned_to_enu(tuple(-value for value in optical_columns[0])),
        ]
        pose_rotation_matrix = [
            [blender_camera_axes[column][row] for column in range(3)]
            for row in range(3)
        ]
        pose_look_at = [
            pose_location[0] + forward_e,
            pose_location[1] + forward_n,
            pose_location[2] - forward_d,
        ]
    digest = hashlib.sha256(
        f"{seed}|{split}|{dict(world.sources)[world.relative_path]}".encode()
    ).hexdigest()
    rng = random.Random(int(digest, 16))
    samples = []
    for index in range(count):
        azimuth = rng.uniform(-math.pi, math.pi)
        distance = rng.uniform(7.0, 38.0)
        height = rng.uniform(3.0, 18.0)
        horizontal_jitter = 0.0 if index == 0 else rng.uniform(-0.3, 0.3) * distance
        vertical_jitter = 0.0 if index == 0 else rng.uniform(-0.15, 0.15) * distance
        if pose_location is not None:
            target_dx = pose_location[0] - world.target_center[0]
            target_dy = pose_location[1] - world.target_center[1]
            distance = math.hypot(target_dx, target_dy)
            height = pose_location[2] - world.target_center[2]
            azimuth = math.atan2(target_dy, target_dx) if distance else 0.0
            horizontal_jitter = vertical_jitter = 0.0
            location = list(pose_location)
            look_at = list(pose_look_at or pose_location)
        else:
            look_at = [
                world.target_center[0] - math.sin(azimuth) * horizontal_jitter,
                world.target_center[1] + math.cos(azimuth) * horizontal_jitter,
                world.target_center[2] + vertical_jitter,
            ]
            location = [
                world.target_center[0] + distance * math.cos(azimuth),
                world.target_center[1] + distance * math.sin(azimuth),
                world.target_center[2] + height,
            ]
        angle = propeller_angle_rad + index * sample_period_s * propeller_spin_rad_s
        samples.append(
            {
                "index": index,
                "split_seed_sha256": digest,
                "azimuth_rad": azimuth,
                "horizontal_distance_m": distance,
                "height_above_target_center_m": height,
                "location": location,
                "look_at": look_at,
                **(
                    {"rotation_matrix": pose_rotation_matrix}
                    if pose_rotation_matrix is not None
                    else {}
                ),
                "target_center": list(world.target_center),
                "look_at_horizontal_jitter_m": horizontal_jitter,
                "look_at_vertical_jitter_m": vertical_jitter,
                "target_present": index % 5 != 4,
                "horizontal_fov_rad": math.radians(60),
                "gimbal_angle_rad": [float(value) for value in gimbal_angle_rad],
                "propeller_angle_rad": angle,
                "propeller_spin_rad_s": float(propeller_spin_rad_s),
                "propeller_state_source": "renderer_input",
            }
        )
    return samples


def apply_propeller_state(objects: Sequence[Any], camera: Mapping[str, Any]) -> bool:
    """Apply input propeller angles to Blender objects that declare rotor indices.

    Current SDF worlds may not contain aircraft visuals; in that case this is
    a no-op while the same state is still sealed in the frame manifest.
    """
    angle = float(camera.get("propeller_angle_rad", 0.0))
    spin = float(camera.get("propeller_spin_rad_s", 0.0))
    applied = False
    for index, obj in enumerate(objects):
        rotor_index = obj.get("propeller_index") if hasattr(obj, "get") else None
        name = str(getattr(obj, "name", "")).lower()
        if rotor_index is None and not any(token in name for token in ("rotor", "propeller")):
            continue
        try:
            rotor_index = int(rotor_index if rotor_index is not None else index)
        except (TypeError, ValueError):
            rotor_index = index
        direction = -1.0 if rotor_index % 2 else 1.0
        obj.rotation_euler.z = direction * angle
        obj["propeller_angle_rad"] = angle
        obj["propeller_spin_rad_s"] = spin
        applied = True
    return applied


def mask_from_rgba(pixels: Sequence[float], width: int, height: int) -> bytes:
    """Blender bottom-up RGBA -> top-down binary mask, >= 0.5 coverage."""
    if len(pixels) != width * height * 4:
        raise SdfRenderError("rendered mask dimensions mismatch")
    return bytes(
        255 if pixels[((height - 1 - y) * width + x) * 4] >= 0.5 else 0
        for y in range(height)
        for x in range(width)
    )


def visible_bbox(mask: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    if len(mask) != width * height:
        raise SdfRenderError("binary mask dimensions mismatch")
    indices = [index for index, value in enumerate(mask) if value]
    if not indices:
        return None
    return (
        min(index % width for index in indices),
        min(index // width for index in indices),
        max(index % width for index in indices) + 1,
        max(index // width for index in indices) + 1,
    )


def yolo_label(box: tuple[int, int, int, int] | None, width: int, height: int) -> str:
    if box is None:
        return ""
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise SdfRenderError("visible bbox escapes image")
    return (
        f"0 {(x1 + x2) / (2 * width):.9f} {(y1 + y2) / (2 * height):.9f} "
        f"{(x2 - x1) / width:.9f} {(y2 - y1) / height:.9f}\n"
    )


def _mask_png(mask: bytes, width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    rows = b"".join(b"\0" + mask[y * width : (y + 1) * width] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _material(
    bpy: Any, name: str, color: Sequence[float], emissive: Sequence[float], mask: bool
) -> Any:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    if mask:
        shader = nodes.new("ShaderNodeEmission")
        shader.inputs["Color"].default_value = (*color[:3], 1.0)
        shader.inputs["Strength"].default_value = 1.0
    else:
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        shader.inputs["Base Color"].default_value = (*color[:3], 1.0)
        shader.inputs["Roughness"].default_value = 0.65
        shader.inputs["Emission Color"].default_value = (*emissive[:3], 1.0)
        shader.inputs["Emission Strength"].default_value = 1.0
    material.node_tree.links.new(shader.outputs[0], output.inputs["Surface"])
    return material


def _build_blender_scene(
    bpy: Any, mathutils: Any, world: World, args: argparse.Namespace
) -> tuple[Any, list[Any]]:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.threads_mode = "FIXED"
    scene.render.threads = args.threads
    scene.render.engine = "BLENDER_EEVEE_NEXT" if args.engine == "EEVEE" else "CYCLES"
    if args.engine == "CYCLES":
        scene.cycles.samples = args.render_samples
        scene.cycles.device = "CPU"
        scene.cycles.use_denoising = False
        scene.cycles.seed = args.seed
    else:
        scene.eevee.taa_render_samples = args.render_samples
    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.world = bpy.data.worlds.new("sdf_background")
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = world.background
    background.inputs["Strength"].default_value = 0.35
    objects = []
    for primitive in world.primitives:
        shape, dimensions = primitive.geometry, primitive.dimensions
        if shape == "box":
            bpy.ops.mesh.primitive_cube_add(size=1)
            scale = dimensions
        elif shape == "cylinder":
            bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=1, depth=1)
            scale = (dimensions[0], dimensions[0], dimensions[1])
        elif shape == "sphere":
            bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=1)
            scale = dimensions * 3
        else:
            bpy.ops.mesh.primitive_plane_add(size=1)
            scale = (*dimensions, 1.0)
        obj = bpy.context.object
        obj.name = primitive.name
        obj.matrix_world = mathutils.Matrix(primitive.matrix) @ mathutils.Matrix.Diagonal(
            (*scale, 1.0)
        )
        obj["sdf_target"] = primitive.target
        obj.data.materials.append(
            _material(bpy, primitive.name, primitive.diffuse, primitive.emissive, False)
        )
        objects.append(obj)
    sun_data = bpy.data.lights.new("sdf_sun", "SUN")
    sun_data.energy = world.sun_intensity * 2.5
    sun_data.color = world.sun_color[:3]
    sun = bpy.data.objects.new("sdf_sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = mathutils.Vector(world.sun_direction).to_track_quat("-Z", "Y").to_euler()
    camera_data = bpy.data.cameras.new("sdf_dataset_camera")
    camera_data.type = "PERSP"
    camera_data.sensor_fit = "HORIZONTAL"
    camera_data.sensor_width = 36.0
    camera_data.lens = 36.0 / (2.0 * math.tan(math.radians(60) / 2.0))
    camera_data.clip_start, camera_data.clip_end = 0.05, 3000.0
    camera = bpy.data.objects.new("sdf_dataset_camera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return scene, objects


def _render_frame(
    bpy: Any,
    mathutils: Any,
    scene: Any,
    objects: list[Any],
    camera: dict[str, Any],
    image_path: Path,
    mask_render_path: Path,
    width: int,
    height: int,
) -> tuple[bytes, list[list[float]]]:
    camera["propeller_state_applied"] = apply_propeller_state(objects, camera)
    scene.camera.location = mathutils.Vector(camera["location"])
    if "rotation_matrix" in camera:
        scene.camera.rotation_euler = mathutils.Matrix(camera["rotation_matrix"]).to_euler()
    else:
        direction = mathutils.Vector(camera["look_at"]) - scene.camera.location
        tracking = direction.to_track_quat("-Z", "Y")
        gimbal = mathutils.Euler(tuple(camera["gimbal_angle_rad"]), "XYZ").to_quaternion()
        scene.camera.rotation_euler = (tracking @ gimbal).to_euler()
    # Remove the target mesh, not its label, for offline negative examples.
    # The same visibility state is used for both RGB and the 3-D mask pass.
    for obj in objects:
        obj.hide_render = bool(obj["sdf_target"] and not camera["target_present"])
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = str(image_path)
    bpy.ops.render.render(write_still=True)
    original_materials = [list(obj.data.materials) for obj in objects]
    background = scene.world.node_tree.nodes.get("Background")
    original_background = tuple(background.inputs["Color"].default_value)
    white = _material(bpy, "visible_target_mask", (1, 1, 1, 1), (0, 0, 0, 1), True)
    black = _material(bpy, "occluding_scene_mask", (0, 0, 0, 1), (0, 0, 0, 1), True)
    try:
        # Keep every non-target mesh in place: depth occlusion survives this pass.
        for obj in objects:
            obj.data.materials.clear()
            obj.data.materials.append(white if obj["sdf_target"] else black)
        background.inputs["Color"].default_value = (0, 0, 0, 1)
        scene.render.image_settings.color_mode = "BW"
        scene.render.filepath = str(mask_render_path)
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(str(mask_render_path), check_existing=False)
        try:
            if tuple(image.size) != (width, height):
                raise SdfRenderError("saved mask render dimensions mismatch")
            pixels = array("f", [0.0]) * (width * height * 4)
            image.pixels.foreach_get(pixels)
            mask = mask_from_rgba(pixels, width, height)
        finally:
            bpy.data.images.remove(image)
    finally:
        for obj, materials in zip(objects, original_materials, strict=True):
            obj.data.materials.clear()
            for material in materials:
                obj.data.materials.append(material)
        background.inputs["Color"].default_value = original_background
        bpy.data.materials.remove(white)
        bpy.data.materials.remove(black)
    return mask, [list(row) for row in scene.camera.matrix_world]


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--samples-per-split", type=int)
    parser.add_argument("--train-count", type=int, default=36)
    parser.add_argument("--val-count", type=int, default=12)
    parser.add_argument("--test-count", type=int, default=12)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--engine", choices=("EEVEE", "CYCLES"), default="EEVEE")
    parser.add_argument("--render-samples", type=int, default=16)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--propeller-angle-rad", type=float, default=0.0)
    parser.add_argument("--propeller-spin-rad-s", type=float, default=0.0)
    parser.add_argument("--sample-period-s", type=float, default=1.0 / 30.0)
    parser.add_argument("--gimbal-pitch-rad", type=float, default=0.0)
    parser.add_argument("--gimbal-yaw-rad", type=float, default=0.0)
    parser.add_argument("--gimbal-roll-rad", type=float, default=0.0)
    parser.add_argument(
        "--camera-pose-json",
        type=Path,
        help="JSON object containing center_ned and optical_to_world for offline replay",
    )
    args = parser.parse_args(argv)
    if args.samples_per_split is not None:
        args.train_count = args.val_count = args.test_count = args.samples_per_split
    if not all(1 <= getattr(args, f"{split}_count") <= 10000 for split in WORLD_PATHS):
        parser.error("each split count must be in [1, 10000]")
    if not 16 <= args.width <= 4096 or not 16 <= args.height <= 4096:
        parser.error("width/height must be in [16, 4096]")
    if not 1 <= args.render_samples <= 4096 or not 0 <= args.seed < 2**32:
        parser.error("invalid render-samples or seed")
    if not 1 <= args.threads <= 1024:
        parser.error("threads must be in [1, 1024]")
    if not all(
        math.isfinite(float(value))
        for value in (
            args.propeller_angle_rad,
            args.propeller_spin_rad_s,
            args.sample_period_s,
            args.gimbal_pitch_rad,
            args.gimbal_yaw_rad,
            args.gimbal_roll_rad,
        )
    ) or args.sample_period_s <= 0.0:
        parser.error("actuator inputs must be finite and sample-period-s must be positive")
    return args


def render_dataset(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_dir.resolve()
    if output.exists():
        raise SdfRenderError("output-dir already exists; refusing overwrite")
    root = args.repo_root.resolve()
    worlds = {split: load_world(root, relative) for split, relative in WORLD_PATHS.items()}
    camera_pose = None
    if args.camera_pose_json is not None:
        try:
            raw_pose = json.loads(args.camera_pose_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SdfRenderError(f"camera pose JSON is invalid: {args.camera_pose_json}") from exc
        if not isinstance(raw_pose, dict):
            raise SdfRenderError("camera pose JSON must contain one object")
        camera_pose = raw_pose
    # Import only after stdlib parsing; tests and --help do not require Blender.
    import bpy  # type: ignore[import-not-found]
    import mathutils  # type: ignore[import-not-found]

    if bpy.app.version[:2] != (4, 5):
        raise SdfRenderError(f"requires Blender 4.5 API, got {bpy.app.version_string}")
    script_sha = sha256(Path(__file__))
    output.mkdir(parents=True, exist_ok=False)
    for group in ("images", "labels", "masks", "mask-renders"):
        for split in WORLD_PATHS:
            (output / group / split).mkdir(parents=True)
    frames = []
    for split, world in worlds.items():
        scene, objects = _build_blender_scene(bpy, mathutils, world, args)
        samples = camera_samples(
            world,
            split,
            args.seed,
            getattr(args, f"{split}_count"),
            propeller_angle_rad=args.propeller_angle_rad,
            propeller_spin_rad_s=args.propeller_spin_rad_s,
            sample_period_s=args.sample_period_s,
            gimbal_angle_rad=(args.gimbal_pitch_rad, args.gimbal_yaw_rad, args.gimbal_roll_rad),
            camera_pose=camera_pose,
        )
        for camera in samples:
            frame_id = f"{split}-{camera['index']:05d}"
            image = output / "images" / split / f"{frame_id}.png"
            raw_mask = output / "mask-renders" / split / f"{frame_id}.png"
            mask_path = output / "masks" / split / f"{frame_id}.png"
            label = output / "labels" / split / f"{frame_id}.txt"
            mask, matrix = _render_frame(
                bpy, mathutils, scene, objects, camera, image, raw_mask, args.width, args.height
            )
            box = visible_bbox(mask, args.width, args.height)
            mask_path.write_bytes(_mask_png(mask, args.width, args.height))
            label.write_text(yolo_label(box, args.width, args.height), encoding="utf-8")
            frames.append(
                {
                    "frame_id": frame_id,
                    "split": split,
                    "width": args.width,
                    "height": args.height,
                    "image_path": image.relative_to(output).as_posix(),
                    "image_sha256": sha256(image),
                    "label_path": label.relative_to(output).as_posix(),
                    "label_sha256": sha256(label),
                    "mask_path": mask_path.relative_to(output).as_posix(),
                    "mask_sha256": sha256(mask_path),
                    "mask_render_path": raw_mask.relative_to(output).as_posix(),
                    "mask_render_sha256": sha256(raw_mask),
                    "world_path": world.relative_path,
                    "world_sha256": dict(world.sources)[world.relative_path],
                    "source_hashes": dict(world.sources),
                    "camera": {**camera, "matrix_world": matrix},
                    "camera_world_pose": {
                        "center": list(camera["location"]),
                        "matrix_world": matrix,
                        "gimbal_angle_rad": list(camera["gimbal_angle_rad"]),
                    },
                    "propeller_state": {
                        "angle_rad": camera["propeller_angle_rad"],
                        "spin_rad_s": camera["propeller_spin_rad_s"],
                        "source": camera["propeller_state_source"],
                        "applied": bool(camera.get("propeller_state_applied", False)),
                    },
                    "gimbal_state_applied": True,
                    "visible_bbox_xyxy_px": box,
                    "visible_target_pixels": sum(value > 0 for value in mask),
                    "target_visible": box is not None,
                    "target_present": camera["target_present"],
                }
            )
            print(
                f"{frame_id}: rendered 3-D RGB + visible mask; "
                f"target_pixels={frames[-1]['visible_target_pixels']}",
                flush=True,
            )
    for world in worlds.values():
        for relative, digest in world.sources:
            if sha256(_source(root, relative)) != digest:
                raise SdfRenderError(
                    f"source changed during render: {relative}; no manifest sealed"
                )
    if sha256(Path(__file__)) != script_sha:
        raise SdfRenderError("renderer source changed; no manifest sealed")
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
        "dataset_yaml_sha256": sha256(yaml_path),
        "frame_count": len(frames),
        "frames": frames,
        "seed": args.seed,
        "split_unit": "source_world",
        "split_worlds": WORLD_PATHS,
        "renderer": {
            "engine": args.engine,
            "blender_version": bpy.app.version_string,
            "script_sha256": script_sha,
            "render_samples": args.render_samples,
            "threads": args.threads,
            "cycles_device": "CPU" if args.engine == "CYCLES" else None,
            "propeller_angle_rad": args.propeller_angle_rad,
            "propeller_spin_rad_s": args.propeller_spin_rad_s,
            "sample_period_s": args.sample_period_s,
            "gimbal_angle_rad": [
                args.gimbal_pitch_rad,
                args.gimbal_yaw_rad,
                args.gimbal_roll_rad,
            ],
            "camera_pose_input": (
                None
                if args.camera_pose_json is None
                else {
                    "path": args.camera_pose_json.resolve().as_posix(),
                    "sha256": sha256(args.camera_pose_json.resolve()),
                }
            ),
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
            "three_static_worlds_one_target_geometry",
            "camera_sampling_uses_known_offline_target_center_with_framing_jitter",
            "every_fifth_frame_target_mesh_removed_for_offline_negative_not_gazebo_capture",
            "no_closed_loop_flight_or_runtime_policy_supervision",
            "no_sensor_noise_or_domain_transfer_acceptance",
            "bbox_and_class_supervision_not_independently_reviewed",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        manifest = render_dataset(args)
    except (SdfRenderError, OSError, ET.ParseError, ImportError) as exc:
        print(f"SDF Blender dataset failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "contract": CONTRACT,
                "frame_count": manifest["frame_count"],
                "training_ready": False,
                "output_dir": str(args.output_dir.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    blender_args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    raise SystemExit(main(blender_args))
