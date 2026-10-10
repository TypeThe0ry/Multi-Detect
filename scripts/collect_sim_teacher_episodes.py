#!/usr/bin/env python3
"""Collect raw SIM teacher episodes in disposable PX4/Gazebo containers.

Runtime recording deliberately does no provenance hashing.  Run the offline
auditor after collection.  ``--dry-run`` validates the scenario and prints the
planned launch without starting Docker.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import shutil
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.sim_teacher import (  # noqa: E402
    EpisodeRecorder,
    ExpertSearchPolicy,
    ExploreBounds,
    ExplorePolicy,
    PymavlinkPrimitiveExecutor,
    SetpointStreamer,
    primitive_to_ned,
)
from multidetect.sim_teacher.isolation import validate_local_endpoint  # noqa: E402
from multidetect.sim_teacher.streams import (  # noqa: E402
    MavlinkReader,
    MavlinkState,
    SensorStreamReader,
    build_observation,
)
from scripts.run_simulator_docker import build_docker_simulator_plan  # noqa: E402

PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
DEFAULT_TIMEOUT_S = 300.0
SENSOR_FRAME_WAIT_S = 15.0
DEFAULT_CAMERA_INFO = {
    "width": 1280,
    "height": 960,
    "fx": 539.93636802453,
    "fy": 539.93636802453,
    "cx": 640.0,
    "cy": 480.0,
}
DEFAULT_RGB_SIZE = (640, 480)
DEFAULT_DEPTH_SIZE = (320, 240)
DEFAULT_DEPTH_ENCODING = "UINT16_MM"


def _parse_size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0 or width > 4096 or height > 4096:
        raise argparse.ArgumentTypeError("size is outside supported range")
    return width, height
DEFAULT_TAKEOFF_ALTITUDE_M = 5.0
DEFAULT_CONTROL_PORT = 14540
DEFAULT_TELEMETRY_PORT = 14541
DEFAULT_HEARTBEAT_TIMEOUT_S = 120.0
DEFAULT_PREARM_HEALTH_TIMEOUT_S = 45.0
ARM_MAX_ATTEMPTS = 6
ARM_RETRY_INTERVAL_S = 1.0
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
    rgb_size: tuple[int, int] = DEFAULT_RGB_SIZE
    depth_size: tuple[int, int] = DEFAULT_DEPTH_SIZE
    depth_encoding: str = DEFAULT_DEPTH_ENCODING

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        result["docker_command"] = docker_command(self)
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


def _validate_runtime(runtime_path: Path) -> dict[str, Any]:
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if not isinstance(runtime, dict):
        raise ValueError("runtime channel must be a JSON object")
    vehicle = runtime.get("vehicle", {})
    model = vehicle.get("model") if isinstance(vehicle, dict) else None
    if model and "multirotor" not in str(model).lower() and "x500" not in str(model).lower():
        raise ValueError("SIM teacher collector currently supports multirotor only")
    for sensor in runtime.get("sensors", []):
        if isinstance(sensor, dict) and isinstance(sensor.get("stream_uri"), str):
            validate_local_endpoint(sensor["stream_uri"])
    return runtime


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
            dry_run=True,
        )
    except Exception as exc:
        raise ValueError(f"unable to build simulator stack plan: {exc}") from exc
    command = list(stack_plan.command)
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
    executable = (
        shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    )
    if not Path(executable).is_file() and not shutil.which(executable):
        return "docker"
    return executable


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
    world = "default"
    model = "x500_mono_cam_0"
    image_topic = f"/world/{world}/model/{model}/link/camera_link/sensor/camera/image"
    camera_topic = f"/world/{world}/model/{model}/link/camera_link/sensor/camera/camera_info"
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
    calibration_path = (
        ROOT / "simulation" / "gazebo" / "calibration" / "gazebo-multirotor-front-camera-v1.json"
    )
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
    if depth is not None:
        depth_topic = (
            f"/world/{world}/model/{model}/link/depth_camera_link/sensor/depth_camera/depth_image"
        )
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


class DockerSimEnvironment:
    """Own one simulator container and always remove it on exit."""

    def __init__(self, plan: CollectionPlan, episode_id: str):
        self.plan = plan
        self.name = f"{plan.container_prefix}-{episode_id}"
        self.started = False
        self._log = None

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

    def __enter__(self) -> DockerSimEnvironment:
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

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.started and not (exc_type is not None and self.plan.keep_container_on_failure):
            subprocess.run(
                ["docker", "rm", "--force", self.name],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.started = False
            self._progress("cleanup", container=self.name, removed=True)
        elif self.started:
            self._progress("cleanup", container=self.name, removed=False, kept=True)
        return False


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


def _truth_target(
    truth_path: Path,
    origin: tuple[float, float, float] = (1.352100, 103.819800, 18.0),
) -> tuple[tuple[float, float, float], str]:
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    targets = data.get("targets", []) if isinstance(data, dict) else []
    target = targets[0] if isinstance(targets, list) and targets else {}
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


def _policy_for(plan: CollectionPlan, truth_path: Path, seed: int):
    if plan.teacher == "explore":
        return ExplorePolicy(seed=seed, bounds=ExploreBounds())
    target, label = _truth_target(truth_path, _scenario_origin(plan))
    return ExpertSearchPolicy(target_ned=target, intrinsics=DEFAULT_CAMERA_INFO), label


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
) -> bool:
    """Persist one policy step and report whether the primitive loop continues."""
    recorder.record_step(
        before,
        action,
        ack=ack,
        outcome=outcome,
        obs_after=obs_after,
        labels=labels,
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


def _set_sim_params(
    plan: CollectionPlan, environment: DockerSimEnvironment
) -> list[dict[str, Any]]:
    """Apply the SIM-only GCS input setting after PX4 heartbeat is live."""
    executable = _docker_executable(plan)
    applied: list[dict[str, Any]] = []
    for parameter, value in SIM_PREARM_PARAMETERS:
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


def _set_mode(connection: Any, mode: str) -> None:
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
        time.sleep(0.02)
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
    mav = getattr(connection, "mav", None)

    def source_id(name: str) -> int | None:
        value = getattr(mav, name, None)
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    while attempts < ARM_MAX_ATTEMPTS and time.monotonic() < deadline:
        attempts += 1
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
        wait_deadline = min(deadline, time.monotonic() + ARM_RETRY_INTERVAL_S)
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


def run_episode(plan: CollectionPlan, truth_path: Path, episode_index: int) -> Path:
    episode_id = f"episode-{episode_index:04d}-{uuid.uuid4().hex[:8]}"
    policy_result = _policy_for(plan, truth_path, plan.seed + episode_index)
    if plan.teacher == "expert_search":
        policy, target_label = policy_result
        task_text = "\u627e\u5230" + str(target_label) + "\u5e76\u8bb0\u5f55\u4f4d\u7f6e"
    else:
        policy = policy_result
        task_text = "\u63a2\u7d22\u573a\u666f\u5e76\u8bb0\u5f55\u89c2\u6d4b"
    recorder = EpisodeRecorder(plan.out, episode_id, task_text=task_text, sensor_status="live")
    started = time.monotonic()
    telemetry_connection = None
    sensor_process = None
    sensor_reader = mav_reader = None
    setpoint_streamer = None
    gcs_heartbeat_stop = gcs_heartbeat_thread = None
    try:
        with DockerSimEnvironment(plan, episode_id) as environment:
            environment.wait(_episode_budget(started, plan.max_episode_s, 20.0))
            _progress("gz_world_loaded", container=environment.name)
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
                f"udpin:127.0.0.1:{plan.telemetry_port}",
                source_system=GCS_SOURCE_SYSTEM,
                source_component=GCS_SOURCE_COMPONENT,
                autoreconnect=False,
            )
            recorder.begin_mavlink_stream()
            mav_reader = MavlinkReader(
                telemetry_connection, on_message=recorder.record_mavlink
            ).start()
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
            if any(
                isinstance(s, dict) and s.get("kind") == "depth_camera"
                for s in runtime.get("sensors", [])
            ):
                # The helper's contract is deterministic and is embedded in
                # the command; SensorStreamReader validates it on every frame.
                model = "x500_mono_cam_0"
                calibration = json.loads(
                    (
                        ROOT
                        / "simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json"
                    ).read_text(encoding="utf-8")
                )["calibration"]
                depth = next(s for s in runtime["sensors"] if s.get("kind") == "depth_camera")
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
            altitude_m = DEFAULT_TAKEOFF_ALTITUDE_M
            start_pos = _state_position(mav_reader.state)
            setpoint_streamer = SetpointStreamer(vehicle, rate_hz=20.0)
            setpoint_streamer.set_target(
                start_pos[0],
                start_pos[1],
                -altitude_m,
                _state_yaw(mav_reader.state),
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
            recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
            arm_ack = _arm(
                telemetry_connection,
                mav_reader,
                mavutil,
                _episode_budget(started, plan.max_episode_s, 30.0),
                setpoint=setpoint_streamer.send_once,
                heartbeat_connection=telemetry_connection,
            )
            _progress("arm_ack", ack=arm_ack)
            # Keep a valid climb target streamed at 20 Hz, starting one second before OFFBOARD.
            setpoint_streamer.set_target(
                start_pos[0], start_pos[1], -altitude_m, _state_yaw(mav_reader.state)
            )
            time.sleep(1.0)
            setpoint_streamer.raise_if_failed()
            offboard_modes = {"393216", "OFFBOARD", "OFFBOARD_NED"}
            last_mode_request = [0.0]

            def _offboard_entered() -> bool:
                setpoint_streamer.raise_if_failed()
                now = time.monotonic()
                if now - last_mode_request[0] >= 2.0:
                    last_mode_request[0] = now
                    _set_mode(telemetry_connection, "OFFBOARD")
                hb = mav_reader.state.get("HEARTBEAT")
                return hb is not None and str(_field(hb, "custom_mode", "")) in offboard_modes

            # The HEARTBEAT seen right after the request still reports the old
            # mode; only treat a later mode change as an unexpected OFFBOARD exit.
            _wait_for(
                _offboard_entered,
                _episode_budget(started, plan.max_episode_s, 10.0),
                phase="offboard_entered",
                health_check=mav_reader.raise_if_failed,
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
                            f"OFFBOARD exited unexpectedly: mode={mode}; statustext={text}"
                        )
                z = _state_position(mav_reader.state)[2]
                now = time.monotonic()
                if now - last_z_report[0] >= 2.0:
                    last_z_report[0] = now
                    _progress("takeoff_progress", z_m=z, target_z_m=-altitude_m)
                return z <= -(altitude_m - 0.5)

            _wait_for(
                _takeoff_ready,
                _episode_budget(started, plan.max_episode_s, 60.0),
                phase="takeoff_reached",
                health_check=mav_reader.raise_if_failed,
            )
            for index in range(plan.steps_per_episode):
                mav_reader.raise_if_failed()
                if time.monotonic() - started > plan.max_episode_s:
                    raise TimeoutError("episode wall-clock timeout")
                before_state = _snapshot_mavlink_state(mav_reader.state)
                pose_sim_stamp_ns = _pose_sim_stamp_ns(before_state)
                before_sample = _fresh_sensor_snapshot(
                    sensor_reader,
                    time.monotonic_ns(),
                    SENSOR_FRAME_WAIT_S,
                    min_sensor_stamp_ns=pose_sim_stamp_ns,
                )
                before_state = mav_reader.aligned_state(before_sample.sensor_stamp_ns)
                before = build_observation(
                    before_sample,
                    before_state,
                    camera_info={
                        **DEFAULT_CAMERA_INFO,
                        "width": plan.rgb_size[0],
                        "height": plan.rgb_size[1],
                        "fx": DEFAULT_CAMERA_INFO["fx"] * plan.rgb_size[0] / 1280.0,
                        "fy": DEFAULT_CAMERA_INFO["fy"] * plan.rgb_size[1] / 960.0,
                        "cx": DEFAULT_CAMERA_INFO["cx"] * plan.rgb_size[0] / 1280.0,
                        "cy": DEFAULT_CAMERA_INFO["cy"] * plan.rgb_size[1] / 960.0,
                    },
                )
                before = _png_observation(before)
                pose = tuple(before["pose"]["local_ned_m"])
                yaw = float(before["attitude"]["yaw_deg"])
                if plan.teacher == "expert_search":
                    action = policy.next_action(pose_ned=pose, yaw_deg=yaw)
                    labels = policy.labels(pose_ned=pose, yaw_deg=yaw)
                else:
                    action = policy.next_action()
                    labels = {}
                commanded_delta = primitive_to_ned(action, yaw)[:3]
                _progress("primitive_start", step=index, action=action.to_dict())
                result = executor.execute(action)
                primitive_end_ns = time.monotonic_ns()
                mav_reader.raise_if_failed()
                _progress(
                    "primitive_end", step=index, status=result.status, elapsed_s=result.elapsed_s
                )
                mav_reader.raise_if_failed()
                # obs_after must be rendered after the primitive completed, not mid-motion.
                after_state = _snapshot_mavlink_state(mav_reader.state)
                after_pose_sim_stamp_ns = _pose_sim_stamp_ns(after_state)
                after_sample = _fresh_sensor_snapshot(
                    sensor_reader,
                    primitive_end_ns,
                    SENSOR_FRAME_WAIT_S,
                    min_sensor_stamp_ns=after_pose_sim_stamp_ns,
                )
                after_state = mav_reader.aligned_state(after_sample.sensor_stamp_ns)
                after = build_observation(
                    after_sample,
                    after_state,
                    camera_info={
                        **DEFAULT_CAMERA_INFO,
                        "width": plan.rgb_size[0],
                        "height": plan.rgb_size[1],
                        "fx": DEFAULT_CAMERA_INFO["fx"] * plan.rgb_size[0] / 1280.0,
                        "fy": DEFAULT_CAMERA_INFO["fy"] * plan.rgb_size[1] / 960.0,
                        "cx": DEFAULT_CAMERA_INFO["cx"] * plan.rgb_size[0] / 1280.0,
                        "cy": DEFAULT_CAMERA_INFO["cy"] * plan.rgb_size[1] / 960.0,
                    },
                )
                after = _png_observation(after)
                ack = {
                    **result.ack,
                    "type": "SET_POSITION_TARGET_LOCAL_NED",
                    # PX4 has no per-position-setpoint COMMAND_ACK.  A
                    # timeout still means the command stream was accepted
                    # and executed until the bounded completion deadline.
                    "action_ack": result.status in {"success", "timeout"},
                    "arm": arm_ack,
                }
                continue_loop = _record_policy_step(
                    recorder,
                    before,
                    action,
                    ack=ack,
                    outcome={
                        "status": result.status,
                        "elapsed_s": result.elapsed_s,
                        "commanded_delta_ned": list(commanded_delta),
                        "achieved_delta_ned": list(result.achieved_delta_ned),
                        "achieved_yaw_delta_deg": result.achieved_yaw_delta_deg,
                    },
                    obs_after=after,
                    labels=labels,
                )
                # STOP is a terminal policy action.  Keep this step in the
                # episode (including its observation pair), then immediately
                # leave the primitive loop so we do not execute redundant
                # STOPs while still performing the normal RTL/LAND teardown.
                if not continue_loop:
                    _progress("policy_stop", step=index)
                    break
            mav_reader.raise_if_failed()
            # Teardown lands in place from the hover altitude.  PX4 RTL first climbs to
            # its return altitude (30 m+), which made the LAND wait time out and the
            # data is not affected by where the vehicle lands.
            mav_reader.raise_if_failed()
            _progress("land")
            executor.land()
            _wait_for(
                lambda: not (int(_field(mav_reader.state.get("HEARTBEAT"), "base_mode", 0)) & 128),
                _episode_budget(started, plan.max_episode_s, 120.0),
                phase="land",
                health_check=mav_reader.raise_if_failed,
            )
            recorder.mark_finished(status="complete")
            recorder.record_mavlink(
                {"message_type": "collector_episode_complete", "container": environment.name}
            )
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
    except Exception as exc:
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
        recorder.mark_finished(status="failed", failure=f"{type(exc).__name__}: {exc}")
        raise
    return recorder.episode_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--teacher", choices=("explore", "expert_search"), required=True)
    parser.add_argument("--episodes", type=int, required=True)
    parser.add_argument("--steps-per-episode", type=int, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--max-episode-s", type=float, default=None)
    parser.add_argument("--heartbeat-timeout-s", type=float, default=DEFAULT_HEARTBEAT_TIMEOUT_S)
    parser.add_argument("--keep-container-on-failure", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rgb-size", type=_parse_size, default=DEFAULT_RGB_SIZE)
    parser.add_argument("--depth-size", type=_parse_size, default=DEFAULT_DEPTH_SIZE)
    parser.add_argument(
        "--depth-encoding", choices=("UINT16_MM", "FLOAT32_M"), default=DEFAULT_DEPTH_ENCODING
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.episodes < 1 or args.steps_per_episode < 1:
        raise SystemExit("--episodes and --steps-per-episode must be positive")
    if not 1.0 <= args.timeout_s <= 86400.0:
        raise SystemExit("--timeout-s must be between 1 and 86400 seconds")
    max_episode_s = args.timeout_s if args.max_episode_s is None else args.max_episode_s
    if not 1.0 <= max_episode_s <= 86400.0:
        raise SystemExit("--max-episode-s must be between 1 and 86400 seconds")
    if not 1.0 <= args.heartbeat_timeout_s <= 86400.0:
        raise SystemExit("--heartbeat-timeout-s must be between 1 and 86400 seconds")
    scenario = args.scenario.resolve()
    if not scenario.is_file():
        raise SystemExit(f"scenario not found: {scenario}")
    runtime_path, truth_path = _scenario_channels(scenario)
    _validate_runtime(runtime_path)
    plan = CollectionPlan(
        str(scenario),
        str(runtime_path),
        str(truth_path),
        args.teacher,
        args.episodes,
        args.steps_per_episode,
        args.seed,
        str(args.out.resolve()),
        timeout_s=args.timeout_s,
        max_episode_s=max_episode_s,
        heartbeat_timeout_s=args.heartbeat_timeout_s,
        keep_container_on_failure=args.keep_container_on_failure,
        rgb_size=args.rgb_size,
        depth_size=args.depth_size,
        depth_encoding=args.depth_encoding,
    )
    if args.dry_run:
        print(json.dumps(plan.to_mapping(), ensure_ascii=False, indent=2))
        return 0
    if not _docker_available():
        raise SystemExit("Docker daemon is not reachable; use --dry-run or start Docker")
    args.out.mkdir(parents=True, exist_ok=True)
    completed: list[str] = []
    for index in range(args.episodes):
        path = run_episode(plan, truth_path, index)
        completed.append(str(path))
        print(json.dumps({"episode": index, "path": str(path)}, ensure_ascii=False))
    print(json.dumps({"episodes": completed, "out": str(args.out.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
