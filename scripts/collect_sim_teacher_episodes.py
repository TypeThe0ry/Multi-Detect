#!/usr/bin/env python3
"""Collect raw SIM teacher episodes in disposable PX4/Gazebo containers.

Runtime recording deliberately does no provenance hashing.  Run the offline
auditor after collection.  ``--dry-run`` validates the scenario and prints the
planned launch without starting Docker.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import types
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.airframe_profile import (  # noqa: E402
    AirframeProfile,
    airframe_uses_multirotor_executor,
    resolve_airframe_profile,
)
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.camera_defaults import DEFAULT_CAMERA_INFO  # noqa: E402
from multidetect.gimbal_controller import Px4GimbalController  # noqa: E402
from multidetect.provenance import build_provenance, model_paths_from_config, snapshot_config  # noqa: E402
from multidetect.px4_mission import (  # noqa: E402
    MAV_CMD_NAV_LAND,
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
)
from multidetect.sim_network import sim_udpin_endpoint  # noqa: E402
from multidetect.sim_teacher import (  # noqa: E402
    ActionPrimitive,
    EpisodeRecorder,
    ExpertSearchPolicy,
    ExploreBounds,
    ExplorePolicy,
    HoverPolicy,
    PymavlinkPrimitiveExecutor,
    SetpointStreamer,
    primitive_to_ned,
)
from multidetect.sim_teacher.continuous_video import ContinuousVideoWriter  # noqa: E402
from multidetect.sim_teacher.fw_executor import FixedWingVtolExecutor  # noqa: E402
from multidetect.sim_teacher.isolation import validate_local_endpoint  # noqa: E402
from multidetect.sim_teacher.policies import load_sdf_obstacles  # noqa: E402
from multidetect.sim_teacher.spawn_clearance import (  # noqa: E402
    require_spawn_clearance,
    teacher_intent_clearance_altitude,
    teacher_intent_clearance_route_points,
)
from multidetect.sim_teacher.streams import (  # noqa: E402
    MavlinkReader,
    MavlinkState,
    SensorStreamReader,
    build_observation,
)
from multidetect.simulator.cpu import resolve_cpu_isolation  # noqa: E402
from multidetect.simulator.stack import SimulatorStackError  # noqa: E402
from multidetect.simulator.stack import _vehicle_spec as _stack_vehicle_spec  # noqa: E402
from scripts.run_simulator_docker import (  # noqa: E402
    CONTAINER_IMAGE_MODEL_ROOT,
    CONTAINER_MODEL_ROOT,
    CONTAINER_SCRIPT_ROOT,
    _split_gpu_list,
    build_docker_simulator_plan,
    resolve_px4_logger_mode,
)
from scripts.sitl_lock import SitlLock, default_lock_path, sitl_lock_context  # noqa: E402

PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
DEFAULT_TIMEOUT_S = 300.0
SENSOR_FRAME_WAIT_S = 15.0
DEFAULT_RGB_SIZE = (640, 480)
DEFAULT_DEPTH_SIZE = (320, 240)
DEFAULT_DEPTH_ENCODING = "UINT16_MM"
DEFAULT_MIN_FREE_GB = 15.0
DEFAULT_DISK_CHECK_INTERVAL_STEPS = 10
DEFAULT_VIDEO_MAX_BYTES = 8 * 1024**3


class LowDiskSpace(RuntimeError):
    """Normal campaign stop caused by the configured free-space floor."""


def free_space_bytes(path: str | Path) -> int:
    return shutil.disk_usage(Path(path).resolve()).free


def disk_space_abort_reason(
    path: str | Path,
    minimum_gb: float,
    *,
    free_space_fn: Any = free_space_bytes,
) -> str | None:
    free = free_space_fn(path)
    if free < minimum_gb * 1024**3:
        return f"free space {free / 1024**3:.2f} GiB below {minimum_gb:.2f} GiB"
    return None


def _parse_size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0 or width > 4096 or height > 4096:
        raise argparse.ArgumentTypeError("size is outside supported range")
    return width, height


def _parse_sim_param(value: str) -> str:
    name, sep, setting = value.partition("=")
    if not sep or not name.strip() or not setting.strip():
        raise argparse.ArgumentTypeError("sim-param must be NAME=VALUE")
    return value


DEFAULT_TAKEOFF_ALTITUDE_M = 5.0
TAKEOFF_SAFETY_MARGIN_M = 4.0
MAX_TAKEOFF_ALTITUDE_M = 40.0
DEFAULT_CONTROL_PORT = 14540
DEFAULT_TELEMETRY_PORT = 14541
# PX4 starts only after Gazebo loads the world, which the container now allows
# 180 s (run_simulator_docker.GAZEBO_WORLD_ATTEMPTS); wait past that.
DEFAULT_HEARTBEAT_TIMEOUT_S = 240.0
DEFAULT_PREARM_HEALTH_TIMEOUT_S = 45.0
DEFAULT_OFFBOARD_ENTER_TIMEOUT_S = 30.0
LAND_BASE_TIMEOUT_S = 60.0
LAND_MIN_TIMEOUT_S = 120.0
# Conservative descent allowance; Linux landing waits use PX4 boot time, while
# takeoff and the established Windows path still use wall-clock budgets.
LAND_DESCENT_RATE_MPS = 0.2
LAND_COMMAND_RETRY_INTERVAL_S = 5.0
LAND_COMMAND_MAX_RETRIES = 5
LAND_DIAGNOSTIC_INTERVAL_S = 5.0
LAND_STABLE_GROUND_S = 3.0
LAND_AUTO_DISARM_WAIT_S = 30.0
LAND_DISARM_CONFIRM_TIMEOUT_S = 10.0
# Fresh descent samples earn a bounded grace period on the landing budget clock.
# The independent wall-clock ceiling also bounds frozen or extremely slow SITL.
LAND_PROGRESS_GRACE_S = 30.0
LAND_PROGRESS_TOLERANCE_M = 0.05
LAND_HARD_CEILING_S = 600.0
FW_LAND_HARD_WALL_CEILING_S = 3600.0
LAND_SETTLED_ALTITUDE_M = 1.5
LAND_SETTLED_VERTICAL_SPEED_MPS = 0.08
LAND_SETTLED_DURATION_S = 10.0
FIXED_WING_LANDING_GLIDE_ANGLE_DEG = 5.0
FIXED_WING_LANDING_APPROACH_ALT_M = 20.0
MAV_CMD_NAV_LOITER_TO_ALT = 31
MAV_FRAME_MISSION = 2
LANDING_TELEMETRY_STALE_S = 30.0
MOTION_ARRIVAL_TOLERANCE_M = 8.0
MOTION_ALONG_TRACK_DONE_M = 5.0
# A fixed-wing reposition may complete by flying past its waypoint, but a
# projection crossing alone is insufficient: the aircraft can cross the target
# bearing tens of metres away while circling.  Bound the cross-track error by
# the same tolerance used for a direct arrival.
MOTION_CROSS_TRACK_DONE_M = MOTION_ARRIVAL_TOLERANCE_M
FIXED_WING_LOITER_CAPTURE_M = 80.0
MOTION_REPOSITION_TIMEOUT_S = 120.0
MOTION_LOITER_MAX_WAIT_S = 60.0
# Hover-mode (MC) VTOL legs settle on their target and heading before the next frame.
MC_ARRIVAL_TOLERANCE_M = 1.5
MC_YAW_TOLERANCE_DEG = 6.0
MC_SETTLED_SPEED_MPS = 0.6
MC_SETTLE_S = 0.5
MC_SPEED_FLOOR_MPS = 3.0
MOTION_TRANSITION_TIMEOUT_S = 90.0
RTL_MODE_CONFIRM_S = 10.0
FIXED_WING_MIN_OPERATING_ALT_M = 30.0
VTOL_TRANSITION_MARGIN_M = 10.0
MAV_CMD_DO_LAND_START = 189
ARM_MAX_ATTEMPTS = 6
ARM_RETRY_INTERVAL_S = 1.0
EPISODE_PROGRESS_STALL_TIMEOUT_S = 180.0
GCS_SOURCE_SYSTEM = 255
GCS_SOURCE_COMPONENT = 190
# The only collector-specific SITL setting is the GCS/RC input mode used by
# the proven Gazebo recipe.  Do not weaken PX4 arming checks or use a force arm.
SIM_PREARM_PARAMETERS = (("COM_RC_IN_MODE", "4"),)
HEALTH_ERROR_BITS = {
    0x000080: "manual_control_input",
    0x001000: "communication_links",
    0x100000: "system",
}
SYS_STATUS_HEALTH_BITS = {
    1: "gyro",
    2: "accelerometer",
    4: "magnetometer",
    8: "absolute_pressure",
    32: "gps",
    64: "optical_flow",
    128: "vision_position",
    256: "laser_position",
    512: "external_ground_truth",
    1024: "angular_rate_control",
    2048: "attitude_stabilization",
    4096: "yaw_position",
    8192: "z_altitude_control",
    16384: "xy_position_control",
    32768: "motor_outputs",
    65536: "rc_receiver",
    268435456: "prearm_check",
    33554432: "battery",
}


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    scenario: str
    runtime: str
    truth: str
    teacher: str
    episodes: int
    steps_per_episode: int
    seed: int
    out: str
    image: str = PINNED_IMAGE
    container_prefix: str = "md-sim-teacher"
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_episode_s: float = DEFAULT_TIMEOUT_S
    heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S
    keep_container_on_failure: bool = False
    control_port: int = DEFAULT_CONTROL_PORT
    telemetry_port: int = DEFAULT_TELEMETRY_PORT
    sim_network: str | None = None
    sim_instance: int = 0
    rgb_size: tuple[int, int] = DEFAULT_RGB_SIZE
    depth_size: tuple[int, int] = DEFAULT_DEPTH_SIZE
    depth_encoding: str = DEFAULT_DEPTH_ENCODING
    segmentation: bool = False
    camera_noise_overlay: bool = False
    wind_vehicle_overlay: bool = False
    # Extra SIM-only PX4 parameters for this run, e.g. a slower simulated
    # battery for an interactive session; batch campaigns pass none.
    extra_sim_params: tuple[tuple[str, str], ...] = ()
    # ``None`` selects the scenario-derived altitude.  An explicit value is
    # retained in the plan so campaign wrappers can override it per run.
    takeoff_altitude_m: float | None = None
    # JSON overrides for ``--teacher onboard`` (model paths, thresholds).
    onboard_config: str | None = None
    depth_format: str = "npy"
    # --no-depth: the frame server still streams depth (its wire contract requires
    # it); only the recorder stops persisting it.
    capture_depth: bool = True
    rgb_format: str = "png"
    rgb_quality: int = 90
    deduplicate_depth: bool = False
    compress_mavlink: bool = False
    onboard_frame_format: str = "png"
    onboard_frame_quality: int = 90
    async_metadata: bool = False
    sensor_poll_interval_s: float = 0.02
    min_free_gb: float = 15.0
    disk_check_interval_steps: int = 10
    video_hz: float = 0.0
    video_max_bytes: int = DEFAULT_VIDEO_MAX_BYTES
    airframe: str = "x500_mono_cam"
    # "true": fly to the true target; "confuser": fly to a look-alike (hard negatives).
    teacher_target: str = "true"
    smoke_flight: bool = False
    takeoff_method: str | None = None
    # Copy PX4's .ulg flight logs out of the container before it is removed.
    keep_px4_ulog: bool = False
    # Attach to a simulator owned by another process (for example run_sim_session).
    # Attached containers are inspected and reused, never started or removed here.
    attach_container: str | None = None
    # Explicit onboard task text; when omitted, generated-index text is used.
    task_text: str | None = None
    # GPU rendering is opt-in.  ``None`` lets MULTIDETECT_SIM_GPU decide.
    gpu_render: bool | None = None
    gpu_device: str | None = None
    gpu_devices: tuple[str, ...] | None = None
    worker_index: int = 0
    operator_port: int = 14580

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        result["linux_primitive_clock_stall_timeout_s"] = _linux_primitive_clock_stall_timeout_s()
        result["px4_logger_mode"] = resolve_px4_logger_mode()
        altitude, source = _takeoff_altitude_for_plan(self)
        result["takeoff_altitude_m"] = altitude
        result["takeoff_altitude_source"] = source
        result["docker_command"] = docker_command(self)
        cpu_isolation = resolve_cpu_isolation(self.worker_index)
        if cpu_isolation is not None:
            result["cpu_isolation"] = cpu_isolation.to_mapping()
        return result


def _scenario_channels(scenario_path: Path) -> tuple[Path, Path]:
    document = json.loads(scenario_path.read_text(encoding="utf-8"))
    channels = document.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("scenario must contain channels")

    def resolve(name: str) -> Path:
        channel = channels.get(name)
        if not isinstance(channel, dict) or not isinstance(channel.get("path"), str):
            raise ValueError(f"scenario channel {name!r} is missing path")
        path = (scenario_path.parent / channel["path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    return resolve("runtime_input"), resolve("truth")


def _teacher_intent_path(scenario_path: Path) -> Path:
    """Resolve the collector-only teacher intent sidecar, when present."""
    path = scenario_path.with_name(scenario_path.name.removesuffix(".scenario.json") + ".teacher_intent.json")
    if not path.is_file():
        return path
    return path


def _validate_runtime(runtime_path: Path, airframe: str = "x500_mono_cam") -> dict[str, Any]:
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if not isinstance(runtime, dict):
        raise ValueError("runtime channel must be a JSON object")
    vehicle = runtime.get("vehicle", {})
    model = vehicle.get("model") if isinstance(vehicle, dict) else None
    requested = str(airframe).strip().lower().replace("-", "_")
    profile = _airframe_profile(airframe)
    category = str(getattr(profile, "category", "")) if profile is not None else ""
    model_text = str(model or "").lower()
    model_category = (
        "fixed_wing" if "fixed_wing" in model_text else
        "vtol" if "vtol" in model_text or "tiltrotor" in model_text else
        "multirotor" if "multirotor" in model_text or "x500" in model_text else
        "unknown"
    )
    if requested in {"x500", "x500_mono_cam", "multirotor"}:
        if model and "multirotor" not in str(model).lower() and "x500" not in str(model).lower():
            raise ValueError("x500_mono_cam requires a multirotor/x500 runtime model")
    elif not model:
        raise ValueError(f"airframe {airframe!r} requires runtime.vehicle.model")
    if category in {"fixed_wing", "multirotor", "vtol"} and model_category in {"fixed_wing", "multirotor", "vtol"} and category != model_category:
        raise ValueError(
            f"airframe {airframe!r} category={category} does not match runtime.vehicle.model={model!r}"
        )
    for sensor in runtime.get("sensors", []):
        if isinstance(sensor, dict) and isinstance(sensor.get("stream_uri"), str):
            validate_local_endpoint(sensor["stream_uri"])
    return runtime


def _airframe_profile(airframe: str, takeoff_method: str | None = None) -> AirframeProfile | None:
    return resolve_airframe_profile(airframe, takeoff_method, base_dir=ROOT)


def _new_video_sample(sample: Any, last_written_ns: int) -> bool:
    """A camera sample the continuous video has not written yet.

    The image pipeline always lags the newest pose, so requiring the sample to
    be at or after the current pose stamp wrote no frame at all; the pose for
    the frame comes from ``aligned_state`` at the image's own stamp instead.
    """
    return sample is not None and int(sample.sensor_stamp_ns) > last_written_ns


def _gazebo_model_name(plan: CollectionPlan, runtime: Mapping[str, Any]) -> str:
    """Name of the spawned Gazebo model whose sensor topics the collector subscribes to.

    Runtime channels name an abstract airframe (``px4_multirotor_standard``,
    ``px4_fixed_wing_standard``); the simulator stack maps it to the Gazebo model it
    spawns, so use that same mapping instead of guessing from the runtime name.
    """
    vehicle = runtime.get("vehicle", {}) if isinstance(runtime, Mapping) else {}
    vehicle = vehicle if isinstance(vehicle, Mapping) else {}
    profile = str(vehicle.get("airframe_profile_id", ""))
    if plan.airframe == "x500_gimbal" or profile == "x500_gimbal":
        return "x500_gimbal_0"
    if plan.airframe == "x500_mono_cam":
        return "x500_mono_cam_0"
    model = str(vehicle.get("model", plan.airframe))
    try:
        _, name = _stack_vehicle_spec(model, str(vehicle.get("airframe_profile_id", "")))
    except SimulatorStackError:
        name = model
    return name if name.endswith("_0") else name + "_0"


_DEFAULT_CAMERA_SENSOR_LINKS = {"rgb": "camera_link", "depth": "depth_camera_link"}
_CAMERA_SENSOR_LINKS = {
    "x500_gimbal": {"rgb": "camera_link", "depth": "depth_camera_link"},
}


def _has_registered_depth_camera(airframe: str) -> bool:
    """Whether the collector may bind the model's repository depth topic by default."""
    normalized = str(airframe).strip().lower().replace("-", "_")
    if normalized == "x500_mono_cam":
        return False
    profile = _airframe_profile(airframe)
    if profile is not None and profile.id == "x500_gimbal":
        overlay = ROOT / "simulation/gazebo/model-overlays/x500_gimbal_seg/model.sdf"
        try:
            model = ET.parse(overlay).getroot().find("model")
        except (OSError, ET.ParseError):
            return False
        return model is not None and model.find(".//sensor[@name='depth_camera']") is not None
    # Preserve the collector's established default for every non-x500 id.
    return True


def _camera_sensor_topics(*, world: str, model: str, airframe: str) -> dict[str, str]:
    """Derive the scoped topics from the merged model sensor structure.

    The gimbal overlay merges ``model://gimbal`` into the x500 composite, so
    its camera links remain direct children of the spawned model.  Keeping the
    link and sensor names in one mapping prevents the collector and frame
    server contract from drifting apart when a model composition changes.
    """
    normalized = str(airframe).strip().lower().replace("-", "_")
    links = _CAMERA_SENSOR_LINKS.get(normalized, _DEFAULT_CAMERA_SENSOR_LINKS)
    camera_link = links["rgb"]
    depth_link = links["depth"]
    model_prefix = f"/world/{world}/model/{model}"
    rgb_prefix = f"{model_prefix}/link/{camera_link}/sensor/camera"
    depth_prefix = f"{model_prefix}/link/{depth_link}/sensor/depth_camera"
    return {
        "image": f"{rgb_prefix}/image",
        "camera_info": f"{rgb_prefix}/camera_info",
        "depth_image": f"{depth_prefix}/depth_image",
        "depth_camera_info": f"{depth_prefix}/camera_info",
        "segmentation": "/multidetect/teacher/segmentation/labels_map",
    }


def _rgb_camera_topics(*, world: str, model: str, airframe: str) -> tuple[str, str]:
    topics = _camera_sensor_topics(world=world, model=model, airframe=airframe)
    return topics["image"], topics["camera_info"]


def _frame_age_limit_s(airframe: str) -> float:
    """Causal frame age budget; high-speed aircraft get the tighter gate."""
    normalized = str(airframe).strip().lower().replace("-", "_")
    if normalized in {"fixed_wing", "fw", "vtol"}:
        return 0.10
    profile = _airframe_profile(airframe)
    return 0.25 if profile is None or profile.category == "multirotor" else 0.10


def docker_command(plan: CollectionPlan, *, name: str | None = None) -> list[str]:
    """Build the repository-owned isolated PX4/Gazebo command for one episode."""
    container_name = name or f"{plan.container_prefix}-preview"
    try:
        stack_plan = build_docker_simulator_plan(
            Path(plan.scenario),
            repository_root=ROOT,
            container_name=container_name,
            job_dir=Path(plan.out) / "jobs" / container_name,
            control_port=plan.control_port,
            telemetry_port=plan.telemetry_port,
            image=plan.image,
            sim_network=plan.sim_network,
            sim_instance=plan.sim_instance,
            dry_run=True,
            gpu_render=plan.gpu_render,
            gpu_device=plan.gpu_device,
            gpu_devices=plan.gpu_devices,
            worker_index=plan.worker_index,
            airframe_profile_id=plan.airframe,
        )
    except Exception as exc:
        raise ValueError(f"unable to build simulator stack plan: {exc}") from exc
    command = list(stack_plan.command)
    if plan.segmentation:
        runtime = json.loads(Path(plan.runtime).read_text(encoding="utf-8"))
        model_name = _gazebo_model_name(plan, runtime).removesuffix("_0")
        target_model_name = model_name
        if plan.wind_vehicle_overlay and model_name == "x500_mono_cam":
            model_name = "x500_mono_cam_seg_wind"
        else:
            model_name = f"{model_name}_seg"
        overlay = ROOT / "simulation/gazebo/model-overlays" / model_name
        if not (overlay / "model.sdf").is_file():
            raise FileNotFoundError(overlay / "model.sdf")
        overlay_target_root = (
            CONTAINER_IMAGE_MODEL_ROOT
            if plan.airframe == "x500_gimbal"
            else CONTAINER_MODEL_ROOT
        )
        overlay_mounts = [
            "--mount",
            f"type=bind,source={overlay.resolve()},"
            f"target={overlay_target_root}/{target_model_name},readonly",
        ]
        if plan.airframe == "x500_gimbal":
            # The RGB sensor is nested in model://gimbal; mount only its SDF
            # file so the image-provided meshes remain available beside it.
            gimbal_model_overlay = overlay / "gimbal" / "model.sdf"
            if not gimbal_model_overlay.is_file():
                raise FileNotFoundError(gimbal_model_overlay)
            overlay_mounts.extend(
                (
                    "--mount",
                    f"type=bind,source={gimbal_model_overlay.resolve()},"
                    f"target={CONTAINER_IMAGE_MODEL_ROOT}/gimbal/model.sdf,readonly",
                )
            )
        parent_mount = next(
            (
                index
                for index, token in enumerate(command)
                if token.startswith("type=bind,")
                and "target=/opt/multidetect-gazebo-models," in token
            ),
            command.index("run"),
        )
        command[parent_mount + 1 : parent_mount + 1] = overlay_mounts
    if plan.camera_noise_overlay:
        # mono_cam comes from the image's model dir; the repo model root is a
        # read-only bind without a mono_cam directory, so Docker cannot create the
        # mount point there ("make mountpoint" failure in SITL).
        overlay = ROOT / "simulation/gazebo/model-overlays/mono_cam_noise"
        overlay_mount = (
            "--mount",
            f"type=bind,source={overlay.resolve()},"
            "target=/opt/px4-gazebo/share/gz/models/mono_cam,readonly",
        )
        parent_mount = next(
            (
                index
                for index, token in enumerate(command)
                if token.startswith("type=bind,")
                and "target=/opt/multidetect-gazebo-models," in token
            ),
            command.index("run"),
        )
        command[parent_mount + 1 : parent_mount + 1] = list(overlay_mount)
    if plan.wind_vehicle_overlay and not plan.segmentation:
        overlay = ROOT / "simulation/gazebo/model-overlays/x500_mono_cam_wind"
        overlay_mount = (
            "--mount",
            f"type=bind,source={overlay.resolve()},"
            "target=/opt/multidetect-gazebo-models/x500_mono_cam,readonly",
        )
        parent_mount = next(
            (
                index
                for index, token in enumerate(command)
                if token.startswith("type=bind,")
                and "target=/opt/multidetect-gazebo-models," in token
            ),
            command.index("run"),
        )
        command[parent_mount + 1 : parent_mount + 1] = list(overlay_mount)
    # PX4 floods stdout (e.g. "ODOMETRY: estimator_type 8 unsupported"); unbounded
    # json-file logs grew Docker's WSL disk by tens of GB. Keep a small rolling tail.
    if "run" in command:
        at = command.index("run") + 1
        command[at:at] = ["--log-opt", "max-size=5m", "--log-opt", "max-file=2"]
    return command


def _progress(phase: str, **fields: object) -> None:
    """Emit one machine-readable progress record on stderr."""
    row = {"event": "sim_teacher", "phase": phase, "monotonic_s": time.monotonic(), **fields}
    print(json.dumps(row, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def _docker_executable(plan: CollectionPlan) -> str:
    executable = shutil.which("docker") or (
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe" if os.name == "nt" else "docker"
    )
    if not Path(executable).is_file() and not shutil.which(executable):
        return "docker"
    return executable


LAUNCH_TOTAL_MASS_KG = 1.7
GRAVITY_MPS2 = 9.80665
LAUNCH_PULSE = {
    # Approximate the impulse of a human throw (released upward of level so the
    # aircraft leaves the ground) or a near-level catapult stroke.
    "hand": {"delta_v_mps": 12.0, "duration_s": 0.25, "pitch_deg": 20.0},
    "catapult": {"delta_v_mps": 17.0, "duration_s": 0.15, "pitch_deg": 10.0},
}


def _launch_force(method: str, yaw_ned_rad: float) -> dict[str, float]:
    """World-frame (ENU) force for the launch pulse.

    The thrown aircraft starts on the ground in SITL, so the pulse also carries its
    weight (m*g up) while it lasts; otherwise a shallow throw only pushes the model
    along on its wheels and the "hand launch" turns into a short ground roll.
    """
    pulse = LAUNCH_PULSE[method]
    speed = pulse["delta_v_mps"]
    pitch = math.radians(pulse["pitch_deg"])
    yaw_enu = math.pi / 2.0 - yaw_ned_rad
    direction = {
        "x": math.cos(pitch) * math.cos(yaw_enu),
        "y": math.cos(pitch) * math.sin(yaw_enu),
        "z": math.sin(pitch),
    }
    force = LAUNCH_TOTAL_MASS_KG * speed / pulse["duration_s"]
    weight = LAUNCH_TOTAL_MASS_KG * GRAVITY_MPS2
    return {
        "x": force * direction["x"],
        "y": force * direction["y"],
        "z": force * direction["z"] + weight,
        "force_n": force,
        "weight_compensation_n": weight,
        "mass_kg": LAUNCH_TOTAL_MASS_KG,
        "delta_v_mps": speed,
        "duration_s": pulse["duration_s"],
        "pitch_deg": pulse["pitch_deg"],
    }


def _apply_gazebo_launch(
    plan: CollectionPlan,
    environment: DockerSimEnvironment,
    model: str,
    yaw_ned_rad: float,
    method: str,
) -> dict[str, Any]:
    """Apply one world-frame force pulse and always clear its persistent wrench."""
    executable = _docker_executable(plan)
    force = _launch_force(method, yaw_ned_rad)
    topic = "/world/default/wrench/persistent"
    clear_topic = "/world/default/wrench/clear"
    link = f"{model}::base_link"
    # gz.msgs.EntityWrench nests force/torque under ``wrench``; the clear topic takes
    # a gz.msgs.Entity.  Both go in through the environment so no shell quoting can
    # strip the quoted link name.
    proto = (
        f'entity {{ name: "{link}" type: LINK }} '
        f"wrench {{ force {{ x: {force['x']:.6f} y: {force['y']:.6f} z: {force['z']:.6f} }} }}"
    )
    clear_proto = f'name: "{link}" type: LINK'
    # Each `gz topic -p` spends most of its time on start-up and discovery before
    # it publishes.  Start the pulse and the delayed clear together so the start-up
    # cost cancels out and the force stays on for about duration_s, not
    # duration_s plus a second process start.
    script = (
        "set -u; "
        "topics=$(gz topic -l); "
        f"case \"$topics\" in *{topic}* ) ;; *) echo 'missing {topic}' >&2; exit 20;; esac; "
        f'case "$topics" in *{clear_topic}* ) ;; '
        f"*) echo 'missing {clear_topic}' >&2; exit 21;; esac; "
        f'gz topic -t {topic} -m gz.msgs.EntityWrench -p "$MD_WRENCH" & '
        f"(sleep {force['duration_s']:.3f}; "
        f'gz topic -t {clear_topic} -m gz.msgs.Entity -p "$MD_CLEAR") & '
        "wait; "
        # Under load the delayed clear can reach Gazebo before the slower pulse
        # publisher does, leaving the force on for good (the aircraft ran away to
        # 3.5 km).  Both publishers have finished here, so this clear always lands
        # after the pulse.
        f'gz topic -t {clear_topic} -m gz.msgs.Entity -p "$MD_CLEAR"'
    )
    started_wall = time.time()
    started_mono = time.monotonic()
    result = subprocess.run(
        [
            executable,
            "exec",
            "-e",
            f"MD_WRENCH={proto}",
            "-e",
            f"MD_CLEAR={clear_proto}",
            environment.name,
            "bash",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=max(8.0, force["duration_s"] + 5.0),
        check=False,
    )
    ended_mono = time.monotonic()
    ended_wall = time.time()
    output = (result.stdout or "")[-1000:] + (result.stderr or "")[-1000:]
    # gz topic exits 0 even when it cannot build the message; treat that as failure.
    if result.returncode != 0 or "Unable to create message" in output or "Error parsing" in output:
        raise RuntimeError(f"Gazebo launch wrench failed ({result.returncode}): {output}")
    return {
        "method": method,
        "topic": topic,
        "clear_topic": clear_topic,
        "link": link,
        "force_world_enu_n": {axis: force[axis] for axis in ("x", "y", "z")},
        **force,
        "started_wall_time": started_wall,
        "ended_wall_time": ended_wall,
        "started_monotonic_s": started_mono,
        "ended_monotonic_s": ended_mono,
        "command_returncode": result.returncode,
        "command_output": output,
    }


def _launch_detected(status_texts: Iterable[str]) -> bool:
    return any(
        "detect" in text.lower() and ("launch" in text.lower() or "takeoff" in text.lower())
        for text in status_texts
    )


def _camera_stream_command(plan: CollectionPlan, environment: DockerSimEnvironment) -> list[str]:
    """Build the exact MDGZSIM3 + MDGZDEP1 helper invocation.

    This intentionally reuses the production wire decoder through streams.py;
    the helper itself runs inside the owned PX4/Gazebo container.
    """
    runtime = json.loads(Path(plan.runtime).read_text(encoding="utf-8"))
    sensors = runtime.get("sensors", [])
    rgb = next((s for s in sensors if isinstance(s, dict) and s.get("kind") == "rgb_camera"), None)
    depth = next(
        (s for s in sensors if isinstance(s, dict) and s.get("kind") == "depth_camera"), None
    )
    if not rgb:
        raise RuntimeError("scenario has no RGB camera")
    if depth is None and _has_registered_depth_camera(plan.airframe):
        # Every *_mono_cam airframe model carries the registered depth camera, and the
        # frame server requires its contract; fall back to the default binding.
        depth = {}
    world = "default"
    model = _gazebo_model_name(plan, json.loads(Path(plan.runtime).read_text(encoding="utf-8")))
    image_topic, camera_topic = _rgb_camera_topics(
        world=world, model=model, airframe=plan.airframe
    )
    navsat_topic = f"/world/{world}/model/{model}/link/base_link/sensor/navsat_sensor/navsat"
    odometry_topic = f"/model/{model}/odometry_with_covariance"
    command = [
        _docker_executable(plan),
        "exec",
        # -i keeps stdin attached; the frame server reads GET requests from it.
        "-i",
        environment.name,
        "python3",
        "/workspace/scripts/sim_teacher_frame_server.py",
        "--request-timeout-s",
        f"{min(60.0, max(5.0, SENSOR_FRAME_WAIT_S + 2.0)):.3f}",
        "--world",
        world,
        "--model-instance",
        model,
        "--image-topic",
        image_topic,
        "--camera-info-topic",
        camera_topic,
        "--navsat-topic",
        navsat_topic,
        "--odometry-topic",
        odometry_topic,
        "--rgb-size",
        f"{plan.rgb_size[0]}x{plan.rgb_size[1]}",
        "--depth-size",
        f"{plan.depth_size[0]}x{plan.depth_size[1]}",
        "--depth-encoding",
        plan.depth_encoding,
    ]
    profile = _airframe_profile(plan.airframe, plan.takeoff_method)
    category = (
        "multirotor"
        if airframe_uses_multirotor_executor(plan.airframe, plan.takeoff_method, base_dir=ROOT)
        else profile.category if profile is not None else "fixed_wing"
    )
    calibration_name = (
        "gazebo-multirotor-front-camera-v1.json"
        if category == "multirotor"
        else "gazebo-front-camera-v1.json"
        if category == "vtol"
        else "gazebo-fixed-wing-front-camera-v1.json"
    )
    calibration_path = ROOT / "simulation" / "gazebo" / "calibration" / calibration_name
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))["calibration"]
    command.extend(
        [
            "--expected-camera-calibration-json",
            json.dumps(
                {
                    "width_px": calibration["width_px"],
                    "height_px": calibration["height_px"],
                    "fx_px": calibration["fx_px"],
                    "fy_px": calibration["fy_px"],
                    "cx_px": calibration["cx_px"],
                    "cy_px": calibration["cy_px"],
                    "distortion": [calibration[k] for k in ("k1", "k2", "p1", "p2", "k3")],
                },
                separators=(",", ":"),
            ),
        ]
    )
    sensor_topics = _camera_sensor_topics(world=world, model=model, airframe=plan.airframe)
    if plan.segmentation:
        command.extend(["--segmentation", "--segmentation-topic", sensor_topics["segmentation"]])
    if depth is not None:
        depth_topic = sensor_topics["depth_image"]
        depth_document = {
            "sensor_id": depth.get("sensor_id", "downward-depth"),
            "source_topic": depth_topic,
            "calibration_id": depth.get("calibration_id", "gazebo-multirotor-depth-v1"),
            "calibration_sha256": canonical_sha256(
                {
                    "schema_version": 1,
                    "calibration_id": depth.get("calibration_id", "gazebo-multirotor-depth-v1"),
                    "sensor_id": depth.get("sensor_id", "downward-depth"),
                    "width_px": 1280,
                    "height_px": 960,
                    "fx_px": calibration["fx_px"],
                    "fy_px": calibration["fy_px"],
                    "cx_px": calibration["cx_px"],
                    "cy_px": calibration["cy_px"],
                    "distortion": [0.0] * 5,
                    "rectification": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    "extrinsics_rgb_to_depth": {
                        "translation_m": [0.0, 0.0, 0.0],
                        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                    },
                    "registration_mode": "registered_to_rgb",
                    "coordinate_space": "rgb_pixel_aligned",
                    "encoding": "FLOAT32_M",
                    "distance_semantics": "optical_axis_z_m",
                    "near_clip_m": 0.1,
                    "far_clip_m": 3000.0,
                }
            ),
            "registration_mode": "registered_to_rgb",
            "coordinate_space": "rgb_pixel_aligned",
            "encoding": "FLOAT32_M",
            "distance_semantics": "optical_axis_z_m",
            "width": 1280,
            "height": 960,
            "minimum_depth_m": 0.1,
            "maximum_depth_m": 3000.0,
            "fx_px": calibration["fx_px"],
            "fy_px": calibration["fy_px"],
            "cx_px": calibration["cx_px"],
            "cy_px": calibration["cy_px"],
        }
        command.extend(["--depth-contract-json", json.dumps(depth_document, separators=(",", ":"))])
    return command


def _docker_available() -> bool:
    executable = shutil.which("docker")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "version"], capture_output=True, timeout=8, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _px4_host_pids(output: str) -> list[int]:
    pids = []
    for line in output.splitlines()[1:]:
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        process_name = fields[1].strip().split(None, 1)[0].rsplit("/", 1)[-1].lower()
        if process_name == "px4":
            pids.append(int(fields[0]))
    return pids


def _host_load_average() -> list[float] | None:
    try:
        return [round(float(value), 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _host_gpu_memory_mib() -> list[dict[str, int]] | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=index,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    rows = []
    try:
        for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
            if len(row) != 3:
                continue
            index, used, total = (int(value.strip()) for value in row)
            rows.append({"index": index, "used_mib": used, "total_mib": total})
    except ValueError:
        return None
    return rows or None


def _failure_class(reason: str, snapshot: dict[str, Any]) -> str:
    state = snapshot.get("state") or {}
    running = state.get("running")
    exit_code = state.get("exit_code")
    px4_alive = snapshot.get("px4_process_alive")
    if state.get("oom_killed") is True:
        return "px4_crashed"
    if px4_alive is False and (running is True or exit_code not in (None, 0)):
        return "px4_crashed"
    if "clock stalled" in reason.lower() and px4_alive is True:
        return "clock_stalled_alive"
    if px4_alive is True:
        return "px4_alive_other_failure"
    if px4_alive is False:
        return "px4_not_alive"
    return "px4_status_unknown"


class DockerSimEnvironment:
    """Own one simulator container and always remove it on exit."""

    def __init__(self, plan: CollectionPlan, episode_id: str):
        self.plan = plan
        self.name = plan.attach_container or f"{plan.container_prefix}-{episode_id}"
        self.attached = bool(plan.attach_container)
        self.started = False
        self._log = None
        self._last_px4_host_pids: list[int] = []
        self.failure_diagnostics: dict[str, Any] | None = None
        self.cleanup_diagnostics: dict[str, Any] | None = None

    def set_logger(self, logger):
        self._log = logger

    def _progress(self, phase: str, **fields: object) -> None:
        payload = {
            "event": "sim_teacher",
            "phase": phase,
            "monotonic_s": time.monotonic(),
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)

    def _container_snapshot(self, *, include_host_metrics: bool = False) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "captured_wall_time": time.time(),
            "container_name": self.name,
            "container_id": None,
            "state": None,
            "px4_process_alive": None,
            "px4_host_pids": list(self._last_px4_host_pids),
            "px4_host_pids_current": None,
        }
        try:
            inspected = subprocess.run(
                [
                    _docker_executable(self.plan),
                    "inspect",
                    "--format",
                    "{{json .}}",
                    self.name,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if inspected.returncode != 0:
                snapshot["container_inspect_error"] = (
                    inspected.stderr or inspected.stdout or f"docker inspect exited {inspected.returncode}"
                ).strip()
            else:
                details = json.loads(inspected.stdout)
                state = details.get("State") or {}
                snapshot["container_id"] = details.get("Id")
                snapshot["state"] = {
                    "running": state.get("Running"),
                    "exit_code": state.get("ExitCode"),
                    "oom_killed": state.get("OOMKilled"),
                    "finished_at": state.get("FinishedAt"),
                }
                if state.get("Running") is False:
                    snapshot["px4_process_alive"] = False
                    snapshot["px4_host_pids_current"] = []
                elif state.get("Running") is True:
                    top = subprocess.run(
                        [_docker_executable(self.plan), "top", self.name, "-eo", "pid,comm"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    if top.returncode == 0:
                        pids = _px4_host_pids(top.stdout)
                        snapshot["px4_host_pids_current"] = pids
                        snapshot["px4_process_alive"] = bool(pids)
                        if pids:
                            self._last_px4_host_pids = pids
                            snapshot["px4_host_pids"] = list(pids)
                    else:
                        snapshot["px4_process_error"] = (
                            top.stderr or top.stdout or f"docker top exited {top.returncode}"
                        ).strip()
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError) as error:
            snapshot["container_inspect_error"] = str(error)
        if include_host_metrics:
            snapshot["host_load_average"] = _host_load_average()
            snapshot["gpu_memory_mib"] = _host_gpu_memory_mib()
        return snapshot

    def remember_px4_process(self) -> dict[str, Any]:
        snapshot = self._container_snapshot()
        return {
            "container_name": self.name,
            "container_id": snapshot.get("container_id"),
            "px4_host_pids": snapshot.get("px4_host_pids", []),
        }

    def capture_failure_diagnostics(
        self, reason: str, *, px4_clock_diagnostics: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.failure_diagnostics is None:
            snapshot = self._container_snapshot(include_host_metrics=True)
            self.failure_diagnostics = {
                "container_name": self.name,
                "container_id": snapshot.get("container_id"),
                "failure_class": _failure_class(reason, snapshot),
                "reason": reason,
                "failure_state": snapshot,
                "cleanup_state": None,
            }
            if px4_clock_diagnostics is not None:
                self.failure_diagnostics["px4_clock_diagnostics"] = px4_clock_diagnostics
            self._progress(
                "container_failure_snapshot",
                container=self.name,
                failure_class=self.failure_diagnostics["failure_class"],
                container_snapshot=snapshot,
            )
        return self.failure_diagnostics

    def capture_cleanup_diagnostics(self) -> dict[str, Any]:
        self.cleanup_diagnostics = self._container_snapshot()
        if self.failure_diagnostics is not None:
            self.failure_diagnostics["cleanup_state"] = self.cleanup_diagnostics
        return self.cleanup_diagnostics

    def __enter__(self) -> DockerSimEnvironment:
        if self.attached:
            self.wait(self.plan.max_episode_s)
            self._progress("container_attached", container=self.name, image=self.plan.image)
            return self
        result = subprocess.run(
            docker_command(self.plan, name=self.name),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Docker launch failed").strip()
            raise RuntimeError(f"Docker launch failed ({result.returncode}): {detail[:500]}")
        self.started = True
        self._progress("container_up", container=self.name, image=self.plan.image)
        return self

    def wait(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", self.name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip().lower() == "true":
                return
            if result.returncode == 0 and result.stdout.strip().lower() == "false":
                raise RuntimeError(f"SIM container exited before readiness: {self.name}")
            time.sleep(0.25)
        raise TimeoutError(f"SIM container did not become running within {timeout_s:.1f}s")

    def copy_px4_ulogs(self) -> list[str]:
        """Copy every PX4 ``.ulg`` in the container into ``<out>/jobs/<name>/px4-ulog``."""
        executable = _docker_executable(self.plan)
        listing = subprocess.run(
            [executable, "exec", self.name, "find", "/", "-xdev", "-name", "*.ulg", "-type", "f"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        destination = Path(self.plan.out) / "jobs" / self.name / "px4-ulog"
        copied: list[str] = []
        for remote in sorted(line.strip() for line in listing.stdout.splitlines() if line.strip()):
            destination.mkdir(parents=True, exist_ok=True)
            local = destination / remote.strip("/").replace("/", "__")
            result = subprocess.run(
                [executable, "cp", f"{self.name}:{remote}", str(local)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode == 0:
                copied.append(str(local))
        self._progress("px4_ulog_copied", container=self.name, files=copied)
        return copied

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.capture_failure_diagnostics(
                f"{exc_type.__name__}: {exc}",
                px4_clock_diagnostics=getattr(exc, "px4_clock_diagnostics", None),
            )
        if self.started and self.plan.keep_px4_ulog:
            try:
                self.copy_px4_ulogs()
            except (OSError, subprocess.SubprocessError) as error:
                self._progress("px4_ulog_copy_failed", container=self.name, error=str(error))
        if self.started and exc_type is not None:
            # Keep the container's own last words: a PX4 that never sends a
            # heartbeat leaves nothing else behind once the container is removed.
            try:
                logs = subprocess.run(
                    [_docker_executable(self.plan), "logs", "--tail", "120", self.name],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self._progress(
                    "container_logs_on_failure",
                    container=self.name,
                    error=exc_type.__name__,
                    tail=(logs.stdout + logs.stderr)[-12000:],
                )
            except (OSError, subprocess.SubprocessError) as error:
                self._progress("container_logs_failed", container=self.name, error=str(error))
        if self.started and not (
            exc_type is not None
            and self.plan.keep_container_on_failure
            and exc_type is not LowDiskSpace
        ):
            cleanup_snapshot = self.capture_cleanup_diagnostics()
            subprocess.run(
                ["docker", "rm", "--force", self.name],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.started = False
            self._progress(
                "cleanup", container=self.name, removed=True, container_snapshot=cleanup_snapshot
            )
        elif self.started:
            cleanup_snapshot = self.capture_cleanup_diagnostics()
            self._progress(
                "cleanup",
                container=self.name,
                removed=False,
                kept=True,
                container_snapshot=cleanup_snapshot,
            )
        elif self.attached:
            cleanup_snapshot = self.capture_cleanup_diagnostics()
            self._progress(
                "cleanup", container=self.name, attached=True, container_snapshot=cleanup_snapshot
            )
        return False


def _mover_truth(truth_path: Path) -> dict[str, Any] | None:
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    target = next((item for item in data.get("targets", ()) if item.get("true_target")), None)
    return (
        target if isinstance(target, dict) and isinstance(target.get("trajectory"), dict) else None
    )


def _start_mover_driver(
    plan: CollectionPlan, environment: DockerSimEnvironment, truth_path: Path
) -> dict[str, Any] | None:
    target = _mover_truth(truth_path)
    if target is None:
        return None
    scenario_id = str(json.loads(truth_path.read_text(encoding="utf-8")).get("scenario_id", ""))
    model = f"target_true_{scenario_id}"
    trajectory_json = json.dumps(target["trajectory"], separators=(",", ":"))
    command = [
        _docker_executable(plan),
        "exec",
        "-d",
        environment.name,
        "python3",
        f"{CONTAINER_SCRIPT_ROOT}/gz_mover_driver.py",
        "--world",
        "default",
        "--model",
        model,
        "--trajectory-json",
        trajectory_json,
        "--z",
        str(float(target.get("pose", {}).get("z_up_m", 0.0))),
        "--period-s",
        "0.1",
        "--log",
        "/tmp/mover_driver.log",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"mover driver failed to start: {(result.stderr or result.stdout)[-500:]}"
        )
    return {"command": command, "model": model, "remote_log": "/tmp/mover_driver.log"}


def _copy_mover_driver_log(
    plan: CollectionPlan,
    environment: DockerSimEnvironment,
    recorder: EpisodeRecorder,
    driver: dict[str, Any] | None,
) -> None:
    if driver is None:
        return
    destination = recorder.episode_dir / "mover_driver.log"
    result = subprocess.run(
        [
            _docker_executable(plan),
            "cp",
            f"{environment.name}:/tmp/mover_driver.log",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to copy mover driver log: {(result.stderr or result.stdout)[-500:]}"
        )
    rows = []
    for line in destination.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    successes = [row for row in rows if row.get("ok")]
    early = [row for row in successes if float(row.get("sim_time_s", 1e9)) <= 10.0]
    if not early:
        raise RuntimeError(
            "mover driver produced no successful set_pose within first 10 sim seconds"
        )
    latencies = [float(row["wall_latency_s"]) for row in successes if "wall_latency_s" in row]
    driver.update(
        {
            "log": str(destination.relative_to(recorder.episode_dir)),
            "update_count": len(rows),
            "successful_update_count": len(successes),
            "median_set_pose_wall_latency_s": statistics.median(latencies) if latencies else None,
            "max_set_pose_wall_latency_s": max(latencies, default=None),
            "max_sim_time_s": max(
                (float(row.get("sim_time_s", 0.0)) for row in rows), default=None
            ),
            "max_apply_sim_time_lag_s": None,
        }
    )


def _wait_for_mover_driver_start(
    plan: CollectionPlan,
    environment: DockerSimEnvironment,
    driver: dict[str, Any] | None,
    timeout_s: float = 90.0,
) -> None:
    if driver is None:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = subprocess.run(
            [_docker_executable(plan), "exec", environment.name, "cat", driver["remote_log"]],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        rows = []
        for line in (result.stdout or "").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        successful = [row for row in rows if row.get("ok")]
        if any(float(row.get("sim_time_s", 0.0)) >= 10.0 for row in rows):
            if not any(float(row.get("sim_time_s", 1e9)) <= 10.0 for row in successful):
                raise RuntimeError(
                    "mover driver has no successful set_pose in first 10 sim seconds"
                )
            return
        time.sleep(0.5)
    raise TimeoutError("mover driver did not reach 10 sim seconds before timeout")


def gps_to_local_ned(
    latitude_deg: float,
    longitude_deg: float,
    altitude_m: float,
    *,
    origin_latitude_deg: float,
    origin_longitude_deg: float,
    origin_altitude_m: float = 0.0,
) -> tuple[float, float, float]:
    """Convert WGS-84 truth coordinates to the PX4 local NED tangent frame."""
    earth_radius_m = 6_378_137.0
    lat0 = math.radians(origin_latitude_deg)
    north = math.radians(latitude_deg - origin_latitude_deg) * earth_radius_m
    east = math.radians(longitude_deg - origin_longitude_deg) * earth_radius_m * math.cos(lat0)
    down = -(float(altitude_m) - float(origin_altitude_m))
    return north, east, down


def _scenario_origin(plan: CollectionPlan) -> tuple[float, float, float]:
    """Read the WGS-84 origin from the scenario-owned world SDF."""
    runtime = json.loads(Path(plan.runtime).read_text(encoding="utf-8"))
    scenario_id = str(runtime.get("scenario_id", "urban_inspection_multirotor_001"))
    world_path = ROOT / "simulation" / "gazebo" / "worlds" / f"{scenario_id}.sdf"
    if not world_path.is_file():
        world_path = ROOT / "simulation" / "gazebo" / "worlds" / "variants" / f"{scenario_id}.sdf"
    if not world_path.is_file():
        return 1.352100, 103.819800, 18.0
    root = ET.fromstring(world_path.read_text(encoding="utf-8"))
    spherical = root.find(".//spherical_coordinates")
    if spherical is None:
        raise ValueError(f"world has no spherical_coordinates: {world_path}")

    def value(name: str) -> float:
        node = spherical.find(name)
        if node is None or node.text is None:
            raise ValueError(f"world origin is missing {name}: {world_path}")
        return float(node.text)

    return value("latitude_deg"), value("longitude_deg"), value("elevation")


def scenario_world_path(runtime_path: str | Path) -> Path | None:
    """Resolve the scenario-owned SDF, returning ``None`` for unknown worlds."""
    try:
        runtime = json.loads(Path(runtime_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    scenario_id = str(runtime.get("scenario_id", "")) if isinstance(runtime, dict) else ""
    if not scenario_id:
        return None
    world_path = ROOT / "simulation" / "gazebo" / "worlds" / f"{scenario_id}.sdf"
    if world_path.is_file():
        return world_path
    variant_path = ROOT / "simulation" / "gazebo" / "worlds" / "variants" / f"{scenario_id}.sdf"
    return variant_path if variant_path.is_file() else None


def auto_takeoff_altitude_m(world_path: str | Path | None) -> tuple[float, str]:
    """Return ``(altitude, source)`` from a generated world's obstacle tops.

    Generated worlds are the only worlds with the obstacle layout contract;
    ordinary fixed worlds therefore retain the historical 5 m default.  SDF
    parser failures also fall back safely instead of preventing a dry run.
    """
    if world_path is None:
        return DEFAULT_TAKEOFF_ALTITUDE_M, "default_non_generated_world"
    path = Path(world_path)
    if not path.is_file() or not path.stem.startswith(
        (
            "gen_w",
            "gen_v2_w",
            "gen_v2a_w",
            "gen_cnt_w",
            "gen_cnt2_w",
            "gen_cfs_w",
            "gen_cfs2_w",
            "gen_mov_w",
        )
    ):
        return DEFAULT_TAKEOFF_ALTITUDE_M, "default_non_generated_world"
    obstacles = load_sdf_obstacles(path)
    if not obstacles:
        return DEFAULT_TAKEOFF_ALTITUDE_M, "default_generated_world_no_obstacles"
    top = max(float(obstacle.top_m) for obstacle in obstacles)
    altitude = min(
        MAX_TAKEOFF_ALTITUDE_M,
        max(DEFAULT_TAKEOFF_ALTITUDE_M, top + TAKEOFF_SAFETY_MARGIN_M),
    )
    return altitude, "auto_max_obstacle_top_plus_4m"


def resolve_takeoff_altitude(
    runtime_path: str | Path,
    explicit_altitude_m: float | None = None,
) -> tuple[float, str]:
    """Resolve an explicit or scenario-derived takeoff altitude."""
    if explicit_altitude_m is not None:
        altitude = float(explicit_altitude_m)
        if not math.isfinite(altitude) or altitude < DEFAULT_TAKEOFF_ALTITUDE_M:
            raise ValueError(
                f"takeoff altitude must be finite and >= {DEFAULT_TAKEOFF_ALTITUDE_M:g} m"
            )
        if altitude > MAX_TAKEOFF_ALTITUDE_M:
            raise ValueError(f"takeoff altitude must be <= {MAX_TAKEOFF_ALTITUDE_M:g} m")
        return altitude, "explicit_cli"
    return auto_takeoff_altitude_m(scenario_world_path(runtime_path))


def _takeoff_altitude_for_plan(plan: CollectionPlan) -> tuple[float, str]:
    altitude, source = resolve_takeoff_altitude(plan.runtime, plan.takeoff_altitude_m)
    # Generated teacher intent is the authoritative camera geometry.  Resolve it
    # before launch so PX4 takeoff and episode metadata agree with the route.
    if plan.teacher == "expert_search" and plan.takeoff_altitude_m is None:
        try:
            sidecar = _teacher_intent_path(Path(plan.scenario))
            data = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
            if not data:
                runtime_data = json.loads(Path(plan.runtime).read_text(encoding="utf-8"))
                data = runtime_data.get("task_intent", {}) if isinstance(runtime_data, dict) else {}
            observation = (
                data.get("confuser_observation", data.get("observation", {}))
                if isinstance(data, dict) and plan.teacher_target == "confuser"
                else data.get("observation", {})
                if isinstance(data, dict)
                else {}
            )
            if isinstance(observation, dict):
                requested = teacher_intent_clearance_altitude(observation)
            else:
                requested = None
            if requested is not None:
                altitude, source = requested, "teacher_intent_camera_altitude"
        except (OSError, json.JSONDecodeError):
            pass
    profile = (
        _airframe_profile(plan.airframe, plan.takeoff_method)
        if not airframe_uses_multirotor_executor(
            plan.airframe, plan.takeoff_method, base_dir=ROOT
        )
        else None
    )
    if profile is None or profile.category == "multirotor":
        return altitude, source
    # The multirotor default (5 m) is far below what a fixed-wing needs to loiter
    # or a VTOL needs to transition: a tiltrotor transitioning at 5 m sank into the
    # ground.  Keep at least FIXED_WING_MIN_OPERATING_ALT_M, and for VTOL at least
    # the profile's minimum transition height plus a margin.
    floor = FIXED_WING_MIN_OPERATING_ALT_M
    if profile.category == "vtol":
        floor = max(floor, profile.transition.minimum_height_m + VTOL_TRANSITION_MARGIN_M)
    if altitude < floor:
        return floor, f"{source}; raised to the {profile.category} operating floor"
    return altitude, source


def _validate_spawn_clearance(plan: CollectionPlan) -> dict[str, Any] | None:
    """Run the generated-world launch clearance check before any simulator work."""
    world_path = scenario_world_path(plan.runtime)
    if world_path is None or not world_path.stem.startswith(("gen_r20_", "gen_r21id_")):
        return None
    runtime = json.loads(Path(plan.runtime).read_text(encoding="utf-8"))
    vehicle = runtime.get("vehicle", {})
    spawn = vehicle.get("spawn_pose") if isinstance(vehicle, dict) else None
    if not isinstance(spawn, dict):
        raise ValueError("generated r20/r21id runtime is missing vehicle.spawn_pose")
    altitude_m, _ = _takeoff_altitude_for_plan(plan)
    route_points = []
    try:
        intent_path = _teacher_intent_path(Path(plan.scenario))
        intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.is_file() else {}
        if isinstance(intent, dict):
            observation = (
                intent.get("confuser_observation", intent.get("observation", {}))
                if plan.teacher_target == "confuser"
                else intent.get("observation", {})
            )
        else:
            observation = {}
        route_points = teacher_intent_clearance_route_points(observation)
    except (OSError, ValueError, TypeError):
        route_points = []
    return require_spawn_clearance(
        world_path, spawn, climb_altitude_m=altitude_m, route_points=route_points
    )


def _truth_target(
    truth_path: Path,
    origin: tuple[float, float, float] = (1.352100, 103.819800, 18.0),
    *,
    select: str = "true",
    seed: int = 0,
) -> tuple[tuple[float, float, float], str]:
    """Where the expert teacher flies: the true target, or (``select="confuser"``)
    one of the look-alike confusers placed next to it, picked by ``seed``.

    Confuser flights keep the episode's task text (find the true target), so the
    frames become hard negatives: a similar object seen up close that is not it.
    """
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    targets = data.get("targets", []) if isinstance(data, dict) else []
    target = targets[0] if isinstance(targets, list) and targets else {}
    if select == "confuser":
        # Placed look-alikes first (confuser_*), then the forced distractors some
        # worlds use for the same purpose (SUV next to a pickup), then any distractor.
        confusers = []
        for prefix in ("confuser_", "forced_", "distractor_"):
            confusers = [
                t
                for t in targets
                if isinstance(t, dict)
                and not t.get("true_target")
                and str(t.get("target_id", "")).startswith(prefix)
            ]
            if confusers:
                break
        if not confusers:
            raise ValueError(f"{truth_path.name} has no confuser to fly to")
        target = confusers[seed % len(confusers)]
    elif select != "true":
        raise ValueError(f"unknown teacher target: {select}")
    label = str(target.get("label", "target")) if isinstance(target, dict) else "target"
    position = target.get("position", {}) if isinstance(target, dict) else {}
    if not isinstance(position, dict):
        return (20.0, 0.0, 0.0), label
    if "north_m" in position or "east_m" in position:
        north = float(position.get("north_m", 20.0))
        east = float(position.get("east_m", 0.0))
        up = float(position.get("up_m", 0.0))
        return (north, east, -up), label
    lat0, lon0, alt0 = origin
    lat = float(position.get("latitude_deg", lat0))
    lon = float(position.get("longitude_deg", lon0))
    raw_alt = position.get("altitude_m")
    reference = str(position.get("altitude_reference", "amsl")).lower()
    if raw_alt is None:
        alt = alt0
    elif reference in {"world", "local", "relative", "agl"}:
        # Generated worlds give altitude relative to the world origin, not AMSL.
        alt = alt0 + float(raw_alt)
    else:
        alt = float(raw_alt)
    return gps_to_local_ned(
        lat,
        lon,
        alt,
        origin_latitude_deg=lat0,
        origin_longitude_deg=lon0,
        origin_altitude_m=alt0,
    ), label


def _camera_info_for(plan: CollectionPlan) -> dict[str, Any]:
    return {
        **DEFAULT_CAMERA_INFO,
        "width": plan.rgb_size[0],
        "height": plan.rgb_size[1],
        "fx": DEFAULT_CAMERA_INFO["fx"] * plan.rgb_size[0] / 1280.0,
        "fy": DEFAULT_CAMERA_INFO["fy"] * plan.rgb_size[1] / 960.0,
        "cx": DEFAULT_CAMERA_INFO["cx"] * plan.rgb_size[0] / 1280.0,
        "cy": DEFAULT_CAMERA_INFO["cy"] * plan.rgb_size[1] / 960.0,
    }


def _onboard_policy(plan: CollectionPlan, task_text: str, work_dir: Path):
    """Truth-free perception policy; it receives only task, map, camera and pose."""
    from scripts.onboard_policy import build_onboard_policy

    altitude_m, _ = _takeoff_altitude_for_plan(plan)
    policy = build_onboard_policy(
        task_text=task_text,
        world_path=scenario_world_path(plan.runtime),
        altitude_m=altitude_m,
        camera_info=_camera_info_for(plan),
        image_size=plan.rgb_size,
        work_dir=work_dir,
        config_path=Path(plan.onboard_config) if plan.onboard_config else None,
        frame_format=plan.onboard_frame_format,
        frame_quality=plan.onboard_frame_quality,
        airframe=_onboard_airframe_id(plan),
        runtime_step_limit=plan.steps_per_episode,
    )
    _start_sim_operator_link(plan, policy)
    return policy


class _SimHoverVehicle:
    """Preflight view for a SIM operator link.

    The collector takes off before the policy runs, so a package arrives while
    the vehicle already hovers; the policy still holds until the plan is
    approved.  The real runtime (scripts/onboard_main.py) keeps the landed and
    disarmed preflight gate.
    """

    @staticmethod
    def snapshot():
        return types.SimpleNamespace(armed=False, landed=True)


def _start_sim_operator_link(plan: CollectionPlan, policy) -> None:
    """With ``mission: operator_link`` the SIM policy takes its mission from a GCS."""
    if not plan.onboard_config:
        return
    config = json.loads(Path(plan.onboard_config).read_text(encoding="utf-8"))
    if config.get("mission") != "operator_link":
        return
    from scripts.onboard_main import _start_operator_link

    args = types.SimpleNamespace(
        operator_bind_host=str(config.get("operator_bind_host", "127.0.0.1")),
        operator_port=int(config.get("operator_port", plan.operator_port)),
        operator_hmac_key_hex=config.get("operator_hmac_key_hex"),
        operator_mavlink_signing_key_hex=config.get("operator_mavlink_signing_key_hex"),
        operator_requires_approval=bool(config.get("operator_requires_approval", True)),
        operator_package_verify_key=config.get("operator_package_verify_key"),
        operator_package_key_hex=config.get("operator_package_key_hex"),
    )
    prepare = getattr(policy, "prepare_operator_package", None)

    def logged_prepare(package):
        # Planning a prompt-only package calls the slow model on the link's
        # planner worker; log each stage so a silent link can be diagnosed.
        _progress("operator_package_received", mission_id=package.mission_id)
        try:
            supervisor = prepare(package)
        except Exception as error:
            _progress("operator_package_rejected", error=f"{type(error).__name__}: {error}")
            raise
        _progress(
            "operator_plan_compiled",
            steps=[step.skill for step in supervisor.executor.plan.steps],
            plan_sha256=supervisor.plan_sha256,
        )
        return supervisor

    if prepare is not None:
        policy.prepare_operator_package = logged_prepare
    server = _start_operator_link(args, policy, _SimHoverVehicle())
    policy.sim_operator_server = server
    _progress("operator_link_listening", host=args.operator_bind_host, port=args.operator_port)

    def report_link_errors() -> None:
        errors = getattr(server, "_background_errors", None)
        stopped = getattr(server, "_stop_event", None)
        while errors is not None and not (stopped is not None and stopped.is_set()):
            try:
                error = errors.get(timeout=1.0)
            except Exception:
                continue
            _progress("operator_link_error", error=f"{type(error).__name__}: {error}")

    threading.Thread(target=report_link_errors, name="sim-operator-errors", daemon=True).start()
    if os.environ.get("MULTIDETECT_SIM_LINK_DEBUG"):
        _trace_operator_link(server)


def _drain_sim_operator_link(policy, timeout_s: float = 30.0) -> None:
    """Let the GCS receive the last findings and the final summary before exit.

    Call before ``policy.close()``: that closes the downlink queue, after which
    nothing more is sent (SITL e2e: the drain then waited 30 s in vain and the
    final summary never reached the GCS).  The host stop is a terminal path, so
    the supervisor's one final summary is written here first when the mission
    has not produced it yet.  The operator server is closed on every path.
    """
    server = getattr(policy, "sim_operator_server", None)
    if server is None:
        return
    try:
        supervisor = getattr(policy, "supervisor", None)
        downlink = getattr(supervisor, "downlink", None)
        if supervisor is None or downlink is None:
            return
        try:
            supervisor.finish("stopped")
        except Exception as error:
            _progress("operator_link_finish_failed", error=f"{type(error).__name__}: {error}")
        log = supervisor.log
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and downlink.acknowledged_sequence < log._sequence:
            time.sleep(0.2)
        _progress(
            "operator_link_drained",
            acknowledged=downlink.acknowledged_sequence,
            logged=log._sequence,
        )
    finally:
        policy.sim_operator_server = None
        try:
            server.close()
        except Exception:
            pass


def _trace_operator_link(server) -> None:
    """Debug-only: log the link filters that drop mission packets without an ACK."""
    adapter = server.mission_adapter
    handle, allow, replay = adapter.handle, server._allow_command, server._replay_state

    def traced_handle(packet, *, peer=None):
        kind = int(packet.message_type)
        body = packet.message.body
        index = int.from_bytes(body[8:10], "big") if kind in (28, 37) and len(body) > 10 else -1
        _progress(
            "operator_link_trace", stage="adapter", kind=kind, index=index, seq=packet.sequence
        )
        return handle(packet, peer=peer)

    def traced_allow(peer, message_type, *, now_s):
        allowed = allow(peer, message_type, now_s=now_s)
        if not allowed:
            _progress("operator_link_trace", stage="rate_limited", kind=int(message_type))
        return allowed

    original_accept = replay.accept_sequence

    def traced_accept(key, sequence, **kwargs):
        accepted = original_accept(key, sequence, **kwargs)
        if not accepted:
            _progress("operator_link_trace", stage="replay_rejected", key=key, seq=sequence)
        return accepted

    adapter.handle = traced_handle
    server._allow_command = traced_allow
    replay.accept_sequence = traced_accept


def _onboard_airframe_id(plan: CollectionPlan) -> str | None:
    """Profile id the onboard policy plans for; None keeps the x500 default untouched."""
    if plan.airframe == "x500_mono_cam":
        return None
    profile = _airframe_profile(plan.airframe, plan.takeoff_method)
    return profile.id if profile is not None else None


def _is_motion_command(action: Any) -> bool:
    return type(action).__name__ == "MotionCommand" and hasattr(action, "kind")


def _wait_motion_command(
    command: Any, reader: MavlinkReader, *, cruise_mps: float
) -> dict[str, Any]:
    """Block like a primitive would: until a reposition arrives or a timed loiter ends.

    Guided fixed-wing commands return on PX4's ACK; without this the onboard policy
    would issue a new command every frame while the aircraft is still turning.
    """
    started = time.monotonic()
    kind = str(command.kind)
    if kind in {"mc_reposition", "mc_hold"} and command.target_ned is not None:
        # A hovering VTOL stops on its target like a multirotor: the next frame must
        # come from the settled pose and heading, not from mid-leg or mid-turn.
        try:
            start = _state_position(reader.state)
        except RuntimeError:
            return {"waited": "no_position", "elapsed_s": 0.0}
        leg = math.hypot(command.target_ned[0] - start[0], command.target_ned[1] - start[1])
        timeout = min(MOTION_REPOSITION_TIMEOUT_S, leg / max(MC_SPEED_FLOOR_MPS, 1.0) * 1.5 + 20.0)
        if kind == "mc_hold":
            timeout = max(timeout, min(float(command.duration_s or 0.0), MOTION_LOITER_MAX_WAIT_S))
        distance = yaw_error = speed = float("inf")
        settled_since: float | None = None
        # Timed on PX4's lockstep clock: on a loaded host the simulation runs well
        # below real time and a wall-clock budget would cut turns short.
        sim_clock = _SimClock(reader)
        sim_started = sim_clock()
        while (
            sim_clock() - sim_started < timeout
            and time.monotonic() - started < PRIMITIVE_MAX_WALL_S
        ):
            reader.raise_if_failed()
            pose = _state_position(reader.state)
            distance = math.hypot(command.target_ned[0] - pose[0], command.target_ned[1] - pose[1])
            yaw_error = (
                abs((float(command.yaw_deg) - _state_yaw(reader.state) + 180.0) % 360.0 - 180.0)
                if command.yaw_deg is not None
                else 0.0
            )
            speed = _state_speed(reader.state)
            settled = (
                distance <= MC_ARRIVAL_TOLERANCE_M
                and yaw_error <= MC_YAW_TOLERANCE_DEG
                and speed <= MC_SETTLED_SPEED_MPS
            )
            now = sim_clock()
            settled_since = (settled_since or now) if settled else None
            held = now - sim_started >= float(command.duration_s or 0.0)
            if settled_since is not None and now - settled_since >= MC_SETTLE_S and held:
                return {
                    "waited": "settled",
                    "distance_m": distance,
                    "yaw_error_deg": yaw_error,
                    "leg_m": leg,
                    "elapsed_s": time.monotonic() - started,
                    "sim_elapsed_s": now - sim_started,
                }
            time.sleep(0.1)
        return {
            "waited": "timeout",
            "distance_m": distance,
            "yaw_error_deg": yaw_error,
            "speed_mps": speed,
            "leg_m": leg,
            "elapsed_s": time.monotonic() - started,
        }
    if kind == "transition":
        target_states = {"MC": {3}, "FW": {4}}.get(str(command.mode or "FW").upper(), {4})
        sim_clock = _SimClock(reader)
        sim_started = sim_clock()
        while (
            sim_clock() - sim_started < MOTION_TRANSITION_TIMEOUT_S
            and time.monotonic() - started < PRIMITIVE_MAX_WALL_S
        ):
            reader.raise_if_failed()
            if _flight_state_snapshot(reader.state)["vtol_state"] in target_states:
                return {"waited": "transitioned", "elapsed_s": time.monotonic() - started}
            time.sleep(0.1)
        return {"waited": "timeout", "elapsed_s": time.monotonic() - started}
    if kind in {"reposition", "mc_reposition"} and command.target_ned is not None:
        try:
            start = _state_position(reader.state)
        except RuntimeError:
            return {"waited": "no_position", "elapsed_s": 0.0}
        leg_n, leg_e = command.target_ned[0] - start[0], command.target_ned[1] - start[1]
        leg = math.hypot(leg_n, leg_e)
        timeout = min(MOTION_REPOSITION_TIMEOUT_S, leg / max(cruise_mps, 1.0) * 1.5 + 15.0)
        distance = leg
        orbit_angle = 0.0
        previous_yaw: float | None = None
        while time.monotonic() - started < timeout:
            reader.raise_if_failed()
            pose = _state_position(reader.state)
            dn, de = command.target_ned[0] - pose[0], command.target_ned[1] - pose[1]
            distance = math.hypot(dn, de)
            current_yaw = _state_yaw(reader.state)
            if previous_yaw is not None:
                orbit_angle += abs((current_yaw - previous_yaw + 180.0) % 360.0 - 180.0)
            previous_yaw = current_yaw
            # A plane passes its target rather than stopping on it: the leg is done
            # once little of it remains along the original track (or it is close).
            if _fixed_wing_reposition_complete(
                dn=dn,
                de=de,
                leg_n=leg_n,
                leg_e=leg_e,
                distance_m=distance,
                loiter_radius_m=float(command.radius_m or FIXED_WING_LOITER_CAPTURE_M),
                yaw_deg=current_yaw,
                centre_n=command.target_ned[0],
                centre_e=command.target_ned[1],
                angle_deg=orbit_angle,
            ):
                return {
                    "waited": "arrived",
                    "distance_m": distance,
                    "leg_m": leg,
                    "elapsed_s": time.monotonic() - started,
                }
            time.sleep(0.1)
        return {
            "waited": "timeout",
            "distance_m": distance,
            "leg_m": leg,
            "elapsed_s": time.monotonic() - started,
        }
    if kind in {"loiter", "orbit", "mc_hold"} and float(command.duration_s or 0.0) > 0.0:
        hold = min(float(command.duration_s), MOTION_LOITER_MAX_WAIT_S)
        while time.monotonic() - started < hold:
            reader.raise_if_failed()
            time.sleep(0.2)
        return {"waited": "duration", "elapsed_s": time.monotonic() - started}
    return {"waited": "none", "elapsed_s": 0.0}


def _fixed_wing_reposition_complete(
    *,
    dn: float,
    de: float,
    leg_n: float,
    leg_e: float,
    distance_m: float,
    loiter_radius_m: float | None = None,
    yaw_deg: float | None = None,
    centre_n: float | None = None,
    centre_e: float | None = None,
    angle_deg: float = 0.0,
) -> bool:
    """Return whether a fixed-wing leg has actually reached or passed its target.

    PX4 fixed-wing guidance normally flies through a reposition waypoint, so a
    direct ``distance`` check is too strict.  The old projection-only check was
    too permissive: any crossing of the target's perpendicular plane counted,
    even with a large cross-track error.  Require either a close arrival or a
    bounded cross-track pass through the target plane.
    """
    if distance_m <= MOTION_ARRIVAL_TOLERANCE_M:
        return True
    # A fixed-wing reposition is captured when PX4 enters its loiter ring;
    # unlike a multirotor it cannot stop at the centre.  The Cessna's deployed
    # NAV_LOITER_RAD is 80 m, so a stable 81--84 m ring is a valid completion.
    if (
        loiter_radius_m is not None
        and yaw_deg is not None
        and centre_n is not None
        and centre_e is not None
        and angle_deg >= 90.0
        and MOTION_ARRIVAL_TOLERANCE_M < distance_m <= max(
        MOTION_ARRIVAL_TOLERANCE_M, float(loiter_radius_m) + 5.0
        )
        and distance_m >= max(0.0, float(loiter_radius_m) - 12.0)
        and _fixed_wing_tangent_error_deg(dn, de, yaw_deg) <= 35.0
    ):
        return True
    leg = math.hypot(leg_n, leg_e)
    if leg <= 1e-6:
        return False
    along = (dn * leg_n + de * leg_e) / leg
    cross = abs(leg_n * de - leg_e * dn) / leg
    return (
        along <= MOTION_ALONG_TRACK_DONE_M
        and cross <= MOTION_CROSS_TRACK_DONE_M
    )


def _fixed_wing_tangent_error_deg(dn: float, de: float, yaw_deg: float) -> float:
    radial_bearing = math.degrees(math.atan2(-de, -dn))
    tangent_a = radial_bearing + 90.0
    tangent_b = radial_bearing - 90.0
    return min(
        abs((yaw_deg - tangent_a + 180.0) % 360.0 - 180.0),
        abs((yaw_deg - tangent_b + 180.0) % 360.0 - 180.0),
    )


def _apply_motion_wait_result(executed: Any, motion_wait: Mapping[str, Any]) -> Any:
    """Fold telemetry completion into the primitive result recorded by a step."""
    if motion_wait.get("waited") != "timeout" or getattr(executed, "status", None) != "success":
        return executed
    return dataclasses.replace(
        executed,
        status="timeout",
        elapsed_s=float(executed.elapsed_s) + float(motion_wait.get("elapsed_s", 0.0)),
    )


def _policy_for(
    plan: CollectionPlan,
    truth_path: Path,
    seed: int,
    *,
    airframe_profile: AirframeProfile | None = None,
):
    if plan.teacher == "explore":
        return ExplorePolicy(seed=seed, bounds=ExploreBounds())
    target, label = _truth_target(
        truth_path, _scenario_origin(plan), select=plan.teacher_target, seed=seed
    )
    altitude_m, _ = _takeoff_altitude_for_plan(plan)
    obstacles = load_sdf_obstacles(scenario_world_path(plan.runtime) or "")
    # Generated campaigns may encode observation geometry as mission intent.
    # Feed it into the expert's actual route and camera model, never vehicle spawn.
    intent: dict[str, Any] = {}
    try:
        intent_path = _teacher_intent_path(Path(plan.scenario))
        task_intent = (
            json.loads(intent_path.read_text(encoding="utf-8"))
            if intent_path.is_file()
            else json.loads(Path(plan.runtime).read_text(encoding="utf-8")).get("task_intent", {})
        )
        if isinstance(task_intent, dict):
            observation = (
                task_intent.get("confuser_observation", task_intent.get("observation", {}))
                if plan.teacher_target == "confuser"
                else task_intent.get("observation", {})
            )
        else:
            observation = {}
        if isinstance(observation, dict):
            intent = observation
    except (OSError, ValueError):
        pass
    if intent.get("camera_altitude_m") is not None:
        requested_altitude = float(intent["camera_altitude_m"])
        if not math.isfinite(requested_altitude) or requested_altitude <= 0.0:
            raise ValueError("task_intent.observation.camera_altitude_m must be finite and positive")
        altitude_m = requested_altitude
    waypoint = intent.get("observation_waypoint", {})
    home_ned = None
    if isinstance(waypoint, dict) and ("x_east_m" in waypoint or "y_north_m" in waypoint):
        home_ned = (
            float(waypoint.get("y_north_m", 0.0)),
            float(waypoint.get("x_east_m", 0.0)),
            -float(waypoint.get("z_up_m", 0.0)),
        )
    camera_pitch_intent = float(intent.get("camera_pitch_rad", -0.18))
    if not math.isfinite(camera_pitch_intent):
        raise ValueError("task_intent.observation.camera_pitch_rad must be finite")
    # Sidecars use aircraft convention (negative nose-down); FOV projection
    # uses positive optical-axis pitch-down in NED.
    camera_pitch = -camera_pitch_intent
    frames = int(intent.get("observation_frames", intent.get("min_observation_frames", 8)))
    if frames < 1:
        raise ValueError("task_intent.observation observation_frames must be positive")
    max_detection_range = intent.get("max_detection_range_m", 80.0)
    max_detection_range = float(max_detection_range)
    if not math.isfinite(max_detection_range) or max_detection_range <= 0.0:
        raise ValueError("task_intent.observation.max_detection_range_m must be finite and positive")
    view_rows = intent.get("views", intent.get("observation_views", ()))
    observation_waypoints = []
    observation_view_names = []
    if isinstance(view_rows, list) and view_rows:
        for row in view_rows:
            if not isinstance(row, dict):
                raise ValueError("task_intent.observation.views entries must be objects")
            view_waypoint = row.get("observation_waypoint", row.get("waypoint", {}))
            if not isinstance(view_waypoint, dict):
                raise ValueError("task_intent.observation.views waypoint must be an object")
            observation_waypoints.append((
                float(view_waypoint.get("y_north_m", 0.0)),
                float(view_waypoint.get("x_east_m", 0.0)),
                -float(view_waypoint.get("z_up_m", 0.0) or altitude_m),
            ))
            observation_view_names.append(str(row.get("view", len(observation_view_names))))
    elif home_ned is not None:
        observation_waypoints.append((home_ned[0], home_ned[1], -altitude_m))
    profile = airframe_profile or _airframe_profile(plan.airframe, plan.takeoff_method)
    camera_mount = (
        profile.camera_mount.to_camera_mount()
        if profile is not None and profile.camera_mount is not None
        else None
    )
    return ExpertSearchPolicy(
        target_ned=target,
        intrinsics=DEFAULT_CAMERA_INFO,
        altitude_m=altitude_m,
        standoff_m=float(intent.get("standoff_m", min(15.0, max_detection_range * 0.5))),
        camera_pitch_rad=camera_pitch,
        camera_mount=camera_mount,
        max_detection_range_m=max_detection_range,
        **({"home_ned": home_ned} if home_ned is not None else {}),
        obstacles=obstacles,
        observation_frames=frames if observation_waypoints else 0,
        observation_waypoints=tuple(observation_waypoints),
        observation_view_names=tuple(observation_view_names),
        fixed_wing_orbit_radius_m=(
            profile.orbit_radius(80.0)
            if profile is not None and profile.category == "fixed_wing"
            else None
        ),
    ), label


def _hover_policy(plan: CollectionPlan, truth_path: Path):
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    target = next(item for item in data.get("targets", []) if item.get("true_target"))
    pose = target.get("pose") or {}
    return HoverPolicy(
        (
            float(pose["y_north_m"]),
            float(pose["x_east_m"]),
            -float(pose.get("z_up_m", 0.0)),
        ),
        trajectory=target.get("trajectory"),
    )


class _SmokeFlightPolicy:
    """Deterministic fixed-wing route used by the real-airframe smoke script."""

    def __init__(self) -> None:
        self._actions = iter(
            (
                ActionPrimitive.translate(35.0, 0.0),
                ActionPrimitive.hold(8.0),
                ActionPrimitive.translate(0.0, 35.0),
                ActionPrimitive.turn(90.0),
                ActionPrimitive.translate(35.0, 0.0),
                ActionPrimitive.hold(8.0),
                ActionPrimitive.stop(),
            )
        )

    def next_action(self):
        try:
            return next(self._actions)
        except StopIteration:
            return ActionPrimitive.stop()

    def close(self) -> None:
        return None


def _field(message: Any, name: str, default: object = None) -> object:
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _state_position(state: Any) -> tuple[float, float, float]:
    msg = state.get("LOCAL_POSITION_NED")
    if msg is None:
        raise RuntimeError("PX4 LOCAL_POSITION_NED is not ready")
    return tuple(float(_field(msg, key, 0.0)) for key in ("x", "y", "z"))


def _state_yaw(state: Any) -> float:
    msg = state.get("ATTITUDE")
    if msg is None:
        raise RuntimeError("PX4 ATTITUDE is not ready")
    return math.degrees(float(_field(msg, "yaw", 0.0)))


def _state_speed(state: Any) -> float:
    msg = state.get("LOCAL_POSITION_NED")
    if msg is None:
        return float("inf")
    return math.sqrt(sum(float(_field(msg, key, 0.0)) ** 2 for key in ("vx", "vy", "vz")))


def _flight_state_snapshot(state: Any) -> dict[str, Any]:
    """Copy airframe state fields into progress/episode records."""
    extended = state.get("EXTENDED_SYS_STATE")
    hud = state.get("VFR_HUD")
    airspeed = _field(hud, "airspeed", _field(hud, "airspeed_mps", None))
    try:
        airspeed = float(airspeed) if airspeed is not None else None
    except (TypeError, ValueError):
        airspeed = None
    return {
        "vtol_state": _field(extended, "vtol_state", None),
        "landed_state": _field(extended, "landed_state", None),
        "airspeed_mps": airspeed,
    }


# Wall-clock bound on one primitive when it is timed on the simulation clock.
PRIMITIVE_MAX_WALL_S = 180.0
LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_S = 45.0
LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_ENV = "LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_S"


def _linux_primitive_clock_stall_timeout_s() -> float:
    raw = os.environ.get(LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_ENV)
    if raw is None:
        return LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_ENV} must be a finite positive number"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_ENV} must be a finite positive number"
        )
    return value


def _primitive_max_wall_s(platform_name: str | None = None) -> float:
    """Bound continuous wall time without PX4 clock progress."""
    platform_name = os.name if platform_name is None else platform_name
    return (
        PRIMITIVE_MAX_WALL_S
        if platform_name == "nt"
        else _linux_primitive_clock_stall_timeout_s()
    )


def _raise_if_px4_clock_stalled(
    result: Any,
    step: int,
    platform_name: str | None = None,
    *,
    sim_clock: Any = None,
) -> None:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name != "nt" and result.ack.get("wall_timeout"):
        timeout_s = _linux_primitive_clock_stall_timeout_s()
        error = TimeoutError(
            "PX4 simulation clock stalled during primitive; "
            f"step={step}; wall_timeout_s={timeout_s:g}"
        )
        if sim_clock is not None and callable(getattr(sim_clock, "diagnostic_snapshot", None)):
            error.px4_clock_diagnostics = sim_clock.diagnostic_snapshot()
        raise error


class _SimClock:
    """Seconds on PX4's lockstep clock, falling back to wall time until it is known.

    Once simulation stamps arrive the clock follows them from the wall time at
    which the first one was seen, so it never jumps backwards.
    """

    def __init__(self, reader: Any, wall: Any = time.monotonic) -> None:
        self._reader = reader
        self._wall = wall
        self._offset: float | None = None
        self._last = float("-inf")
        self._last_at = float("-inf")
        self._last_wall = float("-inf")
        self._missing = False
        self._last_raw_stamp_ns: int | None = None
        self._last_progress_wall: float | None = None
        self._last_progress_clock_s: float | None = None
        self._resumed_after_default_deadline = False

    def __call__(self) -> float:
        stamp = _pose_sim_stamp_ns(self._reader.state)
        wall = self._wall()
        if stamp is None:
            if self._offset is None:
                return wall
            # Stamps went missing: advance on wall time instead of freezing, so
            # primitive timeouts still expire.
            self._last = max(self._last, self._last_at + (wall - self._last_wall))
            self._last_at, self._last_wall = self._last, wall
            self._missing = True
            return self._last
        raw_clock_s = stamp / 1e9
        if self._last_raw_stamp_ns is None or stamp > self._last_raw_stamp_ns:
            if (
                self._last_progress_wall is not None
                and wall - self._last_progress_wall >= LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_S
            ):
                self._resumed_after_default_deadline = True
            self._last_raw_stamp_ns = stamp
            self._last_progress_wall = wall
            self._last_progress_clock_s = raw_clock_s
        if self._offset is None:
            self._offset = wall - stamp / 1e9
        elif self._missing:
            # Stamps are back: continue from the wall-advanced value.
            self._offset = self._last - stamp / 1e9
            self._missing = False
        self._last = max(self._last, self._offset + stamp / 1e9)
        self._last_at, self._last_wall = self._last, wall
        return self._last

    def diagnostic_snapshot(self) -> dict[str, Any]:
        stamp = _pose_sim_stamp_ns(self._reader.state)
        return {
            "px4_clock_s": stamp / 1e9 if stamp is not None else None,
            "last_progress_at_monotonic_s": self._last_progress_wall,
            "last_progress_px4_clock_s": self._last_progress_clock_s,
            "clock_moved_again_after_45s": self._resumed_after_default_deadline,
        }


def _pose_sim_stamp_ns(state: Any) -> int | None:
    """Return PX4's simulation/boot timestamp in nanoseconds when available.

    PX4 SITL publishes ``LOCAL_POSITION_NED.time_boot_ms`` from the same
    lockstep clock as Gazebo.  Keeping this conversion at the collector
    boundary lets frame acquisition enforce a causal ``sensor >= pose``
    relation while remaining compatible with lightweight test doubles that do
    not provide the field.
    """
    message = state.get("LOCAL_POSITION_NED")
    value = _field(message, "time_boot_ms", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return int(value * 1_000_000)


def _snapshot_mavlink_state(state: MavlinkState) -> MavlinkState:
    """Freeze the telemetry sample used to request and label one observation."""
    return MavlinkState(
        messages=dict(state.messages),
        received_monotonic_ns=dict(state.received_monotonic_ns),
        received_wall_time=dict(state.received_wall_time),
        count=state.count,
        last_preflight_failure_ns=state.last_preflight_failure_ns,
        vehicle_time_boot_ms=state.vehicle_time_boot_ms,
        received_vehicle_ms=dict(state.received_vehicle_ms),
    )


def _is_terminal_action(action: Any) -> bool:
    """Return whether a policy action terminates the primitive sequence."""
    return str(getattr(action, "kind", "")) == "stop"


def _record_policy_step(
    recorder: EpisodeRecorder,
    before: dict[str, Any],
    action: Any,
    *,
    ack: dict[str, Any],
    outcome: dict[str, Any],
    obs_after: dict[str, Any],
    labels: dict[str, Any],
    timing: dict[str, Any] | None = None,
) -> bool:
    """Persist one policy step and report whether the primitive loop continues."""
    recorder.record_step(
        before,
        action,
        ack=ack,
        outcome=outcome,
        obs_after=obs_after,
        labels=labels,
        timing=timing,
        observation_view=(
            str(labels.get("observation_view"))
            if isinstance(labels, dict) and labels.get("observation_view") is not None
            else None
        ),
    )
    return not _is_terminal_action(action)


def _message_type(message: Any) -> str:
    getter = getattr(message, "get_type", None)
    if callable(getter):
        return str(getter())
    if isinstance(message, dict):
        return str(message.get("type", message.get("message_type", "")))
    return str(getattr(message, "_type", ""))


def _send_gcs_heartbeat(connection: Any, mavutil_module: Any) -> None:
    sender = getattr(getattr(connection, "mav", None), "heartbeat_send", None)
    constants = getattr(mavutil_module, "mavlink", None)
    if callable(sender):
        sender(
            int(getattr(constants, "MAV_TYPE_GCS", 6)),
            int(getattr(constants, "MAV_AUTOPILOT_INVALID", 8)),
            0,
            0,
            0,
            3,
        )


def _start_gcs_heartbeat_loop(
    connection: Any, mavutil_module: Any
) -> tuple[threading.Event, threading.Thread]:
    """Keep PX4's GCS-liveness gate open throughout a long episode."""
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                _send_gcs_heartbeat(connection, mavutil_module)
            except Exception:
                # The owning episode thread will fail on its next reader or
                # control check if the connection has actually gone away.
                pass
            stop.wait(0.5)

    thread = threading.Thread(target=run, name="sim-teacher-gcs-heartbeat", daemon=True)
    thread.start()
    return stop, thread


def _px4_param_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _profile_landing_parameters(airframe: str) -> tuple[tuple[str, str], ...]:
    """PX4 landing parameters the airframe profile pins (e.g. no-rangefinder landing)."""
    profile = _airframe_profile(airframe)
    if profile is None:
        return ()
    return tuple(
        (ref.px4_name, _px4_param_text(ref.value))
        for ref in profile.landing.parameters.values()
        if ref.px4_name is not None and not ref.unverified
    )


def _profile_launch_parameters(
    airframe: str, takeoff_method: str | None
) -> tuple[tuple[str, str], ...]:
    if takeoff_method not in {"hand", "catapult"}:
        return ()
    profile = _airframe_profile(airframe, takeoff_method)
    if profile is None:
        raise ValueError(f"{takeoff_method} launch requires a fixed-wing airframe profile")
    detection = profile.takeoff.launch_detection
    if detection is None:
        raise ValueError(f"{takeoff_method} launch profile has no launch detector configuration")
    parameters = tuple(
        (ref.px4_name, _px4_param_text(ref.value))
        for ref in (detection.accel_parameter, detection.duration_parameter)
        if ref.px4_name is not None and not ref.unverified
    )
    required = {"FW_LAUN_AC_THLD", "FW_LAUN_AC_T"}
    names = {name for name, _ in parameters}
    if not required.issubset(names):
        raise ValueError(f"verified PX4 launch parameters missing: {sorted(required - names)}")
    return (("FW_LAUN_DETCN_ON", "1"), *parameters)


def _set_sim_params(
    plan: CollectionPlan, environment: DockerSimEnvironment
) -> list[dict[str, Any]]:
    """Apply the SIM-only GCS input setting after PX4 heartbeat is live."""
    executable = _docker_executable(plan)
    applied: list[dict[str, Any]] = []
    for parameter, value in (
        *SIM_PREARM_PARAMETERS,
        *_profile_launch_parameters(plan.airframe, plan.takeoff_method),
        *_profile_landing_parameters(plan.airframe),
        *plan.extra_sim_params,
    ):
        command = [
            executable,
            "exec",
            environment.name,
            "/opt/px4-gazebo/bin/px4-param",
            "set",
            parameter,
            value,
        ]
        result = None
        attempts: list[dict[str, Any]] = []
        for attempt in range(1, 21):
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "")[-500:],
                    "stderr": (result.stderr or "")[-500:],
                }
            )
            if result.returncode == 0:
                break
            time.sleep(0.5)
        assert result is not None
        row = {
            "parameter": parameter,
            "value": value,
            "exit_code": result.returncode,
            "stdout": (result.stdout or "")[-500:],
            "stderr": (result.stderr or "")[-500:],
            "attempt_count": len(attempts),
            "attempts": attempts,
            "scope": "owned disposable PX4/Gazebo SITL only",
        }
        applied.append(row)
        _progress("px4_param", **row)
        if result.returncode != 0:
            raise RuntimeError(f"failed to set {parameter}={value}: {result.stderr[-500:]}")
    return applied


# pymavlink's PX4 mode table uses bare names ("MISSION", "LOITER"); dotted PX4 UI names
# are reported as "Unknown mode" and silently not sent.
_PYMAVLINK_PX4_MODE_ALIASES = {
    "AUTO.MISSION": "MISSION",
    "AUTO.LOITER": "LOITER",
    "AUTO.RTL": "RTL",
    "AUTO.LAND": "LAND",
    "AUTO.TAKEOFF": "TAKEOFF",
}


def _set_mode(connection: Any, mode: str) -> None:
    mode = _PYMAVLINK_PX4_MODE_ALIASES.get(mode, mode)
    setter = getattr(connection, "set_mode", None)
    if callable(setter):
        setter(mode)
        return
    mapping = getattr(connection, "mode_mapping", lambda: {})() or {}
    custom = mapping.get(mode)
    sender = getattr(getattr(connection, "mav", None), "set_mode_send", None)
    if isinstance(custom, (tuple, list)):
        custom = (int(custom[1]) << 16) | (int(custom[2]) << 24)
    if custom is None or not callable(sender):
        raise RuntimeError(f"PX4 mode {mode} cannot be requested")
    sender(int(getattr(connection, "target_system", 1)), 1, int(custom))


def _health_check_names(flags: int) -> list[str]:
    names = [name for bit, name in HEALTH_ERROR_BITS.items() if flags & bit]
    known = sum(HEALTH_ERROR_BITS)
    unknown = flags & ~known
    if unknown:
        names.append(f"unknown_0x{unknown:x}")
    return names


def _sensor_health_names(flags: int) -> list[str]:
    names = [name for bit, name in SYS_STATUS_HEALTH_BITS.items() if flags & bit]
    known = sum(SYS_STATUS_HEALTH_BITS)
    unknown = flags & ~known
    if unknown:
        names.append(f"unknown_0x{unknown:x}")
    return names


def _prearm_health_snapshot(state: Any) -> dict[str, Any] | None:
    """Extract PX4 arming failures from HEALTH_REPORT-like or SYS_STATUS data."""
    for kind in ("HEALTH_REPORT", "SYS_STATUS"):
        message = state.get(kind)
        if message is None:
            continue
        for field_name in ("arming_check_error_flags", "error_flags"):
            value = _field(message, field_name, None)
            if value is not None:
                try:
                    flags = int(value)
                except (TypeError, ValueError):
                    continue
                return {
                    "source": kind,
                    "error_flags": flags,
                    "failed_checks": _health_check_names(flags),
                    "ready": flags == 0,
                }
        present = _field(message, "onboard_control_sensors_present", None)
        health = _field(message, "onboard_control_sensors_health", None)
        if present is not None and health is not None:
            try:
                failed = int(present) & ~int(health)
            except (TypeError, ValueError):
                continue
            return {
                "source": kind,
                "error_flags": failed,
                "failed_checks": _sensor_health_names(failed),
                "ready": failed == 0,
            }
    return None


def _wait_prearm_health(
    reader: MavlinkReader,
    timeout_s: float,
    *,
    heartbeat_connection: Any | None = None,
    mavutil_module: Any | None = None,
    setpoint: callable | None = None,
    warmup_s: float = 10.0,
) -> dict[str, Any]:
    """Wait for health, with a conservative SYS_STATUS/STATUSTEXT fallback.

    Some PX4 builds do not stream ``HEALTH_REPORT``.  In that case require a
    healthy ``SYS_STATUS`` snapshot, no recent ``Preflight Fail`` text, and a
    ten second heartbeat warm-up before declaring the vehicle ready.
    """
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last_report = 0.0
    last_snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        reader.raise_if_failed()
        snapshot = _prearm_health_snapshot(reader.state)
        if snapshot != last_snapshot:
            last_snapshot = snapshot
            _progress(
                "prearm_health",
                source=None if snapshot is None else snapshot["source"],
                error_flags=None if snapshot is None else snapshot["error_flags"],
                failed_checks=[] if snapshot is None else snapshot["failed_checks"],
                ready=False if snapshot is None else snapshot["ready"],
            )
        source = None if snapshot is None else snapshot["source"]
        ready = bool(snapshot is not None and snapshot["ready"])
        if source == "SYS_STATUS":
            # SYS_STATUS has no arming_check_error_flags.  Require the
            # heartbeat warm-up and silence after the last preflight failure.
            warm = time.monotonic() - started >= max(0.0, float(warmup_s))
            recent_failure = (
                time.monotonic_ns() - int(getattr(reader.state, "last_preflight_failure_ns", 0))
            ) / 1e9 < 5.0
            ready = ready and warm and not recent_failure
        if source == "HEALTH_REPORT":
            # HEALTH_REPORT is authoritative, but still avoid arming while a
            # fresh commander preflight failure is being reported.
            if (
                time.monotonic_ns() - int(getattr(reader.state, "last_preflight_failure_ns", 0))
            ) / 1e9 < 5.0:
                ready = False
        if ready:
            _progress("prearm_health_ok", **snapshot)
            result = dict(snapshot)
            result["heartbeat_warmup_s"] = time.monotonic() - started
            return result
        now = time.monotonic()
        if now - last_report >= 5.0:
            last_report = now
            _progress(
                "prearm_health_waiting",
                source=None if snapshot is None else snapshot["source"],
                error_flags=None if snapshot is None else snapshot["error_flags"],
                failed_checks=[] if snapshot is None else snapshot["failed_checks"],
                received_messages=reader.state.count,
            )
        if heartbeat_connection is not None and mavutil_module is not None:
            _send_gcs_heartbeat(heartbeat_connection, mavutil_module)
        if setpoint is not None:
            setpoint()
        time.sleep(0.1)
    details = last_snapshot or {"source": None, "error_flags": None, "failed_checks": ["unknown"]}
    raise TimeoutError(
        "PX4 pre-arm health checks did not pass: " + ", ".join(details["failed_checks"])
    )


def _wait_for(
    predicate: callable,
    timeout_s: float,
    *,
    phase: str,
    interval_s: float = 0.05,
    heartbeat_progress: callable | None = None,
    health_check: callable | None = None,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_report = 0.0
    while time.monotonic() < deadline:
        if health_check is not None:
            health_check()
        if predicate():
            _progress(phase)
            return
        now = time.monotonic()
        if phase == "mavlink_heartbeat" and now - last_report >= 5.0:
            last_report = now
            fields = heartbeat_progress() if heartbeat_progress is not None else {}
            _progress(phase + "_waiting", **fields)
        time.sleep(interval_s)
    raise TimeoutError(f"timed out waiting for {phase} after {timeout_s:.1f}s")


def _wait_for_offboard(
    connection: Any,
    reader: MavlinkReader,
    streamer: SetpointStreamer,
    timeout_s: float = DEFAULT_OFFBOARD_ENTER_TIMEOUT_S,
) -> None:
    """Request OFFBOARD until PX4 reports the mode on HEARTBEAT."""
    offboard_modes = {"393216", "OFFBOARD", "OFFBOARD_NED"}
    last_mode_request = [0.0]

    def entered() -> bool:
        streamer.raise_if_failed()
        now = time.monotonic()
        if now - last_mode_request[0] >= 2.0:
            last_mode_request[0] = now
            _set_mode(connection, "OFFBOARD")
        heartbeat = reader.state.get("HEARTBEAT")
        return heartbeat is not None and str(_field(heartbeat, "custom_mode", "")) in offboard_modes

    _wait_for(
        entered,
        timeout_s,
        phase="offboard_entered",
        health_check=reader.raise_if_failed,
    )


def _landing_altitude(state: Any) -> tuple[float, str]:
    """Return current altitude above the local origin and its telemetry source."""
    local_position = state.get("LOCAL_POSITION_NED")
    z = _field(local_position, "z", None)
    try:
        altitude = -float(z)
    except (TypeError, ValueError):
        altitude = float("nan")
    if math.isfinite(altitude) and altitude >= 0.0:
        return altitude, "LOCAL_POSITION_NED"

    global_position = state.get("GLOBAL_POSITION_INT")
    relative_alt = _field(global_position, "relative_alt", None)
    try:
        altitude = float(relative_alt) / 1000.0
    except (TypeError, ValueError):
        altitude = float("nan")
    if math.isfinite(altitude) and altitude >= 0.0:
        return altitude, "GLOBAL_POSITION_INT"

    return MAX_TAKEOFF_ALTITUDE_M, "fallback_max_takeoff_altitude"


def _takeoff_timeout_s(altitude_m: float) -> float:
    """Same budget shape as landing: the SIM can run at ~0.15x real time under load."""
    return _land_timeout_s(altitude_m)


def _land_timeout_s(altitude_m: float) -> float:
    """Allow a fixed landing overhead plus a conservative altitude-based descent."""
    return max(
        LAND_MIN_TIMEOUT_S,
        LAND_BASE_TIMEOUT_S + max(0.0, float(altitude_m)) / LAND_DESCENT_RATE_MPS,
    )


def _land_mode_custom_values(connection: Any) -> set[int]:
    # PX4 AUTO.LAND is main mode 4, submode 6. Keep this even if a lightweight
    # connection does not expose pymavlink's mode mapping.
    values = {(4 << 16) | (6 << 24)}
    mapping = getattr(connection, "mode_mapping", None)
    if not callable(mapping):
        return values
    modes = mapping() or {}
    land_mode = modes.get("LAND")
    if isinstance(land_mode, (tuple, list)) and len(land_mode) >= 3:
        values.add((int(land_mode[1]) << 16) | (int(land_mode[2]) << 24))
    if isinstance(land_mode, int) and not isinstance(land_mode, bool):
        values.add(land_mode)
    return values


def _land_mode_confirmed(heartbeat: Any, custom_modes: set[int]) -> bool:
    if heartbeat is None:
        return False
    for field_name in ("mode", "flight_mode", "custom_mode_name"):
        value = str(_field(heartbeat, field_name, "")).upper().replace("_", ".")
        if value in {"LAND", "AUTO.LAND"}:
            return True
    custom_mode = _field(heartbeat, "custom_mode", None)
    try:
        return int(custom_mode) in custom_modes if custom_modes else False
    except (TypeError, ValueError):
        return str(custom_mode).upper() in {"LAND", "AUTO.LAND"}


def _plain_record(value: Any) -> Any:
    """JSON-safe copy of executor results (command results are small plain objects)."""
    if isinstance(value, dict):
        return {str(k): _plain_record(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_record(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    attrs = (
        {k: v for k, v in vars(value).items() if not k.startswith("_")}
        if hasattr(value, "__dict__")
        else {}
    )
    if not attrs:
        attrs = {
            k: getattr(value, k) for k in ("status", "ok", "result", "detail") if hasattr(value, k)
        }
    return {"type": type(value).__name__, **{k: _plain_record(v) for k, v in attrs.items()}}


def _dispatch_teacher_gimbal_command(
    controller: Any,
    labels: Mapping[str, Any],
    *,
    vehicle_yaw_deg: float,
    default_mode: str = "lock-horizon",
) -> bool:
    """Actuate a teacher gimbal command and mark it only after dispatch."""
    command = labels.get("gimbal_command")
    if not isinstance(command, Mapping):
        return False
    if controller is None:
        return False
    if "pitch_down_rad" not in command or "yaw_world_rad" not in command:
        return False
    controller.command_pitch_yaw(
        float(command["pitch_down_rad"]),
        float(command["yaw_world_rad"]),
        mode=str(command.get("mode", default_mode)),
        vehicle_yaw_rad=math.radians(float(vehicle_yaw_deg)),
    )
    return True


class _ReaderAckLink:
    """The telemetry link as seen by the fixed-wing executor.

    The MAVLink reader thread owns ``recv_match`` on this connection, so command
    ACKs are read from the reader's latest-state cache instead of being raced for.
    """

    def __init__(self, connection: Any, reader: Any) -> None:
        self._connection = connection
        self._reader = reader

    def __getattr__(self, name: str) -> Any:
        if name == "recv_match":
            raise AttributeError(name)
        return getattr(self._connection, name)

    def wait_command_ack(self, command: int, timeout_s: float) -> Any:
        started_ns = time.monotonic_ns()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            per_command = getattr(self._reader.state, "command_acks", {}).get(int(command))
            if per_command is not None and per_command[1] >= started_ns:
                return {
                    "command": int(command),
                    "result": int(_field(per_command[0], "result", -1)),
                }
            received = getattr(self._reader.state, "received_monotonic_ns", {})
            ack = self._reader.state.get("COMMAND_ACK")
            if (
                ack is not None
                and received.get("COMMAND_ACK", -1) >= started_ns
                and int(_field(ack, "command", -1)) == int(command)
            ):
                return {"command": int(command), "result": int(_field(ack, "result", -1))}
            time.sleep(0.02)
        return None

    def wait_message(
        self, message_types: set[str] | tuple[str, ...], timeout_s: float, *, after_ns: int = 0
    ) -> Any:
        """Read a protocol message from the reader-owned latest state."""
        wanted = set(message_types)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            for name in wanted:
                message = self._reader.state.get(name)
                received = self._reader.state.received_monotonic_ns.get(name, -1)
                if message is not None and received >= after_ns:
                    return message
            time.sleep(0.02)
        return None


def _global_position(state: Any) -> tuple[float, float]:
    message = state.get("GLOBAL_POSITION_INT") or state.get("HOME_POSITION")
    lat = _field(message, "lat", None)
    lon = _field(message, "lon", None)
    if lat is None:
        lat = _field(message, "latitude", None)
    if lon is None:
        lon = _field(message, "longitude", None)
    if lat is None or lon is None:
        raise RuntimeError("PX4 GLOBAL_POSITION_INT/HOME_POSITION is not ready for landing")
    scale = 1e7 if abs(float(lat)) > 90.0 or abs(float(lon)) > 180.0 else 1.0
    return float(lat) / scale, float(lon) / scale


def _home_reference(state: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """PX4 home as ((lat, lon, amsl), local NED) for local-to-global command conversion."""
    home = state.get("HOME_POSITION")
    if home is None:
        raise RuntimeError("PX4 HOME_POSITION is not ready")
    try:
        geodetic = (
            float(_field(home, "latitude", None)) / 1e7,
            float(_field(home, "longitude", None)) / 1e7,
            float(_field(home, "altitude", None)) / 1e3,
        )
        local = tuple(float(_field(home, key, None)) for key in ("x", "y", "z"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"PX4 HOME_POSITION is incomplete: {exc}") from exc
    return geodetic, local


def _home_runway(state: Any) -> tuple[tuple[float, float], float]:
    """Touchdown point and runway heading (deg, NED) from PX4 HOME_POSITION.

    PX4 stores the arming heading as the home yaw and publishes it in the
    HOME_POSITION quaternion, so a runway takeoff defines the landing runway.
    """
    home = state.get("HOME_POSITION")
    if home is None:
        raise RuntimeError("PX4 HOME_POSITION is not ready for landing")
    lat = _field(home, "latitude", None)
    lon = _field(home, "longitude", None)
    if lat is None or lon is None:
        raise RuntimeError("PX4 HOME_POSITION has no latitude/longitude")
    q = _field(home, "q", None)
    heading_deg = 0.0
    if isinstance(q, (list, tuple)) and len(q) == 4 and all(math.isfinite(float(v)) for v in q):
        w, x, y, z = (float(v) for v in q)
        heading_deg = math.degrees(math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))
    return (float(lat) / 1e7, float(lon) / 1e7), heading_deg % 360.0


def _offset_latlon(
    origin: tuple[float, float], bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    lat, lon = origin
    radius = 6371008.8
    bearing = math.radians(bearing_deg)
    return (
        lat + math.degrees(distance_m * math.cos(bearing) / radius),
        lon + math.degrees(distance_m * math.sin(bearing) / (radius * math.cos(math.radians(lat)))),
    )


def _fixed_wing_landing_items(
    state: Any,
    *,
    glide_angle_deg: float = FIXED_WING_LANDING_GLIDE_ANGLE_DEG,
    loiter_radius_m: float = 80.0,
    approach_altitude_m: float = FIXED_WING_LANDING_APPROACH_ALT_M,
) -> tuple[tuple[float, float], tuple[float, float], float, float]:
    """Approach loiter and touchdown for a straight-in landing on the home runway.

    The aircraft descends in a loiter (NAV_LOITER_TO_ALT) upwind of home, leaves
    it tangentially and glides in along the runway heading.  The loiter centre
    sits far enough back that the remaining glide stays shallower than the
    glide angle even after leaving the circle.
    """
    if not math.isfinite(glide_angle_deg) or not 1.0 <= glide_angle_deg <= 30.0:
        raise ValueError("landing glide angle must be between 1 and 30 degrees")
    if not math.isfinite(loiter_radius_m) or loiter_radius_m <= 0.0:
        raise ValueError("landing loiter radius must be positive")
    landing, heading_deg = _home_runway(state)
    glide_m = approach_altitude_m / math.tan(math.radians(glide_angle_deg))
    distance_m = max(250.0, loiter_radius_m + 1.3 * glide_m)
    approach = _offset_latlon(landing, (heading_deg + 180.0) % 360.0, distance_m)
    return approach, landing, approach_altitude_m, heading_deg


def _px4_mode_pair(custom_mode: int) -> tuple[int, int]:
    return (custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF


def _upload_fixed_wing_landing_mission(
    link: _ReaderAckLink,
    reader: MavlinkReader,
    *,
    landing_method: str,
    timeout_s: float,
    glide_angle_deg: float = FIXED_WING_LANDING_GLIDE_ANGLE_DEG,
    loiter_radius_m: float = 80.0,
    mode_timeout_s: float = 15.0,
) -> Any:
    """Upload DO_LAND_START, NAV_LOITER_TO_ALT and NAV_LAND, then enter MISSION."""
    method = str(landing_method).strip().lower()
    if method == "parachute":
        return SimpleNamespace(
            status="failed", ok=False, detail="parachute landing is not verified in PX4/Gazebo SITL"
        )
    if method not in {"runway", "belly"}:
        return SimpleNamespace(
            status="failed", ok=False, detail=f"unsupported fixed-wing landing method: {method}"
        )
    configured_angle = getattr(link, "FW_LND_ANG", None)
    params = getattr(link, "params", None)
    if configured_angle is None and isinstance(params, dict):
        configured_angle = params.get("FW_LND_ANG")
    if configured_angle is not None:
        try:
            glide_angle_deg = float(configured_angle)
        except (TypeError, ValueError):
            return SimpleNamespace(
                status="failed",
                ok=False,
                detail=f"invalid FW_LND_ANG parameter: {configured_angle!r}",
            )
    approach, landing, altitude_m, heading_deg = _fixed_wing_landing_items(
        reader.state,
        glide_angle_deg=glide_angle_deg,
        loiter_radius_m=loiter_radius_m,
    )
    target_system = int(getattr(link, "target_system", 1) or 1)
    target_component = int(getattr(link, "target_component", 1) or 1)
    mission_type = 0

    def latlon(point: tuple[float, float]) -> tuple[int, int]:
        return round(point[0] * 1e7), round(point[1] * 1e7)

    # (frame, command, param1..4, x, y, z).  The pinned PX4 rejects DO_LAND_START
    # in a global frame (MAV_MISSION_UNSUPPORTED); it is a MAV_FRAME_MISSION marker.
    items = [
        (MAV_FRAME_MISSION, MAV_CMD_DO_LAND_START, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0),
        (
            MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            MAV_CMD_NAV_LOITER_TO_ALT,
            0.0,
            float(loiter_radius_m),
            0.0,
            0.0,
            *latlon(approach),
            float(altitude_m),
        ),
        (
            MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            MAV_CMD_NAV_LAND,
            0.0,
            0.0,
            0.0,
            float("nan"),
            *latlon(landing),
            0.0,
        ),
    ]
    sender = getattr(getattr(link, "mav", None), "mission_count_send", None)
    item_sender = getattr(getattr(link, "mav", None), "mission_item_int_send", None)
    if not callable(sender) or not callable(item_sender):
        return SimpleNamespace(
            status="failed", ok=False, detail="MAVLink connection lacks mission upload methods"
        )
    started_ns = time.monotonic_ns()
    sender(target_system, target_component, len(items), mission_type)
    sent: set[int] = set()
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    while time.monotonic() < deadline and len(sent) < len(items):
        request = link.wait_message(("MISSION_REQUEST_INT",), 0.25, after_ns=started_ns)
        if request is None:
            continue
        sequence = int(_field(request, "seq", -1))
        if sequence in sent or not 0 <= sequence < len(items):
            continue
        frame, command, p1, p2, p3, p4, x, y, z = items[sequence]
        item_sender(
            target_system,
            target_component,
            sequence,
            frame,
            command,
            0,
            1,
            p1,
            p2,
            p3,
            p4,
            x,
            y,
            z,
            mission_type,
        )
        sent.add(sequence)
    if len(sent) != len(items):
        return SimpleNamespace(
            status="timeout", ok=False, detail=f"MISSION_REQUEST_INT timeout; sent={sorted(sent)}"
        )
    ack = link.wait_message(("MISSION_ACK",), 1.5, after_ns=started_ns)
    result = _field(ack, "type", None) if ack is not None else None
    if ack is None or int(result) != 0:
        return SimpleNamespace(
            status="failed", ok=False, detail=f"MISSION_ACK rejected or timed out: {result}"
        )
    # PX4 validates a fresh mission asynchronously and answers an early MISSION
    # request with "currently not available"; ask again until the heartbeat
    # reports AUTO.MISSION.
    mode_deadline = time.monotonic() + max(0.0, float(mode_timeout_s))
    while True:
        _set_mode(link, "AUTO.MISSION")
        heartbeat = link.wait_message(("HEARTBEAT",), 1.0, after_ns=time.monotonic_ns())
        custom = _field(heartbeat, "custom_mode", None) if heartbeat is not None else None
        if custom is not None and _px4_mode_pair(int(custom)) == (4, 4):
            break
        if time.monotonic() >= mode_deadline:
            return SimpleNamespace(
                status="failed",
                ok=False,
                detail=f"PX4 did not enter AUTO.MISSION; custom_mode={custom}",
            )
    return SimpleNamespace(
        status="accepted",
        ok=True,
        mission_items=len(items),
        approach=approach,
        landing=landing,
        altitude_m=altitude_m,
        runway_heading_deg=heading_deg,
    )


def _land_diagnostics(
    state: Any,
    custom_modes: set[int],
    *,
    land_started_ns: int,
    now_ns: int | None = None,
) -> dict[str, Any]:
    heartbeat = state.get("HEARTBEAT")
    extended_state = state.get("EXTENDED_SYS_STATE")
    ack = state.get("COMMAND_ACK")
    altitude_m, altitude_source = _landing_altitude(state)
    received = getattr(state, "received_monotonic_ns", {})
    now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
    stale_ns = int(LANDING_TELEMETRY_STALE_S * 1_000_000_000)
    heartbeat_ns = received.get("HEARTBEAT", -1)
    heartbeat_fresh = heartbeat_ns >= land_started_ns and now_ns - heartbeat_ns <= stale_ns
    extended_ns = received.get("EXTENDED_SYS_STATE", -1)
    extended_fresh = extended_ns >= land_started_ns and now_ns - extended_ns <= stale_ns
    ack_ns = received.get("COMMAND_ACK", -1)
    ack_fresh = ack_ns >= land_started_ns and now_ns - ack_ns <= stale_ns
    base_mode = _field(heartbeat, "base_mode", None)
    try:
        armed = bool(int(base_mode) & 128) if heartbeat is not None else None
    except (TypeError, ValueError):
        armed = None
    landed_state = _field(extended_state, "landed_state", None)
    position_ns = max(
        received.get("LOCAL_POSITION_NED", -1),
        received.get("GLOBAL_POSITION_INT", -1),
    )
    altitude_fresh = position_ns >= land_started_ns and now_ns - position_ns <= stale_ns
    local_position = state.get("LOCAL_POSITION_NED")
    vz = _field(local_position, "vz", None)
    try:
        vertical_speed_mps = abs(float(vz))
    except (TypeError, ValueError):
        vertical_speed_mps = float("nan")
    return {
        "mode": _field(heartbeat, "mode", _field(heartbeat, "custom_mode", None)),
        "land_mode_confirmed": heartbeat_fresh and _land_mode_confirmed(heartbeat, custom_modes),
        "landed_state": landed_state,
        "landed_state_fresh": extended_fresh,
        "armed": armed,
        "heartbeat_fresh": heartbeat_fresh,
        "land_ack_result": (
            _field(ack, "result", None)
            if ack_fresh and _field(ack, "command", None) == 176
            else None
        ),
        "altitude_m": round(altitude_m, 3),
        "altitude_source": altitude_source,
        "altitude_fresh": altitude_fresh,
        "vertical_speed_mps": (
            round(vertical_speed_mps, 3) if math.isfinite(vertical_speed_mps) else None
        ),
    }


def _land_and_wait(
    executor: PymavlinkPrimitiveExecutor,
    reader: MavlinkReader,
    connection: Any,
    streamer: SetpointStreamer | None,
    *,
    timeout_s: float | None = None,
    retry_interval_s: float = LAND_COMMAND_RETRY_INTERVAL_S,
    max_retries: int = LAND_COMMAND_MAX_RETRIES,
    stable_ground_s: float = LAND_STABLE_GROUND_S,
    auto_disarm_wait_s: float = LAND_AUTO_DISARM_WAIT_S,
    disarm_confirm_timeout_s: float = LAND_DISARM_CONFIRM_TIMEOUT_S,
    clock: callable = time.monotonic,
    sleep: callable = time.sleep,
) -> dict[str, Any]:
    """Await ground/disarm on PX4 time, with independent wall-clock safety bounds.

    Command retries, diagnostic cadence and receive freshness stay on host time.
    Windows retains its established wall-clock timing and completion condition.
    """
    if streamer is not None:
        streamer.stop()

    state = reader.state
    custom_modes = _land_mode_custom_values(connection)
    altitude_m, _ = _landing_altitude(state)
    wait_timeout_s = _land_timeout_s(altitude_m) if timeout_s is None else float(timeout_s)
    wall_started_s = clock()
    budget_clock = clock if os.name == "nt" else _SimClock(reader, wall=clock)
    started_s = budget_clock()
    hard_deadline = started_s + max(wait_timeout_s, LAND_HARD_CEILING_S)
    wall_deadline = wall_started_s + max(wait_timeout_s, LAND_HARD_CEILING_S)
    deadline = started_s + wait_timeout_s
    attempts = 1
    retries = 0
    last_command_s = wall_started_s
    last_diagnostic_s = last_command_s
    land_started_ns = int(last_command_s * 1_000_000_000)
    ground_since: float | None = None
    lowest_altitude_m = altitude_m if math.isfinite(altitude_m) else None
    last_altitude_progress_s = started_s
    last_budget_s = started_s
    last_clock_progress_wall_s = wall_started_s

    def wait_now() -> float:
        nonlocal last_budget_s, last_clock_progress_wall_s
        wall_now = clock()
        now = budget_clock()
        if os.name == "nt":
            return now
        if now > last_budget_s:
            last_budget_s = now
            last_clock_progress_wall_s = wall_now
        reason = None
        if wall_now >= wall_deadline:
            reason = "hard wall-clock ceiling"
        elif wall_now - last_clock_progress_wall_s >= LANDING_TELEMETRY_STALE_S:
            reason = "PX4 simulation clock stalled"
        if reason is not None:
            diagnostics = _land_diagnostics(
                state,
                custom_modes,
                land_started_ns=land_started_ns,
                now_ns=int(wall_now * 1_000_000_000),
            )
            _progress(
                "land_timeout",
                reason=reason,
                elapsed_wall_s=round(wall_now - wall_started_s, 3),
                elapsed_budget_s=round(now - started_s, 3),
                **diagnostics,
            )
            raise TimeoutError(
                f"timed out waiting for land: {reason}; "
                f"landed_state={diagnostics['landed_state']!r}; "
                f"altitude_m={diagnostics['altitude_m']}"
            )
        return now

    def wait_sleep(interval_s: float, phase_deadline: float) -> None:
        # A simulation-second remainder is not a wall-clock sleep duration.
        # Keep the old exact sleeps on the Windows/fallback wall-clock path.
        if os.name == "nt":
            sleep(min(interval_s, max(0.0, phase_deadline - budget_clock())))
            return
        remaining = max(0.0, wall_deadline - clock())
        if _pose_sim_stamp_ns(state) is None:
            remaining = min(remaining, max(0.0, phase_deadline - budget_clock()))
        sleep(min(interval_s, remaining))

    executor.land()
    _progress(
        "land_command_sent",
        attempt=attempts,
        retries=retries,
        timeout_s=round(wait_timeout_s, 1),
        timeout_clock="wall" if os.name == "nt" else "px4_boot_with_wall_fallback",
        hard_wall_ceiling_s=round(wall_deadline - wall_started_s, 1),
        **_land_diagnostics(
            state,
            custom_modes,
            land_started_ns=land_started_ns,
            now_ns=int(clock() * 1_000_000_000),
        ),
    )

    def collector_disarm(diagnostics: dict[str, Any]) -> dict[str, Any]:
        mav = getattr(connection, "mav", None)
        sender = getattr(mav, "command_long_send", None)
        if not callable(sender):
            raise RuntimeError("MAVLink connection cannot send DISARM command")
        command = 400
        target_system = int(getattr(connection, "target_system", 1) or 1)
        target_component = int(getattr(connection, "target_component", 1) or 1)
        sender(
            target_system,
            target_component,
            command,
            0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        disarm_started_ns = int(clock() * 1_000_000_000)
        disarm_deadline = wait_now() + max(0.0, float(disarm_confirm_timeout_s))
        _progress(
            "land_disarm_command_sent",
            command_id=command,
            timeout_s=disarm_confirm_timeout_s,
            **diagnostics,
        )
        while wait_now() < disarm_deadline:
            reader.raise_if_failed()
            diagnostics = _land_diagnostics(
                state,
                custom_modes,
                land_started_ns=land_started_ns,
                now_ns=int(clock() * 1_000_000_000),
            )
            heartbeat_ns = getattr(state, "received_monotonic_ns", {}).get("HEARTBEAT", -1)
            if heartbeat_ns >= disarm_started_ns and diagnostics["armed"] is False:
                _progress("land", disarm_source="collector", **diagnostics)
                return {
                    "retries": retries,
                    "disarm_source": "collector",
                    **diagnostics,
                    "timeout_s": wait_timeout_s,
                }
            wait_sleep(0.25, disarm_deadline)
        diagnostics = _land_diagnostics(
            state,
            custom_modes,
            land_started_ns=land_started_ns,
            now_ns=int(clock() * 1_000_000_000),
        )
        _progress("land_disarm_timeout", **diagnostics)
        raise TimeoutError(
            "timed out waiting for disarmed heartbeat after collector DISARM; "
            f"landed_state={diagnostics['landed_state']!r}; "
            f"armed={diagnostics['armed']!r}"
        )

    while wait_now() < deadline:
        reader.raise_if_failed()
        diagnostics = _land_diagnostics(
            state,
            custom_modes,
            land_started_ns=land_started_ns,
            now_ns=int(clock() * 1_000_000_000),
        )
        if diagnostics["heartbeat_fresh"] and diagnostics["armed"] is False:
            _progress("land", retries=retries, **diagnostics)
            return {"retries": retries, **diagnostics, "timeout_s": wait_timeout_s}
        if (
            os.name == "nt"
            and diagnostics["landed_state_fresh"]
            and diagnostics["landed_state"] == 1
        ):
            # Preserve the established Windows collector completion condition.
            _progress("land", retries=retries, **diagnostics)
            return {"retries": retries, **diagnostics, "timeout_s": wait_timeout_s}

        now = wait_now()
        wall_now = clock()
        if diagnostics["altitude_fresh"]:
            current_altitude = float(diagnostics["altitude_m"])
            if math.isfinite(current_altitude):
                if (
                    lowest_altitude_m is None
                    or current_altitude < lowest_altitude_m - LAND_PROGRESS_TOLERANCE_M
                ):
                    lowest_altitude_m = current_altitude
                    last_altitude_progress_s = now
                    previous_deadline = deadline
                    deadline = min(
                        hard_deadline,
                        max(deadline, now + max(0.0, float(LAND_PROGRESS_GRACE_S))),
                    )
                    if deadline > previous_deadline:
                        _progress(
                            "land_progress",
                            deadline_s=round(deadline - started_s, 1),
                            hard_ceiling_s=round(hard_deadline - started_s, 1),
                            **diagnostics,
                        )
        on_ground = diagnostics["landed_state_fresh"] and diagnostics["landed_state"] == 1
        settled_near_ground = (
            diagnostics["landed_state_fresh"]
            and diagnostics["landed_state"] == 4
            and diagnostics["altitude_fresh"]
            and diagnostics["altitude_m"] <= LAND_SETTLED_ALTITUDE_M
            and diagnostics["vertical_speed_mps"] is not None
            and diagnostics["vertical_speed_mps"] <= LAND_SETTLED_VERTICAL_SPEED_MPS
            and now - last_altitude_progress_s >= LAND_SETTLED_DURATION_S
        )
        if on_ground:
            if ground_since is None:
                ground_since = now
                _progress("land_ground_stable_wait", stable_s=stable_ground_s, **diagnostics)
            if now - ground_since >= max(0.0, float(stable_ground_s)):
                _progress("land_auto_disarm_wait", timeout_s=auto_disarm_wait_s, **diagnostics)
                auto_disarm_deadline = now + max(0.0, float(auto_disarm_wait_s))
                while wait_now() < auto_disarm_deadline:
                    reader.raise_if_failed()
                    diagnostics = _land_diagnostics(
                        state,
                        custom_modes,
                        land_started_ns=land_started_ns,
                        now_ns=int(clock() * 1_000_000_000),
                    )
                    if diagnostics["heartbeat_fresh"] and diagnostics["armed"] is False:
                        _progress("land", disarm_source="px4_auto", **diagnostics)
                        return {
                            "retries": retries,
                            "disarm_source": "px4_auto",
                            **diagnostics,
                            "timeout_s": wait_timeout_s,
                        }
                    if not (
                        diagnostics["landed_state_fresh"]
                        and diagnostics["landed_state"] == 1
                    ):
                        ground_since = None
                        break
                    wait_sleep(0.5, auto_disarm_deadline)
                else:
                    diagnostics = _land_diagnostics(
                        state,
                        custom_modes,
                        land_started_ns=land_started_ns,
                        now_ns=int(clock() * 1_000_000_000),
                    )
                    if not (
                        diagnostics["landed_state_fresh"]
                        and diagnostics["landed_state"] == 1
                    ):
                        ground_since = None
                    else:
                        return collector_disarm(diagnostics)
        elif settled_near_ground:
            _progress("land_settled_near_ground", **diagnostics)
            return collector_disarm(diagnostics)
        else:
            ground_since = None

        if (
            not diagnostics["land_mode_confirmed"]
            and not (diagnostics["landed_state_fresh"] and diagnostics["landed_state"] == 4)
            and retries < max_retries
            and wall_now - last_command_s >= retry_interval_s
        ):
            executor.land()
            retries += 1
            attempts += 1
            last_command_s = wall_now
            _progress(
                "land_command_sent",
                attempt=attempts,
                retries=retries,
                timeout_s=round(wait_timeout_s, 1),
                **diagnostics,
            )

        if wall_now - last_diagnostic_s >= LAND_DIAGNOSTIC_INTERVAL_S:
            last_diagnostic_s = wall_now
            _progress(
                "land_waiting",
                retries=retries,
                elapsed_wall_s=round(wall_now - wall_started_s, 3),
                elapsed_budget_s=round(now - started_s, 3),
                **diagnostics,
            )
        wait_sleep(0.5, deadline)

    reader.raise_if_failed()
    diagnostics = _land_diagnostics(
        state,
        custom_modes,
        land_started_ns=land_started_ns,
        now_ns=int(clock() * 1_000_000_000),
    )
    _progress("land_timeout", retries=retries, **diagnostics)
    raise TimeoutError(
        f"timed out waiting for land after {wait_timeout_s:.1f}s; "
        f"mode={diagnostics['mode']!r}; landed_state={diagnostics['landed_state']!r}; "
        f"altitude_m={diagnostics['altitude_m']}; retries={retries}"
    )


def _wait_px4_mode(
    reader: MavlinkReader, mode: tuple[int, int], *, timeout_s: float, after_ns: int
) -> dict[str, Any]:
    """Wait for a HEARTBEAT newer than ``after_ns`` reporting PX4 (main, sub) mode."""
    started = time.monotonic()
    last: tuple[int, int] | None = None
    while time.monotonic() - started < timeout_s:
        reader.raise_if_failed()
        heartbeat = reader.state.get("HEARTBEAT")
        received = getattr(reader.state, "received_monotonic_ns", {}).get("HEARTBEAT", -1)
        custom = _field(heartbeat, "custom_mode", None) if heartbeat is not None else None
        if custom is not None and received >= after_ns:
            last = _px4_mode_pair(int(custom))
            if last == tuple(mode):
                return {
                    "confirmed": True,
                    "mode": list(last),
                    "elapsed_s": time.monotonic() - started,
                }
        time.sleep(0.1)
    return {
        "confirmed": False,
        "mode": list(last) if last else None,
        "elapsed_s": time.monotonic() - started,
    }


def _wait_near_home(reader: MavlinkReader, *, radius_m: float, timeout_s: float) -> dict[str, Any]:
    """Wait until the local position is within ``radius_m`` of home (horizontal)."""
    started = time.monotonic()
    distance = float("nan")
    while time.monotonic() - started < timeout_s:
        reader.raise_if_failed()
        position = reader.state.get("LOCAL_POSITION_NED")
        home = reader.state.get("HOME_POSITION")
        if position is not None and home is not None:
            distance = math.hypot(
                float(_field(position, "x", 0.0)) - float(_field(home, "x", 0.0)),
                float(_field(position, "y", 0.0)) - float(_field(home, "y", 0.0)),
            )
            if distance <= radius_m:
                return {
                    "status": "near_home",
                    "distance_m": distance,
                    "elapsed_s": time.monotonic() - started,
                }
        time.sleep(0.25)
    # Landing from farther out still works; the mission flies to the approach first.
    return {"status": "timeout", "distance_m": distance, "elapsed_s": time.monotonic() - started}


def _wait_airframe_landed(
    reader: MavlinkReader,
    *,
    timeout_s: float,
    after_ns: int | None = None,
    clock: callable = time.monotonic,
    sleep: callable = time.sleep,
) -> dict[str, Any]:
    """Await fresh touchdown on PX4 time; bound frozen/very slow SITL on host time.

    A full fixed-wing approach can take hundreds of simulation seconds. Its
    wall ceiling must allow slow lockstep rather than reusing the 600 s MC cap.
    Windows and adapters without PX4 timestamps retain wall-clock budgets.
    """
    wall_started = clock()
    after_ns = int(wall_started * 1e9) if after_ns is None else after_ns
    budget_clock = clock if os.name == "nt" else _SimClock(reader, wall=clock)
    started = last_budget = budget_clock()
    last_progress_wall = wall_started
    wall_ceiling_s = max(timeout_s, FW_LAND_HARD_WALL_CEILING_S)
    while True:
        reader.raise_if_failed()
        wall_now, now = clock(), budget_clock()
        flight = _flight_state_snapshot(reader.state)
        received = getattr(reader.state, "received_monotonic_ns", {})
        heartbeat = reader.state.get("HEARTBEAT")
        base_mode = _field(heartbeat, "base_mode", None)
        disarmed = base_mode is not None and not (int(base_mode) & 128)

        def fresh(name: str) -> bool:
            stamp = received.get(name, -1)
            return stamp >= after_ns and 0 <= wall_now - stamp / 1e9 <= LANDING_TELEMETRY_STALE_S

        reason = None
        if os.name != "nt":
            if now > last_budget:
                last_budget, last_progress_wall = now, wall_now
            if wall_now - wall_started >= wall_ceiling_s:
                reason = "hard wall-clock ceiling"
            elif wall_now - last_progress_wall >= LANDING_TELEMETRY_STALE_S:
                reason = "PX4 simulation clock stalled (telemetry)"
        heartbeat_ns = received.get("HEARTBEAT")
        if reason is None and heartbeat_ns is not None and (
            wall_now - heartbeat_ns / 1e9 > LANDING_TELEMETRY_STALE_S
        ):
            reason = "PX4 telemetry stale (heartbeat)"
        if reason is None and now - started >= timeout_s:
            reason = "landing budget exhausted"
        if reason is not None:
            raise TimeoutError(
                f"timed out waiting for airframe landing: {reason}; "
                f"elapsed_wall_s={wall_now - wall_started:.3f}; "
                f"elapsed_budget_s={now - started:.3f}; "
                f"landed_state={flight['landed_state']}; airspeed_mps={flight['airspeed_mps']}"
            )
        if (fresh("EXTENDED_SYS_STATE") and flight["landed_state"] == 1) or (
            fresh("HEARTBEAT") and disarmed
        ):
            return {
                "status": "landed",
                "timeout_s": timeout_s,
                "elapsed_s": wall_now - wall_started,
                "elapsed_budget_s": now - started,
                "flight_state": flight,
            }
        sleep(0.1)


def _fixed_wing_or_vtol_land_and_wait(
    executor: FixedWingVtolExecutor,
    reader: MavlinkReader,
    *,
    timeout_s: float | None = None,
    clock: callable = time.monotonic,
    sleep: callable = time.sleep,
) -> dict[str, Any]:
    """Wait for the airframe-specific landing state after its command/mission."""
    wait_timeout_s = float(
        timeout_s if timeout_s is not None else FixedWingVtolExecutor.STATE_TIMEOUT_S["land"]
    )
    started = clock()
    started_ns = int(started * 1e9)
    result = executor.land()
    if result.status != "success":
        raise RuntimeError(f"fixed-wing/VTOL landing command failed: {_plain_record(result.ack)}")
    remaining_s = (
        max(0.0, wait_timeout_s - (clock() - started))
        if os.name == "nt" else wait_timeout_s
    )
    landed = _wait_airframe_landed(
        reader, timeout_s=remaining_s, after_ns=started_ns, clock=clock, sleep=sleep
    )
    return {
        **landed,
        "timeout_s": wait_timeout_s,
        "elapsed_s": clock() - started,
        "command": _plain_record(result.ack),
    }


def _takeoff_wait(
    reader: MavlinkReader,
    connection: Any,
    altitude_m: float,
    timeout_s: float,
    streamer: SetpointStreamer,
) -> None:
    """Wait for the NED down coordinate to reach the commanded climb target."""
    deadline = time.monotonic() + timeout_s
    last_report = 0.0
    while time.monotonic() < deadline:
        reader.raise_if_failed()
        streamer.raise_if_failed()
        mode = str(getattr(connection, "flightmode", "") or "").upper()
        if mode and mode not in {"OFFBOARD", "OFFBOARD_NED"}:
            status = reader.state.get("STATUSTEXT")
            text = str(_field(status, "text", ""))
            raise RuntimeError(f"PX4 left OFFBOARD during takeoff: mode={mode}; STATUSTEXT={text}")
        z = float(_state_position(reader.state)[2])
        now = time.monotonic()
        if now - last_report >= 2.0:
            last_report = now
            _progress("takeoff_waiting", z_m=z, target_z_m=-float(altitude_m))
        if z <= -(float(altitude_m) - 0.5):
            _progress("takeoff_reached", z_m=z, target_z_m=-float(altitude_m))
            return
        time.sleep(0.05)
    z = float(_state_position(reader.state)[2])
    raise TimeoutError(f"timed out waiting for takeoff_reached after {timeout_s:.1f}s; z_m={z:.3f}")


def _episode_budget(started: float, maximum_s: float, cap_s: float) -> float:
    remaining = maximum_s - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("episode wall-clock timeout")
    return min(float(cap_s), remaining)


class _EpisodeProgressWatchdog:
    """Bound an episode by vehicle-clock progress after the nominal wall budget."""

    def __init__(
        self,
        started: float,
        maximum_s: float,
        reader: Any,
        *,
        stall_timeout_s: float = EPISODE_PROGRESS_STALL_TIMEOUT_S,
        clock: callable = time.monotonic,
    ) -> None:
        self.started = float(started)
        self.maximum_s = float(maximum_s)
        self.reader = reader
        self.stall_timeout_s = max(1.0, float(stall_timeout_s))
        self.clock = clock
        self.last_vehicle_ms: int | None = None
        self.last_message_count = int(getattr(reader.state, "count", 0))
        self.last_progress_wall = self.clock()

    def check(self) -> None:
        now = self.clock()
        vehicle_ms = getattr(self.reader.state, "vehicle_time_boot_ms", None)
        try:
            vehicle_ms = int(vehicle_ms) if vehicle_ms is not None else None
        except (TypeError, ValueError):
            vehicle_ms = None
        message_count = int(getattr(self.reader.state, "count", self.last_message_count))
        message_progress = message_count > self.last_message_count
        if vehicle_ms is not None and (
            self.last_vehicle_ms is None or vehicle_ms > self.last_vehicle_ms
        ):
            self.last_vehicle_ms = vehicle_ms
            self.last_progress_wall = now
        elif vehicle_ms is None and message_progress:
            self.last_progress_wall = now
        self.last_message_count = message_count
        if now - self.started <= self.maximum_s:
            return
        if now - self.last_progress_wall <= self.stall_timeout_s:
            # Progress only extends the budget up to a hard ceiling: a vehicle that
            # keeps streaming telemetry while stuck in one phase must still end.
            ceiling = self.maximum_s + max(self.maximum_s, 600.0)
            if now - self.started <= ceiling:
                return
            raise TimeoutError(f"episode exceeded its hard wall-clock ceiling ({ceiling:.0f}s)")
        raise TimeoutError(
            "episode telemetry stalled after wall-clock budget "
            f"(vehicle_time_boot_ms={vehicle_ms}, message_count={message_count}, "
            f"stall={now - self.last_progress_wall:.1f}s)"
        )


def _raise_reader_error(reader: MavlinkReader) -> bool:
    raise RuntimeError(
        f"MAVLink reader stopped: {reader.error}; messages={reader.state.count}; "
        f"parse_errors={reader.parse_errors}; bad_crc={reader.bad_crc}"
    )


def _fresh_sensor_snapshot(
    reader: SensorStreamReader,
    previous_ns: int,
    timeout_s: float,
    *,
    min_sensor_stamp_ns: int | None = None,
    poll_interval_s: float = 0.02,
):
    """Wait for a fresh frame at or beyond a requested simulation timestamp.

    SIM rendering in Docker is slow (a frame every few seconds), so frame age is
    recorded in the observation instead of refusing the step.  ``previous_ns``
    is a host monotonic receive watermark; ``min_sensor_stamp_ns`` is the
    optional Gazebo/PX4 lockstep watermark and prevents consuming a backlog
    frame captured before the pose used for this observation.
    """
    reader.request(0 if min_sensor_stamp_ns is None else min_sensor_stamp_ns)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        reader.raise_if_failed()
        sample = reader.latest
        if (
            sample is not None
            and sample.received_monotonic_ns > previous_ns
            and (
                min_sensor_stamp_ns is None
                or int(sample.sensor_stamp_ns) >= int(min_sensor_stamp_ns)
            )
        ):
            return sample
        time.sleep(max(0.001, float(poll_interval_s)))
    raise TimeoutError(f"sensor stream produced no frame within {timeout_s:.1f}s")


def _arm(
    connection: Any,
    reader: MavlinkReader,
    mavutil_module: Any,
    timeout_s: float,
    setpoint: callable | None = None,
    heartbeat_connection: Any | None = None,
) -> dict[str, Any]:
    """Arm normally on the same receiving socket with bounded retries."""
    constants = getattr(mavutil_module, "mavlink", mavutil_module)
    sender = getattr(getattr(connection, "mav", None), "command_long_send", None)
    if not callable(sender):
        raise RuntimeError("MAVLink connection cannot send ARM command")
    command = int(getattr(constants, "MAV_CMD_COMPONENT_ARM_DISARM", 400))
    if command != 400:
        raise RuntimeError(f"unexpected MAV_CMD_COMPONENT_ARM_DISARM={command}")
    accepted = int(getattr(constants, "MAV_RESULT_ACCEPTED", 0))
    temporary = int(getattr(constants, "MAV_RESULT_TEMPORARILY_REJECTED", 1))
    denied = int(getattr(constants, "MAV_RESULT_DENIED", 2))
    in_progress = int(getattr(constants, "MAV_RESULT_IN_PROGRESS", 5))
    deadline = time.monotonic() + timeout_s
    last_ack: int | None = None
    attempts = 0
    accepted_seen = False
    mav = getattr(connection, "mav", None)

    def source_id(name: str) -> int | None:
        value = getattr(mav, name, None)
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    while time.monotonic() < deadline and (attempts < ARM_MAX_ATTEMPTS or accepted_seen):
        if accepted_seen:
            wait_deadline = deadline
            heartbeat = connection
            _send_gcs_heartbeat(heartbeat, mavutil_module)
        else:
            attempts += 1
            wait_deadline = min(deadline, time.monotonic() + ARM_RETRY_INTERVAL_S)
            # The udpin socket learns PX4's peer and must carry both receive and
            # transmit traffic.  A separate control socket can fail the GCS gate.
            heartbeat = connection
            _send_gcs_heartbeat(heartbeat, mavutil_module)
        target_system = int(getattr(connection, "target_system", 1) or 1)
        target_component = int(
            getattr(connection, "target_component", 1)
            or getattr(constants, "MAV_COMP_ID_AUTOPILOT1", 1)
            or 1
        )
        if not accepted_seen:
            sender(
                target_system,
                target_component,
                command,
                0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
            _progress(
                "arm_command_sent",
                attempt=attempts,
                command_id=command,
                target_system=target_system,
                target_component=target_component,
                source_system=source_id("srcSystem"),
                source_component=source_id("srcComponent"),
            )
        while time.monotonic() < wait_deadline:
            reader.raise_if_failed()
            hb = reader.state.get("HEARTBEAT")
            if hb is not None and int(_field(hb, "base_mode", 0)) & int(
                getattr(constants, "MAV_MODE_FLAG_SAFETY_ARMED", 128)
            ):
                _progress("arm_ack", result=last_ack, armed=True, attempts=attempts)
                return {
                    "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                    "command_id": command,
                    "result": last_ack if last_ack is not None else accepted,
                    "armed": True,
                    "attempts": attempts,
                    "target_system": target_system,
                    "target_component": target_component,
                }
            ack = reader.state.get("COMMAND_ACK")
            if ack is not None and int(_field(ack, "command", -1)) == command:
                result = int(_field(ack, "result", -1))
                last_ack = result
                if result == accepted:
                    # PX4 may publish the accepted ACK immediately but carry
                    # the armed bit only on its next slow lockstep heartbeat.
                    # Stop duplicate ARM commands and wait for that heartbeat
                    # until the phase deadline instead of the 1 s retry window.
                    accepted_seen = True
                    wait_deadline = deadline
                if result == denied:
                    health = _prearm_health_snapshot(reader.state)
                    _progress(
                        "prearm_health_waiting",
                        source=None if health is None else health["source"],
                        error_flags=None if health is None else health["error_flags"],
                        failed_checks=[] if health is None else health["failed_checks"],
                        arm_attempt=attempts,
                    )
                if result not in (accepted, temporary, in_progress, denied):
                    raise RuntimeError(
                        f"PX4 rejected ARM command {command} with MAV_RESULT={result}"
                    )
            _send_gcs_heartbeat(heartbeat, mavutil_module)
            if setpoint is not None:
                setpoint()
            time.sleep(0.1)
    raise TimeoutError(
        f"PX4 did not confirm armed heartbeat after {attempts} bounded attempts "
        f"(command_id={command}, last_ack={last_ack})"
    )


def _make_sensor_process(
    plan: CollectionPlan, environment: DockerSimEnvironment
) -> subprocess.Popen[bytes]:
    command = _camera_stream_command(plan, environment)
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("sensor stream pipes unavailable")

    def drain() -> None:
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"message": line[:500]}
                if isinstance(event, dict) and event.get("event") == "gazebo_sensor_stream_started":
                    _progress("sensor_stream_started", details=event)
                else:
                    # Surface every other diagnostic so a dying frame server is debuggable.
                    _progress("sensor_stream_stderr", details=event)

    threading.Thread(target=drain, name="sim-teacher-sensor-stderr", daemon=True).start()
    return process


def _png_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Convert a decoded RGB array or raw BGR24 payload into PNG bytes."""
    raw = observation.get("rgb")
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("live SIM recording requires numpy and Pillow") from exc
    width = int(observation["camera_info"]["width"])
    height = int(observation["camera_info"]["height"])
    if isinstance(raw, (bytes, bytearray, memoryview)):
        bgr = np.frombuffer(raw, dtype=np.uint8)
        if bgr.size != width * height * 3:
            raise RuntimeError("live RGB payload geometry does not match camera_info")
        rgb = bgr.reshape((height, width, 3))[:, :, ::-1]
    else:
        rgb = np.asarray(raw)
        if rgb.shape != (height, width, 3) or rgb.dtype != np.uint8:
            raise RuntimeError("live RGB array geometry or dtype does not match camera_info")
    output = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(output, format="PNG")
    result = dict(observation)
    result["rgb"] = output.getvalue()
    return result


def _index_task_text(truth_path: Path) -> str | None:
    """Natural-language task text from a generated index (fuel_v1 worlds carry one)."""
    index = Path(truth_path).parent / "index.json"
    if not index.is_file():
        return None
    try:
        worlds = json.loads(index.read_text(encoding="utf-8")).get("worlds", [])
    except (OSError, ValueError):
        return None
    scenario_id = Path(truth_path).name.removesuffix(".truth.json")
    for world in worlds:
        if world.get("world_id") != scenario_id:
            continue
        # Generated indexes may use a mover-specific asset mode, but retain the
        # same task-text fields as the original Fuel worlds.
        text = world.get("find_task_text") or world.get("task_text")
        return str(text) if text else None
    return None


def run_episode(
    plan: CollectionPlan,
    truth_path: Path,
    episode_index: int,
    *,
    free_space_fn: Any = free_space_bytes,
) -> Path:
    episode_id = f"episode-{episode_index:04d}-{uuid.uuid4().hex[:8]}"
    # Resolve the airframe mount once per episode; observation packing reuses it.
    airframe_profile = _airframe_profile(
        getattr(plan, "airframe", "x500_mono_cam"), getattr(plan, "takeoff_method", None)
    )
    camera_mount = (
        airframe_profile.camera_mount.to_camera_mount()
        if airframe_profile is not None and airframe_profile.camera_mount is not None
        else None
    )
    initial_reason = disk_space_abort_reason(
        plan.out, plan.min_free_gb, free_space_fn=free_space_fn
    )
    if initial_reason is not None:
        recorder = EpisodeRecorder(plan.out, episode_id, sensor_status="live")
        recorder.meta["episode_index"] = episode_index
        recorder.meta["seed"] = getattr(plan, "seed", 0) + episode_index
        recorder.mark_finished(status="aborted_low_disk", failure=initial_reason)
        _progress("aborted_low_disk", episode=str(recorder.episode_dir), reason=initial_reason)
        return recorder.episode_dir
    if plan.smoke_flight:
        policy = _SmokeFlightPolicy()
        task_text = "固定翼/VTOL 冒烟飞行：起飞、转换、盘旋、三次重定位、返航、降落"
    elif plan.teacher == "onboard":
        # Only the curated task text is read from the generated index; truth is
        # used after the episode by the scorer, never by the policy.
        task_text = plan.task_text or _index_task_text(truth_path) or "找到目标并记录位置"
        policy = _onboard_policy(plan, task_text, Path(plan.out) / f"{episode_id}-onboard-frames")
    elif plan.teacher == "expert_search":
        policy, target_label = _policy_for(
            plan, truth_path, plan.seed + episode_index, airframe_profile=airframe_profile
        )
        task_text = _index_task_text(truth_path) or (
            "\u627e\u5230" + str(target_label) + "\u5e76\u8bb0\u5f55\u4f4d\u7f6e"
        )
    elif plan.teacher == "hover":
        policy = _hover_policy(plan, truth_path)
        task_text = "Hover above the moving target path and record observations."
    else:
        policy, _ = _policy_for(
            plan, truth_path, plan.seed + episode_index, airframe_profile=airframe_profile
        )
        task_text = "\u63a2\u7d22\u573a\u666f\u5e76\u8bb0\u5f55\u89c2\u6d4b"
    recorder = EpisodeRecorder(
        plan.out,
        episode_id,
        task_text=task_text,
        sensor_status="live",
        depth_format=plan.depth_format,
        deduplicate_depth=plan.deduplicate_depth,
        compress_mavlink=plan.compress_mavlink,
        rgb_format=plan.rgb_format,
        rgb_quality=plan.rgb_quality,
        record_depth=plan.capture_depth,
        async_metadata=plan.async_metadata,
    )
    video_writer = (
        ContinuousVideoWriter(
            recorder.episode_dir, hz=plan.video_hz, max_bytes=plan.video_max_bytes
        )
        if plan.video_hz > 0.0
        else None
    )
    last_video_stamp_ns: list[int] = [-1]
    # Persist the effective per-episode seed/index so a campaign can retry an
    # individual failed recording exactly, even when its next invocation uses a
    # different --episodes value.
    recorder.meta["episode_index"] = episode_index
    recorder.meta["seed"] = plan.seed + episode_index
    altitude_m, altitude_source = _takeoff_altitude_for_plan(plan)
    recorder.meta["takeoff_altitude_m"] = altitude_m
    recorder.meta["takeoff_altitude_source"] = altitude_source
    recorder.meta["frame_age_limit_s"] = _frame_age_limit_s(plan.airframe)
    world_path = scenario_world_path(plan.runtime)
    recorder.meta["world_sdf"] = str(world_path) if world_path else None
    config_snapshot = snapshot_config(plan.onboard_config, recorder.episode_dir / "config")
    recorder.meta["provenance"] = build_provenance(
        root=ROOT,
        config_paths=[plan.onboard_config] if plan.onboard_config else [],
        model_paths=model_paths_from_config(plan.onboard_config),
        world_paths=[world_path] if world_path else [],
        command=sys.argv,
        model_cache=Path(plan.out) / f"{episode_id}-onboard-frames" / ".model-hash-cache.json",
    )
    recorder.meta["provenance"]["config_snapshot"] = config_snapshot
    recorder.meta["provenance"]["px4_logger_mode"] = resolve_px4_logger_mode()
    recorder.meta["provenance"]["linux_primitive_clock_stall_timeout_s"] = (
        _linux_primitive_clock_stall_timeout_s()
    )
    cpu_isolation = resolve_cpu_isolation(plan.worker_index)
    if cpu_isolation is not None:
        # Attaching does not apply the planned Docker options to an existing
        # container, so never claim its actual affinity from our environment.
        recorder.meta["provenance"]["simulator_cpu_isolation"] = (
            {"mode": "attached", "cpuset_cpus": None, "cpu_shares": None}
            if plan.attach_container else cpu_isolation.to_mapping()
        )
    recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
    started = time.monotonic()
    telemetry_connection = None
    sensor_process = None
    sensor_reader = mav_reader = None
    launch_status_texts: list[str] = []
    setpoint_streamer = None
    gimbal_controller = None
    mover_driver = None
    gcs_heartbeat_stop = gcs_heartbeat_thread = None

    environment = DockerSimEnvironment(plan, episode_id)

    def record_container_failure(exc: Exception) -> None:
        clock_diagnostics = getattr(exc, "px4_clock_diagnostics", None)
        diagnostics = environment.capture_failure_diagnostics(
            f"{type(exc).__name__}: {exc}",
            px4_clock_diagnostics=clock_diagnostics,
        )
        if diagnostics.get("cleanup_state") is None:
            environment.capture_cleanup_diagnostics()
        recorder.meta["failure_class"] = diagnostics["failure_class"]
        recorder.meta["container_failure_diagnostics"] = diagnostics
        if clock_diagnostics is not None:
            recorder.meta["failure_record"] = {
                "kind": "px4_sim_clock_stall",
                **clock_diagnostics,
            }

    try:
        with environment:
            environment.wait(_episode_budget(started, plan.max_episode_s, 20.0))
            _progress("gz_world_loaded", container=environment.name)
            mover_driver = _start_mover_driver(plan, environment, truth_path)
            if mover_driver is not None:
                recorder.meta["mover_driver"] = mover_driver
                recorder.episode_path.write_text(
                    json.dumps(recorder.meta, indent=2), encoding="utf-8"
                )
                _progress("mover_driver_started", command=mover_driver["command"])
                _wait_for_mover_driver_start(plan, environment, mover_driver)
                _progress("mover_driver_initial_visibility_ready")
            try:
                from pymavlink import mavutil
            except ImportError as exc:
                raise RuntimeError("pymavlink is required for a real SIM episode") from exc
            recorder.record_mavlink(
                {
                    "message_type": "collector_container_started",
                    "container": environment.name,
                    "image": plan.image,
                }
            )
            telemetry_connection = mavutil.mavlink_connection(
                sim_udpin_endpoint(plan.telemetry_port, mode=plan.sim_network),
                source_system=GCS_SOURCE_SYSTEM,
                source_component=GCS_SOURCE_COMPONENT,
                autoreconnect=False,
            )
            if camera_mount is not None and camera_mount.kind == "gimbal":
                def _send_gimbal_command(command: int, params: tuple[float, ...]) -> None:
                    sender = getattr(getattr(telemetry_connection, "mav", None), "command_long_send", None)
                    if not callable(sender):
                        raise RuntimeError("MAVLink connection cannot send gimbal command")
                    sender(
                        int(getattr(telemetry_connection, "target_system", 1) or 1),
                        int(getattr(telemetry_connection, "target_component", 1) or 1),
                        int(command),
                        0,
                        *params,
                    )

                gimbal_controller = Px4GimbalController(camera_mount, _send_gimbal_command)
            recorder.begin_mavlink_stream()

            def _record_mavlink(row: dict[str, Any]) -> None:
                if str(row.get("message_type", "")).upper() == "STATUSTEXT":
                    launch_status_texts.append(str(row.get("text", "")))
                recorder.record_mavlink(row)

            mav_reader = MavlinkReader(telemetry_connection, on_message=_record_mavlink).start()
            episode_watchdog = _EpisodeProgressWatchdog(started, plan.max_episode_s, mav_reader)
            _progress("mavlink_reader_started", telemetry_port=plan.telemetry_port)
            _wait_for(
                lambda: (
                    (_raise_reader_error(mav_reader) or False)
                    if mav_reader.error is not None
                    else mav_reader.state.get("HEARTBEAT") is not None
                ),
                _episode_budget(started, plan.max_episode_s, plan.heartbeat_timeout_s),
                phase="mavlink_heartbeat",
                heartbeat_progress=lambda: {
                    "received_messages": mav_reader.state.count,
                    "message_counts": dict(mav_reader.message_counts),
                    "parse_errors": mav_reader.parse_errors,
                    "bad_crc": mav_reader.bad_crc,
                },
                health_check=mav_reader.raise_if_failed,
            )
            # udpin learns PX4's peer from the received heartbeat.  Start the
            # continuous GCS heartbeat only after that point so every packet
            # is sent back through this same socket to PX4.
            gcs_heartbeat_stop, gcs_heartbeat_thread = _start_gcs_heartbeat_loop(
                telemetry_connection, mavutil
            )
            # Keep the GCS liveness gate open while disposable-SITL arm
            # parameters are applied.  This mirrors r29's connection-before-
            # preflight ordering and prevents PX4 from latching a stale GCS
            # failure during the docker-exec retries.
            sim_parameters = _set_sim_params(plan, environment)
            recorder.meta["sim_only_parameters"] = sim_parameters
            recorder.meta["simulator_container"] = environment.remember_px4_process()
            recorder.meta["gcs_identity"] = {
                "source_system": GCS_SOURCE_SYSTEM,
                "source_component": GCS_SOURCE_COMPONENT,
                "heartbeat_connection": "telemetry",
                "command_connection": "telemetry",
                "single_connection": True,
            }
            recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
            _progress("px4_ready")
            # Use the receiving telemetry connection for every PX4 command.
            # pymavlink's udpin socket learns the PX4 peer from the first
            # packet and writes heartbeats, arm, mode, and setpoints back to
            # that same peer.
            if hasattr(telemetry_connection, "target_system"):
                telemetry_connection.target_system = int(
                    getattr(telemetry_connection, "target_system", 1) or 1
                )
            if hasattr(telemetry_connection, "target_component"):
                telemetry_connection.target_component = int(
                    getattr(telemetry_connection, "target_component", 1) or 1
                )
            sensor_process = _make_sensor_process(plan, environment)
            depth_binding = None
            runtime = json.loads(Path(plan.runtime).read_text(encoding="utf-8"))
            # Must match _sensor_command: non-x500 airframes always stream the
            # registered depth camera even when the scenario omits it.
            declares_depth = any(
                isinstance(s, dict) and s.get("kind") == "depth_camera"
                for s in runtime.get("sensors", [])
            )
            if declares_depth or _has_registered_depth_camera(plan.airframe):
                # The helper's contract is deterministic and is embedded in
                # the command; SensorStreamReader validates it on every frame.
                model = _gazebo_model_name(plan, runtime)
                profile = _airframe_profile(plan.airframe)
                calibration_file = (
                    "gazebo-multirotor-front-camera-v1.json"
                    if profile is None or profile.category == "multirotor"
                    else "gazebo-front-camera-v1.json"
                    if profile.category == "vtol"
                    else "gazebo-fixed-wing-front-camera-v1.json"
                )
                calibration = json.loads(
                    (ROOT / "simulation/gazebo/calibration" / calibration_file).read_text(
                        encoding="utf-8"
                    )
                )["calibration"]
                depth = next(
                    (
                        s
                        for s in runtime.get("sensors", [])
                        if isinstance(s, dict) and s.get("kind") == "depth_camera"
                    ),
                    {},
                )
                depth_id = depth.get("calibration_id", "gazebo-multirotor-depth-v1")
                depth_binding = {
                    "source_topic": (
                        f"/world/default/model/{model}/link/depth_camera_link/"
                        "sensor/depth_camera/depth_image"
                    ),
                    "calibration_id": depth_id,
                    "calibration_sha256": canonical_sha256(
                        {
                            "schema_version": 1,
                            "calibration_id": depth_id,
                            "sensor_id": depth.get("sensor_id", "downward-depth"),
                            "width_px": 1280,
                            "height_px": 960,
                            "fx_px": calibration["fx_px"],
                            "fy_px": calibration["fy_px"],
                            "cx_px": calibration["cx_px"],
                            "cy_px": calibration["cy_px"],
                            "distortion": [0.0] * 5,
                            "rectification": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                            "extrinsics_rgb_to_depth": {
                                "translation_m": [0.0, 0.0, 0.0],
                                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                            },
                            "registration_mode": "registered_to_rgb",
                            "coordinate_space": "rgb_pixel_aligned",
                            "encoding": "FLOAT32_M",
                            "distance_semantics": "optical_axis_z_m",
                            "near_clip_m": 0.1,
                            "far_clip_m": 3000.0,
                        }
                    ),
                }
            if sensor_process.stdout is None:
                raise RuntimeError("sensor stream stdout is unavailable")
            sensor_reader = SensorStreamReader(
                sensor_process.stdout,
                request_stream=sensor_process.stdin,
                depth_binding=depth_binding,
                camera_info={
                    **DEFAULT_CAMERA_INFO,
                    "width": plan.rgb_size[0],
                    "height": plan.rgb_size[1],
                    "fx": DEFAULT_CAMERA_INFO["fx"] * plan.rgb_size[0] / 1280.0,
                    "fy": DEFAULT_CAMERA_INFO["fy"] * plan.rgb_size[1] / 960.0,
                    "cx": DEFAULT_CAMERA_INFO["cx"] * plan.rgb_size[0] / 1280.0,
                    "cy": DEFAULT_CAMERA_INFO["cy"] * plan.rgb_size[1] / 960.0,
                },
                max_frames=max(64, int(plan.max_episode_s * 30.0)),
                require_depth=depth_binding is not None,
                segmentation_topic="/multidetect/teacher/segmentation/labels_map"
                if plan.segmentation
                else None,
            ).start()
            # The collector-owned frame server is request/response based, so
            # explicitly prime it before waiting for the first decoded frame.
            sensor_reader.request(0)
            sensor_reader.wait_first_frame(_episode_budget(started, plan.max_episode_s, 45.0))
            _progress("sensor_stream_first_frame", frames=sensor_reader.frames_received)
            ekf_required = (
                "LOCAL_POSITION_NED",
                "ATTITUDE",
                "GLOBAL_POSITION_INT",
                "GPS_RAW_INT",
                "HOME_POSITION",
            )

            def _ekf_missing() -> list[str]:
                missing = [name for name in ekf_required if mav_reader.state.get(name) is None]
                fix = int(_field(mav_reader.state.get("GPS_RAW_INT"), "fix_type", 0))
                if fix < 3:
                    missing.append(f"gps_fix_type={fix}")
                return missing

            try:
                _wait_for(
                    lambda: not _ekf_missing(),
                    _episode_budget(started, plan.max_episode_s, 90.0),
                    phase="ekf_ok",
                    health_check=mav_reader.raise_if_failed,
                )
            except TimeoutError as exc:
                raise TimeoutError(f"{exc}; still missing: {_ekf_missing()}") from exc
            vehicle = telemetry_connection
            start_pos = _state_position(mav_reader.state)
            primitive_sim_clock = _SimClock(mav_reader)
            if airframe_uses_multirotor_executor(plan.airframe, base_dir=ROOT):
                setpoint_streamer = SetpointStreamer(vehicle, rate_hz=20.0)
                setpoint_streamer.set_target(
                    start_pos[0], start_pos[1], -altitude_m, _state_yaw(mav_reader.state)
                )
                setpoint_streamer.start()
                _progress("setpoint_streamer_started", rate_hz=20.0)
                executor = PymavlinkPrimitiveExecutor(
                    vehicle,
                    endpoint=f"udp://127.0.0.1:{plan.telemetry_port}",
                    position=lambda: _state_position(mav_reader.state),
                    yaw_deg=lambda: _state_yaw(mav_reader.state),
                    speed_mps=lambda: _state_speed(mav_reader.state),
                    criterion=None,
                    streamer=setpoint_streamer,
                    # Time primitives on PX4's lockstep clock: on a loaded host the
                    # simulation ran at 0.22x real time and a third of the 15 m legs
                    # hit the 20 s wall-clock timeout before settling.
                    clock=primitive_sim_clock,
                    max_wall_s=_primitive_max_wall_s(),
                )
            # Let PX4's commander settle and report healthy before arming.
            # The normal arm command does not bypass these checks.
            prearm_health = _wait_prearm_health(
                mav_reader,
                _episode_budget(started, plan.max_episode_s, DEFAULT_PREARM_HEALTH_TIMEOUT_S),
                heartbeat_connection=telemetry_connection,
                mavutil_module=mavutil,
            )
            recorder.meta["prearm_health"] = prearm_health
            multirotor_profile = _airframe_profile(plan.airframe, plan.takeoff_method)
            if (
                airframe_uses_multirotor_executor(
                    plan.airframe, plan.takeoff_method, base_dir=ROOT
                )
                and multirotor_profile is not None
            ):
                recorder.meta["airframe_profile"] = multirotor_profile.to_mapping()
            recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
            if airframe_uses_multirotor_executor(plan.airframe, base_dir=ROOT):
                # This is the original x500 sequence. Keep it byte-for-byte
                # equivalent: setpoints -> arm -> OFFBOARD -> climb.
                arm_ack = _arm(
                    telemetry_connection,
                    mav_reader,
                    mavutil,
                    _episode_budget(started, plan.max_episode_s, 30.0),
                    setpoint=setpoint_streamer.send_once,
                    heartbeat_connection=telemetry_connection,
                )
                _progress("arm_ack", ack=arm_ack)
                setpoint_streamer.set_target(
                    start_pos[0], start_pos[1], -altitude_m, _state_yaw(mav_reader.state)
                )
                time.sleep(1.0)
                setpoint_streamer.raise_if_failed()
                _wait_for_offboard(
                    telemetry_connection,
                    mav_reader,
                    setpoint_streamer,
                    _episode_budget(
                        started, plan.max_episode_s, DEFAULT_OFFBOARD_ENTER_TIMEOUT_S
                    ),
                )
                _progress("offboard_entered")
                last_z_report = [0.0]

                def _takeoff_ready() -> bool:
                    setpoint_streamer.raise_if_failed()
                    hb = mav_reader.state.get("HEARTBEAT")
                    if hb is not None:
                        mode = str(_field(hb, "custom_mode", ""))
                        if mode and mode not in {"393216", "OFFBOARD", "OFFBOARD_NED"}:
                            text = _field(mav_reader.state.get("STATUSTEXT"), "text", "")
                            raise RuntimeError(
                                f"OFFBOARD exited unexpectedly: mode={mode}; STATUSTEXT={text}"
                            )
                    z = _state_position(mav_reader.state)[2]
                    now = time.monotonic()
                    if now - last_z_report[0] >= 2.0:
                        last_z_report[0] = now
                        _progress("takeoff_progress", z_m=z, target_z_m=-altitude_m)
                    return z <= -(altitude_m - 0.5)

                _wait_for(
                    _takeoff_ready,
                    _episode_budget(started, plan.max_episode_s, _takeoff_timeout_s(altitude_m)),
                    phase="takeoff_reached",
                    health_check=mav_reader.raise_if_failed,
                )
            else:
                profile = _airframe_profile(plan.airframe, plan.takeoff_method)
                assert profile is not None
                takeoff_method = (
                    "vtol"
                    if profile.category == "vtol"
                    else plan.takeoff_method or profile.takeoff.methods[0]
                )
                landing_method = profile.landing.methods[0]
                # A VTOL whose onboard loop flies in hover takes off as a multicopter
                # and stays in MC (a forward transition would be undone at step 0,
                # and the SITL tailsitter climbed 30 m and drifted 80 m doing so).
                hover_mission = bool(
                    getattr(getattr(policy, "motion_adapter", None), "hover_ops", False)
                )
                executor = FixedWingVtolExecutor(
                    _ReaderAckLink(vehicle, mav_reader),
                    airframe=profile.id,
                    takeoff_method=takeoff_method,
                    landing_method=landing_method,
                    altitude_m=altitude_m,
                    loiter_radius_m=profile.orbit_radius(),
                    ground_speed_mps=profile.cruise_airspeed_mps,
                    position=lambda: _state_position(mav_reader.state),
                    yaw_deg=lambda: _state_yaw(mav_reader.state),
                    geo_reference=lambda: _home_reference(mav_reader.state),
                    land_mission=(
                        (
                            lambda **kwargs: _upload_fixed_wing_landing_mission(
                                _ReaderAckLink(vehicle, mav_reader),
                                mav_reader,
                                landing_method=kwargs.get("method", landing_method),
                                timeout_s=FixedWingVtolExecutor.STATE_TIMEOUT_S["land"],
                                loiter_radius_m=profile.orbit_radius(),
                            )
                        )
                        if profile.category == "fixed_wing"
                        else None
                    ),
                    transition_after_takeoff=not hover_mission,
                )
                recorder.meta["airframe_profile"] = profile.to_mapping()
                recorder.meta["vtol_hover_mission"] = hover_mission
                recorder.meta["takeoff_method"] = takeoff_method
                recorder.meta["landing_method"] = landing_method
                if takeoff_method in {"hand", "catapult"}:
                    recorder.meta["launch_detection"] = {
                        "status": "waiting_for_px4_detection",
                        "parameters": dict(
                            _profile_launch_parameters(plan.airframe, takeoff_method)
                        ),
                    }
                launch_applied: dict[str, Any] | None = None

                def _wait_fw_state(phase: str, timeout_s: float) -> bool:
                    nonlocal launch_applied
                    if phase == "land":
                        _wait_airframe_landed(mav_reader, timeout_s=timeout_s)
                        return True
                    if phase == "takeoff" and takeoff_method in {"hand", "catapult"}:
                        launch_applied = _apply_gazebo_launch(
                            plan,
                            environment,
                            _gazebo_model_name(plan, runtime),
                            _state_yaw(mav_reader.state),
                            takeoff_method,
                        )
                        recorder.meta["launch_detection"].update(
                            {
                                "applied": launch_applied,
                                "status": "launch_force_applied",
                            }
                        )
                        recorder.record_mavlink(
                            {"message_type": "gazebo_launch_applied", **launch_applied}
                        )
                        recorder.episode_path.write_text(
                            json.dumps(recorder.meta, indent=2), encoding="utf-8"
                        )
                        _progress(
                            "gazebo_launch_applied",
                            method=takeoff_method,
                            force_n=launch_applied["force_n"],
                            duration_s=launch_applied["duration_s"],
                        )
                    deadline = time.monotonic() + timeout_s
                    while time.monotonic() < deadline:
                        mav_reader.raise_if_failed()
                        state = _flight_state_snapshot(mav_reader.state)
                        z = _state_position(mav_reader.state)[2]
                        if phase == "takeoff":
                            detected = _launch_detected(launch_status_texts)
                            climbed = z <= -(altitude_m - 1.0)
                            if takeoff_method in {"hand", "catapult"}:
                                recorder.meta["launch_detection"].update(
                                    {
                                        "px4_detected": detected,
                                        "launch_status_texts": launch_status_texts[-20:],
                                        "climb_altitude_m": -float(z),
                                        "climb_confirmed": climbed,
                                    }
                                )
                            if climbed and (takeoff_method not in {"hand", "catapult"} or detected):
                                if takeoff_method in {"hand", "catapult"}:
                                    recorder.meta["launch_detection"]["status"] = (
                                        "detected_and_climbed"
                                    )
                                    recorder.episode_path.write_text(
                                        json.dumps(recorder.meta, indent=2), encoding="utf-8"
                                    )
                                return True
                        if phase == "transition_fw" and state["vtol_state"] in {4, 5}:
                            return True
                        if phase == "transition_mc" and state["vtol_state"] in {3, 6}:
                            return True
                        if phase == "orbit":
                            speed = state["airspeed_mps"]
                            if (
                                speed is not None
                                and profile.min_airspeed_mps <= speed <= profile.max_airspeed_mps
                            ):
                                return True
                        time.sleep(0.1)
                    return False

                executor._wait_for_state = _wait_fw_state
                arm_ack = executor.arm_and_takeoff(altitude_m)
                if arm_ack.status != "success":
                    raise RuntimeError(f"fixed-wing takeoff failed: {arm_ack.ack}")
                recorder.record_mavlink(
                    {
                        "message_type": "fixed_wing_takeoff_complete",
                        "method": takeoff_method,
                        "ack": _plain_record(arm_ack.ack),
                        "flight_state": _flight_state_snapshot(mav_reader.state),
                    }
                )
                _progress("takeoff_reached", method=takeoff_method)
            for index in range(plan.steps_per_episode):
                # perf_counter_ns is monotonic and has negligible overhead; keep
                # all emitted values in milliseconds for compact JSONL rows.
                step_t0 = time.perf_counter_ns()
                timing: dict[str, Any] = {}
                if index % max(1, plan.disk_check_interval_steps) == 0:
                    reason = disk_space_abort_reason(
                        plan.out, plan.min_free_gb, free_space_fn=free_space_fn
                    )
                    if reason is not None:
                        raise LowDiskSpace(reason)
                mav_reader.raise_if_failed()
                episode_watchdog.check()
                before_state = _snapshot_mavlink_state(mav_reader.state)
                pose_sim_stamp_ns = _pose_sim_stamp_ns(before_state)
                t = time.perf_counter_ns()
                before_sample = _fresh_sensor_snapshot(
                    sensor_reader,
                    time.monotonic_ns(),
                    SENSOR_FRAME_WAIT_S,
                    min_sensor_stamp_ns=pose_sim_stamp_ns,
                    poll_interval_s=plan.sensor_poll_interval_s,
                )
                timing["pre_frame_wait_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                before_state = mav_reader.aligned_state(before_sample.sensor_stamp_ns)
                t = time.perf_counter_ns()
                before = build_observation(
                    before_sample,
                    before_state,
                    ground_agl_max_age_s=(
                        policy.ground_projection.agl_max_age_s
                        if getattr(getattr(policy, "ground_projection", None), "use_agl", False)
                        else None
                    ),
                    camera_info={
                        **DEFAULT_CAMERA_INFO,
                        "width": plan.rgb_size[0],
                        "height": plan.rgb_size[1],
                        "fx": DEFAULT_CAMERA_INFO["fx"] * plan.rgb_size[0] / 1280.0,
                        "fy": DEFAULT_CAMERA_INFO["fy"] * plan.rgb_size[1] / 960.0,
                        "cx": DEFAULT_CAMERA_INFO["cx"] * plan.rgb_size[0] / 1280.0,
                        "cy": DEFAULT_CAMERA_INFO["cy"] * plan.rgb_size[1] / 960.0,
                    },
                    camera_mount=camera_mount,
                )
                before = _png_observation(before)
                timing["observation_pack_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                pose = tuple(before["pose"]["local_ned_m"])
                yaw = float(before["attitude"]["yaw_deg"])
                if plan.teacher == "expert_search":
                    action = policy.next_action(
                        pose_ned=pose,
                        yaw_deg=yaw,
                        camera_pose=before.get("camera_pose"),
                    )
                    labels = policy.labels(
                        pose_ned=pose,
                        yaw_deg=yaw,
                        camera_pose=before.get("camera_pose"),
                    )
                    if _dispatch_teacher_gimbal_command(
                        gimbal_controller,
                        labels,
                        vehicle_yaw_deg=yaw,
                        default_mode=camera_mount.stabilization if camera_mount is not None else "lock-horizon",
                    ):
                        labels["gimbal_dispatched"] = True
                elif plan.teacher == "onboard":
                    t = time.perf_counter_ns()
                    action, labels = policy.act(before, pose_ned=pose, yaw_deg=yaw)
                    timing["policy_total_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                    onboard_labels = labels.get("onboard") or {}
                    if isinstance(onboard_labels.get("fast_latency_s"), (int, float)):
                        timing["detector_ms"] = round(float(onboard_labels["fast_latency_s"]) * 1000.0, 3)
                    slow_rows = onboard_labels.get("slow_calls") or ()
                    if isinstance(slow_rows, (list, tuple)):
                        timing["slow_model_ms"] = round(
                            sum(float(row.get("latency_s", 0.0)) for row in slow_rows if isinstance(row, dict)) * 1000.0,
                            3,
                        )
                    # Health holds and recoveries skip perception: no detections then.
                    onboard_labels = labels.get("onboard") or {}
                    _progress(
                        "onboard_step",
                        step=index,
                        state=onboard_labels.get("state"),
                        detections=onboard_labels.get("detections"),
                        slow_calls=len(onboard_labels.get("slow_calls") or ()),
                    )
                elif plan.teacher == "hover":
                    action = policy.next_action(pose_ned=pose, yaw_deg=yaw)
                    labels = {"teacher": "hover"}
                else:
                    t = time.perf_counter_ns()
                    action = policy.next_action()
                    labels = {}
                    timing["policy_total_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                # Non-multirotor onboard policies emit a MotionCommand (loiter,
                # reposition, transition, ...).  The step record keeps the source
                # primitive (stop detection, dataset format); the command itself is
                # in labels["motion"] and is what the executor flies.
                motion = action if _is_motion_command(action) else None
                step_action = action
                if motion is not None:
                    step_action = (
                        motion.action
                        if motion.action is not None
                        else ActionPrimitive.hold(max(float(motion.duration_s or 0.0), 1.0))
                    )
                    labels = {**labels, "motion": motion.to_dict()}
                    commanded_delta = (
                        tuple(
                            float(t) - float(p)
                            for t, p in zip(motion.target_ned, pose, strict=False)
                        )
                        if motion.target_ned is not None
                        else (0.0, 0.0, 0.0)
                    )
                else:
                    commanded_delta = primitive_to_ned(action, yaw)[:3]
                _progress("primitive_start", step=index, action=action.to_dict())
                # Optional continuous capture runs during the primitive.  It only
                # consumes already decoded latest-wins frames and never changes
                # command timing or policy decisions.
                action_started = time.monotonic()
                primitive_started_ns = time.perf_counter_ns()
                next_video_s = action_started
                result = None
                result_box: list[Any] = []
                execute_error: list[BaseException] = []

                def _execute() -> None:
                    try:
                        executed = executor.execute(action)  # noqa: B023
                        if motion is not None and executed.status == "success":  # noqa: B023
                            motion_wait = _wait_motion_command(
                                motion,  # noqa: B023
                                mav_reader,
                                cruise_mps=getattr(
                                    _airframe_profile(plan.airframe), "cruise_airspeed_mps", 15.0
                                ),
                            )
                            executed.ack["motion_wait"] = motion_wait
                            # COMMAND_ACK acceptance only proves that PX4 took
                            # the command.  A guided fixed-wing leg is complete
                            # only after telemetry reaches the target/pass
                            # geometry; propagate a bounded wait timeout into the
                            # recorded result instead of reporting success.
                            executed = _apply_motion_wait_result(executed, motion_wait)
                        result_box.append(executed)  # noqa: B023
                    except BaseException as exc:
                        execute_error.append(exc)  # noqa: B023

                execute_thread = threading.Thread(target=_execute, name="sim-teacher-action")
                execute_thread.start()
                while execute_thread.is_alive():
                    now = time.monotonic()
                    if video_writer is not None and now >= next_video_s:
                        try:
                            video_stamp = _pose_sim_stamp_ns(
                                _snapshot_mavlink_state(mav_reader.state)
                            )
                            sensor_reader.request(video_stamp or 0)
                            sample = sensor_reader.latest
                            if _new_video_sample(sample, last_video_stamp_ns[0]):
                                last_video_stamp_ns[0] = sample.sensor_stamp_ns
                                aligned = mav_reader.aligned_state(sample.sensor_stamp_ns)
                                video_obs = _png_observation(
                                    build_observation(
                                        sample,
                                        aligned,
                                        camera_info={
                                            **DEFAULT_CAMERA_INFO,
                                            "width": plan.rgb_size[0],
                                            "height": plan.rgb_size[1],
                                            "fx": DEFAULT_CAMERA_INFO["fx"]
                                            * plan.rgb_size[0]
                                            / 1280.0,
                                            "fy": DEFAULT_CAMERA_INFO["fy"]
                                            * plan.rgb_size[1]
                                            / 960.0,
                                            "cx": DEFAULT_CAMERA_INFO["cx"]
                                            * plan.rgb_size[0]
                                            / 1280.0,
                                            "cy": DEFAULT_CAMERA_INFO["cy"]
                                            * plan.rgb_size[1]
                                            / 960.0,
                                        },
                                        camera_mount=camera_mount,
                                    )
                                )
                                video_obs["flight_stage"] = "action"
                                if not plan.capture_depth:
                                    video_obs["depth"] = None
                                video_writer.write(video_obs, action_index=index)
                        except Exception as exc:
                            recorder.record_mavlink(
                                {"message_type": "video_capture_error", "error": str(exc)}
                            )
                        next_video_s += 1.0 / plan.video_hz
                    execute_thread.join(timeout=max(0.001, min(0.02, next_video_s - now)))
                execute_thread.join()
                if execute_error:
                    raise execute_error[0]
                result = result_box[0]
                _raise_if_px4_clock_stalled(result, index, sim_clock=primitive_sim_clock)
                timing["px4_clock_diagnostics"] = primitive_sim_clock.diagnostic_snapshot()
                timing["command_and_primitive_wait_ms"] = round(
                    (time.perf_counter_ns() - primitive_started_ns) / 1e6, 3
                )
                # The executor includes PX4 command transport, ACK polling,
                # motion, completion checks and any settle/timeout wait. Keep
                # explicit keys so offline analysis can distinguish unavailable
                # sub-boundaries from zero-duration phases.
                timing.setdefault("px4_ack_wait_ms", 0.0)
                timing.setdefault("command_dispatch_ms", 0.0)
                timing.setdefault("executor_wait_ms", timing["command_and_primitive_wait_ms"])
                timing.setdefault("motion_ms", round(float(result.elapsed_s) * 1000.0, 3))
                timing.setdefault("settle_ms", 0.0)
                timing.setdefault("completion_check_ms", 0.0)
                timing.setdefault("timeout_ms", timing["command_and_primitive_wait_ms"] if result.status == "timeout" else 0.0)
                timing["primitive_status"] = result.status
                if video_writer is not None:
                    # Ensure the final in-flight frame is represented at the
                    # primitive boundary when the simulator emitted one.
                    sample = sensor_reader.latest
                    if sample is not None:
                        try:
                            aligned = mav_reader.aligned_state(sample.sensor_stamp_ns)
                            final_obs = _png_observation(
                                build_observation(
                                    sample,
                                    aligned,
                                    camera_info={
                                        **DEFAULT_CAMERA_INFO,
                                        "width": plan.rgb_size[0],
                                        "height": plan.rgb_size[1],
                                        "fx": DEFAULT_CAMERA_INFO["fx"] * plan.rgb_size[0] / 1280.0,
                                        "fy": DEFAULT_CAMERA_INFO["fy"] * plan.rgb_size[1] / 960.0,
                                        "cx": DEFAULT_CAMERA_INFO["cx"] * plan.rgb_size[0] / 1280.0,
                                        "cy": DEFAULT_CAMERA_INFO["cy"] * plan.rgb_size[1] / 960.0,
                                    },
                                        camera_mount=camera_mount,
                                )
                            )
                            final_obs["flight_stage"] = "action_end"
                            if not plan.capture_depth:
                                final_obs["depth"] = None
                            video_writer.write(final_obs, action_index=index)
                        except Exception:
                            pass
                primitive_end_ns = time.monotonic_ns()
                mav_reader.raise_if_failed()
                _progress(
                    "primitive_end", step=index, status=result.status, elapsed_s=result.elapsed_s
                )
                mav_reader.raise_if_failed()
                # obs_after must be rendered after the primitive completed, not mid-motion.
                after_state = _snapshot_mavlink_state(mav_reader.state)
                after_pose_sim_stamp_ns = _pose_sim_stamp_ns(after_state)
                t = time.perf_counter_ns()
                after_sample = _fresh_sensor_snapshot(
                    sensor_reader,
                    primitive_end_ns,
                    SENSOR_FRAME_WAIT_S,
                    min_sensor_stamp_ns=after_pose_sim_stamp_ns,
                    poll_interval_s=plan.sensor_poll_interval_s,
                )
                timing["post_frame_wait_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                after_state = mav_reader.aligned_state(after_sample.sensor_stamp_ns)
                t = time.perf_counter_ns()
                after = build_observation(
                    after_sample,
                    after_state,
                    ground_agl_max_age_s=(
                        policy.ground_projection.agl_max_age_s
                        if getattr(getattr(policy, "ground_projection", None), "use_agl", False)
                        else None
                    ),
                    camera_info={
                        **DEFAULT_CAMERA_INFO,
                        "width": plan.rgb_size[0],
                        "height": plan.rgb_size[1],
                        "fx": DEFAULT_CAMERA_INFO["fx"] * plan.rgb_size[0] / 1280.0,
                        "fy": DEFAULT_CAMERA_INFO["fy"] * plan.rgb_size[1] / 960.0,
                        "cx": DEFAULT_CAMERA_INFO["cx"] * plan.rgb_size[0] / 1280.0,
                        "cy": DEFAULT_CAMERA_INFO["cy"] * plan.rgb_size[1] / 960.0,
                    },
                    camera_mount=camera_mount,
                )
                after = _png_observation(after)
                timing["after_observation_pack_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                ack = {
                    **_plain_record(result.ack),
                    "type": "SET_POSITION_TARGET_LOCAL_NED",
                    # PX4 has no per-position-setpoint COMMAND_ACK.  A
                    # timeout still means the command stream was accepted
                    # and executed until the bounded completion deadline.
                    "action_ack": result.status in {"success", "timeout"},
                    "arm": _plain_record(arm_ack),
                }
                achieved_delta = list(result.achieved_delta_ned)
                if motion is not None:
                    # Guided commands report no displacement; measure it from telemetry.
                    achieved_delta = [
                        float(a) - float(b)
                        for a, b in zip(after["pose"]["local_ned_m"], pose, strict=False)
                    ]
                continue_loop = _record_policy_step(
                    recorder,
                    before,
                    step_action,
                    ack=ack,
                    outcome={
                        "status": result.status,
                        "elapsed_s": result.elapsed_s,
                        "commanded_delta_ned": list(commanded_delta),
                        "achieved_delta_ned": achieved_delta,
                        "achieved_yaw_delta_deg": result.achieved_yaw_delta_deg,
                    },
                    obs_after=after,
                    labels=labels,
                    timing={
                        **timing,
                        # Stable phase names consumed by the offline analyzer.
                        "frame_before_wait": timing.get("pre_frame_wait_ms", 0.0),
                        "frame_after_wait": timing.get("post_frame_wait_ms", 0.0),
                        "policy_detection": timing.get("detector_ms", 0.0),
                        "policy_tracking": timing.get("tracking_ms", 0.0),
                        "policy_slow_queue": timing.get("slow_queue_ms", 0.0),
                        "policy_slow_execute": timing.get("slow_model_ms", 0.0),
                        "policy_planning": timing.get("planning_ms", 0.0),
                        "policy_safety": timing.get("safety_ms", 0.0),
                        "command_ack": timing.get("px4_ack_wait_ms", 0.0),
                        "primitive_accept_wait": timing.get("px4_ack_wait_ms", 0.0),
                        "primitive_motion": timing.get("motion_ms", 0.0),
                        "primitive_settle": timing.get("settle_ms", 0.0),
                        "primitive_timeout": (
                            timing.get("motion_ms", 0.0)
                            if timing.get("primitive_status") == "timeout"
                            else 0.0
                        ),
                        "total_ms": round((time.perf_counter_ns() - step_t0) / 1e6, 3),
                    },
                )
                # STOP is a terminal policy action.  Keep this step in the
                # episode (including its observation pair), then immediately
                # leave the primitive loop so we do not execute redundant
                # STOPs while still performing the normal RTL/LAND teardown.
                if not continue_loop:
                    _progress("policy_stop", step=index)
                    break
            mav_reader.raise_if_failed()
            # Teardown lands in place from the hover altitude. Slow SITL lockstep can
            # make a fixed wall-clock wait expire before PX4 reaches the ground.
            mav_reader.raise_if_failed()
            _progress("land")
            if not airframe_uses_multirotor_executor(plan.airframe, base_dir=ROOT):
                rtl_started_ns = time.monotonic_ns()
                executor.rtl()
                # Confirm PX4 is really in AUTO.RTL before anything else is sent:
                # otherwise the landing mission upload races the mode change and
                # PX4 may skip RTL ("No valid mission available").
                rtl_mode = _wait_px4_mode(
                    mav_reader, (4, 5), timeout_s=RTL_MODE_CONFIRM_S, after_ns=rtl_started_ns
                )
                if not rtl_mode["confirmed"]:
                    executor.rtl()
                    rtl_mode = _wait_px4_mode(
                        mav_reader,
                        (4, 5),
                        timeout_s=RTL_MODE_CONFIRM_S,
                        after_ns=time.monotonic_ns(),
                    )
                recorder.meta["rtl_mode"] = rtl_mode
                _progress("rtl", **rtl_mode)
                profile = _airframe_profile(plan.airframe)
                if profile is not None and profile.category in {"fixed_wing", "vtol"}:
                    # Let RTL bring the aircraft back over home before the landing
                    # (mission or VTOL back-transition) replaces it.
                    _progress(
                        "rtl_home",
                        **_wait_near_home(
                            mav_reader,
                            radius_m=2.0 * profile.orbit_radius() + 50.0,
                            timeout_s=FixedWingVtolExecutor.STATE_TIMEOUT_S["rtl"],
                        ),
                    )
            # Fixed-wing/VTOL use their physical landing lifecycle. x500 uses
            # the OFFBOARD -> LAND helper with PX4-time waits on Linux.
            if airframe_uses_multirotor_executor(plan.airframe, base_dir=ROOT):
                land_result = _land_and_wait(
                    executor,
                    mav_reader,
                    telemetry_connection,
                    setpoint_streamer,
                )
            else:
                land_result = _fixed_wing_or_vtol_land_and_wait(executor, mav_reader)
            land_result["flight_state"] = _flight_state_snapshot(mav_reader.state)
            recorder.record_mavlink({"message_type": "collector_landing_complete", **land_result})
            if video_writer is not None:
                recorder.meta["video"] = video_writer.close()
            _copy_mover_driver_log(plan, environment, recorder, mover_driver)
            recorder.meta["mover_driver"] = mover_driver
            recorder.mark_finished(status="complete")
            recorder.record_mavlink(
                {"message_type": "collector_episode_complete", "container": environment.name}
            )
            _drain_sim_operator_link(policy)
            if hasattr(policy, "close"):
                policy.close()
            if gcs_heartbeat_stop is not None:
                gcs_heartbeat_stop.set()
            if gcs_heartbeat_thread is not None:
                gcs_heartbeat_thread.join(timeout=2.0)
            if sensor_reader is not None:
                sensor_reader.stop()
            if setpoint_streamer is not None:
                setpoint_streamer.stop()
            if mav_reader is not None:
                mav_reader.stop()
            if sensor_process is not None:
                sensor_process.terminate()
                sensor_process.wait(timeout=3)
            for connection in (telemetry_connection,):
                if connection is not None:
                    connection.close()
    except LowDiskSpace as exc:
        record_container_failure(exc)
        try:
            _drain_sim_operator_link(policy, timeout_s=10.0)
        except Exception:
            pass
        if hasattr(policy, "close"):
            try:
                policy.close()
            except Exception:
                pass
        if gcs_heartbeat_stop is not None:
            gcs_heartbeat_stop.set()
        if gcs_heartbeat_thread is not None:
            gcs_heartbeat_thread.join(timeout=2.0)
        if sensor_reader is not None:
            sensor_reader.stop()
        if setpoint_streamer is not None:
            setpoint_streamer.stop()
        if mav_reader is not None:
            mav_reader.stop()
        if sensor_process is not None:
            try:
                sensor_process.terminate()
                sensor_process.wait(timeout=3)
            except Exception:
                try:
                    sensor_process.kill()
                except Exception:
                    pass
        for connection in (telemetry_connection,):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if video_writer is not None:
            recorder.meta["video"] = video_writer.close()
        recorder.mark_finished(status="aborted_low_disk", failure=str(exc))
        _progress("aborted_low_disk", episode=str(recorder.episode_dir), reason=str(exc))
        return recorder.episode_dir
    except Exception as exc:
        record_container_failure(exc)
        try:
            _drain_sim_operator_link(policy, timeout_s=10.0)
        except Exception:
            pass
        if hasattr(policy, "close"):
            try:
                policy.close()
            except Exception:
                pass
        if gcs_heartbeat_stop is not None:
            gcs_heartbeat_stop.set()
        if gcs_heartbeat_thread is not None:
            gcs_heartbeat_thread.join(timeout=2.0)
        if sensor_reader is not None:
            sensor_reader.stop()
        if setpoint_streamer is not None:
            setpoint_streamer.stop()
        if mav_reader is not None:
            mav_reader.stop()
        if sensor_process is not None:
            try:
                sensor_process.terminate()
                sensor_process.wait(timeout=3)
            except Exception:
                try:
                    sensor_process.kill()
                except Exception:
                    pass
        for connection in (telemetry_connection,):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if recorder is not None:
            recorder.close_mavlink_stream()
        if video_writer is not None:
            recorder.meta["video"] = video_writer.close()
        recorder.mark_finished(status="failed", failure=f"{type(exc).__name__}: {exc}")
        raise
    return recorder.episode_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument(
        "--airframe",
        default="x500_mono_cam",
        help="airframe profile/model id; x500_mono_cam keeps the legacy multirotor path",
    )
    parser.add_argument("--takeoff-method", choices=("runway", "hand", "catapult", "vtol"))
    parser.add_argument(
        "--smoke-flight", action="store_true", help="use deterministic fixed-wing smoke route"
    )
    parser.add_argument(
        "--teacher", choices=("explore", "expert_search", "onboard", "hover"), required=True
    )
    parser.add_argument(
        "--teacher-target",
        choices=("true", "confuser"),
        default="true",
        help="expert_search flies to the true target or to a look-alike confuser (hard negatives)",
    )
    parser.add_argument("--depth-format", choices=("npy", "npz", "png16"), default="npy")
    parser.add_argument(
        "--no-depth",
        action="store_true",
        help="do not persist depth frames (the sensor stream still carries them)",
    )
    parser.add_argument(
        "--rgb-format",
        choices=("png", "jpeg"),
        default="png",
        help="recorded RGB encoding; segmentation stays lossless PNG either way",
    )
    parser.add_argument(
        "--rgb-quality", type=int, default=90, help="JPEG quality for --rgb-format jpeg"
    )
    parser.add_argument("--deduplicate-depth", action="store_true")
    parser.add_argument("--compress-mavlink", action="store_true")
    parser.add_argument("--onboard-frame-format", choices=("png", "jpeg"), default="png")
    parser.add_argument("--onboard-frame-quality", type=int, default=90)
    parser.add_argument(
        "--async-metadata-writes",
        action="store_true",
        help="write episode.json snapshots on a background thread; steps and images remain synchronous",
    )
    parser.add_argument(
        "--sensor-poll-interval-s",
        type=float,
        default=0.02,
        help="frame wait polling interval; freshness and simulation timestamp gates are unchanged",
    )
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)
    parser.add_argument(
        "--disk-check-interval-steps", type=int, default=DEFAULT_DISK_CHECK_INTERVAL_STEPS
    )
    parser.add_argument(
        "--video-hz",
        type=float,
        default=0.0,
        help="continuous RGB/segmentation/depth video rate during actions; 0 disables it",
    )
    parser.add_argument(
        "--video-max-gb",
        type=float,
        default=DEFAULT_VIDEO_MAX_BYTES / 1024**3,
        help="per-episode continuous-video byte cap in GiB",
    )
    parser.add_argument(
        "--onboard-config",
        type=Path,
        default=None,
        help="JSON overrides for --teacher onboard (model paths, thresholds)",
    )
    parser.add_argument(
        "--task-text",
        default=None,
        help="explicit natural-language task for --teacher onboard (overrides scenario index)",
    )
    parser.add_argument("--episodes", type=int, required=True)
    parser.add_argument("--steps-per-episode", type=int, required=True)
    parser.add_argument("--container-prefix", default="md-sim-teacher")
    parser.add_argument(
        "--image",
        default=os.environ.get("MULTIDETECT_SIM_IMAGE", PINNED_IMAGE),
        help="pinned image, or an explicit local multidetect/px4-sitl-gazebo:<tag> GPU variant",
    )
    parser.add_argument(
        "--gpu-render",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable NVIDIA EGL/Ogre2 headless rendering (also via MULTIDETECT_SIM_GPU)",
    )
    parser.add_argument("--gpu-device", type=str, default=None)
    parser.add_argument(
        "--gpu-devices",
        type=str,
        default=None,
        help="comma-separated GPU pool for workers; defaults to 1,2 and avoids 0,3",
    )
    parser.add_argument(
        "--worker-index",
        type=int,
        default=0,
        help="zero-based worker for GPU/CPU allocation; CPU prefers the leased SITL slot",
    )
    parser.add_argument("--operator-port", type=int, default=14580)
    parser.add_argument("--sitl-lock-path", type=Path, default=default_lock_path())
    parser.add_argument(
        "--attach-container",
        default=None,
        help="reuse an already-running PX4/Gazebo container instead of starting/removing one",
    )
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--telemetry-port", type=int, default=DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--sim-network", choices=("bridge", "host"), default=None)
    parser.add_argument("--sim-instance", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--max-episode-s", type=float, default=None)
    parser.add_argument("--heartbeat-timeout-s", type=float, default=DEFAULT_HEARTBEAT_TIMEOUT_S)
    parser.add_argument("--keep-container-on-failure", action="store_true")
    parser.add_argument(
        "--keep-px4-ulog",
        action="store_true",
        help="copy PX4 .ulg flight logs to <out>/jobs/<container>/px4-ulog before cleanup",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rgb-size", type=_parse_size, default=DEFAULT_RGB_SIZE)
    parser.add_argument("--depth-size", type=_parse_size, default=DEFAULT_DEPTH_SIZE)
    parser.add_argument(
        "--segmentation",
        action="store_true",
        help="mount the teacher camera overlay and require exact-time segmentation frames",
    )
    parser.add_argument(
        "--camera-noise-overlay",
        action="store_true",
        help="mount the repository mono_cam Gaussian-noise overlay",
    )
    parser.add_argument("--wind-vehicle-overlay", action="store_true")
    parser.add_argument(
        "--sim-param",
        action="append",
        default=[],
        type=_parse_sim_param,
        metavar="NAME=VALUE",
        help="extra SIM-only PX4 parameter for this run (repeatable)",
    )
    parser.add_argument(
        "--depth-encoding", choices=("UINT16_MM", "FLOAT32_M"), default=DEFAULT_DEPTH_ENCODING
    )
    parser.add_argument(
        "--takeoff-altitude-m",
        type=float,
        default=None,
        help=(
            "takeoff altitude in metres; defaults to max generated-world obstacle top + 4 m "
            f"(clamped to {DEFAULT_TAKEOFF_ALTITUDE_M:g}..{MAX_TAKEOFF_ALTITUDE_M:g} m)"
        ),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.episodes < 1 or args.steps_per_episode < 1:
        raise SystemExit("--episodes and --steps-per-episode must be positive")
    if args.video_hz < 0.0 or args.video_max_gb <= 0.0:
        raise SystemExit("--video-hz must be non-negative and --video-max-gb positive")
    if not 0.001 <= args.sensor_poll_interval_s <= 1.0:
        raise SystemExit("--sensor-poll-interval-s must be between 0.001 and 1.0")
    if not 1 <= args.rgb_quality <= 100:
        raise SystemExit("--rgb-quality must be between 1 and 100")
    if not 1.0 <= args.timeout_s <= 86400.0:
        raise SystemExit("--timeout-s must be between 1 and 86400 seconds")
    max_episode_s = args.timeout_s if args.max_episode_s is None else args.max_episode_s
    if not 1.0 <= max_episode_s <= 86400.0:
        raise SystemExit("--max-episode-s must be between 1 and 86400 seconds")
    if not 1.0 <= args.heartbeat_timeout_s <= 86400.0:
        raise SystemExit("--heartbeat-timeout-s must be between 1 and 86400 seconds")
    if args.takeoff_altitude_m is not None:
        try:
            resolve_takeoff_altitude("", args.takeoff_altitude_m)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    scenario = args.scenario.resolve()
    if not scenario.is_file():
        raise SystemExit(f"scenario not found: {scenario}")
    runtime_path, truth_path = _scenario_channels(scenario)
    _airframe_profile(args.airframe)
    _validate_runtime(runtime_path, args.airframe)
    plan = CollectionPlan(
        str(scenario),
        str(runtime_path),
        str(truth_path),
        args.teacher,
        args.episodes,
        args.steps_per_episode,
        args.seed,
        str(args.out.resolve()),
        container_prefix=args.container_prefix,
        timeout_s=args.timeout_s,
        max_episode_s=max_episode_s,
        heartbeat_timeout_s=args.heartbeat_timeout_s,
        keep_container_on_failure=args.keep_container_on_failure,
        control_port=args.control_port,
        telemetry_port=args.telemetry_port,
        sim_network=args.sim_network,
        sim_instance=args.sim_instance,
        rgb_size=args.rgb_size,
        depth_size=args.depth_size,
        depth_encoding=args.depth_encoding,
        segmentation=args.segmentation,
        camera_noise_overlay=args.camera_noise_overlay,
        wind_vehicle_overlay=args.wind_vehicle_overlay,
        extra_sim_params=tuple(tuple(item.split("=", 1)) for item in args.sim_param if "=" in item),
        takeoff_altitude_m=args.takeoff_altitude_m,
        onboard_config=str(args.onboard_config.resolve()) if args.onboard_config else None,
        depth_format=args.depth_format,
        capture_depth=not args.no_depth,
        rgb_format=args.rgb_format,
        rgb_quality=args.rgb_quality,
        deduplicate_depth=args.deduplicate_depth,
        compress_mavlink=args.compress_mavlink,
        onboard_frame_format=args.onboard_frame_format,
        onboard_frame_quality=args.onboard_frame_quality,
        async_metadata=args.async_metadata_writes,
        sensor_poll_interval_s=args.sensor_poll_interval_s,
        min_free_gb=args.min_free_gb,
        disk_check_interval_steps=args.disk_check_interval_steps,
        video_hz=args.video_hz,
        video_max_bytes=int(args.video_max_gb * 1024**3),
        airframe=args.airframe,
        teacher_target=args.teacher_target,
        smoke_flight=args.smoke_flight,
        takeoff_method=args.takeoff_method,
        keep_px4_ulog=args.keep_px4_ulog,
        attach_container=args.attach_container,
        task_text=args.task_text,
        image=args.image,
        gpu_render=args.gpu_render,
        gpu_device=args.gpu_device,
        gpu_devices=(
            _split_gpu_list(args.gpu_devices, label="--gpu-devices")
            if args.gpu_devices is not None
            else None
        ),
        worker_index=args.worker_index,
        operator_port=args.operator_port,
    )
    _validate_spawn_clearance(plan)
    # Reject deterministic CPU configuration errors before contacting Docker.
    # After acquiring our own lease below, resolution uses the actual slot.
    resolve_cpu_isolation(plan.worker_index)
    if args.dry_run:
        print(json.dumps(plan.to_mapping(), ensure_ascii=False, indent=2))
        return 0
    if not _docker_available():
        raise SystemExit("Docker daemon is not reachable; use --dry-run or start Docker")
    args.out.mkdir(parents=True, exist_ok=True)
    completed: list[str] = []
    with sitl_lock_context(
        SitlLock(args.sitl_lock_path, label=args.container_prefix, min_memory_gb=5.0)
    ):
        for index in range(args.episodes):
            free = free_space_bytes(args.out)
            if free < plan.min_free_gb * 1024**3:
                print(
                    json.dumps(
                        {
                            "event": "aborted_low_disk",
                            "free_gb": free / 1024**3,
                            "threshold_gb": plan.min_free_gb,
                        }
                    ),
                    file=sys.stderr,
                )
                break
            path = run_episode(plan, truth_path, index)
            completed.append(str(path))
            print(json.dumps({"episode": index, "path": str(path)}, ensure_ascii=False))
            try:
                status = json.loads((Path(path) / "episode.json").read_text(encoding="utf-8")).get(
                    "status"
                )
            except (OSError, ValueError):
                status = None
            if status == "aborted_low_disk":
                print(json.dumps({"event": "aborted_low_disk", "episode": index}), file=sys.stderr)
                break
    print(json.dumps({"episodes": completed, "out": str(args.out.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
