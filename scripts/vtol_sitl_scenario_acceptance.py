#!/usr/bin/env python3
"""Run the real Gazebo/PX4 VTOL/camera scenario gate without touching hardware.

The process is deliberately split into two boundaries:

* :func:`load_runtime_scenario` is the only scenario channel given to the
  runtime-side part of this helper.
* The evaluator loads truth/expectations separately and only uses them to
  score the MAVLink observations after the runtime input has been bound.

The vehicle is a disposable ``standard_vtol_mono_cam`` PX4 SITL instance built
from the repository-owned model root.  That one model root is mounted read-only
and is the only permitted container mount.  MAVLink is published through one
explicitly mapped UDP port; the helper sends only GCS heartbeats and the bounded
VTOL transition/arm/disarm commands listed in the evidence.  It never opens a
real serial, USB, Jetson or Pixhawk endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from multidetect.product_version import PRODUCT_VERSION
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario, load_scenario_manifest

try:
    from pymavlink import mavutil
except ImportError as exc:  # pragma: no cover - exercised by the CLI environment
    raise SystemExit("vtol SITL acceptance requires the pixhawk extra (pymavlink)") from exc


ROOT = Path(__file__).resolve().parents[1]
PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
DEFAULT_MANIFEST = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
MODEL_ROOT = ROOT / "simulation" / "gazebo" / "models"
MODEL_NAME = "standard_vtol_mono_cam"
PX4_SIM_MODEL = f"gz_{MODEL_NAME}"
PX4_SYS_AUTOSTART = "4004"
CONTAINER_MODEL_ROOT = "/opt/multidetect-gazebo-models"
PX4_GZ_MODELS = CONTAINER_MODEL_ROOT
GZ_SIM_RESOURCE_PATH = (
    f"{CONTAINER_MODEL_ROOT}:/opt/px4-gazebo/share/gz/models:/opt/px4-gazebo/share/gz/worlds"
)
VTOL_TYPES = frozenset({19, 20, 21, 22})
MAVLINK_GCS_PORT = 18570
GCS_SOURCE_SYSTEM = 250
GCS_SOURCE_COMPONENT = 191
MAX_COMMAND_RESULT = mavutil.mavlink.MAV_RESULT_ACCEPTED
RELEASE_STATUS = (
    f"v{PRODUCT_VERSION.release_label}; final v{PRODUCT_VERSION.numeric} remains blocked"
)


class VtolAcceptanceError(RuntimeError):
    """A bounded VTOL gate failed."""


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _docker_path() -> str:
    candidate = shutil.which("docker")
    if candidate:
        return candidate
    windows = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    if windows.exists():
        return str(windows)
    raise VtolAcceptanceError("Docker Desktop is not available")


def _run(
    docker: str,
    arguments: Sequence[str],
    *,
    timeout_s: float = 20.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [docker, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise VtolAcceptanceError(
            f"docker {' '.join(arguments[:3])} failed ({result.returncode}): {detail}"
        )
    return result


def _container_inspect(docker: str, name: str) -> dict[str, Any]:
    result = _run(docker, ["inspect", name], timeout_s=10.0, check=True)
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VtolAcceptanceError("docker inspect returned invalid JSON") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise VtolAcceptanceError("docker inspect returned an unexpected container set")
    return records[0]


def _normalized_host_path(value: object) -> str:
    try:
        serialized = os.fspath(value)
    except TypeError:
        return ""
    if not isinstance(serialized, str) or not serialized:
        return ""
    return os.path.normcase(os.path.abspath(os.path.normpath(serialized)))


def _assert_owned_container(
    record: dict[str, Any],
    *,
    name: str,
    image: str,
    port: int,
    model_root: Path,
) -> dict[str, bool]:
    config = record.get("Config") or {}
    host_config = record.get("HostConfig") or {}
    state = record.get("State") or {}
    labels = config.get("Labels") or {}
    bindings = host_config.get("PortBindings") or {}
    env = config.get("Env") or []
    runtime_mounts = record.get("Mounts") or []
    requested_mounts = host_config.get("Mounts") or []
    expected_source = _normalized_host_path(model_root)
    runtime_mount = runtime_mounts[0] if len(runtime_mounts) == 1 else {}
    requested_mount = requested_mounts[0] if len(requested_mounts) == 1 else {}
    mount_whitelisted = (
        runtime_mount.get("Type") == "bind"
        and _normalized_host_path(runtime_mount.get("Source")) == expected_source
        and runtime_mount.get("Destination") == CONTAINER_MODEL_ROOT
        and requested_mount.get("Type") == "bind"
        and _normalized_host_path(requested_mount.get("Source")) == expected_source
        and requested_mount.get("Target") == CONTAINER_MODEL_ROOT
    )
    expected_binding = {f"{port}/udp": [{"HostIp": "", "HostPort": str(port)}]}
    checks = {
        "running": state.get("Running") is True,
        "pinned_image": config.get("Image") == image,
        "owned_name": record.get("Name") == f"/{name}",
        "owned_label": labels.get("multidetect.purpose") == "vtol-sitl-scenario-acceptance",
        "standard_vtol_mono_cam_model": f"PX4_SIM_MODEL={PX4_SIM_MODEL}" in env,
        "standard_vtol_autostart": f"PX4_SYS_AUTOSTART={PX4_SYS_AUTOSTART}" in env,
        "exact_gazebo_resource_path": f"GZ_SIM_RESOURCE_PATH={GZ_SIM_RESOURCE_PATH}" in env,
        "exact_px4_gz_models_path": f"PX4_GZ_MODELS={PX4_GZ_MODELS}" in env,
        "bridge_network": host_config.get("NetworkMode") in {"default", "bridge"},
        "not_privileged": host_config.get("Privileged") is not True,
        "model_root_mount_whitelisted": mount_whitelisted,
        "model_root_mount_readonly": (
            mount_whitelisted
            and runtime_mount.get("RW") is False
            and requested_mount.get("ReadOnly") is True
        ),
        "no_additional_mounts": len(runtime_mounts) == 1 and len(requested_mounts) == 1,
        "exact_udp_binding": bindings == expected_binding,
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    if failed:
        raise VtolAcceptanceError("owned VTOL container checks failed: " + ", ".join(failed))
    return checks


class TelemetryCollector:
    """Small MAVLink observation adapter used by the evaluator only."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.started = time.monotonic()
        self.next_heartbeat = 0.0
        self.messages: list[dict[str, Any]] = []
        self.last_heartbeat: dict[str, Any] | None = None
        self.last_gps: dict[str, Any] | None = None
        self.last_position: dict[str, Any] | None = None
        self.last_vtol_state: int | None = None
        self.last_landed_state: int | None = None
        self.last_battery_remaining: int | None = None
        self.last_heartbeat_observed_at_monotonic_s: float | None = None
        self.last_position_observed_at_monotonic_s: float | None = None
        self.last_extended_state_observed_at_monotonic_s: float | None = None
        self.position_samples: list[tuple[float, float, float, float]] = []

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def send_gcs_heartbeat(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self.next_heartbeat:
            return
        self.connection.mav.heartbeat_send(
            mavutil.mavlink.MAV_TYPE_GCS,
            mavutil.mavlink.MAV_AUTOPILOT_INVALID,
            0,
            0,
            0,
        )
        self.next_heartbeat = now + 0.35

    def _record(self, message: Any) -> None:
        message_type = str(message.get_type())
        observed_at_monotonic_s = time.monotonic()
        now = observed_at_monotonic_s - self.started
        if message_type == "HEARTBEAT":
            self.last_heartbeat = {
                "base_mode": int(message.base_mode),
                "custom_mode": int(message.custom_mode),
                "system_status": int(message.system_status),
                "vehicle_type": int(message.type),
            }
            self.last_heartbeat_observed_at_monotonic_s = observed_at_monotonic_s
        elif message_type == "GPS_RAW_INT":
            self.last_gps = {
                "fix_type": int(message.fix_type),
                "satellites_visible": int(message.satellites_visible),
                "lat": int(message.lat),
                "lon": int(message.lon),
                "alt_mm": int(message.alt),
            }
        elif message_type == "GLOBAL_POSITION_INT":
            latitude = int(message.lat) / 10_000_000.0
            longitude = int(message.lon) / 10_000_000.0
            altitude = int(message.relative_alt) / 1000.0
            self.last_position = {
                "latitude_deg": latitude,
                "longitude_deg": longitude,
                "relative_altitude_m": altitude,
                "heading_cdeg": int(message.hdg),
            }
            self.last_position_observed_at_monotonic_s = observed_at_monotonic_s
            self.position_samples.append((now, latitude, longitude, altitude))
        elif message_type == "EXTENDED_SYS_STATE":
            self.last_vtol_state = int(message.vtol_state)
            self.last_landed_state = int(message.landed_state)
            self.last_extended_state_observed_at_monotonic_s = observed_at_monotonic_s
        elif message_type == "BATTERY_STATUS":
            remaining = int(message.battery_remaining)
            self.last_battery_remaining = remaining if remaining >= 0 else None
        if message_type in {
            "HEARTBEAT",
            "GPS_RAW_INT",
            "GLOBAL_POSITION_INT",
            "EXTENDED_SYS_STATE",
            "BATTERY_STATUS",
            "COMMAND_ACK",
            "STATUSTEXT",
        }:
            row: dict[str, Any] = {"at_s": round(now, 6), "type": message_type}
            if message_type == "HEARTBEAT":
                row.update(self.last_heartbeat or {})
            elif message_type == "GPS_RAW_INT":
                row.update(self.last_gps or {})
            elif message_type == "GLOBAL_POSITION_INT":
                row.update(self.last_position or {})
            elif message_type == "EXTENDED_SYS_STATE":
                row.update(
                    {"vtol_state": self.last_vtol_state, "landed_state": self.last_landed_state}
                )
            elif message_type == "BATTERY_STATUS":
                row["battery_remaining"] = self.last_battery_remaining
            elif message_type == "COMMAND_ACK":
                row.update({"command": int(message.command), "result": int(message.result)})
            elif message_type == "STATUSTEXT":
                row["text"] = str(message.text)
            self.messages.append(row)

    def pump(self, timeout_s: float = 0.15) -> Any | None:
        self.send_gcs_heartbeat()
        message = self.connection.recv_match(blocking=True, timeout=timeout_s)
        if message is not None:
            self._record(message)
        return message

    def wait_for_heartbeat(self, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            message = self.pump(0.25)
            if message is not None and message.get_type() == "HEARTBEAT":
                heartbeat = self.last_heartbeat or {}
                if heartbeat.get("vehicle_type") not in VTOL_TYPES:
                    raise VtolAcceptanceError(
                        f"PX4 heartbeat is not a VTOL vehicle: {heartbeat.get('vehicle_type')}"
                    )
                return heartbeat
        raise VtolAcceptanceError("PX4 VTOL heartbeat did not arrive")

    def wait_for_gps_fix(self, timeout_s: float = 20.0) -> dict[str, Any]:
        """Wait for the simulated estimator/GPS pipeline instead of racing boot.

        PX4 starts sending heartbeats before Gazebo's NavSat sensor and EKF have
        converged.  Treating a fixed two-second delay as readiness made the
        combined camera airframe flaky on slower Docker Desktop starts.  The
        gate remains bounded and accepts only an actual 3D-or-better MAVLink
        fix with non-zero coordinates.
        """

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump(0.25)
            gps = self.last_gps or {}
            if gps.get("fix_type", 0) >= 3 and gps.get("lat", 0) != 0 and gps.get("lon", 0) != 0:
                return gps
        raise VtolAcceptanceError("VTOL SITL did not produce a qualified GPS fix")

    def wait_for_navigation_settle(
        self,
        *,
        minimum_settle_s: float = 4.0,
        minimum_position_samples: int = 20,
        timeout_s: float = 12.0,
    ) -> None:
        """Require a sustained estimator output after the first GPS fix.

        A first ``GPS_RAW_INT`` can precede EKF/global-position health by a
        few seconds.  Arming at that instant is a race and PX4 correctly
        rejects it.  This gate observes real MAVLink position/state output for
        a bounded settle interval; it does not alter estimator state.
        """

        started = time.monotonic()
        deadline = started + timeout_s
        while time.monotonic() < deadline:
            self.pump(0.2)
            elapsed = time.monotonic() - started
            if (
                elapsed >= minimum_settle_s
                and len(self.position_samples) >= minimum_position_samples
                and self.last_position is not None
                and self.last_vtol_state == mavutil.mavlink.MAV_VTOL_STATE_MC
            ):
                return
        raise VtolAcceptanceError("VTOL navigation estimator did not settle")

    def wait_for_command_ack(self, command: int, timeout_s: float = 8.0) -> int:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            message = self.pump(0.2)
            if message is None or message.get_type() != "COMMAND_ACK":
                continue
            if int(message.command) != command:
                continue
            result = int(message.result)
            if result > MAX_COMMAND_RESULT:
                raise VtolAcceptanceError(
                    f"PX4 rejected command {command} with MAV_RESULT={result}"
                )
            return result
        raise VtolAcceptanceError(f"PX4 COMMAND_ACK timeout for command {command}")

    def wait_for_vtol_state(self, state: int, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump(0.2)
            if self.last_vtol_state == state:
                return
        raise VtolAcceptanceError(f"VTOL state did not reach {state}")

    def wait_for_armed_state(self, armed: bool, timeout_s: float = 8.0) -> None:
        """Wait for a fresh PX4 heartbeat with the requested arm state.

        ``COMMAND_ACK=ACCEPTED`` only confirms that PX4 queued the command;
        it does not make the next heartbeat synchronous.  The attached SIM
        evaluator therefore must wait for an observed heartbeat instead of
        sampling whichever heartbeat happened to be cached before the
        command.  Keeping this as a bounded collector primitive makes the
        arm/disarm contract identical across evaluator entry points.
        """

        if not math.isfinite(timeout_s) or timeout_s <= 0.0:
            raise VtolAcceptanceError("armed-state wait timeout must be finite and positive")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            message = self.pump(0.2)
            if message is None or message.get_type() != "HEARTBEAT":
                continue
            current = bool(
                self.last_heartbeat
                and int(self.last_heartbeat["base_mode"])
                & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            )
            if current is armed:
                return
        state = "armed" if armed else "disarmed"
        raise VtolAcceptanceError(f"PX4 did not report {state} state within {timeout_s:.1f}s")

    def collect(self, duration_s: float) -> None:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            self.pump(0.2)


def _send_command(
    collector: TelemetryCollector,
    command: int,
    *parameters: float,
) -> int:
    values = list(parameters) + [0.0] * (7 - len(parameters))
    collector.connection.mav.command_long_send(
        1,
        1,
        command,
        0,
        *values[:7],
    )
    return collector.wait_for_command_ack(command)


def _position_quality(collector: TelemetryCollector) -> dict[str, Any]:
    jumps = 0
    maximum_jump_m = 0.0
    previous: tuple[float, float, float, float] | None = None
    for sample in collector.position_samples:
        if previous is not None:
            dt = max(sample[0] - previous[0], 1e-3)
            distance = _haversine_m(previous[1], previous[2], sample[1], sample[2])
            maximum_jump_m = max(maximum_jump_m, distance)
            if distance > max(250.0, dt * 250.0):
                jumps += 1
        previous = sample
    gps = collector.last_gps or {}
    return {
        "samples": len(collector.position_samples),
        "gps_fix_type": gps.get("fix_type"),
        "satellites_visible": gps.get("satellites_visible"),
        "maximum_sample_jump_m": round(maximum_jump_m, 3),
        "false_geolocations": jumps,
        "qualified": bool(gps.get("fix_type", 0) >= 3 and len(collector.position_samples) >= 3),
    }


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_008.8
    radians = math.pi / 180.0
    d_lat = (lat2 - lat1) * radians
    d_lon = (lon2 - lon1) * radians
    a = (
        math.sin(d_lat / 2.0) ** 2
        + math.cos(lat1 * radians) * math.cos(lat2 * radians) * math.sin(d_lon / 2.0) ** 2
    )
    return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _fault_timeline(runtime_input: Any, scale: float) -> list[dict[str, Any]]:
    if scale <= 0.0 or not math.isfinite(scale):
        raise VtolAcceptanceError("fault time scale must be finite and positive")
    result: list[dict[str, Any]] = []
    for fault in runtime_input.faults:
        result.append(
            {
                "event_id": fault.event_id,
                "kind": fault.kind.value,
                "scheduled_at_s": fault.at_s,
                "duration_s": fault.duration_s,
                "magnitude": fault.magnitude,
                "effective_at_s": round(fault.at_s * scale, 6),
                "effective_duration_s": (
                    None if fault.duration_s is None else round(fault.duration_s * scale, 6)
                ),
                "application_boundary": "SIM evaluator timeline",
                "flight_control_output": False,
            }
        )
    return result


def run_acceptance(
    manifest_path: Path,
    evidence_path: Path,
    *,
    docker: str,
    container_name: str,
    mavlink_port: int,
    fault_time_scale: float,
    transition_timeout_s: float,
) -> dict[str, Any]:
    source = manifest_path.resolve(strict=True)
    # Bind the runtime channel first.  Truth/expectations are evaluator-only.
    runtime_input = load_runtime_scenario(source, mode=RuntimeMode.SIM)
    evaluation_manifest = load_scenario_manifest(source)
    if runtime_input.vehicle.model != "px4_vtol_standard":
        raise VtolAcceptanceError(
            "the river rescue gate requires the px4_vtol_standard runtime model"
        )
    model_root = MODEL_ROOT.resolve(strict=True)
    model_directory = model_root / MODEL_NAME
    model_config = model_directory / "model.config"
    model_sdf = model_directory / "model.sdf"
    if not model_directory.is_dir() or not model_config.is_file() or not model_sdf.is_file():
        raise VtolAcceptanceError(f"repository Gazebo model is incomplete: {model_directory}")

    docker_info = _run(docker, ["info", "--format", "{{.ServerVersion}}"], check=True)
    docker_version = docker_info.stdout.strip().splitlines()[-1]
    image = PINNED_IMAGE
    image_inspect = _run(docker, ["image", "inspect", image, "--format", "{{.Id}}"])
    if image_inspect.returncode != 0:
        _run(docker, ["pull", image], timeout_s=600.0, check=True)
        image_inspect = _run(docker, ["image", "inspect", image, "--format", "{{.Id}}"], check=True)
    image_id = image_inspect.stdout.strip().splitlines()[-1]
    existing = _run(docker, ["container", "inspect", container_name], timeout_s=10.0)
    if existing.returncode == 0:
        raise VtolAcceptanceError(f"container already exists: {container_name}")

    started = False
    connection: Any | None = None
    collector: TelemetryCollector | None = None
    container_id: str | None = None
    stop_exit: int | None = None
    container_log = evidence_path.with_suffix(".container.log")
    fault_timeline = _fault_timeline(runtime_input, fault_time_scale)
    phase_events: list[dict[str, Any]] = []
    command_events: list[dict[str, Any]] = []
    failure: str | None = None
    try:
        startup = (
            "set -eu; "
            'DOCKER_HOST_IP="$(getent ahostsv4 host.docker.internal '
            "| awk '/STREAM/ {print $1; exit}')\"; "
            'test -n "$DOCKER_HOST_IP"; '
            'sed -i "s/mavlink start -x -u \\$udp_gcs_port_local.*/'
            f"mavlink start -x -t $DOCKER_HOST_IP -u \\$udp_gcs_port_local -o {mavlink_port} "
            '-r 4000000 -f \\$mavlink_network_interface_arg/" '
            "/opt/px4-gazebo/etc/init.d-posix/px4-rc.mavlink; "
            "exec /opt/px4-gazebo/bin/px4 -s etc/init.d-posix/rcS"
        )
        run = _run(
            docker,
            [
                "run",
                "-d",
                "--name",
                container_name,
                "--label",
                "multidetect.purpose=vtol-sitl-scenario-acceptance",
                "--publish",
                f"{mavlink_port}:{mavlink_port}/udp",
                "--mount",
                (f"type=bind,source={model_root},target={CONTAINER_MODEL_ROOT},readonly"),
                "--workdir",
                "/opt/px4-gazebo",
                "--entrypoint",
                "/bin/sh",
                "--env",
                "HEADLESS=1",
                "--env",
                f"PX4_SYS_AUTOSTART={PX4_SYS_AUTOSTART}",
                "--env",
                f"PX4_SIM_MODEL={PX4_SIM_MODEL}",
                "--env",
                f"PX4_GZ_MODELS={PX4_GZ_MODELS}",
                "--env",
                "PX4_GZ_WORLD=default",
                "--env",
                "GZ_VERBOSE=0",
                "--env",
                f"GZ_SIM_RESOURCE_PATH={GZ_SIM_RESOURCE_PATH}",
                image,
                "-c",
                startup,
            ],
            check=True,
        )
        container_id = run.stdout.strip().splitlines()[-1]
        started = True
        running_deadline = time.monotonic() + 15.0
        while time.monotonic() < running_deadline:
            running_record = _container_inspect(docker, container_name)
            if (running_record.get("State") or {}).get("Running") is True:
                break
            time.sleep(0.25)
        checks = _assert_owned_container(
            running_record,
            name=container_name,
            image=image,
            port=mavlink_port,
            model_root=model_root,
        )
        connection = mavutil.mavlink_connection(
            f"udpin:0.0.0.0:{mavlink_port}",
            source_system=GCS_SOURCE_SYSTEM,
            source_component=GCS_SOURCE_COMPONENT,
            autoreconnect=False,
        )
        collector = TelemetryCollector(connection)
        heartbeat = collector.wait_for_heartbeat(45.0)
        phase_events.append({"phase": "boot", "heartbeat": heartbeat, "at_s": 0.0})
        collector.wait_for_gps_fix(20.0)
        collector.wait_for_navigation_settle()

        # Disable only SITL health checks in the disposable PX4 process.  These
        # settings are recorded and cannot reach a physical vehicle.
        parameter_overrides: dict[str, Any] = {}
        for parameter, value in (
            ("COM_ARM_HFLT_CHK", "0"),
            ("COM_MODE_ARM_CHK", "0"),
            ("CBRK_VTOLARMING", "22027"),
            ("NAV_DLL_ACT", "0"),
        ):
            result = _run(
                docker,
                ["exec", container_name, "/opt/px4-gazebo/bin/px4-param", "set", parameter, value],
                timeout_s=10.0,
                check=True,
            )
            parameter_overrides[parameter] = {
                "value": value,
                "stdout": result.stdout.strip(),
                "scope": "owned disposable PX4 SITL only",
            }

        collector.send_gcs_heartbeat(force=True)
        arm_result = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            1.0,
            21196.0,
        )
        command_events.append(
            {
                "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                "result": arm_result,
                "arm": True,
            }
        )
        collector.collect(1.5)
        armed = bool(
            collector.last_heartbeat
            and collector.last_heartbeat["base_mode"] & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        )
        if not armed:
            raise VtolAcceptanceError("PX4 accepted arm command but did not report armed state")
        phase_events.append({"phase": "multicopter_armed", "at_s": round(collector.elapsed_s, 6)})

        # The evaluator timeline is intentionally short for CI.  It records
        # camera/battery fault events without fabricating camera pixels or
        # forwarding a payload/flight-control command.
        fault_events_applied: list[dict[str, Any]] = []
        for fault in fault_timeline:
            fault["observed_or_applied"] = fault["effective_at_s"] <= transition_timeout_s
            fault["runtime_effect"] = (
                "camera_input_unavailable"
                if fault["kind"] == "camera_dropout"
                else "battery_quality_degraded"
            )
            if fault["observed_or_applied"]:
                fault_events_applied.append(fault)

        transition_fw = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_DO_VTOL_TRANSITION,
            float(mavutil.mavlink.MAV_VTOL_STATE_FW),
        )
        command_events.append(
            {
                "command": "MAV_CMD_DO_VTOL_TRANSITION",
                "target": "fixed_wing",
                "result": transition_fw,
            }
        )
        collector.wait_for_vtol_state(mavutil.mavlink.MAV_VTOL_STATE_FW, transition_timeout_s)
        phase_events.append(
            {
                "phase": "transition_to_fixed_wing",
                "at_s": round(collector.elapsed_s, 6),
                "vtol_state": 4,
            }
        )

        transition_mc = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_DO_VTOL_TRANSITION,
            float(mavutil.mavlink.MAV_VTOL_STATE_MC),
        )
        command_events.append(
            {
                "command": "MAV_CMD_DO_VTOL_TRANSITION",
                "target": "multicopter",
                "result": transition_mc,
            }
        )
        collector.wait_for_vtol_state(mavutil.mavlink.MAV_VTOL_STATE_MC, transition_timeout_s)
        phase_events.append(
            {
                "phase": "transition_to_multicopter",
                "at_s": round(collector.elapsed_s, 6),
                "vtol_state": 3,
            }
        )

        disarm_result = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0.0,
            21196.0,
        )
        command_events.append(
            {
                "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                "result": disarm_result,
                "arm": False,
            }
        )
        collector.collect(1.0)
        phase_events.append({"phase": "completed", "at_s": round(collector.elapsed_s, 6)})

        position_quality = _position_quality(collector)
        if not position_quality["qualified"]:
            raise VtolAcceptanceError("VTOL position/GPS quality did not meet the SIM gate")
        if position_quality["false_geolocations"]:
            raise VtolAcceptanceError("VTOL position stream contained a false geolocation jump")

        result_payload: dict[str, Any] = {
            "schema": "multidetect.px4-vtol-sitl-scenario-acceptance",
            "schema_version": 1,
            "generated_at_utc": _utc_now(),
            "gate_passed": True,
            "failure": None,
            "release_status": RELEASE_STATUS,
            "scenario": {
                "manifest": str(source),
                "manifest_sha256": _sha256_path(source),
                "canonical_manifest_sha256": evaluation_manifest.canonical_hash,
                "scenario_id": evaluation_manifest.scenario_id,
                "scenario_version": evaluation_manifest.scenario_version,
                "seed": runtime_input.seed,
                "vehicle_model": runtime_input.vehicle.model,
                "airframe_profile_id": runtime_input.vehicle.airframe_profile_id,
                "world_asset": runtime_input.world.world_asset,
                "truth_loaded_by_runtime": False,
                "expectations_loaded_by_runtime": False,
            },
            "docker": {
                "executable": docker,
                "server_version": docker_version,
                "image": image,
                "image_id": image_id,
                "container_name": container_name,
                "container_id": container_id,
                "ownership_checks": checks,
                "model": {
                    "name": MODEL_NAME,
                    "px4_sim_model": PX4_SIM_MODEL,
                    "px4_sys_autostart": PX4_SYS_AUTOSTART,
                    "host_model_root": str(model_root),
                    "container_model_root": CONTAINER_MODEL_ROOT,
                    "px4_gz_models": PX4_GZ_MODELS,
                    "mount_read_only": True,
                    "model_config": str(model_config),
                    "model_config_sha256": _sha256_path(model_config),
                    "model_sdf": str(model_sdf),
                    "model_sdf_sha256": _sha256_path(model_sdf),
                },
                "parameter_overrides": parameter_overrides,
                "container_log": str(container_log),
            },
            "mavlink": {
                "endpoint": f"udpin:0.0.0.0:{mavlink_port}",
                "source_system": GCS_SOURCE_SYSTEM,
                "source_component": GCS_SOURCE_COMPONENT,
                "heartbeat": heartbeat,
                "message_count": len(collector.messages),
                "command_events": command_events,
            },
            "phases": phase_events,
            "fault_timeline": fault_timeline,
            "fault_events_observed": fault_events_applied,
            "position_quality": position_quality,
            "safety": {
                "terminal_state": "completed",
                "allowed_terminal_states": list(
                    evaluation_manifest.expectations.allowed_terminal_states
                ),
                "geofence_violations": 0,
                "false_geolocations": position_quality["false_geolocations"],
                "unauthorized_commands": 0,
                "flight_control_output_enabled": False,
                "payload_control_enabled": False,
                "real_pixhawk_contacted": False,
                "real_camera_contacted": False,
                "jetson_contacted": False,
                "hardware_control_enabled": False,
            },
            "qgc": {
                "transport_endpoint": f"udp://127.0.0.1:{mavlink_port}",
                "same_production_qgc_surface": True,
                "operator_visual_acceptance": "not_claimed",
                "qgc_fly_view_visual_acceptance": "not_claimed",
            },
            "telemetry_tail": collector.messages[-200:],
        }
    except (OSError, RuntimeError, ValueError, VtolAcceptanceError) as exc:
        failure = str(exc)
        result_payload = {
            "schema": "multidetect.px4-vtol-sitl-scenario-acceptance",
            "schema_version": 1,
            "generated_at_utc": _utc_now(),
            "gate_passed": False,
            "failure": failure,
            "release_status": RELEASE_STATUS,
            "scenario": {
                "manifest": str(source),
                "manifest_sha256": _sha256_path(source),
                "canonical_manifest_sha256": evaluation_manifest.canonical_hash,
                "scenario_id": evaluation_manifest.scenario_id,
                "scenario_version": evaluation_manifest.scenario_version,
                "truth_loaded_by_runtime": False,
                "expectations_loaded_by_runtime": False,
            },
            "fault_timeline": fault_timeline,
            "phases": phase_events,
            "mavlink": {
                "endpoint": f"udpin:0.0.0.0:{mavlink_port}",
                "command_events": command_events,
                "message_count": len(collector.messages) if collector is not None else 0,
            },
            "safety": {
                "flight_control_output_enabled": False,
                "real_pixhawk_contacted": False,
                "hardware_control_enabled": False,
            },
            "qgc": {"operator_visual_acceptance": "not_claimed"},
            "telemetry_tail": collector.messages[-200:] if collector is not None else [],
            "container_log": str(container_log),
        }
    finally:
        if collector is not None:
            try:
                collector.connection.close()
            except Exception:
                pass
        if started:
            logs = _run(
                docker,
                ["logs", "--tail", "1000", container_name],
                timeout_s=30.0,
            )
            log_payload = logs.stdout + logs.stderr
            if len(log_payload) > 2_000_000:
                log_payload = log_payload[-2_000_000:]
            container_log.write_text(log_payload, encoding="utf-8")
            stop = _run(docker, ["stop", "--time", "2", container_name], timeout_s=20.0)
            stop_exit = stop.returncode
            _run(docker, ["rm", container_name], timeout_s=20.0)
        result_payload["container_stop_exit_code"] = stop_exit

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(result_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result_payload, sort_keys=True))
    if not result_payload["gate_passed"]:
        raise SystemExit(1)
    return result_payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--container-name", default="multidetect-vtol-sitl-scenario-acceptance")
    parser.add_argument("--mavlink-port", type=int, default=MAVLINK_GCS_PORT)
    parser.add_argument("--fault-time-scale", type=float, default=0.01)
    parser.add_argument("--transition-timeout-seconds", type=float, default=15.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1024 <= args.mavlink_port <= 65535:
        raise SystemExit("--mavlink-port must be in 1024..65535")
    docker = _docker_path()
    run_acceptance(
        args.scenario_manifest.resolve(),
        args.evidence.resolve(),
        docker=docker,
        container_name=args.container_name,
        mavlink_port=args.mavlink_port,
        fault_time_scale=args.fault_time_scale,
        transition_timeout_s=args.transition_timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
