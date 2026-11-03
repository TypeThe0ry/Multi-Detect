#!/usr/bin/env python3
"""Plan a pinned Docker PX4/Gazebo SIM run without starting Docker.

The repository already contains a full VTOL acceptance script which executes
Docker.  This companion is intentionally the *planning* boundary: it resolves
the same repository-owned scenario assets, renders a copy/pasteable
``docker run`` command, and records a cleanup command.  It never invokes the
Docker CLI, opens a socket, or creates a container.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.simulator.cpu import SimulatorCpuIsolation, resolve_cpu_isolation
from multidetect.simulator.stack import (
    SimulatorStackError,
    SimulatorStackPorts,
    build_stack_plan,
)
from multidetect.sim_network import sim_docker_network, sim_listen_host, sim_network_mode

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
CONTAINER_MODEL_ROOT = "/opt/multidetect-gazebo-models"
CONTAINER_WORLD_ROOT = "/opt/px4-gazebo/share/gz/worlds"
CONTAINER_WORLD = f"{CONTAINER_WORLD_ROOT}/default.sdf"
CONTAINER_IMAGE_MODEL_ROOT = "/opt/px4-gazebo/share/gz/models"
CONTAINER_IMAGE_WORLD_ROOT = CONTAINER_WORLD_ROOT
CONTAINER_SCRIPT_ROOT = "/workspace/scripts"
DEFAULT_GPU_DEVICES = ("1", "2")
DEFAULT_GPU_AVOID = ("0", "3")
NVIDIA_EGL_VENDOR_FILE = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
# SIM-only PX4 parameters, applied by rcS from PX4_PARAM_* before
# px4-rc.gzsim starts the simulated sensors.  Without them EKF2's attitude
# drifted from Gazebo's ground truth in banked flight (fixed-wing loiters:
# yaw +3..7 deg, pitch/roll biases absorbed into accel bias):
#
# * The pinned image's Gazebo Harmonic magnetometer applies the vehicle's tilt
#   about the wrong axes (roll and pitch exchanged, i.e. the attitude
#   conjugated by a 90 deg yaw; see PX4's model.sdf note on gz-sim PR #2460).
#   Level it is right; at 15-40 deg of tilt it is 20-40 deg off, and EKF2's
#   3D mag fusion absorbs that into its earth-field and mag-bias states.
#   PX4's own SIM magnetometer (``sensor_mag_sim``: WMM at the ground-truth
#   position rotated by the ground-truth attitude) replaces it.  Both report
#   device id 197388, so the Gazebo one cannot be disabled by calibration
#   priority; Gazebo instead starts from a copy of the image's server.config
#   without the magnetometer system, so the model's sensor never publishes.
# * Gazebo's GNSS and PX4's SIM airspeed have no latency, but the defaults tell
#   EKF2 to expect 110 ms (GNSS) and 100 ms (airspeed).  In a 20+ deg/s turn
#   that mismatch rotates the velocity innovations by a few degrees, which
#   EKF2 turns into a yaw error that outlives the turn.  Real airframes keep
#   their measured delays; these values only describe the simulator.
SIM_SENSOR_PARAMETERS = (
    ("SENS_EN_MAGSIM", "1"),
    ("SENS_GPS0_DELAY", "0"),
    ("EKF2_ASP_DELAY", "0"),
)
IMAGE_SERVER_CONFIG = "/opt/px4-gazebo/share/gz/server.config"
CONTAINER_SERVER_CONFIG = "/tmp/multidetect-gz-server.config"
_DROP_GAZEBO_MAGNETOMETER = (
    f"sed '/gz-sim-magnetometer-system/d' {IMAGE_SERVER_CONFIG} > {CONTAINER_SERVER_CONFIG}; "
    f"grep -q gz-sim-sensors-system {CONTAINER_SERVER_CONFIG} || exit 71; "
    f"if grep -q gz-sim-magnetometer-system {CONTAINER_SERVER_CONFIG}; then exit 70; fi; "
    f"export GZ_SIM_SERVER_CONFIG_PATH={CONTAINER_SERVER_CONFIG}; "
)
# PX4's px4-rc.gzsim gives Gazebo 30 one-second attempts to load the world.  On
# a loaded host the generated worlds took longer and PX4 exited before sending
# a heartbeat ("Timed out waiting for Gazebo world", six launches on
# 2026-10-19/20).  Allow 180 attempts; fail loudly if the script changed.
PX4_GZSIM_RC = "/opt/px4-gazebo/etc/init.d-posix/px4-rc.gzsim"
GAZEBO_WORLD_ATTEMPTS = 180
_WAIT_LONGER_FOR_GAZEBO = (
    f"sed -i 's/^ATTEMPTS=30$/ATTEMPTS={GAZEBO_WORLD_ATTEMPTS}/' {PX4_GZSIM_RC}; "
    f"grep -q '^ATTEMPTS={GAZEBO_WORLD_ATTEMPTS}$' {PX4_GZSIM_RC} || exit 72; "
)
# A world that declares any <plugin> replaces Gazebo's default systems (physics,
# scene broadcaster, sensors), so the world never becomes ready.  Wind variants
# therefore carry only <wind> plus a gust comment, and the WindEffects system is
# added to the patched server config only when the world carries that comment
# (written by scripts/augment_sim_world.py, also without a gust).  Hand-made
# worlds that merely declare <wind> keep their default dynamics.  Parameters
# follow the gz-sim 8 WindEffects schema (magnitude rise/sin/noise, direction).
# With scaling 1 and a 5 s rise, 6 m/s slid the parked x500 (EKF "no heading
# reference") and flipped it at arming; 0.3 and a 60 s rise let it take off first.
_GUST_COMMENT = "multidetect-wind-gust"
_WIND_EFFECTS_PLUGIN = (
    '<plugin entity_name="*" entity_type="world" filename="gz-sim-wind-effects-system"'
    ' name="gz::sim::systems::WindEffects">'
    "<force_approximation_scaling_factor>0.3</force_approximation_scaling_factor>"
    "<horizontal><magnitude><time_for_rise>60</time_for_rise>"
    "<sin><amplitude_percent>GUST_AMP</amplitude_percent><period>GUST_PERIOD</period></sin>"
    '<noise type="gaussian"><mean>0</mean><stddev>0.0002</stddev></noise></magnitude>'
    "<direction><time_for_rise>30</time_for_rise>"
    "<sin><amplitude>5</amplitude><period>20</period></sin>"
    '<noise type="gaussian"><mean>0</mean><stddev>0.03</stddev></noise></direction>'
    "</horizontal>"
    '<vertical><noise type="gaussian"><mean>0</mean><stddev>0.03</stddev></noise></vertical>'
    "</plugin>"
)
_ENABLE_WIND_EFFECTS = (
    f"if grep -q '{_GUST_COMMENT} amplitude_percent=' {CONTAINER_WORLD}; then "
    "GUST=$(sed -n "
    + shlex.quote(
        rf"s/.*{_GUST_COMMENT} amplitude_percent=\([0-9.]*\) period=\([0-9.]*\).*/\1 \2/p"
    )
    + f" {CONTAINER_WORLD} | head -n 1); "
    "GUST_AMP=${GUST% *}; GUST_PERIOD=${GUST#* }; "
    'test -n "$GUST" || { GUST_AMP=0; GUST_PERIOD=10; }; '
    "sed -i "
    + shlex.quote("s#</plugins>#" + _WIND_EFFECTS_PLUGIN + "</plugins>#")
    + f" {CONTAINER_SERVER_CONFIG}; "
    f'sed -i "s/GUST_AMP/$GUST_AMP/; s/GUST_PERIOD/$GUST_PERIOD/" {CONTAINER_SERVER_CONFIG}; '
    f"grep -q gz-sim-wind-effects-system {CONTAINER_SERVER_CONFIG} || exit 73; "
    "fi; "
)
# Shell prefix every PX4/Gazebo SITL container runs before PX4 starts.
SIM_STARTUP_PATCHES = _DROP_GAZEBO_MAGNETOMETER + _ENABLE_WIND_EFFECTS + _WAIT_LONGER_FOR_GAZEBO


class DockerSimulatorPlanError(RuntimeError):
    """Raised when a Docker SIM command cannot satisfy the isolation contract."""


SimulatorDockerPlanError = DockerSimulatorPlanError


def _path(value: str | Path, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Check the user-supplied path before resolving it.  Resolving first would
    # erase the final symlink and make a dangling link indistinguishable from
    # an ordinary path in the isolation gate.
    if candidate.is_symlink():
        raise DockerSimulatorPlanError(f"{label} must not be a symlink: {candidate}")
    return candidate.resolve(strict=False)


def _port(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise DockerSimulatorPlanError(f"{label} must be an integer in 1024..65535")
    if value == 14550:
        raise DockerSimulatorPlanError("UDP 14550 is protected and cannot be used by SIM")
    return value


def _mount(source: Path, target: str) -> str:
    if not source.is_file() and not source.is_dir():
        raise DockerSimulatorPlanError(f"Docker mount source does not exist: {source}")
    return f"type=bind,source={source},target={target},readonly"


def _docker_binary() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    if sys.platform == "win32":
        candidate = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
        return str(candidate) if candidate.is_file() else None
    return None


def _command_has_option(command: tuple[str, ...], option: str, value: str) -> bool:
    """Return whether *command* contains one exact Docker option/value pair."""

    return any(
        token == option and index + 1 < len(command) and command[index + 1] == value
        for index, token in enumerate(command)
    )


def _split_gpu_list(value: str | None, *, label: str) -> tuple[str, ...]:
    """Parse a comma-separated list of non-negative CUDA device ordinals."""

    if value is None or not str(value).strip():
        return ()
    result: list[str] = []
    for raw in str(value).split(","):
        item = raw.strip()
        if not item.isdigit():
            raise DockerSimulatorPlanError(f"{label} must contain only GPU numbers")
        normalized = str(int(item))
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _resolve_gpu_selection(
    *,
    gpu_render: bool | None,
    gpu_device: str | int | None,
    gpu_devices: tuple[str, ...] | None,
    worker_index: int,
) -> tuple[bool, str | None, tuple[str, ...]]:
    """Resolve explicit options and environment defaults for one worker."""

    env_selector = os.environ.get("MULTIDETECT_SIM_GPU", "").strip()
    env_devices = os.environ.get("MULTIDETECT_SIM_GPU_DEVICES")
    if gpu_render is None:
        if not env_selector:
            enabled = False
        elif env_selector.lower() in {"0", "false", "no", "off"}:
            enabled = False
        else:
            enabled = True
    else:
        enabled = bool(gpu_render)
    explicit_device = None if gpu_device is None else str(gpu_device).strip()
    if explicit_device:
        if not explicit_device.isdigit():
            raise DockerSimulatorPlanError("gpu_device must be a non-negative GPU number")
        enabled = True
    elif (
        gpu_render in (None, True)
        and env_selector.isdigit()
        and gpu_devices is None
        and not env_devices
    ):
        explicit_device = str(int(env_selector))
        enabled = True
    if isinstance(worker_index, bool) or not isinstance(worker_index, int) or worker_index < 0:
        raise DockerSimulatorPlanError("worker_index must be a non-negative integer")
    if gpu_devices is None:
        configured = _split_gpu_list(
            os.environ.get("MULTIDETECT_SIM_GPU_DEVICES"),
            label="MULTIDETECT_SIM_GPU_DEVICES",
        )
        if not configured:
            configured = DEFAULT_GPU_DEVICES
    else:
        configured = tuple(str(item) for item in gpu_devices)
    configured = _split_gpu_list(",".join(configured), label="gpu_devices")
    avoid = _split_gpu_list(
        os.environ.get("MULTIDETECT_SIM_GPU_AVOID", ",".join(DEFAULT_GPU_AVOID)),
        label="MULTIDETECT_SIM_GPU_AVOID",
    )
    if not configured:
        raise DockerSimulatorPlanError("gpu_devices must contain at least one GPU")
    if explicit_device is None:
        candidates = tuple(device for device in configured if device not in avoid)
        if not candidates:
            raise DockerSimulatorPlanError("gpu_devices is empty after applying gpu avoid list")
        explicit_device = candidates[worker_index % len(candidates)]
    return enabled, explicit_device if enabled else None, configured


def gpu_assignment_for_worker(worker_index: int) -> str | None:
    """Return the configured GPU for a worker slot, or ``None`` if disabled."""

    enabled, device, _devices = _resolve_gpu_selection(
        gpu_render=None,
        gpu_device=None,
        gpu_devices=None,
        worker_index=worker_index,
    )
    return device if enabled else None


@dataclass(frozen=True, slots=True)
class DockerSimulatorPlan:
    """Hash-bound Docker command and cleanup evidence; no process is started."""

    scenario_id: str
    scenario_manifest_hash: str
    stack_plan_sha256: str
    image: str
    container_name: str
    repository_root: Path
    model_root: Path
    world_path: Path
    scripts_root: Path
    dem_path: Path
    terrain_dem_sha256: str
    host_control_port: int
    host_telemetry_port: int
    command: tuple[str, ...]
    cleanup_commands: tuple[tuple[str, ...], ...]
    docker_executable: str | None
    dry_run: bool = True
    # The fields below make the container-side vehicle/asset contract
    # explicit.  They are appended so callers using the original planner
    # constructor remain source-compatible.
    vehicle_model: str = ""
    model_name: str = ""
    airframe_profile_id: str = ""
    environment: tuple[tuple[str, str], ...] = ()
    container_network_mode: str = "bridge"
    # Retain the resolved asset bundle for the acceptance companion.  This
    # field is intentionally outside the canonical parity source list; it is
    # a planner-to-capture compatibility bridge, not a second hash contract.
    assets: object | None = None
    # Explicit provenance hashes keep the acceptance companion independent of
    # the internal StackAssets object while preserving the exact asset bundle
    # resolved by build_stack_plan.
    world_sdf_sha256: str = ""
    vehicle_model_sha256: str = ""
    vehicle_config_sha256: str = ""
    stack_assets_sha256: str = ""
    model_root_assets_sha256: str = ""
    # Resolved Fuel model cache bind-mounted read-only, or None when absent.
    fuel_resolved_root: Path | None = None
    gpu_render: bool = False
    gpu_device: str | None = None
    gpu_devices: tuple[str, ...] = ()
    worker_index: int = 0
    cpu_isolation: SimulatorCpuIsolation | None = None

    @property
    def command_text(self) -> str:
        # The generated argv is shell-safe for the repository's ordinary
        # Windows paths; JSON evidence remains the canonical machine form.
        return " ".join(_quote(token) for token in self.command)

    @property
    def cap_drop_all(self) -> bool:
        return _command_has_option(self.command, "--cap-drop", "ALL")

    @property
    def no_new_privileges(self) -> bool:
        return _command_has_option(
            self.command,
            "--security-opt",
            "no-new-privileges:true",
        )

    @property
    def container_security_gate_passed(self) -> bool:
        return self.cap_drop_all and self.no_new_privileges

    @property
    def evidence_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        cap_drop_all = self.cap_drop_all
        no_new_privileges = self.no_new_privileges
        container_security_gate_passed = self.container_security_gate_passed
        document = {
            "schema_version": 1,
            "event": "simulator_docker_plan",
            "scenario_id": self.scenario_id,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "stack_plan_sha256": self.stack_plan_sha256,
            "image": self.image,
            "container_name": self.container_name,
            "repository_root": str(self.repository_root),
            "mounts": {
                "model_root": {
                    "host": str(self.model_root),
                    "container": CONTAINER_MODEL_ROOT,
                    "readonly": True,
                },
                "world": {
                    "host": str(self.world_path),
                    "container": CONTAINER_WORLD,
                    "host_root": str(self.world_path.parent),
                    "container_root": CONTAINER_WORLD_ROOT,
                    "readonly": True,
                },
                "scripts": {
                    "host": str(self.scripts_root),
                    "container": CONTAINER_SCRIPT_ROOT,
                    "readonly": True,
                },
            },
            "host_inputs": {
                "terrain_dem": {
                    "host": str(self.dem_path),
                    "sha256": self.terrain_dem_sha256,
                    "readonly": True,
                    "mounted_in_container": False,
                    "consumer": "gazebo_sensor_telemetry_provider",
                },
            },
            "vehicle": {
                "model": self.vehicle_model,
                "model_name": self.model_name,
                "airframe_profile_id": self.airframe_profile_id,
            },
            "environment": dict(self.environment),
            "gpu_render": self.gpu_render,
            "gpu_device": self.gpu_device,
            "gpu_devices": list(self.gpu_devices),
            "worker_index": self.worker_index,
            **({"cpu_isolation": self.cpu_isolation.to_mapping()} if self.cpu_isolation else {}),
            "container_network_mode": self.container_network_mode,
            "container_security": {
                "cap_drop_all": cap_drop_all,
                "no_new_privileges": no_new_privileges,
                "gate_passed": container_security_gate_passed,
            },
            "ports": {
                "control": {
                    "host": self.host_control_port,
                    "container": self.host_control_port,
                    "protocol": "udp",
                    "host_ip": "127.0.0.1",
                },
                "telemetry": {
                    "host": self.host_telemetry_port,
                    # Telemetry is emitted by PX4 towards the host target.  It
                    # is deliberately not a published container port: Docker
                    # Desktop's UDP proxy can hairpin a same-port publication
                    # back into the container and drop the host-bound stream.
                    "container": self.host_telemetry_port,
                    "protocol": "udp",
                    "host_ip": "127.0.0.1",
                    "direction": "container_to_host",
                    "publication": "outbound_only",
                    "published": False,
                },
                "protected_udp_14550_used": False,
                # ``loopback_only`` refers to the host-published boundary.
                # Docker Desktop necessarily uses a bridge-side
                # host.docker.internal target for container -> host telemetry;
                # that distinction is recorded instead of silently calling
                # the bridge address a literal loopback endpoint.
                "loopback_only": True,
                "host_loopback_only": True,
                "container_target": "host.docker.internal",
                "container_target_is_loopback": False,
            },
            "command": list(self.command),
            "command_text": self.command_text,
            "cleanup_commands": [list(item) for item in self.cleanup_commands],
            "docker_executable": self.docker_executable,
            "dry_run": self.dry_run,
            "external_processes_started": False,
            "external_io_opened": False,
            "truth_free": True,
            "validation": {
                "gate_passed": container_security_gate_passed,
                "image_pinned": _is_pinned_image(self.image),
                "mounts_readonly": True,
                "terrain_dem_hash_bound": True,
                "ports_loopback_only": True,
                "telemetry_port_published": False,
                "container_bridge_target_explicit": True,
                "protected_port_untouched": True,
                "cleanup_present": bool(self.cleanup_commands),
                "docker_invoked": False,
                "cap_drop_all": cap_drop_all,
                "no_new_privileges": no_new_privileges,
                "container_security_gate_passed": container_security_gate_passed,
            },
        }
        if self.container_network_mode == "host":
            ports = document["ports"]
            ports["control"]["published"] = False
            ports["telemetry"]["publication"] = "host_network"
            ports["container_target"] = "127.0.0.1"
            ports["container_target_is_loopback"] = True
            document["validation"]["container_bridge_target_explicit"] = False
        return document

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.evidence_hash
        return result

    @property
    def evidence(self) -> dict[str, object]:
        return self.to_mapping_with_hash()

    def write_json(self, path: str | Path) -> Path:
        target = _path(path, label="evidence path")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_mapping_with_hash(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


def _is_pinned_image(image: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}", image))


def _is_local_gpu_image(image: str) -> bool:
    return bool(re.fullmatch(r"multidetect/px4-sitl-gazebo:[A-Za-z0-9_.-]+", image))


def _quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@=+,-]+", value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def build_docker_simulator_plan(
    scenario: str | Path = DEFAULT_SCENARIO,
    *,
    repository_root: str | Path | None = None,
    job_dir: str | Path | None = None,
    container_name: str = "multidetect-sim",
    control_port: int = 14540,
    telemetry_port: int = 14541,
    image: str = PINNED_IMAGE,
    dry_run: bool = True,
    gpu_render: bool | None = None,
    gpu_device: str | int | None = None,
    gpu_devices: tuple[str, ...] | None = None,
    worker_index: int = 0,
    sim_network: str | None = None,
    sim_instance: int = 0,
) -> DockerSimulatorPlan:
    """Resolve assets and render an isolated Docker command without running it."""

    try:
        cpu_isolation = resolve_cpu_isolation(worker_index)
    except ValueError as exc:
        raise DockerSimulatorPlanError(str(exc)) from exc
    gpu_render, selected_gpu, resolved_gpu_devices = _resolve_gpu_selection(
        gpu_render=gpu_render,
        gpu_device=gpu_device,
        gpu_devices=gpu_devices,
        worker_index=worker_index,
    )
    if not _is_pinned_image(image) and not (gpu_render and _is_local_gpu_image(image)):
        raise DockerSimulatorPlanError(
            "image must be pinned by a 64-hex sha256 digest, or a local GPU variant "
            "multidetect/px4-sitl-gazebo:<tag>"
        )
    if not isinstance(container_name, str) or _SAFE_NAME.fullmatch(container_name) is None:
        raise DockerSimulatorPlanError("container_name contains unsupported characters")
    control_port = _port(control_port, label="control_port")
    telemetry_port = _port(telemetry_port, label="telemetry_port")
    if control_port == telemetry_port:
        raise DockerSimulatorPlanError("control and telemetry ports must differ")
    root = _path(repository_root or ROOT, label="repository_root")
    selected_network_mode = sim_network_mode(sim_network)
    if isinstance(sim_instance, bool) or not isinstance(sim_instance, int) or sim_instance < 0:
        raise DockerSimulatorPlanError("sim_instance must be a non-negative integer")
    container_network = "host" if selected_network_mode == "host" else sim_docker_network()
    output_job = _path(
        job_dir or root / "artifacts" / "evaluation" / f"docker-{container_name}",
        label="job_dir",
    )
    output_job.mkdir(parents=True, exist_ok=True)
    ports = SimulatorStackPorts(
        px4_control_port=control_port,
        px4_telemetry_port=telemetry_port,
    )
    try:
        stack = build_stack_plan(
            scenario,
            repository_root=root,
            ports=ports,
            tool_capabilities={"px4": False, "gazebo": False},
        )
    except (SimulatorStackError, OSError, TypeError, ValueError) as exc:
        raise DockerSimulatorPlanError(f"invalid SIM stack: {exc}") from exc
    model_root = stack.assets.model_path.parent.parent.resolve()
    resolved_root = (root / "artifacts" / "cache" / "gz-fuel" / "resolved").resolve()
    has_resolved_root = resolved_root.is_dir()
    world_path = stack.world_path.resolve()
    scripts_root = (root / "scripts").resolve()
    dem_path = stack.dem_path.resolve()
    if not (scripts_root / "gazebo_sensor_stream.py").is_file():
        raise DockerSimulatorPlanError(
            "repository scripts mount is missing gazebo_sensor_stream.py"
        )
    # ``stack`` already validated the vehicle/profile pair and supplied the
    # exact environment used by the native planner.  Convert host paths to
    # container paths while preserving scenario/seed identity and the same
    # PX4/Gazebo model selection.
    stack_environment = stack.environment_mapping
    model_name = stack.model_name
    container_environment = {
        "HEADLESS": "1" if stack.headless else "0",
        "PX4_SYS_AUTOSTART": stack_environment.get(
            "PX4_SYS_AUTOSTART",
            (
                "4004"
                if stack.vehicle_class == "vtol"
                else "4001"
                if stack.vehicle_class == "multirotor"
                else "4003"
            ),
        ),
        "PX4_SIM_MODEL": stack_environment.get("PX4_SIM_MODEL", f"gz_{model_name}"),
        "PX4_GZ_WORLD": "default",
        "PX4_GZ_MODELS": CONTAINER_MODEL_ROOT,
        "PX4_GZ_WORLDS": CONTAINER_WORLD_ROOT,
        "GZ_SIM_RESOURCE_PATH": ":".join(
            value
            for value in (
                "/opt/multidetect-gz-fuel-resolved" if has_resolved_root else "",
                CONTAINER_MODEL_ROOT,
                CONTAINER_WORLD_ROOT,
                CONTAINER_IMAGE_MODEL_ROOT,
                CONTAINER_IMAGE_WORLD_ROOT,
            )
            if value
        ),
        "PX4_SIM_HOST": "host.docker.internal",
        "PX4_MAVLINK_UDP_PORT": str(control_port),
        "PX4_MAVLINK_TELEMETRY_PORT": str(telemetry_port),
        "MD_SIM_SCENARIO_ID": stack.scenario_id,
        "MD_SIM_SCENARIO_SHA256": stack.scenario_manifest_hash,
        "MD_SIM_SEED": str(stack.seed),
        "GZ_VERBOSE": "0",
        **{f"PX4_PARAM_{name}": value for name, value in SIM_SENSOR_PARAMETERS},
    }
    if gpu_render:
        # NVIDIA Container Toolkit injects the driver libraries.  EGL_PLATFORM
        # and the vendor manifest make the selection deterministic in a
        # display-less container; the stock PX4 rc script turns HEADLESS=1 into
        # gz sim --headless-rendering.
        container_environment.update(
            {
                "NVIDIA_DRIVER_CAPABILITIES": "graphics,utility,compute",
                "NVIDIA_VISIBLE_DEVICES": str(selected_gpu),
                "EGL_PLATFORM": "surfaceless",
                "GZ_SIM_RENDER_ENGINE": "ogre2",
                "GZ_SIM_HEADLESS_RENDERING": "1",
                "__EGL_VENDOR_LIBRARY_FILENAMES": NVIDIA_EGL_VENDOR_FILE,
                "LIBGL_ALWAYS_SOFTWARE": "0",
            }
        )
    for key in (
        "PX4_HOME_LAT",
        "PX4_HOME_LON",
        "PX4_HOME_ALT",
        "PX4_GZ_MODEL_POSE",
    ):
        if key in stack_environment:
            container_environment[key] = stack_environment[key]
    if selected_network_mode == "host":
        container_environment["PX4_SIM_HOST"] = "127.0.0.1"
        container_environment["GZ_PARTITION"] = container_name
        container_environment["MD_SIM_INSTANCE"] = str(sim_instance)
    environment_tokens = tuple(sorted(container_environment.items()))
    # The stock image starts Gazebo from rcS/px4-rc.gzsim.  Patch only the
    # disposable container copy: set the explicit host-facing control port,
    # make every stream use that same port, and retain the telemetry output
    # port.  No repository or image layer is modified.
    # Keep the sed programs in single quotes.  The stock rc file contains
    # literal shell variables (``$udp_gcs_port_local`` and
    # ``$mavlink_network_interface_arg``); putting those in a double-quoted
    # sed expression lets the wrapper shell expand them before sed sees the
    # regular expression, silently leaving the startup line unpatched.  A
    # single-quoted expression preserves the dollars and lets PX4 expand them
    # later when it sources the disposable rc copy.
    patch_assignment = (
        "sed -i -E 's|^[[:space:]]*udp_gcs_port_local=.*$|"
        f'udp_gcs_port_local={control_port}|\' "$RC"; '
    )
    mavlink_target = "127.0.0.1" if selected_network_mode == "host" else "$DOCKER_HOST_IP"
    patch_command = (
        "sed -i -E 's|^([[:space:]]*)mavlink start -x -u "
        "[$]udp_gcs_port_local.*$|"
        f"\\1mavlink start -x -t {mavlink_target} -u $udp_gcs_port_local "
        f'-o {telemetry_port} -r 4000000 -f $mavlink_network_interface_arg|\' "$RC"; '
    )
    gpu_gz_wrapper = ""
    if gpu_render:
        # PX4's image starts Gazebo from its rc script.  Keep that startup path,
        # but make the two rendering flags explicit even if a future image
        # changes how HEADLESS is translated into gz arguments.
        gpu_gz_wrapper = (
            "MD_REAL_GZ=$(command -v gz); export MD_REAL_GZ; "
            "mkdir -p /tmp/multidetect-gz-bin; "
            "cat > /tmp/multidetect-gz-bin/gz <<'EOF'\n"
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"${1:-}\" = sim ]; then\n"
            "  has_headless=0; has_engine=0\n"
            "  for arg in \"$@\"; do\n"
            "    [ \"$arg\" = --headless-rendering ] && has_headless=1\n"
            "    [ \"$arg\" = --render-engine ] && has_engine=1\n"
            "  done\n"
            "  [ \"$has_headless\" -eq 1 ] || set -- \"$@\" --headless-rendering\n"
            "  [ \"$has_engine\" -eq 1 ] || set -- \"$@\" --render-engine ogre2\n"
            "fi\n"
            "exec \"$MD_REAL_GZ\" \"$@\"\n"
            "EOF\n"
            "chmod 0755 /tmp/multidetect-gz-bin/gz; "
            "export PATH=/tmp/multidetect-gz-bin:$PATH; "
        )
    # Docker Desktop routes host.docker.internal to the host loopback.  On Linux,
    # send to the exact address the host listens on (the selected network's
    # gateway); the PX4 image has no `ip` to discover it from inside.
    if selected_network_mode == "host":
        docker_host_ip = 'DOCKER_HOST_IP="127.0.0.1"; '
    elif os.name == "nt":
        docker_host_ip = (
            'DOCKER_HOST_IP="$(getent ahostsv4 host.docker.internal | '
            "awk '/STREAM/ {print $1; exit}')\"; "
        )
    elif sim_network is None:
        docker_host_ip = f'DOCKER_HOST_IP="{sim_listen_host(container_network)}"; '
    else:
        docker_host_ip = (
            f'DOCKER_HOST_IP="{sim_listen_host(container_network, mode=selected_network_mode)}"; '
        )
    startup = "".join(
        (
            "set -eu; ",
            docker_host_ip,
            'export DOCKER_HOST_IP; test -n "$DOCKER_HOST_IP"; ',
            "RC=/opt/px4-gazebo/etc/init.d-posix/px4-rc.mavlink; ",
            patch_assignment,
            patch_command,
            (
                "export PX4_NET_INTERFACE=lo; "
                if selected_network_mode == "host"
                else "export PX4_NET_INTERFACE=; "
            ),
            "export PX4_GZ_MODELS=/opt/multidetect-gazebo-models; ",
            "export PX4_GZ_WORLDS=/opt/px4-gazebo/share/gz/worlds; ",
            SIM_STARTUP_PATCHES,
            "export GZ_SIM_RESOURCE_PATH=",
            ("/opt/multidetect-gz-fuel-resolved:" if has_resolved_root else ""),
            "/opt/multidetect-gazebo-models:/opt/px4-gazebo/share/gz/worlds:",
            "/opt/px4-gazebo/share/gz/models:/opt/px4-gazebo/share/gz/worlds; ",
            gpu_gz_wrapper,
            f"exec /opt/px4-gazebo/bin/px4 -i {sim_instance if selected_network_mode == 'host' else 0} -s etc/init.d-posix/rcS",
        )
    )
    network_tokens = (
        ("--network", "host")
        if selected_network_mode == "host"
        else (
            "--add-host",
            "host.docker.internal:host-gateway",
            "--network",
            container_network,
            "--publish",
            f"127.0.0.1:{control_port}:{control_port}/udp",
        )
    )
    command = (
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--label",
        "multidetect.purpose=simulator-docker",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        *(cpu_isolation.docker_options if cpu_isolation else ()),
        *(('--gpus', f'device={selected_gpu}') if gpu_render else ()),
        *network_tokens,
        "--mount",
        _mount(model_root, CONTAINER_MODEL_ROOT),
        *(
            ("--mount", _mount(resolved_root, "/opt/multidetect-gz-fuel-resolved"))
            if has_resolved_root
            else ()
        ),
        "--mount",
        _mount(world_path, CONTAINER_WORLD),
        "--mount",
        _mount(scripts_root, CONTAINER_SCRIPT_ROOT),
        "--workdir",
        "/opt/px4-gazebo",
        "--entrypoint",
        "/bin/sh",
        *(token for key, value in environment_tokens for token in ("--env", f"{key}={value}")),
        image,
        "-c",
        startup,
    )
    cleanup = (
        ("docker", "stop", "--timeout", "2", container_name),
        ("docker", "rm", container_name),
    )
    return DockerSimulatorPlan(
        scenario_id=stack.scenario_id,
        scenario_manifest_hash=stack.scenario_manifest_hash,
        stack_plan_sha256=stack.evidence_hash,
        image=image,
        container_name=container_name,
        repository_root=root,
        model_root=model_root,
        world_path=world_path,
        scripts_root=scripts_root,
        dem_path=dem_path,
        terrain_dem_sha256=stack.assets.dem.sha256,
        host_control_port=control_port,
        host_telemetry_port=telemetry_port,
        command=command,
        cleanup_commands=cleanup,
        docker_executable=_docker_binary(),
        dry_run=dry_run,
        container_network_mode=container_network,
        vehicle_model=stack.vehicle_model,
        model_name=model_name,
        airframe_profile_id=stack.airframe_profile_id,
        environment=environment_tokens,
        assets=stack.assets,
        world_sdf_sha256=stack.assets.world.sha256,
        vehicle_model_sha256=stack.assets.model.sha256,
        vehicle_config_sha256=stack.assets.model_config.sha256,
        stack_assets_sha256=stack.assets.aggregate_sha256,
        model_root_assets_sha256=stack.assets.model_root.aggregate_sha256,
        fuel_resolved_root=resolved_root if has_resolved_root else None,
        gpu_render=gpu_render,
        gpu_device=selected_gpu,
        gpu_devices=resolved_gpu_devices,
        worker_index=worker_index,
        cpu_isolation=cpu_isolation,
    )


build_simulator_docker_plan = build_docker_simulator_plan
plan_simulator_docker = build_docker_simulator_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan an isolated Docker PX4/Gazebo SIM run"
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--repository-root", "--repo-root", dest="repository_root", type=Path)
    parser.add_argument("--job-dir", type=Path)
    parser.add_argument("--container-name", default="multidetect-sim")
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--image", default=PINNED_IMAGE)
    parser.add_argument(
        "--gpu-render",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable NVIDIA EGL/Ogre2 headless rendering (also via MULTIDETECT_SIM_GPU)",
    )
    parser.add_argument("--gpu-device", type=str, default=None)
    parser.add_argument("--gpu-devices", type=str, default=None, help="comma-separated worker GPU pool")
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--sim-network", choices=("bridge", "host"), default=None)
    parser.add_argument("--sim-instance", type=int, default=0)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require Docker to be discoverable",
    )
    args = parser.parse_args(argv)
    try:
        plan = build_docker_simulator_plan(
            args.scenario,
            repository_root=args.repository_root,
            job_dir=args.job_dir,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
            image=args.image,
            dry_run=not args.strict,
            gpu_render=args.gpu_render,
            gpu_device=args.gpu_device,
            gpu_devices=_split_gpu_list(args.gpu_devices, label="--gpu-devices")
            if args.gpu_devices is not None
            else None,
            worker_index=args.worker_index,
            sim_network=args.sim_network,
            sim_instance=args.sim_instance,
        )
        if args.strict and plan.docker_executable is None:
            raise DockerSimulatorPlanError("Docker executable not found")
        if args.out is not None:
            plan.write_json(args.out)
        print(json.dumps(plan.to_mapping_with_hash(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (DockerSimulatorPlanError, OSError, TypeError, ValueError) as exc:
        print(f"simulator docker-plan: {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "PINNED_IMAGE",
    "DockerSimulatorPlanError",
    "SimulatorDockerPlanError",
    "DockerSimulatorPlan",
    "build_docker_simulator_plan",
    "build_simulator_docker_plan",
    "plan_simulator_docker",
    "main",
]
