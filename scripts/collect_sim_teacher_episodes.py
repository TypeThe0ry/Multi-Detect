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
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import types
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
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
from multidetect.sim_teacher.executor import (  # noqa: E402
    GCS_SOURCE_COMPONENT,
    GCS_SOURCE_SYSTEM,
    GIMBAL_MANAGER_DEVICE_ID,
    GIMBAL_MANAGER_FLAGS_PITCH_LOCK,
    GIMBAL_MANAGER_FLAGS_ROLL_LOCK,
    GIMBAL_MANAGER_FLAGS_YAW_IN_VEHICLE_FRAME,
    GIMBAL_MANAGER_STATUS_WALL_TIMEOUT_S,
    MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE,
    MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
)
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
    gimbal_angles_from_status,
    validate_gimbal_recording_observation,
)
from multidetect.sim_teacher.vehicle_loss import VehicleLossDetector  # noqa: E402
from multidetect.simulator.cpu import resolve_cpu_isolation  # noqa: E402
from multidetect.simulator.fixed_wing_reposition import (  # noqa: E402
    DEFAULT_NAV_LOITER_RAD_M,
    reposition_ring_metrics,
)
from multidetect.simulator.px4_image import DEFAULT_GPU_IMAGE, PINNED_IMAGE, resolve_runtime_image  # noqa: E402
from multidetect.simulator.runtime_image import verify_gpu_runtime_image  # noqa: E402
from multidetect.simulator.stack import SimulatorStackError  # noqa: E402
from multidetect.simulator.stack import _vehicle_spec as _stack_vehicle_spec  # noqa: E402
from scripts.analyze_sensor_timeouts import build_failure_snapshot  # noqa: E402
from scripts.px4_clock_stall_capture import capture_stall as capture_px4_clock_stall  # noqa: E402
from scripts.run_simulator_docker import (  # noqa: E402
    CONTAINER_IMAGE_MODEL_ROOT,
    CONTAINER_MODEL_ROOT,
    CONTAINER_SCRIPT_ROOT,
    _split_gpu_list,
    _resolve_gpu_selection,
    build_docker_simulator_plan,
    resolve_px4_daemon_mode,
    resolve_px4_logger_mode,
)
from scripts.sitl_lock import SitlLock, default_lock_path, sitl_lock_context  # noqa: E402


class _TerminationRequested(RuntimeError):
    """Raised by the top-level signal bridge so lock/container finally blocks run."""


def _raise_on_termination(signum: int, _frame: Any) -> None:
    raise _TerminationRequested(f"terminated by signal {signum}")


DEFAULT_TIMEOUT_S = 300.0
SIM_PRE_ABORT_DESCENT_TIMEOUT_S = 45.0
SIM_PRE_ABORT_DESCENT_HARD_WALL_CEILING_S = 900.0
SENSOR_FRAME_WAIT_S = 15.0
# Delivered frames match PX4 pose time exactly, but under heavy host load the frame
# server's callbacks have been seen 7-14 sim seconds behind.  At the delta smoke's
# 0.15-0.2x real time that backlog takes over a minute of wall time to clear.  The
# frame must still reach the watermark.
DELTA_SMOKE_SENSOR_FRAME_WAIT_S = 120.0
DEFAULT_RGB_SIZE = (640, 480)
DEFAULT_DEPTH_SIZE = (320, 240)
DEFAULT_DEPTH_ENCODING = "UINT16_MM"
DEFAULT_MIN_FREE_GB = 15.0
DEFAULT_DISK_CHECK_INTERVAL_STEPS = 10
DEFAULT_VIDEO_MAX_BYTES = 8 * 1024**3
HOST_PRESSURE_SAMPLE_INTERVAL_S = 5.0


def _parse_cpu_set(raw: str) -> tuple[int, ...]:
    cpus: set[int] = set()
    for item in raw.split(","):
        bounds = item.split("-")
        try:
            start = int(bounds[0])
            end = int(bounds[-1])
        except (ValueError, IndexError):
            continue
        if start < 0 or end < start:
            continue
        cpus.update(range(start, end + 1))
    return tuple(sorted(cpus))


def _current_cpuset() -> tuple[int, ...]:
    for path in (
        Path("/sys/fs/cgroup/cpuset.cpus.effective"),
        Path("/sys/fs/cgroup/cpuset/cpuset.cpus").resolve(),
    ):
        try:
            cpus = _parse_cpu_set(path.read_text(encoding="ascii").strip())
        except OSError:
            continue
        if cpus:
            return cpus
    try:
        return tuple(sorted(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return tuple(range(os.cpu_count() or 0))


def _proc_cpu_ticks() -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu") or fields[0] == "cpu":
            continue
        try:
            cpu = int(fields[0][3:])
            ticks = [int(value) for value in fields[1:]]
        except ValueError:
            continue
        if ticks:
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            result[cpu] = (sum(ticks), idle)
    return result


def _pressure_summary(path: str) -> dict[str, dict[str, float]] | None:
    try:
        lines = Path(path).read_text(encoding="ascii").splitlines()
        result = {}
        for line in lines:
            values = line.split()
            if not values:
                continue
            parsed = {
                key: float(value)
                for key, value in (
                    item.split("=", 1) for item in values[1:] if "=" in item
                )
            }
            if parsed:
                result[values[0]] = parsed
        return result or None
    except (OSError, ValueError):
        return None


class HostPressureSampler:
    """Best-effort host load recorder; failures are isolated from collection."""

    def __init__(
        self,
        episode_dir: Path,
        cpus: tuple[int, ...],
        interval_s: float = HOST_PRESSURE_SAMPLE_INTERVAL_S,
    ):
        self.path = episode_dir / "host_pressure.jsonl"
        self.cpus = cpus
        self.interval_s = interval_s
        self.sim_clock_provider: Any | None = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="sim-host-pressure", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=max(1.0, self.interval_s + 1.0))

    def _sample(
        self, previous: dict[int, tuple[int, int]] | None
    ) -> tuple[dict[str, Any], dict[int, tuple[int, int]]]:
        now = time.time()
        ticks = _proc_cpu_ticks()
        per_core: list[float] = []
        if previous is not None:
            for cpu in self.cpus:
                old, current = previous.get(cpu), ticks.get(cpu)
                if old is None or current is None:
                    continue
                total_delta = current[0] - old[0]
                idle_delta = current[1] - old[1]
                if total_delta > 0:
                    per_core.append(100.0 * (total_delta - idle_delta) / total_delta)
        try:
            loadavg = Path("/proc/loadavg").read_text(encoding="ascii").strip()
        except OSError:
            loadavg = None
        row = {
            "wall_time": now,
            "monotonic_ns": time.monotonic_ns(),
            "loadavg": loadavg,
            "cpu_pressure": _pressure_summary("/proc/pressure/cpu"),
            "memory_pressure": _pressure_summary("/proc/pressure/memory"),
            "io_pressure": _pressure_summary("/proc/pressure/io"),
            "cpuset_cpus": list(self.cpus),
            "core_busy_percent_mean": statistics.fmean(per_core) if per_core else None,
            "core_busy_percent_max": max(per_core) if per_core else None,
            "px4_sim_time_boot_ms": self._sim_time_boot_ms(),
        }
        return row, ticks

    def _sim_time_boot_ms(self) -> int | None:
        if self.sim_clock_provider is None:
            return None
        try:
            value = self.sim_clock_provider()
            return value if isinstance(value, int) and not isinstance(value, bool) else None
        except Exception:
            return None

    def _run(self) -> None:
        previous = None
        while not self.stop_event.is_set():
            try:
                row, previous = self._sample(previous)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, separators=(",", ":")) + "\n")
            except Exception:
                pass
            self.stop_event.wait(self.interval_s)


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
    rows: list[dict[str, int]] = []
    try:
        for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
            if len(row) == 3:
                index, used, total = (int(value.strip()) for value in row)
                rows.append({"index": index, "used_mib": used, "total_mib": total})
    except ValueError:
        return None
    return rows or None


def _frame_wait_diagnostics(
    *,
    phase: str,
    sensor_reader: SensorStreamReader,
    mav_reader: MavlinkReader,
    pose_state: MavlinkState,
    min_sensor_stamp_ns: int | None,
    started_monotonic_ns: int,
    primitive_end_monotonic_ns: int | None = None,
    freshness_watermark_ns: int | None = None,
    ended_monotonic_ns: int | None = None,
    started_wall_time: float | None = None,
    ended_wall_time: float | None = None,
    host_pressure_path: Path | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Capture clocks and watermarks surrounding a causal frame wait."""
    now_ns = ended_monotonic_ns if ended_monotonic_ns is not None else time.monotonic_ns()
    state = _snapshot_mavlink_state(mav_reader.state)
    latest = sensor_reader.latest
    local = state.get("LOCAL_POSITION_NED")
    pose_ms = _field(local, "time_boot_ms", None)
    status_text = state.get("STATUSTEXT")
    requested_pose_ms = _field(pose_state.get("LOCAL_POSITION_NED"), "time_boot_ms", None)
    local_received = state.received_monotonic_ns.get("LOCAL_POSITION_NED")
    statustext = _field(status_text, "text", None)
    watermark = freshness_watermark_ns or started_monotonic_ns
    pressure = None
    if host_pressure_path is not None:
        try:
            lines = host_pressure_path.read_text(encoding="utf-8").splitlines()
            if lines:
                value = json.loads(lines[-1])
                pressure = value if isinstance(value, dict) else None
        except (OSError, ValueError):
            pass
    px4_stalled = (
        local_received is not None
        and local_received <= watermark
        and isinstance(requested_pose_ms, (int, float))
        and isinstance(pose_ms, (int, float))
        and pose_ms <= requested_pose_ms
    )
    if ended_monotonic_ns is None:
        classification = "frame_wait_started"
    elif error is None:
        classification = "frame_wait_satisfied"
    elif (
        latest is not None
        and latest.received_monotonic_ns > watermark
        and min_sensor_stamp_ns is not None
        and latest.sensor_stamp_ns < min_sensor_stamp_ns
    ):
        classification = "frame_timestamp_below_pose_watermark"
    elif latest is None or latest.received_monotonic_ns <= watermark:
        classification = (
            "px4_clock_and_telemetry_stalled" if px4_stalled else "no_new_frame_observed"
        )
    else:
        classification = (
            "px4_clock_and_telemetry_stalled"
            if px4_stalled
            else "frame_wait_timeout_unclassified"
        )
    return {
        "phase": phase,
        "started_monotonic_ns": started_monotonic_ns,
        "primitive_end_monotonic_ns": primitive_end_monotonic_ns,
        "started_wall_time": started_wall_time,
        "ended_monotonic_ns": ended_monotonic_ns,
        "ended_wall_time": ended_wall_time if ended_wall_time is not None else time.time(),
        "duration_s": max(0, now_ns - started_monotonic_ns) / 1e9,
        "pose_time_boot_ms": pose_ms,
        "requested_pose_time_boot_ms": requested_pose_ms,
        "pose_received_monotonic_ns": state.received_monotonic_ns.get("LOCAL_POSITION_NED"),
        "pose_received_wall_time": state.received_wall_time.get("LOCAL_POSITION_NED"),
        "required_sensor_stamp_ns": min_sensor_stamp_ns,
        "last_frame_sensor_stamp_ns": latest.sensor_stamp_ns if latest is not None else None,
        "last_frame_received_monotonic_ns": (
            latest.received_monotonic_ns if latest is not None else None
        ),
        "last_frame_received_wall_time": (
            latest.received_wall_time if latest is not None else None
        ),
        "frames_received": sensor_reader.frames_received,
        "last_statustext": statustext,
        "statustext_received_monotonic_ns": state.received_monotonic_ns.get("STATUSTEXT"),
        "statustext_received_wall_time": state.received_wall_time.get("STATUSTEXT"),
        "px4_vehicle_time_boot_ms": state.vehicle_time_boot_ms,
        "host_pressure_sample": pressure,
        "failure_class": classification,
        "error": f"{type(error).__name__}: {error}" if error is not None else None,
    }


class LowDiskSpace(RuntimeError):
    """Normal campaign stop caused by the configured free-space floor."""


class VehicleLostControl(RuntimeError):
    """Recorded telemetry proves an uncommanded fall or sustained tumble."""

    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event
        super().__init__(f"vehicle lost control: {event.get('reason', 'telemetry rule')}")


def sim_pre_abort_descent_altitude(config: Mapping[str, Any], *, teacher: str) -> float | None:
    value = config.get("sim_pre_abort_descent_altitude_m")
    if value is None:
        return None
    if teacher != "onboard" or config.get("sim_climb_timeout_injection") is not True:
        raise ValueError("sim_pre_abort_descent_altitude_m requires SIM onboard climb injection")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.5 <= value < 2.5
    ):
        raise ValueError("sim_pre_abort_descent_altitude_m must be finite and in [0.5, 2.5) metres")
    return float(value)


class SimClimbTimeoutInjector:
    """Inject two return-home climb timeouts while holding the current NED target."""

    def __init__(self, *, enabled: bool, runtime_mode: str, teacher: str) -> None:
        if type(enabled) is not bool:
            raise ValueError("sim_climb_timeout_injection must be a boolean")
        if enabled and (runtime_mode != "sim" or teacher != "onboard"):
            raise ValueError(
                "sim_climb_timeout_injection is only available to the SIM onboard collector"
            )
        self.enabled = enabled
        self.injected = 0

    def matches(
        self,
        action: ActionPrimitive,
        *,
        active_skill: str | None,
        before: Mapping[str, Any] | None,
    ) -> bool:
        pose = before.get("pose", {}) if isinstance(before, Mapping) else {}
        position = pose.get("local_ned_m") if isinstance(pose, Mapping) else None
        flight = before.get("flight_state", {}) if isinstance(before, Mapping) else {}
        landed_state = flight.get("landed_state") if isinstance(flight, Mapping) else None
        try:
            altitude = (
                max(0.0, -float(position[2]))
                if isinstance(position, (list, tuple)) and len(position) >= 3
                else None
            )
        except (TypeError, ValueError):
            altitude = None
        airborne = (
            isinstance(before, Mapping)
            and before.get("landed") is not True
            and (landed_state == 2 or (landed_state is None and before.get("landed") is False))
        )
        return bool(
            self.enabled
            and self.injected < 2
            and active_skill == "return_home"
            and action.kind == "translate"
            and action.up_m > 0
            and action.forward_m == 0
            and action.right_m == 0
            and action.yaw_delta_deg == 0
            and altitude is not None
            and math.isfinite(altitude)
            and altitude <= 2.5
            and airborne
        )

    def execute(self, executor: Any, action: ActionPrimitive) -> Any:
        timeout_s = float(getattr(getattr(executor, "criterion", None), "timeout_s", 20.0))
        held = executor.execute(ActionPrimitive.hold(timeout_s))
        ack = {
            **held.ack,
            "sim_fault_injection": "return_home_climb_timeout",
            "requested_action": action.to_dict(),
        }
        self.injected += 1
        return dataclasses.replace(held, status="timeout", ack=ack)


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
PREARM_HEALTH_HARD_WALL_CEILING_S = 900.0
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
DELTA_TAILSITTER_SMOKE_PROFILES = frozenset(
    {"delta_tailsitter", "delta_tailsitter_gimbal"}
)
DELTA_SMOKE_FW_MIN_DURATION_S = 120.0
DELTA_SMOKE_FW_MAX_DURATION_S = 240.0
DELTA_SMOKE_FW_MIN_ORBIT_DEG = 360.0
DELTA_SMOKE_REPOSITION_TIMEOUT_S = 120.0
DELTA_OVERSPEED_TARGET_MPS = 21.0
DELTA_OVERSPEED_INJECT_MIN_THR = 0.45
DELTA_OVERSPEED_INJECT_TIMEOUT_S = 60.0
DELTA_OVERSPEED_RECOVERY_MAX_S = 40.0
DELTA_OVERSPEED_RECOVERY_HOLD_S = 15.0
DELTA_OVERSPEED_HEIGHT_ERROR_MAX_M = 10.0
MOTION_ALONG_TRACK_DONE_M = 5.0
# A fixed-wing reposition may complete by flying past its waypoint, but a
# projection crossing alone is insufficient: the aircraft can cross the target
# bearing tens of metres away while circling.  Bound the cross-track error by
# the same tolerance used for a direct arrival.
MOTION_CROSS_TRACK_DONE_M = MOTION_ARRIVAL_TOLERANCE_M
FIXED_WING_LOITER_CAPTURE_M = DEFAULT_NAV_LOITER_RAD_M
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
# The delta tailsitter Gazebo model currently runs at about 0.17x real time.
# Keep its normal vehicle-clock episode budget, with a generous host fallback;
# other airframes retain the established 900 s ceiling for a 300 s episode.
DELTA_TAILSITTER_WALL_CEILING_FACTOR = 8.0
DELTA_TAILSITTER_MIN_WALL_CEILING_S = 3600.0
MAVLINK_SILENCE_SIM_TIMEOUT_S = 5.0
MAVLINK_SILENCE_STALLED_SIM_WALL_S = 15.0
MAVLINK_SILENCE_MIN_REALTIME_FACTOR = 0.1
MAVLINK_SILENCE_WALL_CAP_S = 60.0
MAVLINK_SILENCE_MIN_WALL_S = max(
    5.0, MAVLINK_SILENCE_SIM_TIMEOUT_S / MAVLINK_SILENCE_MIN_REALTIME_FACTOR
)
GIMBAL_HOVER_STABLE_S = 3.0
GIMBAL_HOVER_MAX_WALL_S = 300.0
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
    image: str | None = None
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
    sim_climb_timeout_injection: bool = False
    sim_pre_abort_descent_altitude_m: float | None = None
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
    vtol_hover_mission: bool | None = None
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
    requested_image: str | None = None
    runtime_image_verification: dict | None = None

    def __post_init__(self) -> None:
        # Bind the selected image before metadata, progress headers or Docker argv
        # are emitted. Later environment changes cannot silently change a plan.
        enabled, _, _ = _resolve_gpu_selection(
            gpu_render=self.gpu_render,
            gpu_device=self.gpu_device,
            gpu_devices=self.gpu_devices,
            worker_index=self.worker_index,
        )
        object.__setattr__(
            self, "image", resolve_runtime_image(self.image, gpu_render=enabled)
        )

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        result["requested_image"] = self.requested_image or self.image
        result["image_identity_status"] = "content_verified" if self.runtime_image_verification else "planned_reference"
        altitude, source = _takeoff_altitude_for_plan(self)
        result["takeoff_altitude_m"] = altitude
        result["takeoff_altitude_source"] = source
        result["px4_logger_mode"] = _effective_px4_logger_mode(self)
        result["px4_daemon_mode"] = resolve_px4_daemon_mode()
        result["docker_command"] = None if self.attach_container else docker_command(self)
        if self.attach_container:
            # An attachment cannot know its runtime identity before Docker inspect.
            result["requested_image"] = self.image
            result["image"] = None
            result["image_identity_status"] = "pending_container_inspect"
        cpu_isolation = resolve_cpu_isolation(self.worker_index)
        if cpu_isolation is not None:
            result["cpu_isolation"] = cpu_isolation.to_mapping()
        return result


def freeze_collection_runtime(plan: CollectionPlan) -> CollectionPlan:
    if plan.attach_container or plan.image != DEFAULT_GPU_IMAGE:
        return plan
    enabled, device, devices = _resolve_gpu_selection(
        gpu_render=plan.gpu_render, gpu_device=plan.gpu_device,
        gpu_devices=plan.gpu_devices, worker_index=plan.worker_index,
    )
    if not enabled:
        raise ValueError("Qualified GPU runtime cannot launch after GPU rendering was disabled")
    receipt = verify_gpu_runtime_image(plan.image, docker=_docker_executable(plan))
    return replace(plan, image=receipt["image_id"], requested_image=plan.image,
                   runtime_image_verification=receipt, gpu_render=True,
                   gpu_device=device, gpu_devices=devices)


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
    path = scenario_path.with_name(
        scenario_path.name.removesuffix(".scenario.json") + ".teacher_intent.json"
    )
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
        "vtol" if any(token in model_text for token in ("vtol", "tiltrotor", "tailsitter")) else
        "multirotor" if "multirotor" in model_text or "x500" in model_text else
        "unknown"
    )
    if requested in {"x500", "x500_mono_cam", "multirotor"}:
        if model and "multirotor" not in str(model).lower() and "x500" not in str(model).lower():
            raise ValueError("x500_mono_cam requires a multirotor/x500 runtime model")
    elif not model:
        raise ValueError(f"airframe {airframe!r} requires runtime.vehicle.model")
    if (
        category in {"fixed_wing", "multirotor", "vtol"}
        and model_category in {"fixed_wing", "multirotor", "vtol"}
        and category != model_category
    ):
        raise ValueError(
            f"airframe {airframe!r} category={category} does not match runtime.vehicle.model={model!r}"
        )
    for sensor in runtime.get("sensors", []):
        if isinstance(sensor, dict) and isinstance(sensor.get("stream_uri"), str):
            validate_local_endpoint(sensor["stream_uri"])
    return runtime


def _airframe_profile(airframe: str, takeoff_method: str | None = None) -> AirframeProfile | None:
    normalized = str(airframe).strip().lower().replace("-", "_")
    if normalized in {"x500", "x500_mono_cam", "multirotor"}:
        return None
    if normalized in {"fixed_wing", "fw", "vtol"}:
        return None
    selected = normalized
    if normalized == "rc_cessna_mono_cam" and takeoff_method in {"hand", "catapult"}:
        selected = f"rc_cessna_{takeoff_method}"
    candidates = [ROOT / "configs" / "airframes" / f"{selected}.json"]
    aliases = {
        "rc_cessna_mono_cam": "rc_cessna_runway",
        "advanced_plane_mono_cam": "advanced_plane",
        "quadtailsitter_mono_cam": "quadtailsitter",
        "delta_tailsitter_mono_cam": "delta_tailsitter",
        "delta_tailsitter_gimbal_cam": "delta_tailsitter_gimbal",
        "tiltrotor_mono_cam": "tiltrotor",
        "standard_vtol_mono_cam": "standard_vtol",
    }
    if normalized in aliases and selected == normalized:
        candidates.insert(0, ROOT / "configs" / "airframes" / f"{aliases[normalized]}.json")
    for path in candidates:
        if path.is_file():
            return AirframeProfile.from_json(path)
    raise ValueError(f"unknown airframe profile: {airframe}")


def _simulation_gimbal_yaw_correction_deg(plan: CollectionPlan) -> float | None:
    """Return the opt-in Gazebo camera-IMU yaw correction for this episode."""
    profile = _airframe_profile(plan.airframe, plan.takeoff_method)
    gimbal = getattr(getattr(profile, "camera", None), "gimbal", {})
    if (
        not isinstance(gimbal, Mapping)
        or gimbal.get("simulation_yaw_correction") != "spawn_yaw_deg"
    ):
        return None
    try:
        runtime = json.loads(Path(plan.runtime).read_text(encoding="utf-8"))
        yaw_rad = runtime["vehicle"]["spawn_pose"]["yaw_rad"]
        return -math.degrees(float(yaw_rad))
    except (OSError, TypeError, ValueError, KeyError):
        return None

def _default_px4_logger_mode(airframe: str) -> str:
    """Disable PX4 ULog for delta tailsitters; preserve other airframe defaults."""
    profile = _airframe_profile(airframe)
    return (
        "off"
        if profile is not None and profile.id in DELTA_TAILSITTER_SMOKE_PROFILES
        else "default"
    )


def _effective_px4_logger_mode(plan: CollectionPlan) -> str:
    configured = os.environ.get("MULTIDETECT_PX4_LOGGER_MODE")
    return resolve_px4_logger_mode(
        configured if configured is not None else _default_px4_logger_mode(plan.airframe)
    )


def _delta_fw_ring_metrics_from_files(
    mavlink_path: Path, steps_path: Path
) -> dict[str, Any]:
    from scripts.smoke_airframe_flight import _delta_fw_ring_metrics, _jsonl

    return _delta_fw_ring_metrics(_jsonl(mavlink_path), _jsonl(steps_path))


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
    if plan.airframe == "x500_mono_cam":
        return "x500_mono_cam_0"
    if plan.airframe == "x500_gimbal":
        return "x500_gimbal_0"
    vehicle = runtime.get("vehicle", {}) if isinstance(runtime, Mapping) else {}
    vehicle = vehicle if isinstance(vehicle, Mapping) else {}
    model = str(vehicle.get("model", plan.airframe))
    try:
        _, name = _stack_vehicle_spec(model, str(vehicle.get("airframe_profile_id", "")))
    except SimulatorStackError:
        name = model
    return name if name.endswith("_0") else name + "_0"


def _has_registered_depth_camera(airframe: str) -> bool:
    _airframe_profile(airframe)
    if airframe == "x500_gimbal":
        overlay = ROOT / "simulation/gazebo/model-overlays/x500_gimbal_seg/model.sdf"
        try:
            model = ET.parse(overlay).getroot().find("model")
        except (OSError, ET.ParseError):
            return False
        return model is not None and model.find(".//sensor[@name='depth_camera']") is not None
    return airframe != "x500_mono_cam"


def _camera_sensor_topics(*, world: str, model: str, airframe: str) -> dict[str, str]:
    prefix = f"/world/{world}/model/{model}"
    rgb = f"{prefix}/link/camera_link/sensor/camera"
    depth = f"{prefix}/link/depth_camera_link/sensor/depth_camera"
    return {
        "image": f"{rgb}/image",
        "camera_info": f"{rgb}/camera_info",
        "depth_image": f"{depth}/depth_image",
        "depth_camera_info": f"{depth}/camera_info",
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
            px4_logger_mode=_effective_px4_logger_mode(plan),
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
        if plan.airframe == "x500_gimbal" and overlay.name == "x500_gimbal_seg":
            gimbal_model_overlay = overlay / "gimbal" / "model.sdf"
            if not gimbal_model_overlay.is_file():
                raise FileNotFoundError(gimbal_model_overlay)
            overlay_mounts.extend(
                [
                    "--mount",
                    f"type=bind,source={gimbal_model_overlay.resolve()},"
                    f"target={CONTAINER_IMAGE_MODEL_ROOT}/gimbal/model.sdf,readonly",
                ]
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


def _record_container_started(recorder, environment: DockerSimEnvironment) -> None:
    identity = environment.remember_px4_process()
    recorder.meta["simulator_container"] = identity
    recorder.meta["simulator_image"] = identity["image"]
    recorder.meta["requested_simulator_image"] = identity["requested_image"]
    recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
    recorder.record_mavlink({
        "message_type": "collector_container_started",
        "container": environment.name,
        "container_id": identity["container_id"],
        "image": identity["image"],
        "requested_image": identity["requested_image"],
        "image_identity_source": identity["image_identity_source"],
    })


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


def _first_frame_wait_s() -> float | None:
    """Longer first-frame wait for worlds that load slowly; unset keeps the normal timeouts."""
    value = os.environ.get("MULTIDETECT_SIM_FIRST_FRAME_WAIT_S", "").strip()
    return float(value) if value else None


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
    topics = _camera_sensor_topics(world=world, model=model, airframe=plan.airframe)
    image_topic = topics["image"]
    camera_topic = topics["camera_info"]
    navsat_topic = f"/world/{world}/model/{model}/link/base_link/sensor/navsat_sensor/navsat"
    odometry_topic = f"/model/{model}/odometry_with_covariance"
    request_wait_s = min(60.0, max(5.0, SENSOR_FRAME_WAIT_S + 2.0))
    if plan.smoke_flight:
        smoke_profile = _airframe_profile(plan.airframe)
        if smoke_profile is not None and smoke_profile.id in DELTA_TAILSITTER_SMOKE_PROFILES:
            request_wait_s = DELTA_SMOKE_SENSOR_FRAME_WAIT_S + 2.0
    command = [
        _docker_executable(plan),
        "exec",
        # -i keeps stdin attached; the frame server reads GET requests from it.
        "-i",
        environment.name,
        "python3",
        "/workspace/scripts/sim_teacher_frame_server.py",
        "--request-timeout-s",
        f"{request_wait_s:.3f}",
        *(("--first-frame-timeout-s", str(_first_frame_wait_s())) if _first_frame_wait_s() else ()),
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
    category = (
        "multirotor"
        if plan.airframe == "x500_mono_cam"
        else (
            _airframe_profile(plan.airframe).category
            if _airframe_profile(plan.airframe)
            else "fixed_wing"
        )
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
    if plan.segmentation:
        command.extend(
            [
                "--segmentation",
                "--segmentation-topic",
                "/multidetect/teacher/segmentation/labels_map",
            ]
        )
    if depth is not None:
        depth_topic = topics["depth_image"]
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
    if "sensor stream produced no frame" in reason.lower():
        return "sensor_stream_stalled"
    if px4_alive is True:
        return "px4_alive_other_failure"
    if px4_alive is False:
        return "px4_not_alive"
    return "px4_status_unknown"


def _is_sensor_stream_timeout(reason: str) -> bool:
    return "sensor stream produced no frame" in reason.lower()


class DockerSimEnvironment:
    """Own one simulator container and retain best-effort episode evidence."""

    def __init__(self, plan: CollectionPlan, episode_id: str, episode_dir: Path | None = None):
        self.plan = plan
        self.name = plan.attach_container or f"{plan.container_prefix}-{episode_id}"
        self.attached = bool(plan.attach_container)
        self.started = False
        self.runtime_image = None if self.attached else plan.image
        self._attached_container_id: str | None = None
        self.episode_dir = Path(episode_dir) if episode_dir is not None else None
        self._stall_episode_dir = (
            self.episode_dir
            if self.episode_dir is not None
            else Path(plan.out) / "episodes" / episode_id
        )
        self.evidence_dir = (
            self.episode_dir / "container-evidence" if self.episode_dir is not None else None
        )
        self.evidence_index: dict[str, Any] | None = None
        self.px4_threads_evidence: dict[str, Any] | None = None
        self.sim_clock_stamp_ns: int | None = None
        self.sim_clock_progress_wall = time.monotonic()
        self._sim_clock_lock = threading.Lock()
        self._last_px4_host_pids: list[int] = []
        self.failure_diagnostics: dict[str, Any] | None = None
        self.cleanup_diagnostics: dict[str, Any] | None = None
        self.stall_capture: dict[str, Any] | None = None
        self._log = None

    def set_logger(self, logger):
        self._log = logger

    def note_sim_clock(self, stamp_ns: int) -> None:
        with self._sim_clock_lock:
            if self.sim_clock_stamp_ns is None or stamp_ns > self.sim_clock_stamp_ns:
                self.sim_clock_stamp_ns = int(stamp_ns)
                self.sim_clock_progress_wall = time.monotonic()

    def sim_clock_snapshot(self) -> tuple[int | None, float]:
        with self._sim_clock_lock:
            return self.sim_clock_stamp_ns, self.sim_clock_progress_wall

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
                    inspected.stderr
                    or inspected.stdout
                    or f"docker inspect exited {inspected.returncode}"
                ).strip()
            else:
                details = json.loads(inspected.stdout)
                state = details.get("State") or {}
                snapshot["container_id"] = details.get("Id")
                snapshot["image"] = details.get("Image")
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
                        [
                            _docker_executable(self.plan),
                            "top",
                            self.name,
                            "-eo",
                            "pid,comm",
                        ],
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
        if self.plan.runtime_image_verification and not self.attached and (
            snapshot.get("image") != self.runtime_image
            or re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get("container_id", ""))) is None
            or (snapshot.get("state") or {}).get("running") is not True
        ):
            raise RuntimeError("Qualified runtime container identity is missing or changed")
        if self.attached and (
            snapshot.get("container_id") != self._attached_container_id
            or snapshot.get("image") != self.runtime_image
        ):
            raise RuntimeError("Attached container identity changed or is unavailable")
        return {
            "container_name": self.name,
            "container_id": snapshot.get("container_id"),
            "image": self.runtime_image,
            "requested_image": self.plan.requested_image or self.plan.image,
            "runtime_image_verification": self.plan.runtime_image_verification,
            "image_identity_source": "container_inspect" if self.attached or self.plan.runtime_image_verification else "launch_plan",
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

    def capture_stall_evidence(
        self, reason: str, *, px4_clock_diagnostics: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Capture clock-stall evidence while the container is still alive."""
        if self.stall_capture is None:
            try:
                self.stall_capture = capture_px4_clock_stall(
                    self.name,
                    self._stall_episode_dir,
                    docker=_docker_executable(self.plan),
                    host_pids=self._last_px4_host_pids,
                    px4_clock_diagnostics=px4_clock_diagnostics,
                    reason=reason,
                )
            except BaseException as error:  # evidence must never alter outcome
                self.stall_capture = {"capture_error": f"{type(error).__name__}: {error}"}
        return self.stall_capture

    def __enter__(self) -> DockerSimEnvironment:
        if not self.attached:
            self.plan = freeze_collection_runtime(self.plan)
            self.runtime_image = self.plan.image
        if self.attached:
            self.wait(self.plan.max_episode_s)
            snapshot = self._container_snapshot()
            image = snapshot.get("image")
            container_id = snapshot.get("container_id")
            if (
                not isinstance(image, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None
                or not isinstance(container_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
                or (snapshot.get("state") or {}).get("running") is not True
            ):
                raise RuntimeError("Attached container requires inspected immutable image and container identity")
            self.runtime_image = image
            self._attached_container_id = container_id
            self._progress("container_attached", container=self.name, image=image, requested_image=self.plan.image)
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
        executable = _docker_executable(self.plan)
        while time.monotonic() < deadline:
            result = subprocess.run(
                [executable, "inspect", "--format", "{{.State.Running}}", self.name],
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

    def inspect_state(self) -> dict[str, Any] | None:
        try:
            result = subprocess.run(
                [
                    _docker_executable(self.plan),
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    self.name,
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if result.returncode != 0:
                return {"inspect_error": (result.stderr or result.stdout).strip()}
            value = json.loads(result.stdout)
            return value if isinstance(value, dict) else {"inspect_error": "invalid state"}
        except Exception as error:
            return {"inspect_error": f"{type(error).__name__}: {error}"}

    def _capture_px4_threads(self, host_pid: int) -> dict[str, Any]:
        """Save best-effort host-side stacks and task metadata before cleanup."""
        if self.evidence_dir is None:
            return {"error": "episode evidence directory unavailable"}
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        destination = self.evidence_dir / "px4-threads.txt"
        rows = [f"host_pid={host_pid}\n"]
        try:
            result = subprocess.run(
                ["/usr/bin/gdb", "-p", str(host_pid), "-batch", "-ex", "thread apply all bt"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            rows.extend(["[gdb]\n", result.stdout or "", result.stderr or ""])
            rows.append(f"gdb_returncode={result.returncode}\n")
        except Exception as error:
            rows.append(f"[gdb_error] {type(error).__name__}: {error}\n")
        task_root = Path(f"/proc/{host_pid}/task")
        try:
            for task in sorted(task_root.iterdir(), key=lambda path: path.name):
                rows.append(f"\n[task {task.name}]\n")
                for name in ("comm", "stat"):
                    try:
                        rows.append(f"[{name}] { (task / name).read_text().strip()}\n")
                    except OSError as error:
                        rows.append(f"[{name}_error] {error}\n")
        except OSError as error:
            rows.append(f"[proc_error] {error}\n")
        destination.write_text("".join(rows), encoding="utf-8")
        return {"file": destination.name, "host_pid": host_pid}

    def _capture_kernel_segfault(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if self.evidence_dir is None or state.get("ExitCode") != 139:
            return None
        pid = state.get("Pid")
        started_at = state.get("StartedAt")
        if not started_at:
            return {"error": "container StartedAt unavailable", "host_pid": pid}
        try:
            result = subprocess.run(
                ["journalctl", "-k", "--since", str(started_at), "--output=short-iso"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            output = result.stdout or ""
            pid_pattern = re.compile(
                rf"\bpx4(?:-gazebo)?\[{re.escape(str(pid))}\](?:\s|:)", re.I
            )
            matching = [line for line in output.splitlines() if pid_pattern.search(line)]
            content = "\n".join(matching) + ("\n" if matching else "")
            path = self.evidence_dir / "kernel-segfault.log"
            path.write_text(content or (result.stderr or "No matching PX4 PID kernel lines.\n"), encoding="utf-8")
            logger_signature = any(
                re.search(r"0x2885a[89]|\blogger(?:\[\d+\])?\b", line, re.I)
                for line in matching
            )
            info = {
                "file": path.name,
                "host_pid": pid,
                "started_at": started_at,
                "returncode": result.returncode,
                "matching_lines": len(matching),
                "failure_class": "px4_logger_segfault" if logger_signature else "px4_other_segfault",
            }
            return info
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}", "host_pid": pid}

    @staticmethod
    def _console_tail(text: str, *, limit: int = 1200) -> tuple[str, int]:
        ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        cleaned = ansi.sub("", text).replace("\r", "\n")
        rows = []
        prompts_removed = 0
        for line in cleaned.splitlines():
            row = re.sub(r"^\s*pxh>\s?", "", line, flags=re.IGNORECASE)
            if row != line:
                prompts_removed += 1
            if row.strip():
                rows.append(row.rstrip())
        return "\n".join(rows[-limit:]) + ("\n" if rows else ""), prompts_removed

    def _capture_container_path(
        self, remote: str, destination: Path, errors: dict[str, str]
    ) -> bool:
        try:
            destination.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [
                    _docker_executable(self.plan),
                    "cp",
                    f"{self.name}:{remote}/.",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode == 0:
                return True
            errors[remote] = (result.stderr or result.stdout or "docker cp failed").strip()[-1000:]
        except Exception as error:
            errors[remote] = f"{type(error).__name__}: {error}"
        return False

    def _capture_evidence(self) -> dict[str, Any]:
        assert self.evidence_dir is not None
        result: dict[str, Any] = {
            "container": self.name,
            "captured_wall_time": time.time(),
            "running_before_cleanup": None,
            "state_before_cleanup": None,
            "sim_clock_stamp_ns_before_cleanup": None,
            "sim_clock_advancing_before_cleanup": None,
            "exit_code": None,
            "oom_killed": None,
            "errors": {},
        }
        errors: dict[str, str] = result["errors"]
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        initial_state = self.inspect_state()
        result["state_before_cleanup"] = initial_state
        if initial_state is not None:
            result["running_before_cleanup"] = initial_state.get("Running")
            if initial_state.get("Running") is False and initial_state.get("ExitCode") == 139:
                result["kernel_segfault"] = self._capture_kernel_segfault(initial_state)
        sim_stamp_ns, sim_progress_wall = self.sim_clock_snapshot()
        result["sim_clock_stamp_ns_before_cleanup"] = sim_stamp_ns
        result["sim_clock_advancing_before_cleanup"] = (
            time.monotonic() - sim_progress_wall <= 2.5 if sim_stamp_ns is not None else None
        )
        keep_running = bool(
            self.started
            and self.plan.keep_container_on_failure
            and self._exit_error is not None
            and self._exit_error is not LowDiskSpace
        )
        if self.started and not keep_running:
            try:
                stopped = subprocess.run(
                    [_docker_executable(self.plan), "stop", "--timeout", "10", self.name],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                result["stop_returncode"] = stopped.returncode
                if stopped.returncode != 0:
                    errors["docker_stop"] = (stopped.stderr or stopped.stdout).strip()[-1000:]
            except Exception as error:
                errors["docker_stop"] = f"{type(error).__name__}: {error}"
        state = self.inspect_state()
        result["state_after_cleanup_stop"] = state
        if isinstance(state, dict):
            result["exit_code"] = state.get("ExitCode")
            result["oom_killed"] = state.get("OOMKilled")
        if initial_state is not None and initial_state.get("Running") is False:
            result["exit_code_source"] = "container_exit_before_cleanup"
        elif state is not None and state.get("Running") is False and result.get("stop_returncode") == 0:
            result["exit_code_source"] = "collector_stop"
        elif state is not None and state.get("Running") is True:
            result["exit_code_source"] = "container_still_running"
        else:
            result["exit_code_source"] = "state_after_cleanup_stop"
        try:
            logs = subprocess.run(
                [_docker_executable(self.plan), "logs", "--tail", "5000", self.name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            raw_logs = (logs.stdout or "") + (logs.stderr or "")
            (self.evidence_dir / "container.log").write_text(raw_logs, encoding="utf-8")
            console_tail, removed = self._console_tail(raw_logs)
            (self.evidence_dir / "px4-console-tail.log").write_text(
                console_tail, encoding="utf-8"
            )
            result["container_logs"] = {
                "returncode": logs.returncode,
                "console_tail_lines": len(console_tail.splitlines()),
                "pxh_prompt_lines_removed": removed,
            }
            if logs.returncode != 0:
                errors["docker_logs"] = (logs.stderr or "docker logs failed").strip()[-1000:]
        except Exception as error:
            errors["docker_logs"] = f"{type(error).__name__}: {error}"

        paths = {
            "/opt/px4-gazebo/log": self.evidence_dir / "px4-ulog",
            "/root/.gz/sim/log": self.evidence_dir / "gz-sim-log",
        }
        copied = {
            remote: self._capture_container_path(remote, local, errors)
            for remote, local in paths.items()
        }
        ulogs = sorted((self.evidence_dir / "px4-ulog").rglob("*.ulg"))
        analyzed: list[str] = []
        for path in ulogs:
            try:
                from scripts.analyze_px4_ulog import parse_ulog, render_markdown

                report = render_markdown(parse_ulog(path))
                report_path = path.with_suffix(path.suffix + ".analysis.md")
                report_path.write_text(report, encoding="utf-8")
                analyzed.append(str(report_path.relative_to(self.episode_dir)))
            except Exception as error:
                errors[f"ulog_analysis:{path.name}"] = f"{type(error).__name__}: {error}"
        server_logs = sorted(
            path.relative_to(self.evidence_dir).as_posix()
            for path in (self.evidence_dir / "gz-sim-log").rglob("server_console.log")
        ) if (self.evidence_dir / "gz-sim-log").is_dir() else []
        result.update(
            {
                "copied_paths": copied,
                "ulog_files": [str(path.relative_to(self.episode_dir)) for path in ulogs],
                "ulog_analysis": analyzed,
                "server_console_logs": server_logs,
                "sim_clock_stamp_ns": self.sim_clock_snapshot()[0],
                "evidence_directory": str(self.evidence_dir.relative_to(self.episode_dir)),
                "px4_threads": self.px4_threads_evidence,
            }
        )
        if self.plan.keep_px4_ulog and ulogs:
            legacy = Path(self.plan.out) / "jobs" / self.name / "px4-ulog"
            for path in ulogs:
                try:
                    legacy.mkdir(parents=True, exist_ok=True)
                    relative = path.relative_to(self.evidence_dir / "px4-ulog")
                    shutil.copy2(path, legacy / relative.as_posix().replace("/", "__"))
                except Exception as error:
                    errors[f"legacy_ulog_copy:{path.name}"] = f"{type(error).__name__}: {error}"
        (self.evidence_dir / "index.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return result

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._exit_error = exc_type
        if exc_type is not None:
            reason = f"{exc_type.__name__}: {exc}"
            clock_diagnostics = getattr(exc, "px4_clock_diagnostics", None)
            diagnostics = self.capture_failure_diagnostics(
                reason,
                px4_clock_diagnostics=clock_diagnostics,
            )
            if "clock stalled" in reason.lower() or _is_sensor_stream_timeout(reason):
                evidence = self.capture_stall_evidence(
                    reason,
                    px4_clock_diagnostics=clock_diagnostics,
                )
                diagnostics["stall_capture"] = {
                    "path": evidence.get("path"),
                    "samples": len(evidence.get("samples", [])),
                }
        try:
            evidence_captured = False
            if self.evidence_dir is not None and self.started:
                try:
                    self.evidence_index = self._capture_evidence()
                    evidence_captured = True
                    self._progress(
                        "container_evidence_captured",
                        container=self.name,
                        ulog_files=len(self.evidence_index.get("ulog_files", [])),
                        errors=self.evidence_index.get("errors", {}),
                    )
                except Exception as error:
                    self.evidence_index = {
                        "container": self.name,
                        "evidence_directory": str(self.evidence_dir.relative_to(self.episode_dir)),
                        "capture_error": f"{type(error).__name__}: {error}",
                    }
                    try:
                        self.evidence_dir.mkdir(parents=True, exist_ok=True)
                        (self.evidence_dir / "capture-error.json").write_text(
                            json.dumps(self.evidence_index, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                    self._progress(
                        "container_evidence_failed", container=self.name,
                        error=f"{type(error).__name__}: {error}",
                    )
            if self.started or self.attached or self.failure_diagnostics is not None:
                try:
                    if evidence_captured:
                        self.cleanup_diagnostics = self.evidence_index.get(
                            "state_after_cleanup_stop"
                        )
                        if self.failure_diagnostics is not None:
                            self.failure_diagnostics["cleanup_state"] = self.cleanup_diagnostics
                    else:
                        self.capture_cleanup_diagnostics()
                except Exception as error:
                    self._progress(
                        "container_cleanup_snapshot_failed",
                        container=self.name,
                        error=f"{type(error).__name__}: {error}",
                    )
        finally:
            keep_running = bool(
                exc_type is not None
                and self.plan.keep_container_on_failure
                and exc_type is not LowDiskSpace
            )
            if self.started and not keep_running:
                try:
                    subprocess.run(
                        [_docker_executable(self.plan), "rm", "--force", self.name],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                except Exception as error:
                    self._progress("container_remove_failed", container=self.name, error=str(error))
                self.started = False
                self._progress("cleanup", container=self.name, removed=True)
            elif self.started:
                self._progress("cleanup", container=self.name, removed=False, kept=True)
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
            "rw_",
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
            observation = data.get("observation", {}) if isinstance(data, dict) else {}
            if isinstance(observation, dict):
                requested = teacher_intent_clearance_altitude(observation)
            else:
                requested = None
            if requested is not None:
                altitude, source = requested, "teacher_intent_camera_altitude"
        except (OSError, json.JSONDecodeError):
            pass
    profile = _airframe_profile(plan.airframe) if plan.airframe != "x500_mono_cam" else None
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
        observation = intent.get("observation", {}) if isinstance(intent, dict) else {}
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
    profile = _airframe_profile(plan.airframe, plan.takeoff_method)
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
        runtime_mode="sim",
        camera_mount=(
            profile.camera_mount.to_mapping()
            if profile is not None and profile.camera_mount is not None
            else None
        ),
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
    profile = _airframe_profile(plan.airframe)
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


def _wait_smoke_reposition(
    target_ned: tuple[float, float, float],
    reader: MavlinkReader,
    *,
    category: str,
    timeout_s: float = DELTA_SMOKE_REPOSITION_TIMEOUT_S,
) -> dict[str, Any]:
    """Require sustained capture of the active PX4 fixed-wing loiter ring."""
    started_wall = time.monotonic()
    started_sim_ns = _pose_sim_stamp_ns(reader.state)
    if started_sim_ns is None:
        return {
            "waited": "timeout",
            "distance_m": None,
            "tolerance_m": 8.0,
            "sim_elapsed_s": None,
            "sim_clock_available": False,
            "elapsed_s": 0.0,
            "target_ned_m": list(target_ned),
            **reposition_ring_metrics([], target_ned=target_ned, radius_m=FIXED_WING_LOITER_CAPTURE_M),
        }
    wall_ceiling = max(180.0, float(timeout_s) * 10.0)
    distance = float("inf")
    sim_elapsed_s = 0.0
    observations: list[dict[str, Any]] = []
    while time.monotonic() - started_wall < wall_ceiling:
        reader.raise_if_failed()
        current_sim_ns = _pose_sim_stamp_ns(reader.state)
        if current_sim_ns is not None:
            sim_elapsed_s = max(0.0, (current_sim_ns - started_sim_ns) / 1e9)
        position = _state_position(reader.state)
        distance = math.sqrt(
            sum((float(target) - float(actual)) ** 2 for target, actual in zip(target_ned, position, strict=False))
        )
        state = _flight_state_snapshot(reader.state)["vtol_state"]
        fw_active = category == "fixed_wing" or state == 4
        observations.append({
            "sim_time_s": sim_elapsed_s,
            "x": position[0],
            "y": position[1],
            "fw_active": fw_active,
        })
        ring = reposition_ring_metrics(
            observations,
            target_ned=target_ned,
            radius_m=FIXED_WING_LOITER_CAPTURE_M,
        )
        if ring["fw_reposition_success"]:
            return {
                "waited": "arrived",
                "distance_m": distance,
                "tolerance_m": 8.0,
                "sim_elapsed_s": sim_elapsed_s if current_sim_ns is not None else None,
                "sim_clock_available": current_sim_ns is not None,
                "elapsed_s": time.monotonic() - started_wall,
                "target_ned_m": list(target_ned),
                **ring,
            }
        if (
            current_sim_ns is not None
            and sim_elapsed_s >= timeout_s
        ):
            break
        time.sleep(0.1)
    return {
        "waited": "timeout",
        "distance_m": distance,
        "tolerance_m": 8.0,
        "sim_elapsed_s": sim_elapsed_s,
        "sim_clock_available": True,
        "elapsed_s": time.monotonic() - started_wall,
        "target_ned_m": list(target_ned),
        **reposition_ring_metrics(
            observations,
            target_ned=target_ned,
            radius_m=FIXED_WING_LOITER_CAPTURE_M,
        ),
    }


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


def _policy_for(plan: CollectionPlan, truth_path: Path, seed: int):
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
        observation = task_intent.get("observation", {}) if isinstance(task_intent, dict) else {}
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
    profile = _airframe_profile(plan.airframe, plan.takeoff_method)
    default_hover_mission = bool(
        profile is not None
        and profile.category == "vtol"
        and profile.subtype == "tailsitter"
        and profile.id.startswith("delta_tailsitter")
    )
    vtol_hover_mission = (
        default_hover_mission
        if plan.vtol_hover_mission is None
        else bool(plan.vtol_hover_mission)
    )
    camera_mount = (
        profile.camera_mount.to_camera_mount()
        if profile is not None and profile.camera_mount is not None
        else None
    )
    return ExpertSearchPolicy(
        target_ned=target,
        intrinsics=DEFAULT_CAMERA_INFO,
        altitude_m=altitude_m,
        camera_pitch_rad=camera_pitch,
        camera_mount=camera_mount,
        max_detection_range_m=max_detection_range,
        **({"home_ned": home_ned} if home_ned is not None else {}),
        obstacles=obstacles,
        observation_frames=frames if observation_waypoints else 0,
        observation_waypoints=tuple(observation_waypoints),
        observation_view_names=tuple(observation_view_names),
        fixed_wing_mode=(
            profile is not None
            and profile.category == "fixed_wing"
            or profile is not None
            and profile.category == "vtol"
            and not vtol_hover_mission
        ),
        vtol_hover_mission=vtol_hover_mission,
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

    def __init__(self, *, strict_delta_smoke: bool = False) -> None:
        actions = (
            (
                ActionPrimitive.translate(35.0, 0.0),
                ActionPrimitive.hold(DELTA_SMOKE_FW_MIN_DURATION_S),
                ActionPrimitive.stop(),
            )
            if strict_delta_smoke
            else (
                ActionPrimitive.translate(35.0, 0.0),
                ActionPrimitive.hold(8.0),
                ActionPrimitive.translate(0.0, 35.0),
                ActionPrimitive.turn(90.0),
                ActionPrimitive.translate(35.0, 0.0),
                ActionPrimitive.hold(8.0),
                ActionPrimitive.stop(),
            )
        )
        self._actions = iter(actions)

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
    gimbal = state.get("GIMBAL_DEVICE_ATTITUDE_STATUS")
    gimbal_manager = state.get("GIMBAL_MANAGER_STATUS")
    # Keep raw status fields in episode telemetry.  PX4 versions expose either
    # quaternion/device flags or manager angles; preserving both lets offline
    # consumers reconstruct the actual joint state without assuming one dialect.
    gimbal_status = None
    if gimbal is not None or gimbal_manager is not None:
        gimbal_status = {
            "device": MavlinkReader._mapping(gimbal) if gimbal is not None else None,
            "angles_deg": gimbal_angles_from_status(gimbal) if gimbal is not None else None,
        "manager": (
            MavlinkReader._mapping(gimbal_manager) if gimbal_manager is not None else None
        ),
        }
    attitude = state.get("ATTITUDE")
    attitude_deg = None
    if attitude is not None:
        attitude_deg = {
            axis: math.degrees(float(_field(attitude, source, 0.0)))
            for axis, source in (("roll", "roll"), ("pitch", "pitch"), ("yaw", "yaw"))
        }
    return {
        "vtol_state": _field(extended, "vtol_state", None),
        "landed_state": _field(extended, "landed_state", None),
        "airspeed_mps": airspeed,
        "attitude_deg": attitude_deg,
        "gimbal_status": gimbal_status,
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
    ack = getattr(result, "ack", {})
    if platform_name != "nt" and isinstance(ack, Mapping) and ack.get("wall_timeout"):
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


class MavlinkTelemetrySilenceWatchdog:
    """Fail waits when wire silence outlasts both vehicle time and host time."""

    def __init__(
        self,
        reader: MavlinkReader,
        environment: DockerSimEnvironment,
        recorder: EpisodeRecorder,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        self.reader = reader
        self.environment = environment
        self.recorder = recorder
        self.clock = clock
        self.last_count = int(reader.state.count)
        received = getattr(reader.state, "received_monotonic_ns", {})
        latest_message_ns = max(received.values(), default=None)
        self.last_message_wall = (
            latest_message_ns / 1e9 if latest_message_ns is not None else self.clock()
        )
        self.last_vehicle_ms = reader.state.vehicle_time_boot_ms
        sim_stamp, sim_progress_wall = environment.sim_clock_snapshot()
        self.last_telemetry_sim_ns = sim_stamp
        self.last_observed_sim_stamp_ns = sim_stamp
        self.last_sim_progress_wall = sim_progress_wall
        self.fired = False

    def check(self) -> None:
        if self.fired:
            return
        now = self.clock()
        state = self.reader.state
        if int(state.count) != self.last_count:
            self.last_count = int(state.count)
            self.last_message_wall = now
            self.last_vehicle_ms = getattr(state, "vehicle_time_boot_ms", None)
            sim_stamp, self.last_sim_progress_wall = self.environment.sim_clock_snapshot()
            self.last_telemetry_sim_ns = sim_stamp
            self.last_observed_sim_stamp_ns = sim_stamp
            return

        sim_stamp_ns, sim_progress_wall = self.environment.sim_clock_snapshot()
        if sim_stamp_ns is not None and (
            self.last_observed_sim_stamp_ns is None
            or sim_stamp_ns > self.last_observed_sim_stamp_ns
        ):
            self.last_observed_sim_stamp_ns = sim_stamp_ns
            self.last_sim_progress_wall = sim_progress_wall
        wall_silent_s = max(0.0, now - self.last_message_wall)
        sim_base_ns = self.last_telemetry_sim_ns
        if sim_base_ns is None and self.last_vehicle_ms is not None:
            sim_base_ns = int(self.last_vehicle_ms) * 1_000_000
        sim_elapsed_s = (
            max(0.0, (sim_stamp_ns - sim_base_ns) / 1e9)
            if sim_stamp_ns is not None and sim_base_ns is not None
            else None
        )
        sim_stalled_s = max(0.0, now - self.last_sim_progress_wall)
        failed = wall_silent_s >= MAVLINK_SILENCE_WALL_CAP_S
        failed = failed or (
            wall_silent_s >= MAVLINK_SILENCE_MIN_WALL_S
            and sim_elapsed_s is not None
            and sim_elapsed_s >= MAVLINK_SILENCE_SIM_TIMEOUT_S
        )
        failed = failed or (
            sim_stalled_s >= MAVLINK_SILENCE_STALLED_SIM_WALL_S
        )
        if not failed:
            return

        self.fired = True
        state_snapshot = self.environment.inspect_state()
        container_running = state_snapshot.get("Running") if state_snapshot else None
        reader_stats = self.reader.stats
        if container_running is False:
            failure_class = (
                "px4_other_segfault"
                if state_snapshot and state_snapshot.get("ExitCode") == 139
                else "flight"
            )
        elif reader_stats.get("thread_alive") is False:
            failure_class = "telemetry_receiver"
            failure_detail = "receiver_thread_exit"
        elif reader_stats.get("last_exception"):
            failure_class = "telemetry_receiver"
            failure_detail = "receiver_exception"
        else:
            failure_class = "px4_hang"
            failure_detail = None
        if container_running is False:
            failure_detail = "container_exit"
        kernel_capture = getattr(self.environment, "_capture_kernel_segfault", None)
        kernel_segfault = (
            kernel_capture(state_snapshot)
            if callable(kernel_capture)
            and container_running is False
            and state_snapshot
            and state_snapshot.get("ExitCode") == 139
            else None
        )
        if kernel_segfault and kernel_segfault.get("failure_class"):
            failure_class = kernel_segfault["failure_class"]
        sim_clock_advancing = sim_stamp_ns is not None and now - sim_progress_wall <= 2.5
        thread_evidence = None
        if container_running is True:
            host_pid = state_snapshot.get("Pid") if state_snapshot else None
            if isinstance(host_pid, int) and host_pid > 0:
                try:
                    thread_evidence = self.environment._capture_px4_threads(host_pid)
                except Exception as error:
                    thread_evidence = {"error": f"{type(error).__name__}: {error}", "host_pid": host_pid}
                self.environment.px4_threads_evidence = thread_evidence
        diagnostic = {
            "error": "MAVLink telemetry silent",
            "failure_class": failure_class,
            "failure_detail": failure_detail,
            "wall_silent_s": wall_silent_s,
            "sim_elapsed_since_last_mavlink_s": sim_elapsed_s,
            "sim_time_stalled_wall_s": sim_stalled_s,
            "sim_clock_stamp_ns": sim_stamp_ns,
            "sim_clock_advancing": (
                sim_clock_advancing
            ),
            "px4_threads": thread_evidence,
            "container": {
                "name": self.environment.name,
                "running": container_running,
                "state": state_snapshot,
            },
            "mavlink_reader": reader_stats,
            "receiver_thread_alive": reader_stats.get("thread_alive"),
            "receiver_last_exception": reader_stats.get("last_exception"),
            "receiver_message_counts": reader_stats.get("message_counts"),
            "receiver_socket": reader_stats.get("socket"),
            "receiver_recent_messages": reader_stats.get("recent_messages"),
            "last_message_type": getattr(self.reader, "last_message_type", None),
            "last_message_monotonic_ns": getattr(self.reader, "last_message_monotonic_ns", None),
            "last_message_vehicle_ms": getattr(self.reader, "last_message_vehicle_ms", None),
            "thresholds": {
                "sim_s": MAVLINK_SILENCE_SIM_TIMEOUT_S,
                "stalled_sim_wall_s": MAVLINK_SILENCE_STALLED_SIM_WALL_S,
                "minimum_realtime_factor": MAVLINK_SILENCE_MIN_REALTIME_FACTOR,
                "wall_cap_s": MAVLINK_SILENCE_WALL_CAP_S,
                "minimum_wall_s": MAVLINK_SILENCE_MIN_WALL_S,
            },
        }
        self.recorder.meta["telemetry_silence"] = diagnostic
        self.recorder.episode_path.write_text(
            json.dumps(self.recorder.meta, indent=2), encoding="utf-8"
        )
        self.recorder.record_mavlink(
            {"message_type": "collector_telemetry_silence", **diagnostic}
        )
        (self.recorder.episode_dir / "telemetry-silence.json").write_text(
            json.dumps(diagnostic, indent=2), encoding="utf-8"
        )
        raise TimeoutError(
            "MAVLink telemetry silent: "
            f"wall_s={wall_silent_s:.1f}, sim_elapsed_s={sim_elapsed_s}, "
            f"sim_time_advancing={diagnostic['sim_clock_advancing']}, "
            f"container_running={diagnostic['container']['running']}"
        )


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
    camera_mount: Any = None,
    ack: dict[str, Any],
    outcome: dict[str, Any],
    obs_after: dict[str, Any],
    labels: dict[str, Any],
    timing: dict[str, Any] | None = None,
) -> bool:
    """Persist one policy step and report whether the primitive loop continues."""
    if camera_mount is not None and camera_mount.kind == "gimbal":
        validate_gimbal_recording_observation(before)
        validate_gimbal_recording_observation(obs_after)
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


def _delta_fw_thr_min_baseline(plan: CollectionPlan) -> str:
    airframe = ROOT / "simulation" / "px4" / "airframes" / "4050_gz_delta_tailsitter"
    match = re.search(
        r"^param set-default FW_THR_MIN (\S+)$",
        airframe.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError("4050 airframe does not define FW_THR_MIN")
    value = match.group(1)
    for name, override in plan.extra_sim_params:
        if name == "FW_THR_MIN":
            value = override
    return value


def _set_runtime_sim_param(
    plan: CollectionPlan,
    environment: DockerSimEnvironment,
    parameter: str,
    value: str,
) -> dict[str, Any]:
    command = [
        _docker_executable(plan),
        "exec",
        environment.name,
        "/opt/px4-gazebo/bin/px4-param",
        "set",
        parameter,
        value,
    ]
    attempts = []
    for attempt in range(1, 4):
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
            return {"parameter": parameter, "value": value, "attempts": attempts}
        time.sleep(0.25)
    raise RuntimeError(f"could not set PX4 parameter {parameter}={value}: {attempts[-1]}")


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


def _achieved_delta_ned(before_pose: Any, after_pose: Any) -> list[float]:
    """Return this primitive's measured local-NED displacement."""
    return [float(after_pose[index]) - float(before_pose[index]) for index in range(3)]


def _wait_prearm_health(
    reader: MavlinkReader,
    timeout_s: float,
    *,
    max_wall_s: float = PREARM_HEALTH_HARD_WALL_CEILING_S,
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
    wall_started = time.monotonic()
    budget_clock = (
        time.monotonic if os.name == "nt" else _SimClock(reader, wall=time.monotonic)
    )
    started = budget_clock()
    deadline = started + float(timeout_s)
    wall_deadline = wall_started + (
        float(timeout_s) if os.name == "nt" else max(0.0, float(max_wall_s))
    )
    last_report = 0.0
    last_snapshot: dict[str, Any] | None = None
    while True:
        reader.raise_if_failed()
        wall_now = time.monotonic()
        budget_now = budget_clock()
        if wall_now >= wall_deadline or budget_now >= deadline:
            break
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
            warm = budget_now - started >= max(0.0, float(warmup_s))
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
            result["heartbeat_warmup_s"] = budget_now - started
            return result
        if wall_now - last_report >= 5.0:
            last_report = wall_now
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


def _wait_for_sim_pre_abort_descent(
    reader: Any,
    predicate: callable,
    *,
    timeout_s: float = SIM_PRE_ABORT_DESCENT_TIMEOUT_S,
    max_wall_s: float = SIM_PRE_ABORT_DESCENT_HARD_WALL_CEILING_S,
    phase: str = "sim_pre_abort_descent_reached",
    interval_s: float = 0.05,
) -> None:
    """Wait on PX4 lockstep time while bounding a frozen SITL by wall time."""
    wall_started = time.monotonic()
    sim_clock = _SimClock(reader, wall=time.monotonic)
    started = sim_clock()
    deadline = started + float(timeout_s)
    wall_deadline = wall_started + max(0.0, float(max_wall_s))
    while True:
        reader.raise_if_failed()
        if predicate():
            _progress(phase)
            return
        wall_now = time.monotonic()
        sim_now = sim_clock()
        if wall_now >= wall_deadline:
            raise TimeoutError(
                f"timed out waiting for {phase} after {timeout_s:.1f}s of PX4 time; "
                f"wall_ceiling_s={max_wall_s:.1f}"
            )
        if sim_now >= deadline:
            raise TimeoutError(f"timed out waiting for {phase} after {timeout_s:.1f}s of PX4 time")
        time.sleep(interval_s)


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
    command_owner: Mapping[str, Any] = labels
    command = command_owner.get("gimbal_command")
    onboard_labels = labels.get("onboard")
    if not isinstance(command, Mapping) and isinstance(onboard_labels, Mapping):
        command_owner = onboard_labels
        command = command_owner.get("gimbal_command")
    if not isinstance(command, Mapping) or controller is None:
        return False
    if command_owner.get("gimbal_dispatched") is True:
        return False
    if "pitch_down_rad" not in command or "yaw_world_rad" not in command:
        return False
    controller.command_pitch_yaw(
        float(command["pitch_down_rad"]),
        float(command["yaw_world_rad"]),
        mode=str(command.get("mode", default_mode)),
        vehicle_yaw_rad=math.radians(float(vehicle_yaw_deg)),
    )
    if isinstance(command_owner, dict):
        command_owner["gimbal_dispatched"] = True
    return True


def _gimbal_attitude_angles_deg(
    status: Any,
    *,
    vehicle_yaw_deg: float | None = None,
    yaw_correction_deg: float = 0.0,
) -> dict[str, float] | None:
    """Decode the device quaternion, converting earth-frame yaw to body yaw."""
    q = _field(status, "q", None)
    if not isinstance(q, (list, tuple)) or len(q) < 4:
        return None
    w, x, y, z = (float(value) for value in q[:4])
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if not math.isfinite(norm) or norm <= 1e-9:
        return None
    w, x, y, z = (value / norm for value in (w, x, y, z))
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    yaw_deg = math.degrees(yaw)
    # MAVLink GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME is bit 6.  PX4 uses this
    # flag for the tailsitter bridge while roll/pitch remain device axes.
    flags = int(_field(status, "flags", 0) or 0)
    if vehicle_yaw_deg is not None and flags & 64:
        yaw_deg = (yaw_deg - float(vehicle_yaw_deg) + 180.0) % 360.0 - 180.0
    yaw_deg = (yaw_deg + float(yaw_correction_deg) + 180.0) % 360.0 - 180.0
    return {
        "yaw": yaw_deg,
        "roll": math.degrees(roll),
        "pitch": math.degrees(pitch),
    }


def _gimbal_attitude_error_deg(
    status: Any,
    target: Mapping[str, float],
    *,
    vehicle_yaw_deg: float | None = None,
    yaw_correction_deg: float = 0.0,
) -> dict[str, float] | None:
    actual = _gimbal_attitude_angles_deg(
        status,
        vehicle_yaw_deg=vehicle_yaw_deg,
        yaw_correction_deg=yaw_correction_deg,
    )
    if actual is None:
        return None
    return {
        axis: abs((actual[axis] - float(target[axis]) + 180.0) % 360.0 - 180.0)
        for axis in ("yaw", "roll", "pitch")
    }


def _command_and_wait_gimbal(
    connection: Any,
    reader: Any,
    target: Mapping[str, float],
    *,
    timeout_s: float = 15.0,
    tolerance_deg: float = 4.0,
    yaw_correction_deg: float = 0.0,
) -> dict[str, Any]:
    """Acquire gimbal-manager control, command pitch/yaw, and verify fresh telemetry."""
    sender = getattr(getattr(connection, "mav", None), "command_long_send", None)
    if sender is None:
        raise RuntimeError("MAVLink connection cannot send gimbal command")
    target_system = int(getattr(connection, "target_system", 1) or 1)
    target_component = int(getattr(connection, "target_component", 1) or 1)
    command_started_ns = time.monotonic_ns()
    sender(
        target_system, target_component, MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE, 0,
        float(GCS_SOURCE_SYSTEM), float(GCS_SOURCE_COMPONENT),
        -1.0, -1.0, 0.0, 0.0, float(GIMBAL_MANAGER_DEVICE_ID),
    )
    configure_ack = _ReaderAckLink(connection, reader).wait_command_ack(
        MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE, min(timeout_s, 5.0)
    )
    if configure_ack is None or int(configure_ack.get("result", -1)) != 0:
        return {
            "status": "manager_configure_rejected" if configure_ack is not None else "manager_configure_ack_timeout",
            "target_deg": dict(target), "configure_ack": configure_ack, "tolerance_deg": tolerance_deg,
        }
    ack_state = getattr(reader.state, "command_acks", {})
    configure_ack_entry = ack_state.get(MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE) if isinstance(ack_state, Mapping) else None
    configure_ack_ns = int(configure_ack_entry[1]) if configure_ack_entry is not None else command_started_ns
    manager_deadline = time.monotonic() + GIMBAL_MANAGER_STATUS_WALL_TIMEOUT_S
    primary = 0
    primary_component = 0
    manager_confirmed = False
    while time.monotonic() < manager_deadline:
        manager = reader.state.get("GIMBAL_MANAGER_STATUS")
        received = getattr(reader.state, "received_monotonic_ns", {})
        if isinstance(reader.state, Mapping):
            received = reader.state.get("received_monotonic_ns", received)
        manager_ns = received.get("GIMBAL_MANAGER_STATUS", -1) if isinstance(received, Mapping) else -1
        primary = int(_field(manager, "primary_control_sysid", 0) or 0)
        primary_component = int(_field(manager, "primary_control_compid", 0) or 0)
        if (manager is not None and int(manager_ns) >= configure_ack_ns
                and primary == GCS_SOURCE_SYSTEM and primary_component == GCS_SOURCE_COMPONENT):
            manager_confirmed = True
            break
        time.sleep(0.02)
    if not manager_confirmed:
        return {
            "status": "manager_control_not_owned", "target_deg": dict(target),
            "configure_ack": configure_ack, "primary_control_sysid": primary,
            "primary_control_compid": primary_component,
            "tolerance_deg": tolerance_deg,
        }
    command_started_ns = time.monotonic_ns()
    sender(
        target_system, target_component, MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW, 0,
        float(target["pitch"]), float(target["yaw"]), math.nan, math.nan,
        float(
            GIMBAL_MANAGER_FLAGS_ROLL_LOCK
            | GIMBAL_MANAGER_FLAGS_PITCH_LOCK
            | GIMBAL_MANAGER_FLAGS_YAW_IN_VEHICLE_FRAME
        ),
        0.0, float(GIMBAL_MANAGER_DEVICE_ID),
    )
    ack = _ReaderAckLink(connection, reader).wait_command_ack(
        MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW, min(timeout_s, 5.0)
    )
    if ack is None or int(ack.get("result", -1)) != 0:
        return {
            "status": "command_rejected" if ack is not None else "command_ack_timeout",
            "target_deg": dict(target), "configure_ack": configure_ack,
            "command_id": MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
            "command_ack": ack,
            "tolerance_deg": tolerance_deg,
        }
    started = time.monotonic()
    last_error = None
    while time.monotonic() - started < timeout_s:
        reader.raise_if_failed()
        status = reader.state.get("GIMBAL_DEVICE_ATTITUDE_STATUS")
        received = (
            reader.state.get("received_monotonic_ns", {})
            if isinstance(reader.state, dict)
            else getattr(reader.state, "received_monotonic_ns", {})
        )
        attitude = reader.state.get("ATTITUDE")
        vehicle_yaw_deg = (
            math.degrees(float(_field(attitude, "yaw", 0.0))) if attitude is not None else None
        )
        error = (
            _gimbal_attitude_error_deg(
                status,
                target,
                vehicle_yaw_deg=vehicle_yaw_deg,
                yaw_correction_deg=yaw_correction_deg,
            )
            if received.get("GIMBAL_DEVICE_ATTITUDE_STATUS", -1) >= command_started_ns
            else None
        )
        if error is not None:
            last_error = error
            if max(error.values()) <= tolerance_deg:
                return {
                    "status": "reached",
                    "target_deg": dict(target),
                    "actual_deg": _gimbal_attitude_angles_deg(
                        status,
                        vehicle_yaw_deg=vehicle_yaw_deg,
                        yaw_correction_deg=yaw_correction_deg,
                    ),
                    "error_deg": error,
                    "tolerance_deg": tolerance_deg,
                }
        time.sleep(0.05)
    return {"status": "timeout", "target_deg": dict(target), "error_deg": last_error,
            "configure_ack": configure_ack, "command_ack": ack,
            "command_id": MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW, "tolerance_deg": tolerance_deg}


def _return_onboard_gimbal_to_search_angle(
    plan: CollectionPlan,
    profile: AirframeProfile,
    connection: Any,
    reader: Any,
    *,
    strict_delta_smoke: bool,
    yaw_correction_deg: float = 0.0,
) -> dict[str, Any] | None:
    """Restore onboard search framing after the diagnostic -45-degree check."""
    if plan.teacher != "onboard" or strict_delta_smoke:
        return None
    gimbal = profile.camera.gimbal
    defaults = gimbal.get("default_angles_deg", {}) if isinstance(gimbal, Mapping) else {}
    target = defaults.get("mc") if isinstance(defaults, Mapping) else None
    if not isinstance(target, Mapping):
        raise RuntimeError("gimbal MC search angle is missing from the airframe profile")
    result = _command_and_wait_gimbal(
        connection,
        reader,
        {axis: float(target[axis]) for axis in ("yaw", "roll", "pitch")},
        tolerance_deg=4.0,
        yaw_correction_deg=yaw_correction_deg,
    )
    if result.get("status") != "reached":
        raise RuntimeError(f"gimbal did not return to onboard search angle: {result}")
    return result


def _wait_for_stable_mc_hover(
    reader: MavlinkReader,
    *,
    stable_s: float = GIMBAL_HOVER_STABLE_S,
    timeout_s: float = 60.0,
    wall_timeout_s: float = GIMBAL_HOVER_MAX_WALL_S,
    clock: Any = time.monotonic,
) -> dict[str, Any]:
    wall_started = clock()
    started_boot_ms = reader.state.vehicle_time_boot_ms
    stable_since_ms: int | None = None
    sample_count = 0
    maximum_abs_roll = 0.0
    maximum_abs_pitch = 0.0
    last_state: dict[str, Any] = {}
    while clock() - wall_started < wall_timeout_s:
        reader.raise_if_failed()
        attitude = reader.state.get("ATTITUDE")
        vtol_state = _field(reader.state.get("EXTENDED_SYS_STATE"), "vtol_state", None)
        boot_ms = _field(attitude, "time_boot_ms", reader.state.vehicle_time_boot_ms)
        if isinstance(boot_ms, (int, float)) and not isinstance(boot_ms, bool):
            boot_ms = int(boot_ms)
        else:
            boot_ms = None
        if attitude is not None and boot_ms is not None:
            roll = math.degrees(float(_field(attitude, "roll", 0.0)))
            pitch = math.degrees(float(_field(attitude, "pitch", 0.0)))
            maximum_abs_roll = max(maximum_abs_roll, abs(roll))
            maximum_abs_pitch = max(maximum_abs_pitch, abs(pitch))
            last_state = {
                "vtol_state": vtol_state,
                "roll_deg": roll,
                "pitch_deg": pitch,
                "time_boot_ms": boot_ms,
            }
            if vtol_state == 3 and abs(roll) < 5.0 and abs(pitch) < 5.0:
                stable_since_ms = boot_ms if stable_since_ms is None else stable_since_ms
                sample_count += 1
                if boot_ms - stable_since_ms >= int(stable_s * 1000.0):
                    return {
                        "status": "stable",
                        "vtol_state": vtol_state,
                        "stable_duration_s": (boot_ms - stable_since_ms) / 1000.0,
                        "sample_count": sample_count,
                        "maximum_abs_roll_deg": maximum_abs_roll,
                        "maximum_abs_pitch_deg": maximum_abs_pitch,
                        "flight_state": _flight_state_snapshot(reader.state),
                        "elapsed_wall_s": clock() - wall_started,
                    }
            else:
                stable_since_ms = None
            if (
                started_boot_ms is not None
                and boot_ms - int(started_boot_ms) >= int(timeout_s * 1000.0)
            ):
                break
        time.sleep(0.05)
    raise TimeoutError(
        "timed out waiting for stable MC hover: "
        f"stable_s={stable_s:.1f}, timeout_sim_s={timeout_s:.1f}, "
        f"last_state={last_state}"
    )


def _request_fresh_sensor_sample(
    sensor_reader: Any,
    mav_reader: MavlinkReader,
    *,
    timeout_s: float = 15.0,
) -> Any:
    previous_count = int(sensor_reader.frames_received)
    minimum_stamp_ns = _pose_sim_stamp_ns(mav_reader.state) or 0
    sensor_reader.request(minimum_stamp_ns)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        mav_reader.raise_if_failed()
        sensor_reader.raise_if_failed()
        if int(sensor_reader.frames_received) > previous_count:
            sample = sensor_reader.latest
            if sample is not None and int(sample.sensor_stamp_ns) >= minimum_stamp_ns:
                return sample
        time.sleep(0.02)
    raise TimeoutError(
        "timed out waiting for a fresh Gazebo camera/joint-state sample "
        f"at or after sim_ns={minimum_stamp_ns}"
    )


def _gimbal_measurement_snapshot(reader: MavlinkReader, sensor_sample: Any) -> dict[str, Any]:
    state = reader.state
    aligned_state = reader.aligned_state(sensor_sample.sensor_stamp_ns)
    device = state.get("GIMBAL_DEVICE_ATTITUDE_STATUS")
    attitude = aligned_state.get("ATTITUDE")
    received = getattr(state, "received_monotonic_ns", {})
    sidecar = getattr(sensor_sample, "joint_state", None)
    gimbal_joints = sidecar.get("gimbal_joint_state") if isinstance(sidecar, dict) else None
    return {
        "body_attitude_deg": _flight_state_snapshot(aligned_state)["attitude_deg"],
        "body_attitude_time_boot_ms": _field(attitude, "time_boot_ms", None),
        "body_pose_alignment": getattr(aligned_state, "pose_alignment", None),
        "vtol_state": _flight_state_snapshot(state)["vtol_state"],
        "gimbal_device_attitude_status": (
            MavlinkReader._mapping(device) if device is not None else None
        ),
        "gimbal_device_receive_monotonic_ns": received.get(
            "GIMBAL_DEVICE_ATTITUDE_STATUS"
        ),
        "gimbal_joint_state": gimbal_joints,
        "sensor_stamp_ns": int(sensor_sample.sensor_stamp_ns),
    }


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


def _delta_native_rtl_requested() -> bool:
    """Diagnostic switch: let PX4's RTL do its own back-transition and landing."""
    return os.environ.get("MULTIDETECT_DELTA_NATIVE_RTL") == "1"


def _wait_native_rtl_landing(reader: MavlinkReader, *, timeout_s: float = 3600.0) -> dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        reader.raise_if_failed()
        state = _flight_state_snapshot(reader.state)
        if state.get("landed_state") == 1:
            return {"status": "landed", "method": "native_rtl", "elapsed_s": time.monotonic() - started}
        time.sleep(0.2)
    raise TimeoutError(f"native RTL did not land within {timeout_s:.0f} s")


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
    # RTL can complete the physical landing before the collector reaches its
    # teardown call.  Do not upload a second landing mission in that case.
    flight = _flight_state_snapshot(reader.state)
    received = getattr(reader.state, "received_monotonic_ns", {})
    landed_stamp = received.get("EXTENDED_SYS_STATE", -1)
    landed_fresh = 0 <= time.monotonic_ns() - landed_stamp <= int(
        LANDING_TELEMETRY_STALE_S * 1e9
    )
    if flight["landed_state"] == 1 and landed_fresh:
        return {
            "status": "landed",
            "timeout_s": wait_timeout_s,
            "elapsed_s": 0.0,
            "elapsed_budget_s": 0.0,
            "flight_state": flight,
            "command": None,
            "already_landed": True,
        }
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


def _episode_wall_ceiling_s(airframe: str, maximum_s: float) -> float:
    normalized = str(airframe).strip().lower().replace("-", "_")
    if normalized in {
        "delta_tailsitter",
        "delta_tailsitter_mono_cam",
        "delta_tailsitter_gimbal_cam",
    }:
        return max(
            DELTA_TAILSITTER_MIN_WALL_CEILING_S,
            float(maximum_s) * DELTA_TAILSITTER_WALL_CEILING_FACTOR,
        )
    return float(maximum_s) + max(float(maximum_s), 600.0)


class _EpisodeProgressWatchdog:
    """Bound an episode by vehicle-clock progress after the nominal wall budget."""

    def __init__(
        self,
        started: float,
        maximum_s: float,
        reader: Any,
        *,
        stall_timeout_s: float = EPISODE_PROGRESS_STALL_TIMEOUT_S,
        wall_ceiling_s: float | None = None,
        clock: callable = time.monotonic,
    ) -> None:
        self.started = float(started)
        self.maximum_s = float(maximum_s)
        self.reader = reader
        self.stall_timeout_s = max(1.0, float(stall_timeout_s))
        self.wall_ceiling_s = (
            float(wall_ceiling_s)
            if wall_ceiling_s is not None
            else self.maximum_s + max(self.maximum_s, 600.0)
        )
        self.clock = clock
        self.last_vehicle_ms: int | None = None
        self.sim_started_ms: int | None = None
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
            if self.sim_started_ms is None:
                self.sim_started_ms = vehicle_ms
            self.last_vehicle_ms = vehicle_ms
            self.last_progress_wall = now
        elif vehicle_ms is None and message_progress:
            self.last_progress_wall = now
        self.last_message_count = message_count
        if (
            self.sim_started_ms is not None
            and vehicle_ms is not None
            and vehicle_ms - self.sim_started_ms >= self.maximum_s * 1000.0
        ):
            raise TimeoutError(
                "episode exceeded its simulation-time budget "
                f"({self.maximum_s:.0f}s; vehicle_time_boot_ms={vehicle_ms})"
            )
        if now - self.started <= self.maximum_s:
            return
        if now - self.last_progress_wall <= self.stall_timeout_s:
            # Progress only extends the budget up to a hard ceiling: a vehicle that
            # keeps streaming telemetry while stuck in one phase must still end.
            if now - self.started <= self.wall_ceiling_s:
                return
            raise TimeoutError(
                "episode exceeded its hard wall-clock ceiling "
                f"({self.wall_ceiling_s:.0f}s)"
            )
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
    reconnect: Callable[[], SensorStreamReader | None] | None = None,
    max_reconnects: int = 0,
):
    """Wait for a fresh frame at or beyond a requested simulation timestamp.

    SIM rendering in Docker is slow (a frame every few seconds), so frame age is
    recorded in the observation instead of refusing the step.  ``previous_ns``
    is a host monotonic receive watermark; ``min_sensor_stamp_ns`` is the
    optional Gazebo/PX4 lockstep watermark and prevents consuming a backlog
    frame captured before the pose used for this observation. The requested
    simulation watermark is fixed for this wait, even while PX4 advances.
    """
    reconnects = 0
    reconnect_error: str | None = None
    while True:
        reader.request(0 if min_sensor_stamp_ns is None else min_sensor_stamp_ns)
        deadline = time.monotonic() + timeout_s
        restarted = False
        while time.monotonic() < deadline:
            try:
                reader.raise_if_failed()
            except Exception:
                if reconnect is None or reconnects >= max(0, int(max_reconnects)):
                    raise
                reconnects += 1
                try:
                    replacement = reconnect()
                    if replacement is None:
                        raise RuntimeError("sensor reconnect callback returned no reader")
                    reader = replacement
                    reconnect_error = None
                    restarted = True
                except Exception as exc:
                    reconnect_error = f"{type(exc).__name__}: {exc}"
                break
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
        if restarted:
            continue
        sample = reader.latest
        latest_stamp_ns = getattr(sample, "sensor_stamp_ns", None)
        latest_received_ns = getattr(sample, "received_monotonic_ns", None)
        latest_age_s = (
            round(max(0.0, (time.monotonic_ns() - int(latest_received_ns)) / 1e9), 3)
            if latest_received_ns is not None
            else None
        )
        reader_thread = getattr(reader, "_thread", None)
        reader_done = getattr(reader, "done", None)
        reader_error = getattr(reader, "error", None)
        diagnostics = {
            "min_sensor_stamp_ns": min_sensor_stamp_ns,
            "sensor_watermark_lag_s": (
                round(max(0, int(min_sensor_stamp_ns) - int(latest_stamp_ns)) / 1e9, 3)
                if min_sensor_stamp_ns is not None and latest_stamp_ns is not None
                else None
            ),
            "previous_received_monotonic_ns": previous_ns,
            "frames_received": getattr(reader, "frames_received", None),
            "frames_dropped": getattr(reader, "frames_dropped", None),
            "latest_sensor_stamp_ns": latest_stamp_ns,
            "latest_received_monotonic_ns": latest_received_ns,
            "latest_age_s": latest_age_s,
            "reader_thread_alive": bool(reader_thread and reader_thread.is_alive()),
            "reader_done": bool(reader_done and reader_done.is_set()),
            "reader_error": f"{type(reader_error).__name__}: {reader_error}"
            if reader_error is not None
            else None,
            "reconnect_attempts": reconnects,
            "reconnect_error": reconnect_error,
        }
        if reconnect is not None and reconnects < max(0, int(max_reconnects)):
            reconnects += 1
            try:
                replacement = reconnect()
                if replacement is None:
                    raise RuntimeError("sensor reconnect callback returned no reader")
                reader = replacement
                reconnect_error = None
                continue
            except Exception as exc:
                reconnect_error = f"{type(exc).__name__}: {exc}"
                diagnostics["reconnect_attempts"] = reconnects
                diagnostics["reconnect_error"] = reconnect_error
        raise TimeoutError(
            f"sensor stream produced no frame within {timeout_s:.1f}s; "
            f"diagnostics={json.dumps(diagnostics, sort_keys=True, default=str)}"
        )


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
                elif isinstance(event, dict) and event.get("event") == "sim_teacher_sim_clock":
                    try:
                        environment.note_sim_clock(int(event["stamp_ns"]))
                    except (KeyError, TypeError, ValueError):
                        _progress("sensor_sim_clock_invalid", details=event)
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
    plan = freeze_collection_runtime(plan)
    episode_id = f"episode-{episode_index:04d}-{uuid.uuid4().hex[:8]}"
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
    airframe_profile = _airframe_profile(plan.airframe, plan.takeoff_method)
    use_multirotor_executor = airframe_uses_multirotor_executor(
        airframe_profile or plan.airframe, plan.takeoff_method, base_dir=ROOT
    )
    camera_mount = (
        airframe_profile.camera_mount.to_camera_mount()
        if airframe_profile is not None and airframe_profile.camera_mount is not None
        else None
    )
    strict_delta_smoke = False
    sensor_frame_wait_s = SENSOR_FRAME_WAIT_S
    if plan.smoke_flight:
        smoke_profile = _airframe_profile(plan.airframe)
        strict_delta_smoke = bool(
            smoke_profile is not None
            and smoke_profile.id in DELTA_TAILSITTER_SMOKE_PROFILES
        )
        if strict_delta_smoke:
            sensor_frame_wait_s = DELTA_SMOKE_SENSOR_FRAME_WAIT_S
        policy = _SmokeFlightPolicy(strict_delta_smoke=strict_delta_smoke)
        task_text = (
            "delta tailsitter 冒烟飞行：起飞、转换、120 秒盘旋、到点重定位、返航、降落"
            if strict_delta_smoke
            else "固定翼/VTOL 冒烟飞行：起飞、转换、盘旋、重定位、返航、降落"
        )
    elif plan.teacher == "onboard":
        # Only the curated task text is read from the generated index; truth is
        # used after the episode by the scorer, never by the policy.
        task_text = plan.task_text or _index_task_text(truth_path) or "找到目标并记录位置"
        policy = _onboard_policy(plan, task_text, Path(plan.out) / f"{episode_id}-onboard-frames")
    elif plan.teacher == "expert_search":
        policy, target_label = _policy_for(plan, truth_path, plan.seed + episode_index)
        task_text = _index_task_text(truth_path) or (
            "\u627e\u5230" + str(target_label) + "\u5e76\u8bb0\u5f55\u4f4d\u7f6e"
        )
    elif plan.teacher == "hover":
        policy = _hover_policy(plan, truth_path)
        task_text = "Hover above the moving target path and record observations."
    else:
        policy = _policy_for(plan, truth_path, plan.seed + episode_index)
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
    recorder.meta["delta_tailsitter_smoke_gates"] = strict_delta_smoke
    px4_logger_mode = _effective_px4_logger_mode(plan)
    recorder.meta["px4_logger_mode"] = px4_logger_mode
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
    recorder.meta["provenance"]["px4_logger_mode"] = px4_logger_mode
    recorder.meta["provenance"]["px4_ulog_status"] = (
        "disabled"
        if px4_logger_mode == "off"
        else "requested" if plan.keep_px4_ulog else "not_requested"
    )
    recorder.meta["provenance"]["px4_daemon_mode"] = resolve_px4_daemon_mode()
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
    telemetry_watchdog = None
    environment = None
    launch_status_texts: list[str] = []
    setpoint_streamer = None
    mover_driver = None
    gcs_heartbeat_stop = gcs_heartbeat_thread = None
    gimbal_controller = None
    try:
        sampler_cpus = (
            _parse_cpu_set(cpu_isolation.cpuset_cpus)
            if cpu_isolation is not None
            else _current_cpuset()
        )
    except (AttributeError, OSError):
        sampler_cpus = tuple(range(os.cpu_count() or 0))
    host_pressure_sampler = HostPressureSampler(recorder.episode_dir, sampler_cpus)

    environment = DockerSimEnvironment(plan, episode_id, recorder.episode_dir)

    def record_container_failure(exc: Exception) -> None:
        failure_text = f"{type(exc).__name__}: {exc}"
        recorder.meta["failure"] = failure_text
        clock_diagnostics = getattr(exc, "px4_clock_diagnostics", None)
        diagnostics = environment.capture_failure_diagnostics(
            failure_text,
            px4_clock_diagnostics=clock_diagnostics,
        )
        sensor_stream_timeout = _is_sensor_stream_timeout(failure_text)
        if sensor_stream_timeout:
            evidence = environment.capture_stall_evidence(
                failure_text,
                px4_clock_diagnostics=clock_diagnostics,
            )
            diagnostics["stall_capture"] = {
                "path": evidence.get("path"),
                "samples": len(evidence.get("samples", [])),
            }
        if diagnostics.get("cleanup_state") is None:
            environment.capture_cleanup_diagnostics()
        failure_class = diagnostics["failure_class"]
        if isinstance(exc, VehicleLostControl):
            failure_class = "vehicle_lost_control"
            recorder.meta["vehicle_lost_control"] = exc.event
        recorder.meta["failure_class"] = failure_class
        recorder.meta["container_failure_diagnostics"] = diagnostics
        if (
            clock_diagnostics is not None
            or "clock stalled" in failure_text.lower()
            or sensor_stream_timeout
        ):
            recorder.meta["failure_record"] = {
                "kind": "sensor_stream_stalled"
                if sensor_stream_timeout
                else "px4_sim_clock_stall",
                **(clock_diagnostics or {}),
                "stall_capture_path": (diagnostics.get("stall_capture") or {}).get("path"),
            }
        recorder.meta["failure_snapshot"] = build_failure_snapshot(
            recorder.meta,
            timeline=recorder.meta.get("frame_wait_timeline"),
            pressure_path=host_pressure_sampler.path,
            capture=environment.stall_capture,
            final_failure_class=failure_class,
        )

    try:
        host_pressure_sampler.start()
    except Exception:
        # Host diagnostics are strictly best effort and must never abort SIM.
        pass
    try:
        with environment:
            environment.wait(_episode_budget(started, plan.max_episode_s, 20.0))
            _progress("gz_world_loaded", container=environment.name)
            _record_container_started(recorder, environment)
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
            telemetry_connection = mavutil.mavlink_connection(
                sim_udpin_endpoint(plan.telemetry_port, mode=plan.sim_network),
                source_system=GCS_SOURCE_SYSTEM,
                source_component=GCS_SOURCE_COMPONENT,
                autoreconnect=False,
            )
            if camera_mount is not None and camera_mount.kind == "gimbal":

                def _send_gimbal_command(command: int, params: tuple[float, ...]) -> None:
                    sender = getattr(
                        getattr(telemetry_connection, "mav", None), "command_long_send", None
                    )
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
                for candidate in (
                    getattr(policy, "executor", None),
                    getattr(getattr(policy, "supervisor", None), "executor", None),
                ):
                    if candidate is not None and hasattr(candidate, "gimbal_controller"):
                        candidate.gimbal_controller = gimbal_controller
            recorder.begin_mavlink_stream()

            def _record_mavlink(row: dict[str, Any]) -> None:
                if str(row.get("message_type", "")).upper() == "STATUSTEXT":
                    launch_status_texts.append(str(row.get("text", "")))
                recorder.record_mavlink(row)

            mav_reader = MavlinkReader(telemetry_connection, on_message=_record_mavlink).start()
            host_pressure_sampler.sim_clock_provider = (
                lambda: mav_reader.state.vehicle_time_boot_ms
            )
            episode_watchdog = _EpisodeProgressWatchdog(
                started,
                plan.max_episode_s,
                mav_reader,
                wall_ceiling_s=_episode_wall_ceiling_s(plan.airframe, plan.max_episode_s),
            )
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
            simulation_gimbal_yaw_correction_deg = _simulation_gimbal_yaw_correction_deg(plan)
            # Must match _sensor_command: non-x500 airframes always stream the
            # registered depth camera even when the scenario omits it.
            declares_depth = any(
                isinstance(s, dict) and s.get("kind") == "depth_camera"
                for s in runtime.get("sensors", [])
            )
            if declares_depth or plan.airframe != "x500_mono_cam":
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
            def _start_sensor_reader(process: subprocess.Popen[bytes]) -> SensorStreamReader:
                if process.stdout is None:
                    raise RuntimeError("sensor stream stdout is unavailable")
                return SensorStreamReader(
                    process.stdout,
                    request_stream=process.stdin,
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

            sensor_reader = _start_sensor_reader(sensor_process)
            # The collector-owned frame server is request/response based, so
            # explicitly prime it before waiting for the first decoded frame.
            sensor_reader.request(0)
            sensor_reader.wait_first_frame(_episode_budget(started, plan.max_episode_s, 45.0))
            _progress("sensor_stream_first_frame", frames=sensor_reader.frames_received)

            def _reconnect_sensor_stream() -> SensorStreamReader:
                nonlocal sensor_process, sensor_reader
                if sensor_reader is not None:
                    sensor_reader.stop(timeout_s=1.0)
                if sensor_process is not None:
                    try:
                        sensor_process.terminate()
                        sensor_process.wait(timeout=2.0)
                    except Exception:
                        try:
                            sensor_process.kill()
                        except Exception:
                            pass
                sensor_process = _make_sensor_process(plan, environment)
                sensor_reader = _start_sensor_reader(sensor_process)
                sensor_reader.request(0)
                _progress("sensor_stream_reconnected", frames=sensor_reader.frames_received)
                return sensor_reader

            def wait_for_observation_frame(
                phase: str,
                pose_state: MavlinkState,
                min_sensor_stamp_ns: int | None,
                primitive_end_ns: int | None = None,
            ) -> Any:
                reader = sensor_reader
                if reader is None:
                    raise RuntimeError("sensor reader is unavailable")
                freshness_watermark_ns = primitive_end_ns
                wait_started_ns = time.monotonic_ns()
                wait_started_wall = time.time()
                start_row = _frame_wait_diagnostics(
                    phase=phase,
                    sensor_reader=reader,
                    mav_reader=mav_reader,
                    pose_state=pose_state,
                    min_sensor_stamp_ns=min_sensor_stamp_ns,
                    started_monotonic_ns=wait_started_ns,
                    primitive_end_monotonic_ns=freshness_watermark_ns,
                    freshness_watermark_ns=freshness_watermark_ns,
                    started_wall_time=wait_started_wall,
                    host_pressure_path=host_pressure_sampler.path,
                )
                recorder.meta.setdefault("frame_wait_timeline", []).append(start_row)
                try:
                    sample = _fresh_sensor_snapshot(
                        reader,
                        freshness_watermark_ns or wait_started_ns,
                        sensor_frame_wait_s,
                        min_sensor_stamp_ns=min_sensor_stamp_ns,
                        poll_interval_s=plan.sensor_poll_interval_s,
                        reconnect=_reconnect_sensor_stream,
                        max_reconnects=1,
                    )
                except BaseException as error:
                    current_reader = sensor_reader or reader
                    end_row = _frame_wait_diagnostics(
                        phase=phase,
                        sensor_reader=current_reader,
                        mav_reader=mav_reader,
                        pose_state=pose_state,
                        min_sensor_stamp_ns=min_sensor_stamp_ns,
                        started_monotonic_ns=wait_started_ns,
                        primitive_end_monotonic_ns=freshness_watermark_ns,
                        freshness_watermark_ns=freshness_watermark_ns,
                        started_wall_time=wait_started_wall,
                        ended_wall_time=time.time(),
                        ended_monotonic_ns=time.monotonic_ns(),
                        host_pressure_path=host_pressure_sampler.path,
                        error=error,
                    )
                    recorder.meta.setdefault("frame_wait_timeline", []).append(end_row)
                    try:
                        recorder.record_mavlink(
                            {"message_type": "frame_wait_diagnostic", **end_row}
                        )
                        recorder.episode_path.write_text(
                            json.dumps(recorder.meta, indent=2), encoding="utf-8"
                        )
                    except Exception as diagnostic_error:
                        end_row["diagnostic_write_error"] = (
                            f"{type(diagnostic_error).__name__}: {diagnostic_error}"
                        )
                    raise
                current_reader = sensor_reader or reader
                end_row = _frame_wait_diagnostics(
                    phase=phase,
                    sensor_reader=current_reader,
                    mav_reader=mav_reader,
                    pose_state=pose_state,
                    min_sensor_stamp_ns=min_sensor_stamp_ns,
                    started_monotonic_ns=wait_started_ns,
                    primitive_end_monotonic_ns=freshness_watermark_ns,
                    freshness_watermark_ns=freshness_watermark_ns,
                    started_wall_time=wait_started_wall,
                    ended_wall_time=time.time(),
                    ended_monotonic_ns=time.monotonic_ns(),
                    host_pressure_path=host_pressure_sampler.path,
                )
                end_row["last_frame_sensor_stamp_ns"] = sample.sensor_stamp_ns
                end_row["last_frame_received_monotonic_ns"] = sample.received_monotonic_ns
                end_row["last_frame_received_wall_time"] = sample.received_wall_time
                recorder.meta.setdefault("frame_wait_timeline", []).append(end_row)
                try:
                    recorder.record_mavlink(
                        {"message_type": "frame_wait_diagnostic", **end_row}
                    )
                except Exception:
                    pass
                return sample

            telemetry_watchdog = MavlinkTelemetrySilenceWatchdog(
                mav_reader, environment, recorder
            )
            mav_reader.set_health_check(telemetry_watchdog.check)
            # The collector-owned frame server is request/response based, so
            # explicitly prime it before waiting for the first decoded frame.
            sensor_reader.request(0)
            sensor_reader.wait_first_frame(
                _episode_budget(started, plan.max_episode_s, _first_frame_wait_s() or 45.0)
            )
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
            primitive_sim_clock = _SimClock(mav_reader)
            start_pos = _state_position(mav_reader.state)
            if use_multirotor_executor:
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
                    airframe_profile=airframe_profile,
                    telemetry_reader=mav_reader,
                    gimbal_yaw_correction_deg=simulation_gimbal_yaw_correction_deg or 0.0,
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
                max_wall_s=_episode_budget(
                    started, plan.max_episode_s, PREARM_HEALTH_HARD_WALL_CEILING_S
                ),
                heartbeat_connection=telemetry_connection,
                mavutil_module=mavutil,
            )
            recorder.meta["prearm_health"] = prearm_health
            recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
            if use_multirotor_executor:
                # All multirotors use the original x500 lifecycle:
                # setpoints -> arm -> OFFBOARD -> climb.
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
                    getattr(policy, "vtol_hover_mission", False)
                    or getattr(getattr(policy, "motion_adapter", None), "hover_ops", False)
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
                    transition_after_takeoff=(
                        not hover_mission and profile.camera.mount != "gimbal"
                    ),
                    airframe_profile=profile,
                    telemetry_reader=mav_reader,
                    gimbal_yaw_correction_deg=simulation_gimbal_yaw_correction_deg or 0.0,
                    # Fixed-wing/VTOL command durations follow PX4's lockstep
                    # clock; a separate wall ceiling is enforced by callbacks
                    # below so a stalled simulator still terminates.
                    clock=primitive_sim_clock,
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

                def _record_delta_smoke_event(event: str, **fields: Any) -> None:
                    stamp_ns = _pose_sim_stamp_ns(_snapshot_mavlink_state(mav_reader.state))
                    recorder.record_mavlink(
                        {
                            "message_type": f"delta_smoke_{event}",
                            "sim_time_s": stamp_ns / 1e9 if stamp_ns is not None else None,
                            "vehicle_time_boot_ms": stamp_ns // 1_000_000
                            if stamp_ns is not None
                            else None,
                            "receive_monotonic_ns": time.monotonic_ns(),
                            "flight_state": _flight_state_snapshot(mav_reader.state),
                            **fields,
                        }
                    )

                def _run_delta_fw_overspeed_recovery() -> dict[str, Any]:
                    from scripts.smoke_airframe_flight import _percentile

                    baseline = _delta_fw_thr_min_baseline(plan)
                    commanded_altitude = -float(executor._commanded[2])
                    peak_mps: float | None = None
                    injection_status = "command_failed"
                    injection_failure: str | None = None
                    injection_elapsed = 0.0
                    restored = False
                    continuous_fw = True
                    recovery_status = "not_run"
                    recovery_elapsed: float | None = None
                    stable_s = 0.0
                    recovery_height_errors: list[float] = []

                    _record_delta_smoke_event(
                        "fw_overspeed_start",
                        target_airspeed_mps=DELTA_OVERSPEED_TARGET_MPS,
                        injection_timeout_s=DELTA_OVERSPEED_INJECT_TIMEOUT_S,
                        recovery_timeout_s=DELTA_OVERSPEED_RECOVERY_MAX_S,
                        recovery_hold_s=DELTA_OVERSPEED_RECOVERY_HOLD_S,
                        fw_thr_min_baseline=baseline,
                        fw_thr_min_injected=DELTA_OVERSPEED_INJECT_MIN_THR,
                    )
                    try:
                        _set_runtime_sim_param(
                            plan,
                            environment,
                            "FW_THR_MIN",
                            str(DELTA_OVERSPEED_INJECT_MIN_THR),
                        )
                        injection_start_ns = _pose_sim_stamp_ns(
                            _snapshot_mavlink_state(mav_reader.state)
                        )
                        if injection_start_ns is None:
                            injection_status = "telemetry_missing"
                            injection_failure = "missing_sim_clock_at_injection_start"
                        else:
                            wall_deadline = time.monotonic() + max(
                                300.0, DELTA_OVERSPEED_INJECT_TIMEOUT_S * 10.0
                            )
                            last_sample_ns = injection_start_ns - 1
                            while time.monotonic() < wall_deadline:
                                mav_reader.raise_if_failed()
                                snapshot = _snapshot_mavlink_state(mav_reader.state)
                                stamp_ns = _pose_sim_stamp_ns(snapshot)
                                if stamp_ns is None or stamp_ns <= last_sample_ns:
                                    time.sleep(0.1)
                                    continue
                                elapsed_s = (stamp_ns - injection_start_ns) / 1e9
                                injection_elapsed = max(injection_elapsed, elapsed_s)
                                last_sample_ns = stamp_ns
                                state = _flight_state_snapshot(mav_reader.state)
                                if state.get("vtol_state") != 4:
                                    continuous_fw = False
                                    injection_status = "fw_exit"
                                    injection_failure = "left_fw_during_injection"
                                    break
                                speed = state.get("airspeed_mps")
                                try:
                                    speed = float(speed)
                                except (TypeError, ValueError):
                                    speed = math.nan
                                if math.isfinite(speed):
                                    peak_mps = speed if peak_mps is None else max(peak_mps, speed)
                                    if speed >= DELTA_OVERSPEED_TARGET_MPS:
                                        injection_status = "reached"
                                        break
                                if elapsed_s >= DELTA_OVERSPEED_INJECT_TIMEOUT_S:
                                    injection_status = "timeout"
                                    injection_failure = "injection_failed_to_reach_21_mps"
                                    break
                                time.sleep(0.1)
                            if injection_status == "command_failed":
                                injection_status = "wall_timeout"
                                injection_failure = "injection_wall_timeout"
                    except Exception as exc:
                        injection_status = "command_failed"
                        injection_failure = f"injection_error:{type(exc).__name__}:{exc}"
                    _record_delta_smoke_event(
                        "fw_overspeed_injection_end",
                        status=injection_status,
                        elapsed_s=injection_elapsed,
                        peak_airspeed_mps=peak_mps,
                        failure_reason=injection_failure,
                    )

                    try:
                        _set_runtime_sim_param(plan, environment, "FW_THR_MIN", baseline)
                        restored = True
                        _record_delta_smoke_event(
                            "fw_overspeed_parameter_restored",
                            status="restored",
                            fw_thr_min=baseline,
                        )
                    except Exception as exc:
                        _record_delta_smoke_event(
                            "fw_overspeed_parameter_restored",
                            status="restore_failed",
                            fw_thr_min=baseline,
                            failure_reason=f"restore_error:{type(exc).__name__}:{exc}",
                        )
                        _record_delta_smoke_event(
                            "fw_overspeed_end",
                            status="restore_failed",
                            recovery_s=None,
                            stable_s=0.0,
                            continuous_fw=continuous_fw,
                            height_error_p95_m=None,
                            failure_reason="fw_thr_min_restore_failed",
                        )
                        raise RuntimeError("could not restore FW_THR_MIN after overspeed injection") from exc

                    if injection_status == "reached" and restored and continuous_fw:
                        recovery_start_ns = _pose_sim_stamp_ns(
                            _snapshot_mavlink_state(mav_reader.state)
                        )
                        if recovery_start_ns is None:
                            recovery_status = "telemetry_missing"
                        else:
                            _record_delta_smoke_event(
                                "fw_overspeed_recovery_start",
                                recovery_timeout_s=DELTA_OVERSPEED_RECOVERY_MAX_S,
                                recovered_airspeed_mps=19.0,
                                required_hold_s=DELTA_OVERSPEED_RECOVERY_HOLD_S,
                            )
                            recovery_status = "timeout"
                            stable_start_ns: int | None = None
                            last_sample_ns = recovery_start_ns - 1
                            wall_deadline = time.monotonic() + max(
                                300.0, DELTA_OVERSPEED_RECOVERY_MAX_S * 10.0
                            )
                            while time.monotonic() < wall_deadline:
                                mav_reader.raise_if_failed()
                                snapshot = _snapshot_mavlink_state(mav_reader.state)
                                stamp_ns = _pose_sim_stamp_ns(snapshot)
                                if stamp_ns is None or stamp_ns <= last_sample_ns:
                                    time.sleep(0.1)
                                    continue
                                recovery_elapsed = max(
                                    0.0, (stamp_ns - recovery_start_ns) / 1e9
                                )
                                last_sample_ns = stamp_ns
                                state = _flight_state_snapshot(mav_reader.state)
                                if state.get("vtol_state") != 4:
                                    continuous_fw = False
                                    recovery_status = "fw_exit"
                                    break
                                try:
                                    altitude = -float(_state_position(mav_reader.state)[2])
                                    recovery_height_errors.append(
                                        abs(altitude - commanded_altitude)
                                    )
                                except (TypeError, ValueError):
                                    pass
                                speed = state.get("airspeed_mps")
                                try:
                                    speed = float(speed)
                                except (TypeError, ValueError):
                                    speed = math.nan
                                if math.isfinite(speed) and speed <= 19.0:
                                    if stable_start_ns is None:
                                        stable_start_ns = stamp_ns
                                    stable_s = max(0.0, (stamp_ns - stable_start_ns) / 1e9)
                                else:
                                    stable_start_ns = None
                                    stable_s = 0.0
                                height_error_p95 = _percentile(recovery_height_errors, 95)
                                if (
                                    stable_s >= DELTA_OVERSPEED_RECOVERY_HOLD_S
                                    and recovery_elapsed <= DELTA_OVERSPEED_RECOVERY_MAX_S
                                ):
                                    if (
                                        len(recovery_height_errors) >= 30
                                        and height_error_p95 is not None
                                        and height_error_p95 <= DELTA_OVERSPEED_HEIGHT_ERROR_MAX_M
                                    ):
                                        recovery_status = "complete"
                                    else:
                                        recovery_status = "height_error"
                                    break
                                if recovery_elapsed >= DELTA_OVERSPEED_RECOVERY_MAX_S:
                                    break
                                time.sleep(0.1)
                    elif injection_status != "reached":
                        recovery_status = "not_run_injection_failed"
                    elif not continuous_fw:
                        recovery_status = "fw_exit"

                    height_error_p95 = _percentile(recovery_height_errors, 95)
                    _record_delta_smoke_event(
                        "fw_overspeed_end",
                        status=recovery_status,
                        recovery_s=recovery_elapsed,
                        stable_s=stable_s,
                        continuous_fw=continuous_fw,
                        height_error_p95_m=height_error_p95,
                        height_error_sample_count=len(recovery_height_errors),
                        failure_reason=(
                            injection_failure
                            or (
                                "airspeed_not_recovered_within_40_sim_s_and_15_s_hold"
                                if recovery_status == "timeout"
                                else "height_error_exceeded_10_m"
                                if recovery_status == "height_error"
                                else "left_fw_during_overspeed_step"
                                if recovery_status == "fw_exit"
                                else None
                            )
                        ),
                    )
                    return {
                        "injection_status": injection_status,
                        "peak_airspeed_mps": peak_mps,
                        "recovery_status": recovery_status,
                        "recovery_s": recovery_elapsed,
                        "stable_s": stable_s,
                        "height_error_p95_m": height_error_p95,
                        "continuous_fw": continuous_fw,
                        "parameter_restored": restored,
                    }

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
                    state_clock = executor._clock
                    started_wall = time.monotonic()
                    started_sim = state_clock()
                    if phase == "orbit" and strict_delta_smoke:
                        timeout_s = DELTA_SMOKE_FW_MAX_DURATION_S
                        started_wall = time.monotonic()
                        started_sim_ns = _pose_sim_stamp_ns(mav_reader.state)
                        commanded_altitude_m = -float(executor._commanded[2])
                        orbit_center = tuple(float(value) for value in executor._commanded[:2])
                        _record_delta_smoke_event(
                            "fw_sustain_start",
                            commanded_altitude_m=commanded_altitude_m,
                            orbit_center_ned_m=list(orbit_center),
                            minimum_duration_s=DELTA_SMOKE_FW_MIN_DURATION_S,
                            minimum_orbit_degrees=DELTA_SMOKE_FW_MIN_ORBIT_DEG,
                        )
                        if started_sim_ns is None:
                            _record_delta_smoke_event(
                                "fw_sustain_end",
                                status="missing_sim_clock",
                                fw_duration_s=0.0,
                                elapsed_s=0.0,
                                orbit_signed_degrees=0.0,
                                commanded_altitude_m=commanded_altitude_m,
                                continuous_fw=False,
                            )
                            return False
                        wall_ceiling = max(300.0, timeout_s * 10.0)
                        fw_duration_s = 0.0
                        orbit_degrees = 0.0
                        previous_sim_ns = started_sim_ns
                        previous_fw = False
                        fw_seen = False
                        continuous_fw = True
                        previous_bearing: float | None = None
                        latest_state: dict[str, Any] = {}
                        distance_from_center_m = 0.0
                        while time.monotonic() - started_wall < wall_ceiling:
                            mav_reader.raise_if_failed()
                            now_sim_ns = _pose_sim_stamp_ns(mav_reader.state)
                            if now_sim_ns is None:
                                time.sleep(0.1)
                                continue
                            elapsed_s = max(0.0, (now_sim_ns - started_sim_ns) / 1e9)
                            latest_state = _flight_state_snapshot(mav_reader.state)
                            in_fw = (
                                latest_state["vtol_state"] == 4
                                if profile.category == "vtol"
                                else True
                            )
                            if in_fw:
                                fw_seen = True
                            elif fw_seen:
                                continuous_fw = False
                                break
                            delta_sim_s = max(0.0, (now_sim_ns - previous_sim_ns) / 1e9)
                            if in_fw and previous_fw:
                                if delta_sim_s <= 1.0:
                                    fw_duration_s += delta_sim_s
                                else:
                                    continuous_fw = False
                                    break
                            previous_fw = in_fw
                            position_now = _state_position(mav_reader.state)
                            dn = position_now[0] - orbit_center[0]
                            de = position_now[1] - orbit_center[1]
                            distance_from_center_m = math.hypot(dn, de)
                            if in_fw and distance_from_center_m >= 10.0:
                                bearing = math.atan2(de, dn)
                                if previous_bearing is not None:
                                    delta = (bearing - previous_bearing + math.pi) % (2.0 * math.pi) - math.pi
                                    orbit_degrees += math.degrees(delta)
                                previous_bearing = bearing
                            else:
                                previous_bearing = None
                            if (
                                continuous_fw
                                and fw_seen
                                and in_fw
                                and fw_duration_s >= DELTA_SMOKE_FW_MIN_DURATION_S
                                and abs(orbit_degrees) >= DELTA_SMOKE_FW_MIN_ORBIT_DEG
                            ):
                                _record_delta_smoke_event(
                                    "fw_sustain_end",
                                    status="complete",
                                    fw_duration_s=fw_duration_s,
                                    elapsed_s=elapsed_s,
                                    orbit_signed_degrees=orbit_degrees,
                                    orbit_distance_from_center_m=distance_from_center_m,
                                    commanded_altitude_m=commanded_altitude_m,
                                    continuous_fw=continuous_fw,
                                )
                                return True
                            if elapsed_s >= timeout_s:
                                break
                            previous_sim_ns = now_sim_ns
                            time.sleep(0.1)
                        final_sim_ns = _pose_sim_stamp_ns(mav_reader.state)
                        _record_delta_smoke_event(
                            "fw_sustain_end",
                            status="timeout" if continuous_fw else "interrupted",
                            fw_duration_s=fw_duration_s,
                            elapsed_s=(final_sim_ns - started_sim_ns) / 1e9
                            if final_sim_ns is not None
                            else None,
                            orbit_signed_degrees=orbit_degrees,
                            orbit_distance_from_center_m=distance_from_center_m,
                            commanded_altitude_m=commanded_altitude_m,
                            continuous_fw=continuous_fw,
                        )
                        return False
                    transition_altitude_m = None
                    transition_max_altitude_m = None
                    if phase == "transition_mc" and strict_delta_smoke:
                        transition_altitude_m = -float(_state_position(mav_reader.state)[2])
                        transition_max_altitude_m = transition_altitude_m
                        _record_delta_smoke_event(
                            "post_transition_start",
                            commanded_altitude_m=transition_altitude_m,
                        )
                    # Allow very slow lockstep (the delta tailsitter measured
                    # about 0.12x realtime) while retaining a finite fallback.
                    wall_ceiling = max(180.0, float(timeout_s) * 10.0)
                    while state_clock() - started_sim < timeout_s and time.monotonic() - started_wall < wall_ceiling:
                        mav_reader.raise_if_failed()
                        state = _flight_state_snapshot(mav_reader.state)
                        z = _state_position(mav_reader.state)[2]
                        if transition_altitude_m is not None:
                            transition_max_altitude_m = max(
                                float(transition_max_altitude_m), -float(z)
                            )
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
                            if transition_altitude_m is not None:
                                climb_m = max(
                                    0.0,
                                    float(transition_max_altitude_m) - transition_altitude_m,
                                )
                                recorder.meta["delta_tailsitter_post_transition_climb_m"] = climb_m
                                recorder.episode_path.write_text(
                                    json.dumps(recorder.meta, indent=2), encoding="utf-8"
                                )
                                _record_delta_smoke_event(
                                    "post_transition_end",
                                    status="complete",
                                    commanded_altitude_m=transition_altitude_m,
                                    maximum_altitude_m=transition_max_altitude_m,
                                    climb_m=climb_m,
                                )
                            return True
                        if phase == "orbit":
                            speed = state["airspeed_mps"]
                            if (
                                speed is not None
                                and profile.min_airspeed_mps <= speed <= profile.max_airspeed_mps
                            ):
                                return True
                        time.sleep(0.1)
                    if transition_altitude_m is not None:
                        climb_m = max(
                            0.0,
                            float(transition_max_altitude_m) - transition_altitude_m,
                        )
                        recorder.meta["delta_tailsitter_post_transition_climb_m"] = climb_m
                        recorder.episode_path.write_text(
                            json.dumps(recorder.meta, indent=2), encoding="utf-8"
                        )
                        _record_delta_smoke_event(
                            "post_transition_end",
                            status="timeout",
                            commanded_altitude_m=transition_altitude_m,
                            maximum_altitude_m=transition_max_altitude_m,
                            climb_m=climb_m,
                        )
                    return False

                executor._wait_for_state = _wait_fw_state
                # Confirm the ARM command on the same telemetry socket before
                # sending NAV_VTOL_TAKEOFF.  A raw pymavlink ``arducopter_arm``
                # only transmits and returns immediately; on the gimbal model
                # PX4 can transiently answer TEMPORARILY_REJECTED just after
                # SYS_STATUS becomes healthy.  The bounded helper retries and
                # waits for the armed heartbeat, avoiding a false takeoff
                # timeout while preserving PX4's pre-arm checks.
                if isinstance(executor, FixedWingVtolExecutor):
                    confirmed_arm = _arm(
                        telemetry_connection,
                        mav_reader,
                        mavutil,
                        _episode_budget(started, plan.max_episode_s, 30.0),
                        heartbeat_connection=telemetry_connection,
                    )
                    executor._armed = bool(confirmed_arm.get("armed"))
                    _progress("fixed_wing_arm_confirmed", ack=confirmed_arm)
                arm_ack = executor.arm_and_takeoff(altitude_m)
                if arm_ack.status != "success":
                    raise RuntimeError(f"fixed-wing takeoff failed: {arm_ack.ack}")
                takeoff_stamp_ns = _pose_sim_stamp_ns(mav_reader.state)
                recorder.record_mavlink(
                    {
                        "message_type": "fixed_wing_takeoff_complete",
                        "sim_time_s": takeoff_stamp_ns / 1e9 if takeoff_stamp_ns is not None else None,
                        "receive_monotonic_ns": time.monotonic_ns(),
                        "method": takeoff_method,
                        "ack": _plain_record(arm_ack.ack),
                        "flight_state": _flight_state_snapshot(mav_reader.state),
                    }
                )
                _progress("takeoff_reached", method=takeoff_method)
                if profile.camera.mount == "gimbal":
                    hover_stability = _wait_for_stable_mc_hover(mav_reader)
                    before_sample = _request_fresh_sensor_sample(
                    sensor_reader, mav_reader, timeout_s=sensor_frame_wait_s
                )
                    before_measurement = _gimbal_measurement_snapshot(
                        mav_reader, before_sample
                    )
                    if before_measurement["gimbal_joint_state"] is None:
                        raise RuntimeError(
                            "Gazebo gimbal joint-state sample is missing one or more axes"
                        )
                    recorder.record_mavlink(
                        {
                            "message_type": "gimbal_smoke_measurement",
                            "phase": "before_command",
                            "stable_hover": hover_stability,
                            **before_measurement,
                        }
                    )
                    gimbal_result = _command_and_wait_gimbal(
                        vehicle,
                        mav_reader,
                        {"yaw": 0.0, "roll": 0.0, "pitch": -45.0},
                        tolerance_deg=4.0,
                        yaw_correction_deg=simulation_gimbal_yaw_correction_deg or 0.0,
                    )
                    after_sample = _request_fresh_sensor_sample(
                    sensor_reader, mav_reader, timeout_s=sensor_frame_wait_s
                )
                    after_measurement = _gimbal_measurement_snapshot(
                        mav_reader, after_sample
                    )
                    if after_measurement["gimbal_joint_state"] is None:
                        raise RuntimeError(
                            "Gazebo gimbal joint-state sample is missing one or more axes"
                        )
                    gimbal_result["stable_hover"] = hover_stability
                    gimbal_result["measurement_before_command"] = before_measurement
                    gimbal_result["measurement_after_command"] = after_measurement
                    recorder.meta["gimbal_smoke"] = gimbal_result
                    recorder.record_mavlink({"message_type": "gimbal_smoke_result", **gimbal_result})
                    recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
                    if gimbal_result["status"] != "reached":
                        raise RuntimeError(f"gimbal target not reached: {gimbal_result}")
                    search_angle_result = _return_onboard_gimbal_to_search_angle(
                        plan,
                        profile,
                        vehicle,
                        mav_reader,
                        strict_delta_smoke=strict_delta_smoke,
                        yaw_correction_deg=simulation_gimbal_yaw_correction_deg or 0.0,
                    )
                    if search_angle_result is not None:
                        recorder.meta["gimbal_search_angle"] = search_angle_result
                        recorder.record_mavlink(
                            {"message_type": "gimbal_search_angle_result", **search_angle_result}
                        )
                        recorder.episode_path.write_text(
                            json.dumps(recorder.meta, indent=2), encoding="utf-8"
                        )
                    if not hover_mission:
                        transition = executor._invoke(
                            "vtol_transition", "FW", timeout_s=executor._timeout()
                        )
                        if not executor._ok(transition):
                            raise RuntimeError(
                                f"failed to transition to FW after gimbal measurement: "
                                f"{_plain_record(transition)}"
                            )
                        if not _wait_fw_state(
                            "transition_fw", FixedWingVtolExecutor.STATE_TIMEOUT_S["transition_fw"]
                        ):
                            raise TimeoutError(
                                "timed out transitioning to FW after gimbal measurement"
                            )
                        executor.mode = "FW"
                        recorder.record_mavlink(
                            {
                                "message_type": "gimbal_measurement_fw_transition_complete",
                                "flight_state": _flight_state_snapshot(mav_reader.state),
                            }
                        )
            vehicle_loss_detector = VehicleLossDetector()
            if plan.sim_pre_abort_descent_altitude_m is not None:
                if setpoint_streamer is None:
                    raise RuntimeError(
                        "sim_pre_abort_descent_altitude_m requires a multirotor setpoint streamer"
                    )
                descent_altitude_m = plan.sim_pre_abort_descent_altitude_m
                setpoint_streamer.set_target(
                    start_pos[0], start_pos[1], -descent_altitude_m, _state_yaw(mav_reader.state)
                )
                _progress("sim_pre_abort_descent_started", target_altitude_m=descent_altitude_m)
                _wait_for_sim_pre_abort_descent(
                    mav_reader,
                    lambda: (
                        0.5 <= -_state_position(mav_reader.state)[2] <= descent_altitude_m + 0.25
                        and _flight_state_snapshot(mav_reader.state)["landed_state"] == 2
                    ),
                    max_wall_s=min(
                        SIM_PRE_ABORT_DESCENT_HARD_WALL_CEILING_S,
                        _episode_budget(
                            started,
                            plan.max_episode_s,
                            SIM_PRE_ABORT_DESCENT_HARD_WALL_CEILING_S,
                        ),
                    ),
                )
                recorder.meta["sim_pre_abort_descent_altitude_m"] = descent_altitude_m
                recorder.episode_path.write_text(
                    json.dumps(recorder.meta, indent=2), encoding="utf-8"
                )
            climb_fault = SimClimbTimeoutInjector(
                enabled=plan.sim_climb_timeout_injection,
                runtime_mode="sim",
                teacher=plan.teacher,
            )
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
                before_sample = wait_for_observation_frame(
                    "obs_before",
                    before_state,
                    pose_sim_stamp_ns,
                )
                timing["pre_frame_wait_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                before_state = mav_reader.aligned_state(before_sample.sensor_stamp_ns)
                t = time.perf_counter_ns()
                before = build_observation(
                    before_sample,
                    before_state,
                    camera_mount=camera_mount,
                    ground_agl_max_age_s=(
                        policy.ground_projection.agl_max_age_s
                        if getattr(getattr(policy, "ground_projection", None), "use_agl", False)
                        else None
                    ),
                    gimbal_yaw_correction_deg=simulation_gimbal_yaw_correction_deg,
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
                if camera_mount is not None and camera_mount.kind == "gimbal":
                    validate_gimbal_recording_observation(before)
                timing["observation_pack_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                pose = tuple(before["pose"]["local_ned_m"])
                yaw = float(before["attitude"]["yaw_deg"])
                if plan.teacher == "expert_search":
                    action = policy.next_action(
                        pose_ned=pose, yaw_deg=yaw, camera_pose=before.get("camera_pose")
                    )
                    labels = policy.labels(
                        pose_ned=pose, yaw_deg=yaw, camera_pose=before.get("camera_pose")
                    )
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
                if camera_mount is not None and camera_mount.kind == "gimbal":
                    _dispatch_teacher_gimbal_command(
                        gimbal_controller,
                        labels,
                        vehicle_yaw_deg=yaw,
                        default_mode=camera_mount.stabilization,
                    )
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
                active_skill = getattr(policy, "active_mission_skill", lambda: None)()
                inject_climb_timeout = climb_fault.matches(
                    step_action, active_skill=active_skill, before=before
                )
                if inject_climb_timeout:
                    labels = {
                        **labels,
                        "sim_fault_injection": {
                            "fault": "return_home_climb_timeout",
                            "attempt": climb_fault.injected + 1,
                            "active_skill": active_skill,
                        },
                    }
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
                        if (
                            strict_delta_smoke
                            and getattr(action, "kind", None) == "translate"
                            and isinstance(executor, FixedWingVtolExecutor)
                        ):
                            # A gimballed tailsitter climbs in MC before the
                            # smoke path transitions to FW. Rebase immediately
                            # before its first guided leg so translation starts
                            # from the post-transition measured pose, not the
                            # preflight/takeoff position cached by the executor.
                            executor.sync_commanded_state()
                        executed = (
                            climb_fault.execute(executor, step_action)  # noqa: B023
                            if inject_climb_timeout  # noqa: B023
                            else executor.execute(action)  # noqa: B023
                        )
                        if (
                            strict_delta_smoke
                            and getattr(action, "kind", None) == "translate"
                            and executed.status == "success"
                        ):
                            smoke_wait = _wait_smoke_reposition(
                                tuple(float(value) for value in executor._commanded),
                                mav_reader,
                                category=profile.category,
                            )
                            executed.ack["smoke_reposition_wait"] = smoke_wait
                            if smoke_wait["waited"] != "arrived":
                                executed = dataclasses.replace(
                                    executed,
                                    status="timeout",
                                    elapsed_s=float(executed.elapsed_s)
                                    + float(smoke_wait.get("elapsed_s", 0.0)),
                                )
                            else:
                                executor.note_loiter_captured()
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
                                        camera_mount=camera_mount,
                                        gimbal_yaw_correction_deg=simulation_gimbal_yaw_correction_deg,
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
                _raise_if_px4_clock_stalled(
                    result, index, sim_clock=primitive_sim_clock
                )
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
                                    camera_mount=camera_mount,
                                    gimbal_yaw_correction_deg=simulation_gimbal_yaw_correction_deg,
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
                after_sample = wait_for_observation_frame(
                    "obs_after",
                    after_state,
                    after_pose_sim_stamp_ns,
                    primitive_end_ns,
                )
                timing["post_frame_wait_ms"] = round((time.perf_counter_ns() - t) / 1e6, 3)
                after_state = mav_reader.aligned_state(after_sample.sensor_stamp_ns)
                t = time.perf_counter_ns()
                after = build_observation(
                    after_sample,
                    after_state,
                    camera_mount=camera_mount,
                    ground_agl_max_age_s=(
                        policy.ground_projection.agl_max_age_s
                        if getattr(getattr(policy, "ground_projection", None), "use_agl", False)
                        else None
                    ),
                    gimbal_yaw_correction_deg=simulation_gimbal_yaw_correction_deg,
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
                climb_failure = False
                report_outcome = getattr(policy, "report_primitive_outcome", None)
                if callable(report_outcome):
                    climb_failure = bool(
                        report_outcome(
                            step_action,
                            result.status,
                            before=before,
                            after=after,
                        )
                    )
                    if climb_failure:
                        labels = {
                            **labels,
                            "onboard": {
                                **(
                                    labels.get("onboard", {})
                                    if isinstance(labels, dict)
                                    else {}
                                ),
                                "recovery_outcome": "climb_failed_suspected_loss_of_control",
                            },
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
                    camera_mount=camera_mount,
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
                if climb_failure:
                    continue_loop = False
                loss_event = vehicle_loss_detector.update(
                    before,
                    after,
                    {"kind": step_action.kind, "up_m": step_action.up_m},
                    commanded_delta,
                )
                if loss_event is not None:
                    loss_event["step_index"] = index
                    recorder.record_mavlink(
                        {"message_type": "collector_vehicle_lost_control", **loss_event}
                    )
                    recorder.meta["failure_class"] = "vehicle_lost_control"
                    recorder.meta["vehicle_lost_control"] = loss_event
                    raise VehicleLostControl(loss_event)
                if (
                    strict_delta_smoke
                    and getattr(action, "kind", None) == "hold"
                    and result.status == "success"
                ):
                    with recorder._mavlink_lock:
                        if recorder._mavlink_file is not None:
                            recorder._mavlink_file.flush()
                    ring_judgement = _delta_fw_ring_metrics_from_files(
                        recorder.mavlink_path, recorder.steps_path
                    )
                    _record_delta_smoke_event(
                        "fw_ring_judgement",
                        judgement_complete=True,
                        **ring_judgement,
                    )
                    _progress("fw_ring_judgement", **ring_judgement)
                    overspeed_result = _run_delta_fw_overspeed_recovery()
                    recorder.meta["delta_tailsitter_fw_overspeed"] = overspeed_result
                    recorder.episode_path.write_text(
                        json.dumps(recorder.meta, indent=2), encoding="utf-8"
                    )
                    _progress("fw_overspeed_recovery_end", **overspeed_result)
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
            if not use_multirotor_executor:
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
                if (
                    profile is not None
                    and profile.category in {"fixed_wing", "vtol"}
                    and not (strict_delta_smoke and _delta_native_rtl_requested())
                ):
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
            if strict_delta_smoke and _delta_native_rtl_requested():
                land_result = _wait_native_rtl_landing(mav_reader)
            elif use_multirotor_executor:
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
            host_pressure_sampler.close()
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
        if environment is not None and environment.evidence_index is not None:
            recorder.meta["container_evidence"] = environment.evidence_index
            recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
    except LowDiskSpace as exc:
        host_pressure_sampler.close()
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
        if environment is not None and environment.evidence_index is not None:
            recorder.meta["container_evidence"] = environment.evidence_index
        recorder.mark_finished(status="aborted_low_disk", failure=str(exc))
        _progress("aborted_low_disk", episode=str(recorder.episode_dir), reason=str(exc))
        return recorder.episode_dir
    except Exception as exc:
        host_pressure_sampler.close()
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
        if environment is not None and environment.evidence_index is not None:
            recorder.meta["container_evidence"] = environment.evidence_index
        failure_text = f"{type(exc).__name__}: {exc}"
        record_container_failure(exc)
        if "sensor stream produced no frame within" in failure_text:
            diagnostics: dict[str, Any] = {}
            marker = "diagnostics="
            if marker in failure_text:
                raw = failure_text.split(marker, 1)[1]
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        diagnostics = parsed
                except json.JSONDecodeError:
                    diagnostics = {"raw": raw[:2000]}
            recorder.meta["sensor_stream_stall"] = {
                "failure_class": "sensor_stream_stall",
                "diagnosis": (
                    "Gazebo camera/depth/odometry topics and rendering initialized; "
                    "container and PX4 telemetry remained alive. The retained evidence "
                    "cannot distinguish a frame-server/bridge stall from a renderer stall."
                ),
                "last_frame_sensor_stamp_ns": diagnostics.get("latest_sensor_stamp_ns"),
                "last_frame_received_monotonic_ns": diagnostics.get(
                    "latest_received_monotonic_ns"
                ),
                "last_frame_age_s": diagnostics.get("latest_age_s"),
                **diagnostics,
            }
        recorder.mark_finished(status="failed", failure=failure_text)
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
    parser.add_argument(
        "--vtol-hover-mission",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="keep a tailsitter in MC for expert_search; --no-vtol-hover-mission selects FW",
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
        help=(
            "write episode.json snapshots on a background thread; steps and images "
            "remain synchronous"
        ),
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
        default=None,
        help="explicit image; otherwise MULTIDETECT_SIM_IMAGE, then the resolved GPU/CPU default",
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
        help=(
            "also copy episode-retained PX4 .ulg files to the legacy "
            "<out>/jobs/<container>/px4-ulog path"
        ),
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
    sim_climb_timeout_injection = False
    if args.onboard_config is not None:
        try:
            onboard_options = json.loads(args.onboard_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid onboard config: {exc}") from exc
        if not isinstance(onboard_options, dict):
            raise SystemExit("onboard config must be a JSON object")
        sim_climb_timeout_injection = onboard_options.get(
            "sim_climb_timeout_injection", False
        )
        if type(sim_climb_timeout_injection) is not bool:
            raise SystemExit("sim_climb_timeout_injection must be a boolean")
        if sim_climb_timeout_injection and args.teacher != "onboard":
            raise SystemExit("sim_climb_timeout_injection requires --teacher onboard")
        try:
            sim_pre_abort_descent_altitude_m = sim_pre_abort_descent_altitude(
                onboard_options, teacher=args.teacher
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        sim_pre_abort_descent_altitude_m = None
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
        sim_climb_timeout_injection=sim_climb_timeout_injection,
        sim_pre_abort_descent_altitude_m=sim_pre_abort_descent_altitude_m,
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
        vtol_hover_mission=args.vtol_hover_mission,
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
    # Convert TERM/INT into an Exception.  The collector's existing exception
    # cleanup then stops readers, removes its owned container, and the enclosing
    # sitl_lock_context finally releases the global lease.
    signal.signal(signal.SIGTERM, _raise_on_termination)
    signal.signal(signal.SIGINT, _raise_on_termination)
    raise SystemExit(main())
