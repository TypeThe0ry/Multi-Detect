#!/usr/bin/env python3
"""按设计报告重建三角翼占位模型；有 visual.dae/stl 时自动使用真实外观。"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.delta_tailsitter import (  # noqa: E402
    ASSEMBLY_INERTIA_KGM2,
    ASSEMBLY_MASS_KG,
    CAMERA_POSITION_M,
    cad_to_base_m,
    select_visual_mesh,
)

# 与 4018 相同：0/1 对角 CCW、2/3 对角 CW。元组为 CAD (x,z)。
ROTORS = ((-600, -150), (600, 150), (600, -150), (-600, 150))
CAMERA_POSE = "0.046 0 0.388 0 -0.3490658503988659 0"
# Gazebo/DART becomes poorly conditioned when camera articulation links use the
# former 1e-12 kg placeholders.  These are still conservative allocation
# masses (the C11 shell itself is already included in ASSEMBLY_MASS_KG), but
# have physical scale and safe inertias for the articulated solver.
GIMBAL_MOUNT_MASS = 0.005
GIMBAL_YAW_MASS = 0.002
GIMBAL_ROLL_MASS = 0.002
CAMERA_MASS = 0.010
DEPTH_CAMERA_MASS = 0.001
SEGMENTATION_CAMERA_MASS = 0.001
GIMBAL_MASS = GIMBAL_MOUNT_MASS + GIMBAL_YAW_MASS + GIMBAL_ROLL_MASS + CAMERA_MASS + DEPTH_CAMERA_MASS
GIMBAL_CHILD_INERTIA = (1e-8, 1e-8, 1e-8)
CAMERA_INERTIA = (2.0e-5, 2.0e-5, 2.0e-5)
PROP_MASS = 0.018
PROP_RADIUS = 0.127
PROP_INERTIA = (PROP_MASS * PROP_RADIUS**2 / 4,) * 2 + (PROP_MASS * PROP_RADIUS**2 / 2,)


def _element(parent: ET.Element, tag: str, text: object | None = None, **attrs) -> ET.Element:
    result = ET.SubElement(parent, tag, **attrs)
    if text is not None:
        result.text = str(text)
    return result



def _odometry_publisher(model: ET.Element) -> None:
    """采集器的帧服务要 /model/<name>/odometry_with_covariance，每个相机包装都必须带。"""
    plugin = _element(model, "plugin", filename="gz-sim-odometry-publisher-system", name="gz::sim::systems::OdometryPublisher")
    _element(plugin, "dimensions", 3)
    _element(plugin, "odom_publish_frequency", 50)


def _gimbal_joint_state_publisher(model: ET.Element) -> None:
    plugin = _element(
        model,
        "plugin",
        filename="gz-sim-joint-state-publisher-system",
        name="gz::sim::systems::JointStatePublisher",
    )
    _element(plugin, "update_rate", 100)
    for name in ("c11_yaw_joint", "c11_roll_joint", "c11_pitch_joint"):
        _element(plugin, "joint_name", name)

def _vector(values) -> str:
    return " ".join(f"{value:.12g}" for value in values)


def _inertial(link, mass, diagonal, *, position=(0, 0, 0), products=(0, 0, 0)):
    inertial = _element(link, "inertial")
    _element(inertial, "pose", _vector((*position, 0, 0, 0)))
    _element(inertial, "mass", f"{mass:.12g}")
    inertia = _element(inertial, "inertia")
    for name, value in zip(
        ("ixx", "iyy", "izz", "ixy", "ixz", "iyz"), (*diagonal, *products), strict=True
    ):
        _element(inertia, name, f"{value:.12g}")


def _visual(link, name, position, shape, dimensions, color="0.3 0.35 0.4 1"):
    visual = _element(link, "visual", name=name)
    _element(visual, "pose", _vector((*position, 0, 0, 0)))
    geometry = _element(_element(visual, "geometry"), shape)
    for key, value in dimensions.items():
        _element(geometry, key, value)
    material = _element(visual, "material")
    _element(material, "ambient", color)
    _element(material, "diffuse", color)
    return visual


def _mesh_visual(link, name, uri, *, color="0.3 0.35 0.4 1", scale="1 1 1"):
    visual = _element(link, "visual", name=name)
    geometry = _element(_element(visual, "geometry"), "mesh")
    _element(geometry, "uri", uri)
    _element(geometry, "scale", scale)
    material = _element(visual, "material")
    _element(material, "ambient", color)
    _element(material, "diffuse", color)
    return visual


def _gimbal_controller(model, joint_name, topic, *, p_gain=0.8, cmd_max=0.3):
    plugin = _element(
        model,
        "plugin",
        name="gz::sim::systems::JointPositionController",
        filename="gz-sim-joint-position-controller-system",
    )
    for key, value in (
        ("joint_name", joint_name),
        ("sub_topic", topic),
        ("p_gain", p_gain),
        ("i_gain", 0.01245),
        ("d_gain", 0.015),
        ("i_max", 0),
        ("i_min", 0),
        ("cmd_max", cmd_max),
        ("cmd_min", -cmd_max),
    ):
        _element(plugin, key, value)


def _revolute_joint(model, name, parent, child, axis, *, pose="0 0 0 0 0 0", lower=-2 * math.pi, upper=2 * math.pi, initial=0.0):
    joint = _element(model, "joint", name=name, type="revolute")
    _element(joint, "parent", parent)
    _element(joint, "child", child)
    _element(joint, "pose", pose)
    axis_element = _element(joint, "axis")
    _element(axis_element, "xyz", axis)
    _element(axis_element, "initial_position", initial)
    limit = _element(axis_element, "limit")
    _element(limit, "lower", lower)
    _element(limit, "upper", upper)
    _element(limit, "effort", 100)
    _element(limit, "velocity", -1)
    dynamics = _element(axis_element, "dynamics")
    _element(dynamics, "damping", 0.1)
    return joint


def _gimbal(model, meshes: Path):
    """Add C11-mounted yaw/roll/pitch gimbal and RGB/depth camera."""
    mount = _element(model, "link", name="c11_mount_link")
    _element(mount, "pose", _vector((*CAMERA_POSITION_M, 0, 0, 0)), relative_to="base_link")
    # The C11 shell visual is already part of the assembly budget; these links
    # carry explicit solver-safe articulation masses.
    _inertial(mount, GIMBAL_MOUNT_MASS, (1e-6, 1e-6, 1e-6))
    fixed = _element(model, "joint", name="c11_mount_joint", type="fixed")
    _element(fixed, "parent", "base_link")
    _element(fixed, "child", "c11_mount_link")
    if (meshes / "c11.stl").is_file():
        _mesh_visual(mount, "c11_step_visual", "model://delta_tailsitter/meshes/c11.stl")
    else:
        _visual(mount, "c11_bbox_visual", (0, 0, 0), "box", {"size": "0.084 0.062 0.064"})

    yaw_link = _element(model, "link", name="c11_yaw_link")
    _element(yaw_link, "pose", _vector((*CAMERA_POSITION_M, 0, 0, 0)), relative_to="base_link")
    _inertial(yaw_link, GIMBAL_YAW_MASS, GIMBAL_CHILD_INERTIA)
    _visual(yaw_link, "yaw_arm", (0, 0, -0.045), "box", {"size": "0.02 0.02 0.09"}, "0.12 0.12 0.12 1")
    # PX4 gz-gimbal uses the upstream yaw/roll axis signs.  The command topics
    # carry positive MAVLink angles; keeping these signs aligned avoids a
    # fixed 180-degree mirror in the reported device attitude.
    _revolute_joint(model, "c11_yaw_joint", "c11_mount_link", "c11_yaw_link", "0 0 -1", lower=-2 * math.pi, upper=2 * math.pi)

    roll_link = _element(model, "link", name="c11_roll_link")
    _element(roll_link, "pose", _vector((*CAMERA_POSITION_M, 0, 0, 0)), relative_to="base_link")
    _inertial(roll_link, GIMBAL_ROLL_MASS, GIMBAL_CHILD_INERTIA)
    _visual(roll_link, "roll_arm", (0, 0, -0.08), "box", {"size": "0.08 0.02 0.02"}, "0.12 0.12 0.12 1")
    _revolute_joint(model, "c11_roll_joint", "c11_yaw_link", "c11_roll_link", "-1 0 0", pose="0 0 -0.08 0 0 0", lower=-0.785398, upper=0.785398)

    camera = _element(model, "link", name="camera_link")
    _inertial(
        camera,
        CAMERA_MASS,
        CAMERA_INERTIA,
        position=(0, 0, 0),
    )
    _visual(camera, "camera_body", (0.11, 0, -0.08), "box", {"size": "0.08 0.06 0.05"}, "0.08 0.08 0.08 1")
    _revolute_joint(model, "c11_pitch_joint", "c11_roll_link", "camera_link", "0 1 0", pose="-0.04 0 -0.08 0 0 0", lower=-2.35619, upper=0.7854)
    # The optical centre clears both the C11 shell and the camera-body visual.
    camera_pose = "0.16 0 -0.08 0 0 0"
    sensor = _element(camera, "sensor", name="camera", type="camera")
    _element(sensor, "gz_frame_id", "camera_link")
    _element(sensor, "pose", camera_pose)
    _element(sensor, "always_on", 1)
    _element(sensor, "update_rate", 30)
    _element(sensor, "visualize", True)
    camera_config = _element(sensor, "camera")
    _element(camera_config, "horizontal_fov", 1.74)
    image = _element(camera_config, "image")
    _element(image, "format", "R8G8B8")
    _element(image, "width", 1280)
    _element(image, "height", 960)
    clip = _element(camera_config, "clip")
    _element(clip, "near", 0.05)
    _element(clip, "far", 15000)

    depth = _element(model, "link", name="depth_camera_link")
    _inertial(depth, DEPTH_CAMERA_MASS, GIMBAL_CHILD_INERTIA)
    _element(depth, "pose", "0 0 0 0 0 0", relative_to="camera_link")
    sensor = _element(depth, "sensor", name="depth_camera", type="depth_camera")
    _element(sensor, "gz_frame_id", "depth_camera_link")
    _element(sensor, "pose", "0.16 0 -0.08 0 0 0")
    _element(sensor, "always_on", 1)
    _element(sensor, "update_rate", 30)
    _element(sensor, "visualize", True)
    camera_config = _element(sensor, "camera")
    _element(camera_config, "horizontal_fov", 1.74)
    image = _element(camera_config, "image")
    _element(image, "format", "R_FLOAT32")
    _element(image, "width", 1280)
    _element(image, "height", 960)
    clip = _element(camera_config, "clip")
    _element(clip, "near", 0.05)
    _element(clip, "far", 15000)
    depth_joint = _element(model, "joint", name="DepthCameraJoint", type="fixed")
    _element(depth_joint, "parent", "camera_link")
    _element(depth_joint, "child", "depth_camera_link")
    _element(depth_joint, "pose", "0 0 0 0 0 0", relative_to="camera_link")
    _gimbal_controller(model, "c11_roll_joint", "command/gimbal_roll")
    _gimbal_controller(model, "c11_pitch_joint", "command/gimbal_pitch")
    _gimbal_controller(model, "c11_yaw_joint", "command/gimbal_yaw", p_gain=0.3)


def _write_camera_models(model_dir: Path, meshes: Path) -> None:
    """Write fixed and articulated sensor wrappers around the dynamics model."""
    base_path = model_dir / "model.sdf"
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.parse(base_path, parser=parser).getroot()
    model = root.find("model")
    assert model is not None

    # The C11 housing is a visual part of the airframe, not a camera mount.
    base = model.find("link[@name='base_link']")
    assert base is not None
    shell = _element(base, "visual", name="c11_shell")
    _element(shell, "pose", _vector((*CAMERA_POSITION_M, 0, 0, 0)))
    geometry = _element(_element(shell, "geometry"), "box")
    _element(geometry, "size", "0.084 0.062 0.064")
    material = _element(shell, "material")
    _element(material, "ambient", "0.3 0.35 0.4 1")
    _element(material, "diffuse", "0.3 0.35 0.4 1")

    camera_names = {"c11_mount_link", "c11_yaw_link", "c11_roll_link", "camera_link", "depth_camera_link"}
    gimbal_joints = {"c11_mount_joint", "c11_yaw_joint", "c11_roll_joint", "c11_pitch_joint", "DepthCameraJoint"}
    gimbal_plugins = {"gz::sim::systems::JointPositionController"}
    camera_links = [item for item in model.findall("link") if item.get("name") in camera_names]
    camera_joints = [item for item in model.findall("joint") if item.get("name") in gimbal_joints]
    camera_plugins = [item for item in model.findall("plugin") if item.get("name") in gimbal_plugins]

    wrapper = ET.Element("sdf", version="1.9")
    wrapped_model = _element(wrapper, "model", name="delta_tailsitter_gimbal_cam")
    include = _element(wrapped_model, "include", merge="true")
    _element(include, "uri", "model://delta_tailsitter")
    mount = _element(wrapped_model, "link", name="c11_mount_link")
    _element(mount, "pose", _vector((*CAMERA_POSITION_M, 0, 0, 0)), relative_to="base_link")
    _inertial(mount, GIMBAL_MOUNT_MASS, (1e-6, 1e-6, 1e-6))
    fixed = _element(wrapped_model, "joint", name="c11_mount_joint", type="fixed")
    _element(fixed, "parent", "base_link")
    _element(fixed, "child", "c11_mount_link")
    for item in camera_links:
        if item.get("name") != "c11_mount_link":
            wrapped_model.append(item)
    for item in camera_joints:
        if item.get("name") != "c11_mount_joint":
            wrapped_model.append(item)
    for item in camera_plugins:
        wrapped_model.append(item)
    # PX4 的 gz 云台桥用 camera_link 上的 camera_imu 估计云台姿态并上报
    # GIMBAL_DEVICE_ATTITUDE_STATUS；与上游 gimbal 模型一致，位姿与相机传感器相同。
    camera_link = wrapped_model.find("link[@name='camera_link']")
    camera_sensor = camera_link.find("sensor[@name='camera']")
    imu = _element(camera_link, "sensor", name="camera_imu", type="imu")
    _element(imu, "gz_frame_id", "camera_link")
    _element(imu, "always_on", 1)
    _element(imu, "update_rate", 250)
    # The IMU is already attached to camera_link.  Reusing the optical sensor
    # pose here translated it a second time and made the PX4 gimbal bridge
    # report a frame that was not the articulated camera frame.
    _element(imu, "pose", "0 0 0 0 0 3.141592653589793")
    _odometry_publisher(wrapped_model)
    _gimbal_joint_state_publisher(wrapped_model)
    ET.indent(wrapper, space="  ")
    gimbal_dir = model_dir.parent / "delta_tailsitter_gimbal_cam"
    gimbal_dir.mkdir(parents=True, exist_ok=True)
    (gimbal_dir / "model.sdf").write_text(ET.tostring(wrapper, encoding="unicode") + "\n", encoding="utf-8")
    (gimbal_dir / "model.config").write_text(
        '<?xml version="1.0"?>\n<model><name>delta_tailsitter_gimbal_cam</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><author><name>Multi-Detect</name></author><description>折叠尾座三角翼，C11 三轴云台 RGB/深度相机。</description></model>\n',
        encoding="utf-8",
    )

    # Remove camera articulation and sensors from the dynamics-only model.
    for item in camera_links + camera_joints + camera_plugins:
        model.remove(item)
    _element(base, "visual", name="c11_shell_mesh")
    mesh_visual = base.find("visual[@name='c11_shell_mesh']")
    assert mesh_visual is not None
    _element(mesh_visual, "pose", _vector((*CAMERA_POSITION_M, 0, 0, 0)))
    mesh_geo = _element(_element(mesh_visual, "geometry"), "mesh")
    _element(mesh_geo, "uri", "model://delta_tailsitter/meshes/c11.stl")
    for visual in list(base.findall("visual[@name='c11_shell']")):
        base.remove(visual)
    ET.indent(root, space="  ")
    base_path.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")

    fixed_root = ET.Element("sdf", version="1.9")
    fixed_model = _element(fixed_root, "model", name="delta_tailsitter_mono_cam")
    include = _element(fixed_model, "include", merge="true")
    _element(include, "uri", "model://delta_tailsitter")
    camera = _element(fixed_model, "link", name="camera_link")
    _element(camera, "pose", _vector((*CAMERA_POSITION_M, 0, -0.3490658503988659, 0)), relative_to="base_link")
    # The fixed wrapper carries the complete articulated payload allocation in
    # one rigid link; this keeps the shared base model's mass/CG budget exact.
    # Match the shared assembly inertia after replacing the old numeric
    # placeholders with the wrapper's rigid payload link.
    _inertial(camera, GIMBAL_MASS - DEPTH_CAMERA_MASS, (2.102e-5, 2.102e-5, 2.102e-5))
    sensor = _element(camera, "sensor", name="camera", type="camera")
    _element(sensor, "gz_frame_id", "camera_link")
    _element(sensor, "pose", "0.09 0 0 0 0 0")
    _element(sensor, "always_on", 1)
    _element(sensor, "update_rate", 30)
    _element(sensor, "visualize", True)
    config = _element(sensor, "camera")
    _element(config, "horizontal_fov", 1.74)
    image = _element(config, "image")
    _element(image, "format", "R8G8B8")
    _element(image, "width", 1280)
    _element(image, "height", 960)
    clip = _element(config, "clip")
    _element(clip, "near", 0.05)
    _element(clip, "far", 15000)
    # A pose relative to base_link does not make the wrapper link part of the
    # model's rigid-body graph.  Keep the fixed camera payload explicitly
    # attached; without this joint Gazebo may construct a disconnected link
    # and the fixed-camera variant can terminate with SIGSEGV during startup.
    camera_joint = _element(fixed_model, "joint", name="CameraMountJoint", type="fixed")
    _element(camera_joint, "parent", "base_link")
    _element(camera_joint, "child", "camera_link")
    _element(camera_joint, "pose", "0 0 0 0 0 0", relative_to="base_link")
    depth = _element(fixed_model, "link", name="depth_camera_link")
    _element(depth, "pose", "0 0 0 0 0 0", relative_to="camera_link")
    _inertial(depth, DEPTH_CAMERA_MASS, GIMBAL_CHILD_INERTIA)
    sensor = _element(depth, "sensor", name="depth_camera", type="depth_camera")
    _element(sensor, "gz_frame_id", "depth_camera_link")
    _element(sensor, "pose", "0.09 0 0 0 0 0")
    _element(sensor, "always_on", 1)
    _element(sensor, "update_rate", 30)
    _element(sensor, "visualize", True)
    config = _element(sensor, "camera")
    _element(config, "horizontal_fov", 1.74)
    image = _element(config, "image")
    _element(image, "format", "R_FLOAT32")
    _element(image, "width", 1280)
    _element(image, "height", 960)
    clip = _element(config, "clip")
    _element(clip, "near", 0.05)
    _element(clip, "far", 15000)
    joint = _element(fixed_model, "joint", name="DepthCameraJoint", type="fixed")
    _element(joint, "parent", "camera_link")
    _element(joint, "child", "depth_camera_link")
    _element(joint, "pose", "0 0 0 0 0 0", relative_to="camera_link")
    _odometry_publisher(fixed_model)
    ET.indent(fixed_root, space="  ")
    fixed_dir = model_dir.parent / "delta_tailsitter_mono_cam"
    fixed_dir.mkdir(parents=True, exist_ok=True)
    (fixed_dir / "model.sdf").write_text(ET.tostring(fixed_root, encoding="unicode") + "\n", encoding="utf-8")
    (fixed_dir / "model.config").write_text(
        '<?xml version="1.0"?>\n<model><name>delta_tailsitter_mono_cam</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><author><name>Multi-Detect</name></author><description>折叠尾座三角翼，C11 固定 RGB/深度相机。</description></model>\n',
        encoding="utf-8",
    )

    def write_segmentation_overlay(wrapper_dir: Path, model_name: str) -> None:
        overlay_root = ET.parse(wrapper_dir / "model.sdf").getroot()
        overlay_model = overlay_root.find("model")
        assert overlay_model is not None
        seg = _element(overlay_model, "link", name="segmentation_camera_link")
        _element(seg, "pose", "0 0 0 0 0 0", relative_to="camera_link")
        _inertial(seg, SEGMENTATION_CAMERA_MASS, GIMBAL_CHILD_INERTIA)
        sensor = _element(seg, "sensor", name="segmentation_camera", type="segmentation")
        _element(sensor, "gz_frame_id", "segmentation_camera_link")
        _element(sensor, "topic", "/multidetect/teacher/segmentation")
        _element(sensor, "pose", "0 0 0 0 0 0")
        config = _element(sensor, "camera")
        _element(config, "segmentation_type", "instance")
        _element(config, "horizontal_fov", 1.74)
        image = _element(config, "image")
        _element(image, "width", 1280)
        _element(image, "height", 960)
        _element(image, "format", "R8G8B8")
        clip = _element(config, "clip")
        _element(clip, "near", 0.05)
        _element(clip, "far", 15000)
        _element(sensor, "always_on", 1)
        _element(sensor, "update_rate", 30)
        _element(sensor, "visualize", False)
        joint = _element(overlay_model, "joint", name="SegmentationCameraJoint", type="fixed")
        _element(joint, "parent", "camera_link")
        _element(joint, "child", "segmentation_camera_link")
        _element(joint, "pose", "0 0 0 0 0 0", relative_to="camera_link")
        # 里程计插件随包装模型一起复制过来，这里不再重复添加。
        overlay_dir = model_dir.parent / ".." / "model-overlays" / f"{model_name}_seg"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        ET.indent(overlay_root, space="  ")
        (overlay_dir / "model.sdf").write_text(ET.tostring(overlay_root, encoding="unicode") + "\n", encoding="utf-8")
        (overlay_dir / "model.config").write_text(
            f'<?xml version="1.0"?>\n<model><name>{model_name}</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><author><name>Multi-Detect</name></author><description>分割相机覆盖。</description></model>\n',
            encoding="utf-8",
        )

    write_segmentation_overlay(fixed_dir, "delta_tailsitter_mono_cam")
    write_segmentation_overlay(gimbal_dir, "delta_tailsitter_gimbal_cam")


def _wing_mesh(path: Path, polygon: list[tuple[float, float, float]], thickness_mm=12.0):
    """凸翼面挤出为闭合二进制 STL；顶点已变换为 CG 原点、m 单位。"""
    vertices = [
        cad_to_base_m((x, y, z + dz))
        for dz in (-thickness_mm / 2, thickness_mm / 2)
        for x, y, z in polygon
    ]
    n = len(polygon)
    triangles = []
    for i in range(1, n - 1):
        triangles.extend(((0, i + 1, i), (n, n + i, n + i + 1)))
    for i in range(n):
        j = (i + 1) % n
        triangles.extend(((i, j, n + j), (i, n + j, n + i)))
    # 多边形左右镜像时统一朝外；法向由三角形实际方向计算。
    signed_volume = sum(
        sum(
            vertices[a][i]
            * (
                vertices[b][(i + 1) % 3] * vertices[c][(i + 2) % 3]
                - vertices[b][(i + 2) % 3] * vertices[c][(i + 1) % 3]
            )
            for i in range(3)
        )
        / 6
        for a, b, c in triangles
    )
    if signed_volume < 0:
        triangles = [(a, c, b) for a, b, c in triangles]
    with path.open("wb") as stream:
        stream.write(b"Multi-Detect delta placeholder, metres".ljust(80, b"\0"))
        stream.write(struct.pack("<I", len(triangles)))
        for triangle in triangles:
            a, b, c = [vertices[i] for i in triangle]
            u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
            normal = tuple(
                u[(i + 1) % 3] * v[(i + 2) % 3] - u[(i + 2) % 3] * v[(i + 1) % 3] for i in range(3)
            )
            length = math.sqrt(sum(x * x for x in normal))
            stream.write(struct.pack("<12fH", *(x / length for x in normal), *a, *b, *c, 0))


def _sensors(base):
    # 与上游 4018 相同的名称/帧/频率；噪声保持上游量级。
    for name, kind, rate in (
        ("imu_sensor", "imu", 250),
        ("air_pressure_sensor", "air_pressure", 50),
        ("magnetometer_sensor", "magnetometer", 100),
        ("navsat_sensor", "navsat", 30),
    ):
        sensor = _element(base, "sensor", name=name, type=kind)
        _element(sensor, "gz_frame_id", "base_link")
        _element(sensor, "always_on", 1)
        _element(sensor, "update_rate", rate)
        if kind == "imu":
            imu = _element(sensor, "imu")
            for signal, stddev, bias, correlation in (
                ("angular_velocity", 0.0003394, 3.8785e-5, 1000),
                ("linear_acceleration", 0.004, 0.006, 300),
            ):
                part = _element(imu, signal)
                for axis in "xyz":
                    noise = _element(_element(part, axis), "noise", type="gaussian")
                    for key, value in (
                        ("mean", 0),
                        ("stddev", stddev),
                        ("dynamic_bias_stddev", bias),
                        ("dynamic_bias_correlation_time", correlation),
                    ):
                        _element(noise, key, value)
        elif kind == "air_pressure":
            noise = _element(_element(_element(sensor, kind), "pressure"), "noise", type="gaussian")
            _element(noise, "stddev", 0.01)
        elif kind == "magnetometer":
            mag = _element(sensor, kind)
            for axis in "xyz":
                noise = _element(_element(mag, axis), "noise", type="gaussian")
                _element(noise, "stddev", 0.0001)


def _aero(model):
    area, central, outer = 0.607, 0.399, 0.208
    cos_d, sin_d = math.cos(math.radians(35)), math.sin(math.radians(35))
    # 外翼迎角及升力投影各带 cos(35°)，合计 cos²。
    effective = central + outer * cos_d**2
    cla = 2.82 * area / effective
    # 中央 NP y=612 mm，整体 NP y=666 mm，CG y=646 mm。
    # 用相同投影权重反解外翼 CP；得到 y≈820.4 mm。
    outer_cp_y = (666 * effective - 612 * central) / (outer * cos_d**2)
    rho, ar, efficiency = 1.15, 1.250**2 / area, 0.8
    k = 1 / (math.pi * efficiency * ar)
    alpha_trim = ASSEMBLY_MASS_KG * 9.80665 / (0.5 * rho * 15**2 * area * 2.82)
    # Equivalent reflex camber: solve the local LiftDrag angles, sweep
    # projection, drag and r x F terms at the report's 15 m/s level-flight trim.
    # This keeps the design mass/lift and the 3.5% static margin unchanged.
    central_a0 = math.radians(1.0647676978297171)
    outer_a0 = math.radians(-2.2741621307571376)
    # 简单 LiftDrag 只有 |cda*alpha|，没有 CD0 或二次诱导阻力。
    # 在 15 m/s 配平迎角处线性化 k*CL²；前飞之外只是近似。
    cda = k * 2.82**2 * alpha_trim * area / (central + outer * cos_d)
    comment = (
        "设计报告方案 E：S=0.607，中央 0.399，外翼 4×0.052 m²；反角 ±35°。\n"
        f"      CLA={cla:.9g}，使中央+外翼投影后的整体 CLα=2.82/rad。\n"
        "      CP：中央 CAD y=612；外翼反解到整体 NP y=666。CG y=646，"
        "SM=(666-646)/569=3.51%。\n"
        f"      ρ=1.15，AR=1.25²/0.607={ar:.6g}，e=0.8；k={k:.6g}。\n"
        f"      15 m/s 配平 α={alpha_trim:.6g}，诱导阻力 cda={cda:.6g}。\n"
        "      CLmax 取 0.8：整体失速 α=0.8/2.82；失速后斜率暂取 -1/rad。\n"
        f"      反弯等效：central a0={math.degrees(central_a0):.4f}°，"
        f"outer a0={math.degrees(outer_a0):.4f}°；以 LiftDrag 局部迎角校准，"
        f"15 m/s、alpha={math.degrees(alpha_trim):.4f}° 时翼面升力=重力且 My≈0。"
    )
    model.append(ET.Comment(comment))

    def surface(name, surface_area, cp, upward, lift_slope, drag_slope, stall, a0=0):
        plugin = _element(
            model, "plugin", name="gz::sim::systems::LiftDrag", filename="gz-sim-lift-drag-system"
        )
        plugin.append(ET.Comment(name))
        for key, value in (
            ("a0", a0),
            ("cla", lift_slope),
            ("cda", drag_slope),
            ("cma", 0),
            ("alpha_stall", stall),
            ("cla_stall", -1 if lift_slope else 0),
            ("cda_stall", 1 if lift_slope else drag_slope),
            ("cma_stall", 0),
            ("area", surface_area),
            ("air_density", rho),
            ("cp", _vector(cp)),
            ("forward", "0 0 1"),
            ("upward", _vector(upward)),
            ("link_name", "base_link"),
            ("radial_symmetry", "false"),
        ):
            _element(plugin, key, value)

    # The shared central panel had its CP at y=0, so the LiftDrag system had
    # no spanwise arm for roll damping. Keep its area and coefficients while
    # assigning each half its own local flow sample and CP.
    for side in (-1, 1):
        surface(
            f"central_wing_{side}",
            central / 2,
            cad_to_base_m((side * 262, 612, 0)),
            (-1, 0, 0),
            cla,
            cda,
            0.8 / 2.82,
            a0=central_a0,
        )
    for side in (-1, 1):
        for tier in (-1, 1):
            # CAD 外翼向外、向上/下展开；法向 CAD (-side*tier*sinδ,0,cosδ)。
            upward = (-cos_d, -side * tier * sin_d, 0)
            surface(
                f"outer_wing_{side}_{tier}",
                outer / 4,
                cad_to_base_m((side * 492.5, outer_cp_y, tier * 75)),
                upward,
                cla,
                cda,
                math.atan(math.tan(0.8 / 2.82) * cos_d),
                a0=outer_a0,
            )
    model.append(
        ET.Comment(
            "零升力阻力：两个无升力、半面积的虚拟阻力面，a0=±0.5、cda=0.064；\n"
            "      |α|<0.5 rad 时平均 CD=0.064*(|0.5+α|+|-0.5+α|)/2=0.032。\n"
            "      放在 CG，不添加虚构配平力矩；大迎角/悬停平板风载未经标定。"
        )
    )
    for sign in (-1, 1):
        surface(
            f"parasitic_drag_{sign}",
            area / 2,
            (0, 0, 0),
            (-1, 0, 0),
            0,
            0.064,
            math.pi / 2,
            a0=sign * 0.5,
        )
    # gz-sim-multicopter-motor-model has no forward-flight advance-ratio
    # input. Approximate the lost 10x4.7 prop thrust with extra q-scaled drag:
    # CD0=0.108 in addition to the airframe's 0.032. At 24 m/s this contributes
    # about 21.6 N; it is a speed-limiting proxy, not a propeller map.
    model.append(
        ET.Comment(
            "桨前进比损失近似：电机插件只有 kT*ω²，无空速/桨距速度输入；"
            "额外阻力面等效 ΔCD0=0.108，24 m/s 时约 21.6 N。"
            "这是限速代理，不是 10×4.7 桨的实测推力曲线。"
        )
    )
    for sign in (-1, 1):
        surface(
            f"propeller_advance_drag_{sign}",
            area / 2,
            (0, 0, 0),
            (-1, 0, 0),
            0,
            0.216,
            math.pi / 2,
            a0=sign * 0.5,
        )


def generate(model_dir: Path) -> None:
    meshes = model_dir / "meshes"
    meshes.mkdir(parents=True, exist_ok=True)
    root = ET.Element("sdf", version="1.9")
    model = _element(root, "model", name="delta_tailsitter")
    model.append(
        ET.Comment(
            "由 scripts/generate_delta_tailsitter_model.py 生成。"
            "来源：折叠尾座式三角翼设计计算报告，\n"
            "    方案 E（壳体优化目标），未经实飞验证；STEP 外观由转换器按部件导出。\n"
            "    CAD mm -> base_link m: (-(z+9), x, 646-y)/1000；原点为报告 CG。\n"
            "    base_link 是 MC FLU；PX4 FRD=(x,-y,-z)，前飞时俯仰约 -90°。\n"
            "    整机质量 2.929 kg、CG 原点惯量 diag(0.431,0.097,0.373)。\n"
            "    C11 与电子设备质量已经计入整机，包装相机/云台质量由包装链节显式分配。"
        )
    )
    base = _element(model, "link", name="base_link")
    positions = [cad_to_base_m((x, 605, z)) for x, z in ROTORS]
    parts = [(PROP_MASS, p, PROP_INERTIA) for p in positions]
    # The articulated camera allocation is carried by wrapper links and is
    # removed from the common dynamics body so both variants retain the same
    # 2.929 kg assembly mass and CG.
    parts += [(GIMBAL_MOUNT_MASS, CAMERA_POSITION_M, (1e-6,) * 3)]
    parts += [(GIMBAL_YAW_MASS, CAMERA_POSITION_M, GIMBAL_CHILD_INERTIA)]
    parts += [(GIMBAL_ROLL_MASS, CAMERA_POSITION_M, GIMBAL_CHILD_INERTIA)]
    parts += [(CAMERA_MASS, CAMERA_POSITION_M, CAMERA_INERTIA)]
    parts += [(DEPTH_CAMERA_MASS, CAMERA_POSITION_M, GIMBAL_CHILD_INERTIA)]
    body_mass = ASSEMBLY_MASS_KG - sum(m for m, _, _ in parts)
    body_cg = tuple(-sum(m * p[i] for m, p, _ in parts) / body_mass for i in range(3))
    # 先从整机 CG 惯量扣除桨/传感器，再用平行轴定理移到主体自身 CG。
    diagonal = tuple(
        ASSEMBLY_INERTIA_KGM2[i]
        - sum(
            inertia[i] + m * sum(p[j] ** 2 for j in range(3) if j != i) for m, p, inertia in parts
        )
        - body_mass * sum(body_cg[j] ** 2 for j in range(3) if j != i)
        for i in range(3)
    )
    products = tuple(
        sum(m * p[i] * p[j] for m, p, _ in parts) + body_mass * body_cg[i] * body_cg[j]
        for i, j in ((0, 1), (0, 2), (1, 2))
    )
    _inertial(base, body_mass, diagonal, position=body_cg, products=products)
    _element(base, "gravity", "true")
    _element(base, "self_collide", "false")
    # 机身长度 850、宽 300、厚 158 mm（报告）；最尾端由支脚定义在 y=1055。
    center = cad_to_base_m((0, 625, 0))
    _visual(base, "placeholder_body", center, "box", {"size": "0.158 0.300 0.850"})
    collision = _element(base, "collision", name="body_collision")
    _element(collision, "pose", _vector((*center, 0, 0, 0)))
    _element(_element(_element(collision, "geometry"), "box"), "size", "0.158 0.300 0.850")
    for side in (-1, 1):
        # 根弦 500，展向 150->385 mm，前缘后掠 39°；厚度仅为显示占位。
        tip_le = 500 + 235 * math.tan(math.radians(39))
        polygon = [
            (side * 150, 500, 0),
            (side * 385, tip_le, 0),
            (side * 385, 1000, 0),
            (side * 150, 1000, 0),
        ]
        filename = f"placeholder_inner_{side}.stl"
        _wing_mesh(meshes / filename, polygon)
        _visual(
            base,
            f"placeholder_inner_{side}",
            (0, 0, 0),
            "mesh",
            {"uri": f"model://delta_tailsitter/meshes/{filename}"},
        )
        for tier in (-1, 1):
            # 262 mm 展长 × ±35°，尖端约 (±600, ±150) mm；根/梢弦 287/167。
            tip_x = 385 + 262 * math.cos(math.radians(35))
            tip_z = tier * 262 * math.sin(math.radians(35))
            root_le, tip_le = 704, 704 - 262 * math.tan(math.radians(12))
            polygon = [
                (side * 385, root_le, 0),
                (side * tip_x, tip_le, tip_z),
                (side * tip_x, tip_le + 167, tip_z),
                (side * 385, root_le + 287, 0),
            ]
            filename = f"placeholder_outer_{side}_{tier}.stl"
            _wing_mesh(meshes / filename, polygon)
            _visual(
                base,
                f"placeholder_outer_{side}_{tier}",
                (0, 0, 0),
                "mesh",
                {"uri": f"model://delta_tailsitter/meshes/{filename}"},
                "0.6 0.65 0.7 1",
            )
    for i, ((x, z), position) in enumerate(zip(ROTORS, positions, strict=True)):
        _visual(
            base,
            f"placeholder_nacelle_{i}",
            cad_to_base_m((x, 734.5, z)),
            "cylinder",
            {"radius": 0.025, "length": 0.211},
        )
        _visual(
            base,
            f"placeholder_leg_{i}",
            cad_to_base_m((x, 947.5, z)),
            "cylinder",
            {"radius": 0.008, "length": 0.215},
        )
        _visual(
            base,
            f"placeholder_motor_{i}",
            cad_to_base_m((x, 629, z)),
            "cylinder",
            {"radius": 0.014, "length": 0.03},
            "0.1 0.1 0.1 1",
        )
        # 小接触球下端 y=1055 mm；四点共面，摩擦明确，机身不先触地。
        foot_position = cad_to_base_m((x, 1043, z))
        _visual(base, f"placeholder_foot_{i}", foot_position, "sphere", {"radius": 0.012})
        collision = _element(base, "collision", name=f"foot_{i}_collision")
        _element(collision, "pose", _vector((*foot_position, 0, 0, 0)))
        _element(_element(_element(collision, "geometry"), "sphere"), "radius", 0.012)
        surface = _element(collision, "surface")
        friction = _element(_element(surface, "friction"), "ode")
        _element(friction, "mu", 0.8)
        _element(friction, "mu2", 0.8)
        contact = _element(_element(surface, "contact"), "ode")
        _element(contact, "kp", 100000)
        _element(contact, "kd", 100)
        _element(contact, "max_vel", 0.1)
        _element(contact, "min_depth", 0.001)
        rotor = _element(model, "link", name=f"rotor_{i}")
        _element(rotor, "pose", _vector((*position, 0, 0, 0)), relative_to="base_link")
        _inertial(rotor, PROP_MASS, PROP_INERTIA)
        _visual(
            rotor,
            f"propeller_{i}",
            (0, 0, 0),
            "box",
            {"size": "0.254 0.018 0.003"},
            "0.1 0.3 0.8 1" if i < 2 else "0.15 0.15 0.15 1",
        )
        rotor_mesh = meshes / f"rotor_{i}.stl"
        if rotor_mesh.is_file():
            for visual in list(rotor.findall("visual")):
                rotor.remove(visual)
            _mesh_visual(
                rotor,
                f"propeller_{i}",
                f"model://delta_tailsitter/meshes/{rotor_mesh.name}",
                color="0.1 0.3 0.8 1" if i < 2 else "0.15 0.15 0.15 1",
            )
        joint = _element(model, "joint", name=f"rotor_{i}_joint", type="revolute")
        _element(joint, "parent", "base_link")
        _element(joint, "child", f"rotor_{i}")
        axis = _element(joint, "axis")
        _element(axis, "xyz", "0 0 1")
        limit = _element(axis, "limit")
        _element(limit, "lower", -1e16)
        _element(limit, "upper", 1e16)
        plugin = _element(
            model,
            "plugin",
            name="gz::sim::systems::MulticopterMotorModel",
            filename="gz-sim-multicopter-motor-model-system",
        )
        plugin.append(
            ET.Comment(
                "X2216 4S/10 寸：ωmax=1100 rad/s≈10504 rpm；kT=16.2/1100²。\n"
                "      KM=0.025 m 是 10 寸桨初估，PX4 CA_ROTORn_KM 同值；推力沿 +Z。\n"
                "      THR_MDL_FAC=1 将 44% 归一化推力映射到 sqrt(0.44)*ωmax。"
            )
        )
        for key, value in (
            ("jointName", f"rotor_{i}_joint"),
            ("linkName", f"rotor_{i}"),
            ("turningDirection", "ccw" if i < 2 else "cw"),
            ("timeConstantUp", 0.0125),
            ("timeConstantDown", 0.025),
            ("maxRotVelocity", 1100),
            ("motorConstant", 16.2 / 1100**2),
            ("momentConstant", 0.025),
            ("commandSubTopic", "command/motor_speed"),
            ("motorNumber", i),
            ("rotorDragCoefficient", 8.06428e-5),
            ("rollingMomentCoefficient", 1e-6),
            ("rotorVelocitySlowdownSim", 10),
            ("motorType", "velocity"),
        ):
            _element(plugin, key, value)
    _gimbal(model, meshes)
    _sensors(base)
    # 继承上游空速传感器资源；动力学/电机/机翼均为本机独立定义。
    include = _element(model, "include", merge="true")
    _element(include, "uri", "model://airspeed")
    _element(include, "pose", "0 0 0.1 0 -1.5707963267948966 0", relative_to="base_link")
    joint = _element(model, "joint", name="airspeed_joint", type="fixed")
    _element(joint, "parent", "base_link")
    _element(joint, "child", "airspeed_link")
    _aero(model)
    ET.indent(root, space="  ")
    (model_dir / "model.sdf").write_text(
        ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8"
    )
    selected = select_visual_mesh(model_dir)
    if selected:
        (model_dir / "model.sdf").write_text(selected[0], encoding="utf-8")
    _write_camera_models(model_dir, meshes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir", type=Path, default=ROOT / "simulation/gazebo/models/delta_tailsitter"
    )
    args = parser.parse_args(argv)
    generate(args.model_dir)
    print(args.model_dir / "model.sdf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
