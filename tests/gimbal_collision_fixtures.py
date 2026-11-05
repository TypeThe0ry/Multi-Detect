"""CPU fixtures built from the retained pinned x500 gimbal source closure."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.sim_gimbal_collision import SOURCE_KEYS, load_gimbal_collision_geometry
from multidetect.autonomy.sim_static_route_clearance import _one, _pose
from multidetect.autonomy.sim_teacher_environment import load_static_teacher_environment

_SOURCE = (
    Path(__file__).resolve().parents[1] / "simulation/gazebo/source-assets/x500_gimbal_collision"
)


def _binding(root, path, pointer=None):
    raw = (root / path).read_bytes()
    return EvidenceBinding(
        path,
        len(raw),
        hashlib.sha256(raw).hexdigest(),
        "CPU-gimbal-fixture",
        pointer,
    )


def gimbal_sources(root):
    directory = Path("source-assets/x500_gimbal_collision")
    target = root / directory
    target.mkdir(parents=True, exist_ok=True)
    for name in SOURCE_KEYS:
        shutil.copyfile(_SOURCE / name, target / name)
    return {
        name: _binding(
            root,
            (directory / name).as_posix(),
            "" if name == "extraction.json" else None,
        )
        for name in SOURCE_KEYS
    }


def _link_pose_element(name, pose):
    link = ET.Element("link", name=name)
    rotation = pose[0]
    pitch = math.asin(max(-1.0, min(1.0, -rotation[2][0])))
    if abs(math.cos(pitch)) > 1e-10:
        roll = math.atan2(rotation[2][1], rotation[2][2])
        yaw = math.atan2(rotation[1][0], rotation[0][0])
    else:
        roll = 0.0
        yaw = math.atan2(-rotation[0][1], rotation[1][1])
    values = " ".join(format(value, ".17g") for value in (*pose[1], roll, pitch, yaw))
    ET.SubElement(link, "pose").text = values
    return link


def _add_geography(world):
    spherical = ET.SubElement(world, "spherical_coordinates")
    ET.SubElement(spherical, "surface_model").text = "EARTH_WGS84"
    ET.SubElement(spherical, "world_frame_orientation").text = "ENU"
    ET.SubElement(spherical, "latitude_deg").text = "1.3521"
    ET.SubElement(spherical, "longitude_deg").text = "103.8198"
    ET.SubElement(spherical, "elevation").text = "18"


def _add_ground(world):
    ground = ET.SubElement(world, "model", name="ground")
    ET.SubElement(ground, "static").text = "true"
    link = ET.SubElement(ground, "link", name="base")
    collision = ET.SubElement(link, "collision", name="ground")
    geometry = ET.SubElement(collision, "geometry")
    plane = ET.SubElement(geometry, "plane")
    ET.SubElement(plane, "normal").text = "0 0 1"
    ET.SubElement(plane, "size").text = "100 100"


def gimbal_reply(root, sources, *, model_pose="0 0 .003 0 0 0", static_world=True):
    identities = {}
    load_gimbal_collision_geometry(root, sources, identities)
    source_dir = root / Path(sources["gimbal_model.sdf"].path).parent
    base_sdf = ET.fromstring((source_dir / "x500_base_model.sdf").read_bytes())
    base = base_sdf.find("model")
    vehicle = ET.Element("model", name="x500_gimbal_0")
    ET.SubElement(vehicle, "pose").text = model_pose
    for source_link in base.findall("link"):
        link = ET.fromstring(ET.tostring(source_link))
        pose = link.find("pose")
        if pose is None:
            pose = ET.SubElement(link, "pose")
            pose.text = "0 0 .24 0 0 0"
        else:
            xyzrpy = [float(item) for item in pose.text.split()]
            xyzrpy[2] += 0.24
            pose.text = " ".join(format(value, ".17g") for value in xyzrpy)
        vehicle.append(link)

    gimbal_sdf = ET.fromstring((source_dir / "gimbal_model.sdf").read_bytes())
    gimbal_model = gimbal_sdf.find("model")
    composite_sdf = ET.fromstring((source_dir / "x500_gimbal_model.sdf").read_bytes())
    composite = composite_sdf.find("model")
    include = next(
        item for item in composite.findall("include") if _one(item, "uri").text == "model://gimbal"
    )
    mount_pose = _pose(include)
    for name, pose in (
        ("cgo3_mount_link", mount_pose),
        ("cgo3_vertical_arm_link", mount_pose),
        ("cgo3_horizontal_arm_link", mount_pose),
        ("camera_link", mount_pose),
    ):
        source_link = next(
            link for link in gimbal_model.findall("link") if link.attrib.get("name") == name
        )
        link = _link_pose_element(name, pose)
        if name == "camera_link":
            collision = ET.fromstring(ET.tostring(_one(source_link, "collision")))
            link.append(collision)
        vehicle.append(link)

    world = ET.Element("world", name="default")
    _add_geography(world)
    physics = ET.SubElement(world, "physics", name="px4_ode", type="ode")
    ET.SubElement(physics, "max_step_size").text = ".004"
    pad = ET.SubElement(world, "model", name="multirotor_home_pad")
    if static_world:
        ET.SubElement(pad, "static").text = "true"
    ET.SubElement(pad, "pose").text = "0 0 .04 0 0 0"
    pad_link = ET.SubElement(pad, "link", name="pad_link")
    pad_collision = ET.SubElement(pad_link, "collision", name="pad_collision")
    pad_geometry = ET.SubElement(pad_collision, "geometry")
    cylinder = ET.SubElement(pad_geometry, "cylinder")
    ET.SubElement(cylinder, "radius").text = "5"
    ET.SubElement(cylinder, "length").text = ".08"
    _add_ground(world)
    world.append(vehicle)
    sdf = ET.Element("sdf", version="1.10")
    sdf.append(world)
    response = b"data: " + json.dumps(ET.tostring(sdf, encoding="unicode")).encode("utf-8")
    (root / "gimbal-reply.bin").write_bytes(response)
    return _binding(root, "gimbal-reply.bin")


def gimbal_contact_environment(root, sources, *, model_pose="0 0 .003 0 0 0"):
    vehicle_source = gimbal_reply(root, sources, model_pose=model_pose)
    world = ET.Element("world", name="default")
    _add_geography(world)
    physics = ET.SubElement(world, "physics", name="px4_ode", type="ode")
    ET.SubElement(physics, "max_step_size").text = ".004"
    pad = ET.SubElement(world, "model", name="multirotor_home_pad")
    ET.SubElement(pad, "static").text = "true"
    ET.SubElement(pad, "pose").text = "0 0 .04 0 0 0"
    pad_link = ET.SubElement(pad, "link", name="pad_link")
    pad_collision = ET.SubElement(pad_link, "collision", name="pad_collision")
    geometry = ET.SubElement(pad_collision, "geometry")
    cylinder = ET.SubElement(geometry, "cylinder")
    ET.SubElement(cylinder, "radius").text = "5"
    ET.SubElement(cylinder, "length").text = ".08"
    _add_ground(world)
    declared = ET.Element("sdf", version="1.9")
    declared.append(world)
    (root / "gimbal-world.sdf").write_bytes(ET.tostring(declared))
    world_source = _binding(root, "gimbal-world.sdf")
    loaded = ET.fromstring(ET.tostring(declared))
    loaded_world = loaded.find("world")
    mono = ET.SubElement(loaded_world, "model", name="x500_mono_cam_0")
    ET.SubElement(mono, "static").text = "false"
    ET.SubElement(mono, "link", name="base_link")
    static_response = b"data: " + json.dumps(ET.tostring(loaded, encoding="unicode")).encode(
        "utf-8"
    )
    static_reply_path = root / "static-reply.bin"
    static_reply_path.write_bytes(static_response)
    generated_source = _binding(root, "static-reply.bin")
    environment = load_static_teacher_environment(
        artifact_root=root,
        world_source=world_source,
        generated_sdf_source=generated_source,
        include_model_sources={},
    )
    return environment, vehicle_source
