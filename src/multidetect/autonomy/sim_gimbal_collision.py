"""Source-bound x500 gimbal collision expansion and joint-range bounds."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .onboard_executed_trajectory import EvidenceBinding
from .serialization import AutonomyContractError, strict_json_loads
from .sim_static_route_clearance import (
    _IDENTITY,
    _ZERO,
    StaticCollision,
    _bound_bytes,
    _collision,
    _compose,
    _mv,
    _name,
    _one,
    _pose,
    _read_bound_sdf,
    _vector,
)

CONTRACT = "multidetect.px4-x500-gimbal-collision-source-assets-v1"
PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
ASSET_PATHS = {
    "4001_gz_x500": "/opt/px4-gazebo/etc/init.d-posix/airframes/4001_gz_x500",
    "4019_gz_x500_gimbal": "/opt/px4-gazebo/etc/init.d-posix/airframes/4019_gz_x500_gimbal",
    "gimbal_model.sdf": "/opt/px4-gazebo/share/gz/models/gimbal/model.sdf",
    "x500_base_model.sdf": "/opt/px4-gazebo/share/gz/models/x500_base/model.sdf",
    "x500_gimbal_model.sdf": "/opt/px4-gazebo/share/gz/models/x500_gimbal/model.sdf",
    "x500_model.sdf": "/opt/px4-gazebo/share/gz/models/x500/model.sdf",
}
ASSET_SHA256 = {
    "4001_gz_x500": "1db18c52bee06a339d0aae9767cc08fead182cf95c9149ab8cc070458f730351",
    "4019_gz_x500_gimbal": "7bb9d28b8116ace7cdd14cfde6a033ad6dafd7bf150331fe734b87900b38822d",
    "gimbal_model.sdf": "040314986e177e24755822ca0b3be11dbf07c8a391eae39c554d22b461f7c451",
    "x500_base_model.sdf": "e807dca3406f7cd5cb3d545898601c3ec17e0e5242e25cf467a69df1812cf436",
    "x500_gimbal_model.sdf": "72b0e39876073d4f4ccfdc94cd20dbb3d3256969e823725d9512a91a1434f574",
    "x500_model.sdf": "cb76a4afd439f253a8c516b1fcd3528f80347619cfa46bebb620557054fbf125",
}
SOURCE_KEYS = frozenset({"extraction.json", *ASSET_PATHS})
RANGE_SAMPLE_STEP_RAD = math.radians(1.0)
RANGE_OUTWARD_PAD_M = 1e-7
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_BASE_LINKS = {"base_link", *(f"rotor_{i}" for i in range(4))}
_GIMBAL_LINKS = {
    "cgo3_mount_link",
    "cgo3_vertical_arm_link",
    "cgo3_horizontal_arm_link",
    "camera_link",
}


def _require(condition, message):
    if not condition:
        raise AutonomyContractError(message)


def _close(a, b, tolerance=1e-8):
    return len(a) == len(b) and all(
        math.isclose(x, y, abs_tol=tolerance, rel_tol=0) for x, y in zip(a, b, strict=True)
    )


def _rotation(axis, angle):
    x, y, z = axis
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return (
        (t * x * x + c, t * x * y - s * z, t * x * z + s * y),
        (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
        (t * x * z - s * y, t * y * z + s * x, t * z * z + c),
    )


def _axis_pose(joint, angle):
    # The SDF joint frame is child-relative, so motion is T_joint * R * inverse(T_joint).
    inverse = _inverse(joint.pose)
    return _compose(
        joint.pose,
        _compose((_rotation(joint.axis, angle), _ZERO), inverse),
    )


def _inverse(pose):
    rotation, offset = pose
    transpose = tuple(tuple(rotation[j][i] for j in range(3)) for i in range(3))
    inverse_offset = tuple(-value for value in _mv(transpose, offset))
    return transpose, inverse_offset


def _range_values(lower, upper):
    count = max(1, math.ceil((upper - lower) / RANGE_SAMPLE_STEP_RAD))
    step = (upper - lower) / count
    return tuple(lower + step * i for i in range(count + 1)), step


def _number(element, name):
    text = element.text
    value = text.strip(" \t\r\n") if isinstance(text, str) else ""
    _require(
        value.isascii() and _NUMBER.fullmatch(value) is not None,
        f"{name} must be SDF numeric text",
    )
    result = float(value)
    _require(
        math.isfinite(result) and abs(result) <= 1e16,
        f"{name} is outside the supported limit domain",
    )
    return result


@dataclass(frozen=True, slots=True)
class GimbalJoint:
    name: str
    parent: str
    child: str
    pose: tuple
    axis: tuple[float, float, float]
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class GimbalCollisionGeometry:
    source_bindings: tuple[EvidenceBinding, ...]
    model_offset: tuple[float, float, float]
    base_collisions: Mapping[str, StaticCollision]
    gimbal_collision: StaticCollision
    include_pose: tuple
    yaw_joint: GimbalJoint
    roll_joint: GimbalJoint
    pitch_joint: GimbalJoint
    sphere_radius_m: float
    camera_collision_pose: tuple

    @property
    def joint_limits(self):
        return {
            joint.name: {"lower_rad": joint.lower, "upper_rad": joint.upper}
            for joint in (self.yaw_joint, self.roll_joint, self.pitch_joint)
        }

    @property
    def collision_definitions(self):
        return {
            **self.base_collisions,
            "camera_link/cgo3_camera_collision": self.gimbal_collision,
        }

    @property
    def source_sha256(self):
        return self.gimbal_collision.source_sha256

    def _after_yaw(self, roll, pitch):
        roll_joint = _axis_pose(self.roll_joint, roll)
        pitch_joint = _axis_pose(self.pitch_joint, pitch)
        collision_pose = self.camera_collision_pose
        return _compose(roll_joint, _compose(pitch_joint, collision_pose))

    def center_body(self, roll=0.0, pitch=0.0, yaw=0.0):
        after_yaw = self._after_yaw(roll, pitch)
        chain = _compose(
            self.include_pose,
            _compose(_axis_pose(self.yaw_joint, yaw), after_yaw),
        )
        return tuple(chain[1][i] - self.model_offset[i] for i in range(3))

    def _sweep_values(self):
        rolls, roll_step = _range_values(self.roll_joint.lower, self.roll_joint.upper)
        pitches, pitch_step = _range_values(self.pitch_joint.lower, self.pitch_joint.upper)
        cover = (
            self.roll_lever_m * roll_step / 2
            + self.pitch_lever_m * pitch_step / 2
            + RANGE_OUTWARD_PAD_M
        )
        yaw_inverse = _inverse(self.yaw_joint.pose)
        pitch_transforms = tuple(_axis_pose(self.pitch_joint, pitch) for pitch in pitches)
        include_rotation, include_translation = self.include_pose
        for roll in rolls:
            roll_transform = _axis_pose(self.roll_joint, roll)
            for pitch_transform in pitch_transforms:
                downstream = _compose(
                    roll_transform,
                    _compose(pitch_transform, self.camera_collision_pose),
                )
                in_yaw = _compose(yaw_inverse, downstream)[1]
                yaw_center = _compose(
                    self.yaw_joint.pose,
                    (_IDENTITY, (0, 0, in_yaw[2])),
                )[1]
                body_center = _mv(include_rotation, yaw_center)
                body_center = tuple(
                    include_translation[i] + body_center[i] - self.model_offset[i] for i in range(3)
                )
                yaw_radius = math.hypot(*in_yaw[:2])
                radial = math.hypot(*body_center[:2]) + yaw_radius
                height = body_center[2]
                yield radial, height, cover

    @property
    def roll_lever_m(self):
        return math.dist(self.pitch_joint.pose[1], _ZERO) + math.dist(
            self.camera_collision_pose[1], _ZERO
        )

    @property
    def pitch_lever_m(self):
        return math.dist(self.camera_collision_pose[1], _ZERO)

    def swept_bounds(self, maximum_tilt_deg=180.0):
        tilt = math.radians(maximum_tilt_deg)
        rows = tuple(self._sweep_values())
        cover = max(row[2] for row in rows)
        rho = max(row[0] for row in rows)
        z_min = min(row[1] for row in rows)
        z_max = max(row[1] for row in rows)
        center_radius = max(math.hypot(row[0], row[1]) for row in rows)
        top = max(_maximum_up(row[0], row[1], tilt) for row in rows)
        bottom = max(_maximum_up(row[0], -row[1], tilt) for row in rows)
        return {
            "horizontal_radius_m": center_radius + self.sphere_radius_m + cover,
            "bottom_offset_m": -bottom - self.sphere_radius_m - cover,
            "top_offset_m": top + self.sphere_radius_m + cover,
            "level_horizontal_radius_m": rho + self.sphere_radius_m + cover,
            "level_bottom_offset_m": z_min - self.sphere_radius_m - cover,
            "level_top_offset_m": z_max + self.sphere_radius_m + cover,
            "center_horizontal_radius_m": rho + cover,
            "center_min_up_m": z_min - cover,
            "center_max_up_m": z_max + cover,
            "joint_range_cover_pad_m": cover,
            "joint_range_sample_step_deg": math.degrees(RANGE_SAMPLE_STEP_RAD),
        }

    def vertical_range_at_body_rotation(self, rotation):
        up = tuple(rotation[2][i] for i in range(3))
        include_rotation, include_offset = self.include_pose
        local_up = tuple(sum(include_rotation[j][i] * up[j] for j in range(3)) for i in range(3))
        rolls, roll_step = _range_values(self.roll_joint.lower, self.roll_joint.upper)
        pitches, pitch_step = _range_values(self.pitch_joint.lower, self.pitch_joint.upper)
        cover = (
            self.roll_lever_m * roll_step / 2
            + self.pitch_lever_m * pitch_step / 2
            + RANGE_OUTWARD_PAD_M
        )
        horizontal_up = math.hypot(*local_up[:2])
        yaw_inverse = _inverse(self.yaw_joint.pose)
        pitch_transforms = tuple(_axis_pose(self.pitch_joint, pitch) for pitch in pitches)
        include_rotation, include_translation = self.include_pose
        min_z, max_z = math.inf, -math.inf
        for roll in rolls:
            roll_transform = _axis_pose(self.roll_joint, roll)
            for pitch_transform in pitch_transforms:
                downstream = _compose(
                    roll_transform,
                    _compose(pitch_transform, self.camera_collision_pose),
                )
                in_yaw = _compose(yaw_inverse, downstream)[1]
                yaw_center = _compose(
                    self.yaw_joint.pose,
                    (_IDENTITY, (0, 0, in_yaw[2])),
                )[1]
                center_body = _mv(include_rotation, yaw_center)
                center_body = tuple(
                    include_translation[i] + center_body[i] - self.model_offset[i] for i in range(3)
                )
                yaw_radius = math.hypot(*in_yaw[:2])
                projection = sum(up[i] * center_body[i] for i in range(3))
                min_z = min(min_z, projection - horizontal_up * yaw_radius)
                max_z = max(max_z, projection + horizontal_up * yaw_radius)
        return (
            min_z - cover - self.sphere_radius_m,
            max_z + cover + self.sphere_radius_m,
        )

    def bottom_at_body_rotation(self, rotation):
        return self.vertical_range_at_body_rotation(rotation)[0]


def _maximum_up(rho, z, tilt):
    angles = [0.0, tilt]
    stationary = math.atan2(rho, z)
    if stationary <= tilt:
        angles.append(stationary)
    return max(z * math.cos(angle) + rho * math.sin(angle) for angle in angles)


def _base_collisions(root, base_source, identities):
    sdf = _read_bound_sdf(root, base_source, identities)
    _require(
        len(sdf) == 1 and sdf[0].tag == "model" and sdf[0].attrib == {"name": "x500_base"},
        "gimbal base model source differs",
    )
    model = sdf[0]
    _require(
        all(child.tag in {"pose", "static", "self_collide", "link", "joint"} for child in model),
        "base nested includes, frames, plugins or extensions are unsupported",
    )
    model_pose = _pose(model)
    _require(
        model_pose == (_IDENTITY, (0, 0, 0.24)),
        "only the original x500 base installation is supported",
    )
    links = model.findall("link")
    _require(
        len(links) == 5 and {_name(link) for link in links} == _BASE_LINKS,
        "complete base and four rotor links required",
    )
    for link in links:
        expected_count = 5 if _name(link) == "base_link" else 1
        collisions = link.findall("collision")
        _require(len(collisions) == expected_count, "base collision count differs")
    joints = model.findall("joint")
    _require(len(joints) == 4, "exactly four source rotor joints required")
    for index in range(4):
        matches = [joint for joint in joints if joint.attrib.get("name") == f"rotor_{index}_joint"]
        _require(len(matches) == 1, "unique source rotor joint required")
        joint = matches[0]
        axis_element = _one(joint, "axis")
        _require(
            joint.attrib == {"name": f"rotor_{index}_joint", "type": "revolute"}
            and _one(joint, "parent").text == "base_link"
            and _one(joint, "child").text == f"rotor_{index}"
            and joint.find("pose") is None
            and joint.find("axis2") is None
            and _one(axis_element, "xyz").attrib == {},
            "rotor source joint hierarchy differs",
        )
        axis = axis_element
        _require(
            _vector(_one(axis, "xyz").text, 3, "rotor axis") == (0, 0, 1),
            "rotor axis must be body-up",
        )
    result = {}
    for link in links:
        name = _name(link)
        for collision in link.findall("collision"):
            key = f"{name}/{_name(collision)}"
            parsed = _collision(
                collision,
                _compose(model_pose, _pose(link)),
                key,
                base_source.sha256,
            )
            _require(
                parsed.kind == "box"
                and all(0 < value <= 2 for value in parsed.half_extents_local_m),
                "only bounded source collision boxes are supported",
            )
            if name != "base_link":
                _require(
                    _pose(collision) == (_IDENTITY, _ZERO)
                    and _pose(link)[0] == _IDENTITY
                    and _close(tuple(parsed.rotation[i][2] for i in range(3)), (0, 0, 1)),
                    "rotor source collision must be a body-up sweep box",
                )
            result[key] = parsed
    _require(len(result) == 9, "exactly nine source base collisions required")
    return model_pose[1], result


def _joint(model, name, parent, child, axis_expected):
    matches = [joint for joint in model.findall("joint") if joint.attrib.get("name") == name]
    _require(len(matches) == 1, "unique named gimbal joint required")
    joint = matches[0]
    _require(
        joint.attrib == {"name": name, "type": "revolute"}
        and _one(joint, "parent").text == parent
        and _one(joint, "child").text == child,
        "gimbal joint parent/child/type differs",
    )
    pose = _pose(joint)
    axis_element = _one(joint, "axis")
    axis = _vector(_one(axis_element, "xyz").text, 3, "gimbal joint axis")
    _require(_close(axis, axis_expected), "gimbal joint axis differs from source contract")
    limit = _one(axis_element, "limit")
    lower = _number(_one(limit, "lower"), f"{name} lower")
    upper = _number(_one(limit, "upper"), f"{name} upper")
    _require(lower < upper, "gimbal joint limit range is empty")
    return GimbalJoint(name, parent, child, pose, axis, lower, upper), joint


def load_gimbal_collision_geometry(
    root: Path,
    gimbal_sources: Mapping[str, EvidenceBinding],
    identities: dict,
):
    """Validate the pinned seven-binding input and expand its source collisions."""
    _require(
        isinstance(gimbal_sources, Mapping) and set(gimbal_sources) == SOURCE_KEYS,
        "exact gimbal collision source closure required",
    )
    bindings = {}
    manifest_source = gimbal_sources["extraction.json"]
    _require(
        type(manifest_source) is EvidenceBinding and manifest_source.json_pointer == "",
        "whole gimbal extraction manifest required",
    )
    manifest_path = Path(manifest_source.path)
    manifest = strict_json_loads(_bound_bytes(root, manifest_source, 1024 * 1024, identities))
    _require(
        type(manifest) is dict
        and set(manifest) == {"assets", "contract", "image", "image_revision", "schema_version"}
        and manifest.get("contract") == CONTRACT
        and manifest.get("schema_version") == 1
        and manifest.get("image") == PINNED_IMAGE
        and manifest.get("image_revision") == PINNED_IMAGE,
        "pinned original-image gimbal extraction manifest required",
    )
    assets = manifest.get("assets")
    _require(
        type(assets) is dict and set(assets) == set(ASSET_PATHS),
        "complete six-asset gimbal extraction required",
    )
    for key, image_path in ASSET_PATHS.items():
        source = gimbal_sources[key]
        _require(
            type(source) is EvidenceBinding
            and source.json_pointer is None
            and Path(source.path).parent == manifest_path.parent
            and Path(source.path).name == key,
            "gimbal source must be the sibling original extracted asset",
        )
        raw = _bound_bytes(root, source, 4 * 1024 * 1024, identities)
        record = assets[key]
        _require(
            type(record) is dict
            and record == {"image_path": image_path, "sha256": ASSET_SHA256[key]}
            and source.sha256 == ASSET_SHA256[key]
            and len(raw) == source.bytes,
            "gimbal asset differs from the pinned source closure hash",
        )
        bindings[key] = source

    def model_for(key, name):
        sdf = _read_bound_sdf(root, bindings[key], identities)
        _require(
            len(sdf) == 1 and sdf[0].tag == "model" and sdf[0].attrib == {"name": name},
            f"{name} model source identity differs",
        )
        return sdf[0]

    model_for("x500_base_model.sdf", "x500_base")
    model_offset, base_collisions = _base_collisions(
        root,
        bindings["x500_base_model.sdf"],
        identities,
    )
    x500 = model_for("x500_model.sdf", "x500")
    _require(
        x500.find("pose") is None
        and len(x500.findall("include")) == 1
        and _one(_one(x500, "include"), "uri").text == "model://x500_base"
        and _one(x500, "include").attrib == {"merge": "true"}
        and not x500.findall("collision")
        and not x500.findall("model"),
        "gimbal x500 expansion must merge exactly its source x500_base",
    )
    composite = model_for("x500_gimbal_model.sdf", "x500_gimbal")
    includes = composite.findall("include")
    _require(
        len(includes) == 2 and not composite.findall("pose"),
        "gimbal composite requires exactly two source includes",
    )
    include_map = {_one(item, "uri").text: item for item in includes}
    _require(
        set(include_map) == {"model://x500", "model://gimbal"},
        "gimbal composite includes differ",
    )
    _require(
        include_map["model://x500"].attrib == {"merge": "true"}
        and len(include_map["model://x500"]) == 1,
        "x500 gimbal base merge differs",
    )
    mount_include = include_map["model://gimbal"]
    _require(
        mount_include.attrib == {"merge": "true"} and len(mount_include) == 2,
        "gimbal merge pose/include differs",
    )
    include_pose_element = _one(mount_include, "pose")
    _require(not include_pose_element.attrib, "gimbal merge relative frame is unsupported")
    include_pose = _pose(mount_include)
    _require(
        _close(
            _vector(include_pose_element.text, 6, "gimbal model include pose"),
            (0, 0, 0.26, 0, 0, 3.14),
        ),
        "gimbal model include pose differs",
    )
    attach = [
        joint
        for joint in composite.findall("joint")
        if joint.attrib.get("name") == "GimbalAttachJoint"
    ]
    _require(
        len(attach) == 1
        and attach[0].attrib == {"name": "GimbalAttachJoint", "type": "fixed"}
        and _one(attach[0], "parent").text == "base_link"
        and _one(attach[0], "child").text == "cgo3_mount_link"
        and attach[0].find("pose") is None
        and len(composite.findall("joint")) == 1,
        "fixed gimbal attachment source joint differs",
    )

    gimbal = model_for("gimbal_model.sdf", "gimbal")
    links = gimbal.findall("link")
    _require(
        len(links) == 4 and {_name(link) for link in links} == _GIMBAL_LINKS,
        "gimbal link closure differs",
    )
    _require(
        all(link.find("pose") is None for link in links),
        "gimbal source link frames must retain their implicit model-frame poses",
    )
    collisions = [item for link in links for item in link.findall("collision")]
    _require(
        len(collisions) == 1
        and _name(next(link for link in links if _name(link) == "camera_link")) == "camera_link"
        and len(next(link for link in links if _name(link) == "camera_link").findall("collision"))
        == 1,
        "only the source camera sphere may be a gimbal collision",
    )
    yaw, yaw_element = _joint(
        gimbal,
        "cgo3_vertical_arm_joint",
        "cgo3_mount_link",
        "cgo3_vertical_arm_link",
        (0, 0, -1),
    )
    roll, _ = _joint(
        gimbal,
        "cgo3_horizontal_arm_joint",
        "cgo3_vertical_arm_link",
        "cgo3_horizontal_arm_link",
        (-1, 0, 0),
    )
    pitch, _ = _joint(
        gimbal,
        "cgo3_camera_joint",
        "cgo3_horizontal_arm_link",
        "camera_link",
        (0, 1, 0),
    )
    _require(
        len(gimbal.findall("joint")) == 3
        and yaw.lower == -1e16
        and yaw.upper == 1e16
        and yaw.upper - yaw.lower >= 2 * math.pi,
        "gimbal yaw joint must cover a complete revolution",
    )
    _require(
        _close((roll.lower, roll.upper), (-0.785398, 0.785398))
        and _close((pitch.lower, pitch.upper), (-2.35619, 0.7854)),
        "gimbal roll/pitch limits differ from original SDF",
    )
    yaw_pose = _pose(yaw_element)
    _require(
        _close(yaw_pose[1], (-0.02552, 0, 0))
        and yaw_pose[0] == _IDENTITY
        and _close(roll.pose[1], (0, 0, -0.1619))
        and _close(pitch.pose[1], (-0.0412, 0, -0.162)),
        "source gimbal joint pivot poses differ",
    )
    camera_link = next(link for link in links if _name(link) == "camera_link")
    camera_collision = _one(camera_link, "collision")
    _require(
        _name(camera_collision) == "cgo3_camera_collision",
        "source camera collision name differs",
    )
    collision_pose_element = _one(camera_collision, "pose")
    _require(not collision_pose_element.attrib, "camera collision relative frame unsupported")
    collision_pose = _pose(camera_collision)
    _require(
        _close(collision_pose[1], (-0.0412, 0, -0.162)),
        "camera collision offset differs",
    )
    geometry = _one(camera_collision, "geometry")
    sphere = _one(geometry, "sphere")
    radius = _vector(_one(sphere, "radius").text, 1, "camera sphere radius")[0]
    _require(
        sphere.attrib == {} and _close((radius,), (0.035,)),
        "source gimbal collision sphere differs",
    )
    center_zero = _compose(
        include_pose,
        _compose(
            _axis_pose(yaw, 0.0),
            _compose(
                _axis_pose(roll, 0.0),
                _compose(_axis_pose(pitch, 0.0), collision_pose),
            ),
        ),
    )
    gimbal_collision = StaticCollision(
        "camera_link/cgo3_camera_collision",
        "sphere",
        center_zero[1],
        center_zero[0],
        (radius, radius, radius),
        tuple(center_zero[1][i] - radius for i in range(3)),
        tuple(center_zero[1][i] + radius for i in range(3)),
        bindings["gimbal_model.sdf"].sha256,
    )
    expanded = GimbalCollisionGeometry(
        source_bindings=tuple(gimbal_sources[key] for key in sorted(SOURCE_KEYS)),
        model_offset=model_offset,
        base_collisions=base_collisions,
        gimbal_collision=gimbal_collision,
        include_pose=include_pose,
        yaw_joint=yaw,
        roll_joint=roll,
        pitch_joint=pitch,
        sphere_radius_m=radius,
        camera_collision_pose=collision_pose,
    )
    return expanded


__all__ = ["GimbalCollisionGeometry", "SOURCE_KEYS", "load_gimbal_collision_geometry"]
