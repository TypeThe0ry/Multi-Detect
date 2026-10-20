#!/usr/bin/env python3
"""Plan a pinned Docker Desktop PX4/Gazebo SIM run without starting Docker.

The repository already contains a full VTOL acceptance script which executes
Docker.  This companion is intentionally the *planning* boundary: it resolves
the same repository-owned scenario assets, renders a copy/pasteable
``docker run`` command, and records a cleanup command.  It never invokes the
Docker CLI, opens a socket, or creates a container.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.simulator.stack import (
    SimulatorStackError,
    SimulatorStackPorts,
    build_stack_plan,
)

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
# Shell prefix every PX4/Gazebo SITL container runs before PX4 starts.
SIM_STARTUP_PATCHES = _DROP_GAZEBO_MAGNETOMETER + _WAIT_LONGER_FOR_GAZEBO


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
    candidate = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    return str(candidate) if candidate.is_file() else None


def _command_has_option(command: tuple[str, ...], option: str, value: str) -> bool:
    """Return whether *command* contains one exact Docker option/value pair."""

    return any(
        token == option and index + 1 < len(command) and command[index + 1] == value
        for index, token in enumerate(command)
    )


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
        return {
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
) -> DockerSimulatorPlan:
    """Resolve assets and render an isolated Docker command without running it."""

    if not _is_pinned_image(image):
        raise DockerSimulatorPlanError("image must be pinned by a 64-hex sha256 digest")
    if not isinstance(container_name, str) or _SAFE_NAME.fullmatch(container_name) is None:
        raise DockerSimulatorPlanError("container_name contains unsupported characters")
    control_port = _port(control_port, label="control_port")
    telemetry_port = _port(telemetry_port, label="telemetry_port")
    if control_port == telemetry_port:
        raise DockerSimulatorPlanError("control and telemetry ports must differ")
    root = _path(repository_root or ROOT, label="repository_root")
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
    for key in (
        "PX4_HOME_LAT",
        "PX4_HOME_LON",
        "PX4_HOME_ALT",
        "PX4_GZ_MODEL_POSE",
    ):
        if key in stack_environment:
            container_environment[key] = stack_environment[key]
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
    patch_command = (
        "sed -i -E 's|^([[:space:]]*)mavlink start -x -u "
        "[$]udp_gcs_port_local.*$|"
        "\\1mavlink start -x -t $DOCKER_HOST_IP -u $udp_gcs_port_local "
        f'-o {telemetry_port} -r 4000000 -f $mavlink_network_interface_arg|\' "$RC"; '
    )
    startup = "".join(
        (
            "set -eu; ",
            'DOCKER_HOST_IP="$(getent ahostsv4 host.docker.internal | ',
            "awk '/STREAM/ {print $1; exit}')\"; ",
            'export DOCKER_HOST_IP; test -n "$DOCKER_HOST_IP"; ',
            "RC=/opt/px4-gazebo/etc/init.d-posix/px4-rc.mavlink; ",
            patch_assignment,
            patch_command,
            "export PX4_NET_INTERFACE=; ",
            "export PX4_GZ_MODELS=/opt/multidetect-gazebo-models; ",
            "export PX4_GZ_WORLDS=/opt/px4-gazebo/share/gz/worlds; ",
            SIM_STARTUP_PATCHES,
            "export GZ_SIM_RESOURCE_PATH=",
            ("/opt/multidetect-gz-fuel-resolved:" if has_resolved_root else ""),
            "/opt/multidetect-gazebo-models:/opt/px4-gazebo/share/gz/worlds:",
            "/opt/px4-gazebo/share/gz/models:/opt/px4-gazebo/share/gz/worlds; ",
            "exec /opt/px4-gazebo/bin/px4 -i 0 -s etc/init.d-posix/rcS",
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
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        f"127.0.0.1:{control_port}:{control_port}/udp",
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
    )


build_simulator_docker_plan = build_docker_simulator_plan
plan_simulator_docker = build_docker_simulator_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan an isolated Docker Desktop PX4/Gazebo SIM run"
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--repository-root", "--repo-root", dest="repository_root", type=Path)
    parser.add_argument("--job-dir", type=Path)
    parser.add_argument("--container-name", default="multidetect-sim")
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--image", default=PINNED_IMAGE)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require Docker Desktop to be discoverable",
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
        )
        if args.strict and plan.docker_executable is None:
            raise DockerSimulatorPlanError("Docker Desktop executable not found")
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
