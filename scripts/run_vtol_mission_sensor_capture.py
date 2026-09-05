#!/usr/bin/env python3
"""Capture real Gazebo pixels during one completed owned-SITL VTOL mission.

This script is the only lifecycle owner for its disposable PX4/Gazebo
container.  It starts no QGC process and has no desktop/UI dependency.  The
sequence is deliberately strict:

1. allocate six distinct, unused high loopback UDP ports;
2. build and validate the repository-owned, digest-pinned Docker plan;
3. start and exact-ID validate one hardened disposable VTOL container;
4. start the bounded evaluator router and wait for all three router sockets;
5. start ``vtol_sitl_mission_acceptance.py`` in the background;
6. wait for its transaction-bound fixed-wing flight-capture barrier;
7. capture production wire-v2 Image/CameraInfo/NavSat/Odometry records from
   that same still-running container; and
8. wait for FW -> MC -> VTOL_LAND -> landed/disarmed completion before a
   graceful router stop and exact-ID container removal.

The resulting evidence is SIM/software-only.  QGC rendering, manual visual
acceptance, physical hardware, HIL, REAL, and outdoor GNSS remain explicitly
unclaimed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

_repository_scripts = Path(__file__).resolve().parent
if str(_repository_scripts) not in sys.path:
    # Several existing acceptance adapters intentionally use sibling imports
    # so they can also run as direct scripts.  Keep that contract when this
    # orchestrator is imported as ``scripts.run_vtol_mission_sensor_capture``.
    sys.path.insert(0, str(_repository_scripts))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.backends.gazebo_sim import (  # noqa: E402
    GAZEBO_SIM_SOURCE_ROW_ORDER,
    GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
    GAZEBO_SIM_WIRE_ROW_ORDER,
)
from multidetect.mission_journal import (  # noqa: E402
    DurableMissionJournal,
    MissionJournalError,
)
from multidetect.product_version import PRODUCT_VERSION  # noqa: E402
from multidetect.px4_gazebo_qgc_router import (  # noqa: E402
    PX4_AUTO_MISSION_CUSTOM_MODE,
    run_router,
)
from multidetect.px4_mission import (  # noqa: E402
    MAV_VTOL_STATE_FW,
    MissionTransactionState,
    Px4MissionError,
    Px4MissionItemInt,
    px4_wire_hash,
)

try:
    from scripts.run_simulator_docker import (
        DEFAULT_SCENARIO,
        PINNED_IMAGE,
        DockerSimulatorPlan,
        build_docker_simulator_plan,
    )
    from scripts.run_simulator_docker_acceptance import (
        GAZEBO_BGR24_FRAME_SET_MANIFEST,
        _capture_gazebo_sensor_transport,
        _container_inspect,
        _result_returncode,
        _result_text,
        _run,
        _validate_owned_container,
    )
    from scripts.run_simulator_docker_acceptance import (
        _docker_path as _resolve_docker_path,
    )
    from scripts.vtol_sitl_mission_acceptance import validate_owned_container_record
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from run_simulator_docker import (  # type: ignore[no-redef]
        DEFAULT_SCENARIO,
        PINNED_IMAGE,
        DockerSimulatorPlan,
        build_docker_simulator_plan,
    )
    from run_simulator_docker_acceptance import (  # type: ignore[no-redef]
        GAZEBO_BGR24_FRAME_SET_MANIFEST,
        _capture_gazebo_sensor_transport,
        _container_inspect,
        _result_returncode,
        _result_text,
        _run,
        _validate_owned_container,
    )
    from run_simulator_docker_acceptance import (
        _docker_path as _resolve_docker_path,
    )
    from vtol_sitl_mission_acceptance import (  # type: ignore[no-redef]
        validate_owned_container_record,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "evaluation"
DEFAULT_RUN_DIRECTORY = DEFAULT_ARTIFACT_ROOT / (
    f"vtol-mission-sensor-capture-{PRODUCT_VERSION.release_label}"
)
DEFAULT_EVIDENCE_PATH = DEFAULT_ARTIFACT_ROOT / (
    f"vtol-mission-sensor-capture-{PRODUCT_VERSION.release_label}.json"
)
DEFAULT_CONTAINER_NAME = "multidetect-vtol-flight-capture"
EXPECTED_CONTAINER_LABEL = "simulator-docker"
EXPECTED_SCENARIO_ID = "river_rescue_vtol_001"
EXPECTED_VEHICLE_MODEL = "px4_vtol_standard"
EXPECTED_MODEL_NAME = "standard_vtol_mono_cam"
EXPECTED_AUTOSTART = "4004"
EXPECTED_SIM_MODEL = "gz_standard_vtol_mono_cam"
PROTECTED_GROUND_STATION_PORT = 14550
HIGH_PORT_MINIMUM = 30_000
HIGH_PORT_MAXIMUM = 60_000
PORT_COUNT = 6
MINIMUM_FLIGHT_DISTANCE_M = 35.0
MINIMUM_FLIGHT_ALTITUDE_M = 10.0
DEFAULT_FRAME_COUNT = 3
DEFAULT_FRAME_WIDTH = 1280
DEFAULT_FRAME_HEIGHT = 960
DEFAULT_SENSOR_TIMEOUT_S = 120.0
DEFAULT_MISSION_TIMEOUT_S = 360.0
DEFAULT_READY_TIMEOUT_S = 180.0
DEFAULT_ROUTER_READY_TIMEOUT_S = 10.0
MAXIMUM_FLIGHT_CAPTURE_START_DELAY_S = 5.0
MAXIMUM_FLIGHT_STATE_AGE_S = 1.5
EVALUATOR_SYSTEM_ID = 250
EVALUATOR_COMPONENT_ID = 191
_DOCKER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FLIGHT_CAPTURE_COMPLETE_CHECK_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "host_monotonic_clock",
        "capture_interval",
        "exact_container_id",
        "transaction_id",
        "committed_protocol_sha256",
        "scenario_binding",
        "frame_set_binding",
        "truth_free",
    }
)
RELEASE_STATUS = (
    f"v{PRODUCT_VERSION.release_label}; final v{PRODUCT_VERSION.numeric} remains blocked"
)


class VtolMissionSensorCaptureError(RuntimeError):
    """A headless VTOL mission sensor-capture invariant failed."""


@dataclass(frozen=True, slots=True)
class CapturePorts:
    """The six isolated loopback UDP endpoints used by one capture run."""

    px4_input: int
    px4_telemetry: int
    qgc_sink: int
    qgc_router_return: int
    evaluator: int
    evaluator_router_return: int

    @property
    def values(self) -> tuple[int, ...]:
        return (
            self.px4_input,
            self.px4_telemetry,
            self.qgc_sink,
            self.qgc_router_return,
            self.evaluator,
            self.evaluator_router_return,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "host": "127.0.0.1",
            "px4_input_udp": self.px4_input,
            "px4_telemetry_udp": self.px4_telemetry,
            "qgc_sink_udp": self.qgc_sink,
            "qgc_router_return_udp": self.qgc_router_return,
            "evaluator_udp": self.evaluator,
            "evaluator_router_return_udp": self.evaluator_router_return,
            "all_distinct": len(set(self.values)) == PORT_COUNT,
            "all_high_ports": all(
                HIGH_PORT_MINIMUM <= port <= HIGH_PORT_MAXIMUM for port in self.values
            ),
            "protected_udp_14550_used": PROTECTED_GROUND_STATION_PORT in self.values,
            "qgc_process_started": False,
        }


@dataclass(frozen=True, slots=True)
class CapturePaths:
    run_directory: Path
    evidence: Path
    flight_ready: Path
    flight_capture_complete: Path
    mission_journal: Path
    mission_evidence: Path
    mission_stdout: Path
    mission_stderr: Path
    frame_directory: Path
    docker_job_directory: Path

    def to_mapping(self) -> dict[str, str]:
        return {
            "run_directory": str(self.run_directory),
            "evidence": str(self.evidence),
            "flight_capture_ready": str(self.flight_ready),
            "flight_capture_complete": str(self.flight_capture_complete),
            "mission_journal": str(self.mission_journal),
            "mission_evidence": str(self.mission_evidence),
            "mission_stdout": str(self.mission_stdout),
            "mission_stderr": str(self.mission_stderr),
            "frame_directory": str(self.frame_directory),
            "docker_job_directory": str(self.docker_job_directory),
        }


@dataclass(slots=True)
class _RouterThread:
    thread: threading.Thread
    ready_event: threading.Event
    stop_event: threading.Event
    result_box: dict[str, object]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_unused_loopback_port(port: int) -> bool:
    """Probe both UDP and TCP so a selected endpoint has no visible owner."""

    sockets: list[socket.socket] = []
    try:
        for socket_type in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
            channel = socket.socket(socket.AF_INET, socket_type)
            sockets.append(channel)
            if os.name == "nt":
                channel.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                channel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            channel.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        for channel in sockets:
            channel.close()


def validate_capture_ports(
    ports: CapturePorts,
    *,
    port_probe: Callable[[int], bool] = _is_unused_loopback_port,
) -> CapturePorts:
    values = ports.values
    if any(isinstance(port, bool) or not isinstance(port, int) for port in values):
        raise VtolMissionSensorCaptureError("all six UDP ports must be integers")
    if len(set(values)) != PORT_COUNT:
        raise VtolMissionSensorCaptureError("all six loopback UDP ports must be distinct")
    if PROTECTED_GROUND_STATION_PORT in values:
        raise VtolMissionSensorCaptureError("UDP 14550 is protected and cannot be used")
    if not all(HIGH_PORT_MINIMUM <= port <= HIGH_PORT_MAXIMUM for port in values):
        raise VtolMissionSensorCaptureError(
            f"all UDP ports must be in {HIGH_PORT_MINIMUM}..{HIGH_PORT_MAXIMUM}"
        )
    occupied = [port for port in values if not port_probe(port)]
    if occupied:
        raise VtolMissionSensorCaptureError(
            "one or more loopback ports are already occupied: "
            + ", ".join(str(port) for port in occupied)
        )
    return ports


def allocate_capture_ports(
    *,
    port_probe: Callable[[int], bool] = _is_unused_loopback_port,
    random_source: random.Random | random.SystemRandom | None = None,
    maximum_attempts: int = 20_000,
) -> CapturePorts:
    """Choose six unused high ports without touching QGC's protected 14550."""

    chooser = random_source or random.SystemRandom()
    selected: list[int] = []
    attempts = 0
    while len(selected) < PORT_COUNT and attempts < maximum_attempts:
        attempts += 1
        candidate = chooser.randint(HIGH_PORT_MINIMUM, HIGH_PORT_MAXIMUM)
        if candidate == PROTECTED_GROUND_STATION_PORT or candidate in selected:
            continue
        if port_probe(candidate):
            selected.append(candidate)
    if len(selected) != PORT_COUNT:
        raise VtolMissionSensorCaptureError(
            f"unable to allocate {PORT_COUNT} distinct unused high loopback UDP ports"
        )
    return CapturePorts(*selected)


def _absolute_unsymlinked(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    for item in (candidate, *candidate.parents):
        if item.exists() and item.is_symlink():
            raise VtolMissionSensorCaptureError(f"{label} must not traverse a symlink: {item}")
    return candidate.resolve(strict=False)


def _require_new_path(path: Path, *, label: str) -> Path:
    resolved = _absolute_unsymlinked(path, label=label)
    if path.is_symlink() or resolved.exists():
        raise VtolMissionSensorCaptureError(f"{label} must be a new path: {resolved}")
    return resolved


def prepare_capture_paths(
    run_directory: Path,
    evidence_path: Path,
    *,
    flight_ready_file: Path | None = None,
    flight_capture_complete_file: Path | None = None,
    mission_journal_path: Path | None = None,
    frame_output_directory: Path | None = None,
) -> CapturePaths:
    run = _require_new_path(run_directory, label="run_directory")
    evidence = _require_new_path(evidence_path, label="evidence_path")
    ready = _require_new_path(
        flight_ready_file or run / "vtol-flight-capture-ready.json",
        label="flight_capture_ready_file",
    )
    capture_complete = _require_new_path(
        flight_capture_complete_file or run / "vtol-sensor-capture-complete.json",
        label="flight_capture_complete_file",
    )
    journal = _require_new_path(
        mission_journal_path or run / "vtol-mission.transaction.jsonl",
        label="mission_journal_path",
    )
    frames = _require_new_path(
        frame_output_directory or run / "gazebo-wire-v2-frames",
        label="frame_output_directory",
    )
    mission_evidence = _require_new_path(
        run / "vtol-mission-acceptance.json",
        label="mission_evidence_path",
    )
    mission_stdout = _require_new_path(
        run / "vtol-mission.stdout.jsonl",
        label="mission_stdout_path",
    )
    mission_stderr = _require_new_path(
        run / "vtol-mission.stderr.log",
        label="mission_stderr_path",
    )
    docker_job = _require_new_path(run / "docker-plan", label="docker_job_directory")
    generated = {
        run,
        evidence,
        ready,
        capture_complete,
        journal,
        frames,
        mission_evidence,
        mission_stdout,
        mission_stderr,
        docker_job,
    }
    if len(generated) != 10:
        raise VtolMissionSensorCaptureError("capture output paths must be pairwise distinct")
    managed_outputs = (
        evidence,
        ready,
        capture_complete,
        journal,
        frames,
        mission_evidence,
        mission_stdout,
        mission_stderr,
        docker_job,
    )
    if any(output in run.parents for output in managed_outputs):
        raise VtolMissionSensorCaptureError(
            "no output file or directory may be an ancestor of run_directory"
        )
    overlapping = [
        (left, right)
        for index, left in enumerate(managed_outputs)
        for right in managed_outputs[index + 1 :]
        if left in right.parents or right in left.parents
    ]
    if overlapping:
        raise VtolMissionSensorCaptureError(
            "capture output files and directories must not contain one another"
        )
    run.parent.mkdir(parents=True, exist_ok=True)
    if not run.parent.is_dir():
        raise VtolMissionSensorCaptureError("run_directory parent is not a directory")
    run.mkdir()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    return CapturePaths(
        run_directory=run,
        evidence=evidence,
        flight_ready=ready,
        flight_capture_complete=capture_complete,
        mission_journal=journal,
        mission_evidence=mission_evidence,
        mission_stdout=mission_stdout,
        mission_stderr=mission_stderr,
        frame_directory=frames,
        docker_job_directory=docker_job,
    )


def _atomic_write_new_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish the already-flushed same-directory inode atomically while
        # retaining create-only semantics.  A competing writer that wins the
        # target name can never be overwritten by this process.
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _command_pair_present(command: Sequence[str], option: str, value: str) -> bool:
    return any(
        command[index] == option and command[index + 1] == value
        for index in range(len(command) - 1)
    )


def validate_capture_plan(
    plan: DockerSimulatorPlan,
    *,
    ports: CapturePorts,
    container_name: str,
) -> dict[str, bool]:
    environment = dict(plan.environment)
    checks = {
        "scenario_id": plan.scenario_id == EXPECTED_SCENARIO_ID,
        "pinned_image": plan.image == PINNED_IMAGE and "@sha256:" in plan.image,
        "exact_container_name": plan.container_name == container_name,
        "headless": environment.get("HEADLESS") == "1",
        "vtol_vehicle_model": plan.vehicle_model == EXPECTED_VEHICLE_MODEL,
        "vtol_gazebo_model": plan.model_name == EXPECTED_MODEL_NAME,
        "vtol_autostart": environment.get("PX4_SYS_AUTOSTART") == EXPECTED_AUTOSTART,
        "vtol_sim_model": environment.get("PX4_SIM_MODEL") == EXPECTED_SIM_MODEL,
        "control_port": plan.host_control_port == ports.px4_input,
        "telemetry_port": plan.host_telemetry_port == ports.px4_telemetry,
        "cap_drop_all": bool(getattr(plan, "cap_drop_all", False)),
        "no_new_privileges": bool(getattr(plan, "no_new_privileges", False)),
        "container_security_gate": bool(getattr(plan, "container_security_gate_passed", False)),
        "command_cap_drop_all": _command_pair_present(plan.command, "--cap-drop", "ALL"),
        "command_no_new_privileges": _command_pair_present(
            plan.command,
            "--security-opt",
            "no-new-privileges:true",
        ),
        "model_mount_exists": plan.model_root.is_dir(),
        "world_mount_exists": plan.world_path.is_file(),
        "scripts_mount_exists": plan.scripts_root.is_dir(),
        "protected_udp_14550_unused": PROTECTED_GROUND_STATION_PORT
        not in (plan.host_control_port, plan.host_telemetry_port),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VtolMissionSensorCaptureError(
            "headless VTOL Docker plan checks failed: " + ", ".join(failed)
        )
    return checks


def _start_router_thread(
    *,
    ports: CapturePorts,
    mission_timeout_s: float,
    router_runner: Callable[..., dict[str, object]],
) -> _RouterThread:
    ready_event = threading.Event()
    stop_event = threading.Event()
    result_box: dict[str, object] = {}
    kwargs = {
        "qgc_port": ports.qgc_sink,
        "qgc_router_port": ports.qgc_router_return,
        "evaluator_port": ports.evaluator,
        "evaluator_router_port": ports.evaluator_router_return,
        "px4_telemetry_port": ports.px4_telemetry,
        "px4_input_port": ports.px4_input,
        "evaluator_system_id": EVALUATOR_SYSTEM_ID,
        "evaluator_component_id": EVALUATOR_COMPONENT_ID,
        "duration_seconds": min(600.0, mission_timeout_s + 120.0),
        "acknowledge_owned_disposable_sitl": True,
        "ready_event": ready_event,
        "stop_event": stop_event,
    }

    def target() -> None:
        try:
            result_box["result"] = router_runner(**kwargs)
        except BaseException as exc:  # surfaced synchronously by lifecycle owner
            result_box["error"] = exc

    thread = threading.Thread(
        target=target,
        name="multidetect-vtol-sensor-router",
        daemon=True,
    )
    thread.start()
    return _RouterThread(thread, ready_event, stop_event, result_box)


def _wait_router_ready(handle: _RouterThread, timeout_s: float) -> None:
    if handle.ready_event.wait(timeout_s):
        return
    error = handle.result_box.get("error")
    if error is not None:
        raise VtolMissionSensorCaptureError(f"evaluator router failed before ready: {error}")
    if not handle.thread.is_alive():
        raise VtolMissionSensorCaptureError("evaluator router exited before its ready barrier")
    raise VtolMissionSensorCaptureError("evaluator router ready barrier timed out")


def _stop_router(handle: _RouterThread | None, *, timeout_s: float = 10.0) -> dict[str, object]:
    if handle is None:
        return {
            "started": False,
            "ready": False,
            "stopped": False,
            "result": None,
            "error": None,
        }
    handle.stop_event.set()
    handle.thread.join(timeout_s)
    error = handle.result_box.get("error")
    return {
        "started": True,
        "ready": handle.ready_event.is_set(),
        "stop_requested": True,
        "stopped": not handle.thread.is_alive(),
        "result": handle.result_box.get("result"),
        "error": f"{type(error).__name__}: {error}" if error is not None else None,
    }


def _process_arguments(
    *,
    paths: CapturePaths,
    scenario: Path,
    container_name: str,
    container_id: str,
    evaluator_port: int,
    airframe_profile_sha256: str,
    runtime_descriptor_sha256: str,
    runtime_provenance_sha256: str,
    mission_timeout_s: float,
    docker_executable: str,
) -> list[str]:
    return [
        sys.executable,
        str((ROOT / "scripts" / "vtol_sitl_mission_acceptance.py").resolve()),
        "--scenario-manifest",
        str(scenario),
        "--evidence",
        str(paths.mission_evidence),
        "--container-name",
        container_name,
        "--expected-container-id",
        container_id,
        "--expected-container-label",
        EXPECTED_CONTAINER_LABEL,
        "--evaluator-port",
        str(evaluator_port),
        "--airframe-profile-sha256",
        airframe_profile_sha256,
        "--runtime-descriptor-sha256",
        runtime_descriptor_sha256,
        "--runtime-provenance-sha256",
        runtime_provenance_sha256,
        "--flight-capture-ready-file",
        str(paths.flight_ready),
        "--flight-capture-complete-file",
        str(paths.flight_capture_complete),
        "--mission-journal",
        str(paths.mission_journal),
        "--mission-timeout-seconds",
        str(mission_timeout_s),
        "--docker",
        docker_executable,
        "--source-system",
        str(EVALUATOR_SYSTEM_ID),
        "--source-component",
        str(EVALUATOR_COMPONENT_ID),
        "--acknowledge-owned-disposable-sitl",
    ]


def _start_mission_process(
    arguments: Sequence[str],
    *,
    paths: CapturePaths,
    process_factory: Callable[..., Any],
) -> tuple[Any, Any, Any]:
    environment = os.environ.copy()
    source_path = str((ROOT / "src").resolve())
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    stdout_handle = paths.mission_stdout.open("x", encoding="utf-8", newline="\n")
    stderr_handle = paths.mission_stderr.open("x", encoding="utf-8", newline="\n")
    kwargs: dict[str, object] = {
        "cwd": str(ROOT),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": stdout_handle,
        "stderr": stderr_handle,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = process_factory(list(arguments), **kwargs)
    except Exception:
        stdout_handle.close()
        stderr_handle.close()
        raise
    return process, stdout_handle, stderr_handle


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VtolMissionSensorCaptureError(f"{label} is unreadable JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise VtolMissionSensorCaptureError(f"{label} must contain one JSON object")
    return payload


def validate_flight_capture_ready(
    payload: Mapping[str, Any],
    *,
    container_id: str,
) -> dict[str, bool]:
    container = _mapping(payload.get("container"))
    transaction = _mapping(payload.get("transaction"))
    flight = _mapping(payload.get("flight"))
    progress = _mapping(flight.get("mission_progress"))
    first_activity = _mapping(progress.get("first_activity"))
    position = _mapping(flight.get("position_at_ready"))
    telemetry_ages = _mapping(flight.get("telemetry_ages_s"))
    distance = _finite_float(flight.get("maximum_distance_from_home_m"))
    distance_policy = _finite_float(flight.get("minimum_required_distance_from_home_m"))
    altitude = _finite_float(flight.get("maximum_relative_altitude_m"))
    altitude_policy = _finite_float(flight.get("minimum_required_relative_altitude_m"))
    latitude = _finite_float(position.get("latitude_deg"))
    longitude = _finite_float(position.get("longitude_deg"))
    relative_altitude = _finite_float(position.get("relative_altitude_m"))
    ready_monotonic = _finite_float(payload.get("captured_at_monotonic_s"))
    first_activity_at = _finite_float(first_activity.get("at_s"))
    heartbeat_age = _finite_float(telemetry_ages.get("heartbeat"))
    position_age = _finite_float(telemetry_ages.get("position"))
    extended_state_age = _finite_float(telemetry_ages.get("extended_system_state"))
    checks = {
        "schema": payload.get("schema") == "multidetect.px4-vtol-flight-capture-ready",
        "schema_version": payload.get("schema_version") == 1,
        "exact_container_id": _DOCKER_ID_RE.fullmatch(container_id) is not None
        and container.get("id") == container_id,
        "exact_container_id_bound": container.get("exact_id_bound") is True,
        "transaction_id": isinstance(transaction.get("id"), str)
        and bool(str(transaction.get("id")).strip()),
        "transaction_start_permitted": transaction.get("state") == "start_permitted"
        and transaction.get("start_permitted") is True,
        "committed_protocol_sha256": _valid_sha256(transaction.get("committed_protocol_sha256")),
        "host_monotonic_timestamp": payload.get("captured_at_clock") == "host_monotonic"
        and ready_monotonic is not None
        and ready_monotonic > 0.0,
        "exact_auto_mission": flight.get("auto_mission_active") is True
        and flight.get("auto_mission_custom_mode") == PX4_AUTO_MISSION_CUSTOM_MODE,
        "armed": flight.get("armed") is True,
        "in_air": flight.get("in_air_at_ready") is True,
        "fixed_wing": flight.get("fixed_wing_observed") is True
        and flight.get("current_vtol_state") == MAV_VTOL_STATE_FW,
        "distance": distance is not None and distance >= MINIMUM_FLIGHT_DISTANCE_M,
        "distance_policy": distance_policy == MINIMUM_FLIGHT_DISTANCE_M,
        "altitude": altitude is not None and altitude >= MINIMUM_FLIGHT_ALTITUDE_M,
        "altitude_policy": altitude_policy == MINIMUM_FLIGHT_ALTITUDE_M,
        "mission_progress": progress.get("observed") is True
        and bool(first_activity)
        and first_activity_at is not None
        and first_activity_at >= 0.0,
        "position_at_ready": bool(position)
        and latitude is not None
        and -90.0 < latitude < 90.0
        and longitude is not None
        and -180.0 <= longitude <= 180.0
        and relative_altitude is not None
        and relative_altitude > 0.0,
        "telemetry_fresh": flight.get("telemetry_fresh") is True
        and flight.get("maximum_telemetry_age_s") == MAXIMUM_FLIGHT_STATE_AGE_S
        and heartbeat_age is not None
        and 0.0 <= heartbeat_age <= MAXIMUM_FLIGHT_STATE_AGE_S
        and position_age is not None
        and 0.0 <= position_age <= MAXIMUM_FLIGHT_STATE_AGE_S
        and extended_state_age is not None
        and 0.0 <= extended_state_age <= MAXIMUM_FLIGHT_STATE_AGE_S,
        "no_visual_claim": payload.get("operator_visual_acceptance") == "not_claimed",
        "no_real_hardware": payload.get("real_pixhawk_contacted") is False
        and payload.get("real_jetson_contacted") is False
        and payload.get("real_camera_contacted") is False,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VtolMissionSensorCaptureError(
            "flight-capture ready checks failed: " + ", ".join(failed)
        )
    return checks


def _wait_for_flight_ready(
    *,
    path: Path,
    process: Any,
    router: _RouterThread,
    container_id: str,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, bool]]:
    deadline = clock() + timeout_s
    last_parse_error: str | None = None
    while clock() < deadline:
        router_error = router.result_box.get("error")
        if router_error is not None:
            raise VtolMissionSensorCaptureError(
                f"evaluator router failed while awaiting flight barrier: {router_error}"
            )
        exit_code = process.poll()
        if exit_code is not None:
            raise VtolMissionSensorCaptureError(
                f"VTOL mission helper exited before flight-capture barrier (exit {exit_code})"
            )
        if path.is_file():
            try:
                payload = _read_json_object(path, label="flight-capture ready file")
                checks = validate_flight_capture_ready(payload, container_id=container_id)
                return payload, checks
            except VtolMissionSensorCaptureError as exc:
                last_parse_error = str(exc)
        sleeper(0.05)
    detail = f": {last_parse_error}" if last_parse_error else ""
    raise VtolMissionSensorCaptureError(f"flight-capture ready barrier timed out{detail}")


def validate_mission_evidence(
    payload: Mapping[str, Any],
    *,
    container_id: str,
    scenario_manifest_sha256: str,
    airframe_profile_hash: str,
    descriptor_hash: str,
    provenance_hash: str,
    evaluator_port: int,
    barrier: Mapping[str, Any],
    flight_ready_path: Path,
    flight_complete_path: Path,
    flight_complete_file_sha256: str,
    flight_complete_document: Mapping[str, Any],
) -> dict[str, bool]:
    container = _mapping(payload.get("container"))
    ownership = _mapping(container.get("ownership_checks"))
    transaction = _mapping(payload.get("transaction"))
    flight = _mapping(payload.get("flight"))
    protocol = _mapping(payload.get("protocol"))
    documents = _mapping(payload.get("documents"))
    bindings = _mapping(documents.get("bindings"))
    safety = _mapping(payload.get("safety"))
    scenario = _mapping(payload.get("scenario"))
    mavlink = _mapping(payload.get("mavlink"))
    barrier_transaction = _mapping(barrier.get("transaction"))
    completion_checks = _mapping(flight.get("flight_capture_complete_checks"))
    interval_samples = _sequence(flight.get("flight_capture_interval_samples"))
    distance = _finite_float(flight.get("maximum_distance_from_home_m"))
    altitude = _finite_float(flight.get("maximum_relative_altitude_m"))
    committed_hash = protocol.get("committed_protocol_sha256")
    barrier_ready_time = _finite_float(barrier.get("captured_at_monotonic_s"))
    completion_started = _finite_float(
        flight_complete_document.get("capture_started_at_monotonic_s")
    )
    completion_completed = _finite_float(
        flight_complete_document.get("capture_completed_at_monotonic_s")
    )
    sample_times: list[float] = []
    interval_sample_checks: list[bool] = []
    for raw_sample in interval_samples:
        sample = _mapping(raw_sample)
        captured_at = _finite_float(sample.get("captured_at_monotonic_s"))
        if captured_at is not None:
            sample_times.append(captured_at)
        maximum_state_age = _finite_float(sample.get("maximum_state_age_s"))
        heartbeat_age = _finite_float(sample.get("heartbeat_age_s"))
        position_age = _finite_float(sample.get("position_age_s"))
        extended_state_age = _finite_float(sample.get("extended_system_state_age_s"))
        interval_sample_checks.append(
            sample.get("captured_at_clock") == "host_monotonic"
            and captured_at is not None
            and barrier_ready_time is not None
            and captured_at >= barrier_ready_time
            and sample.get("auto_mission_active") is True
            and sample.get("armed") is True
            and sample.get("in_air") is True
            and sample.get("vtol_state") == MAV_VTOL_STATE_FW
            and maximum_state_age == MAXIMUM_FLIGHT_STATE_AGE_S
            and heartbeat_age is not None
            and 0.0 <= heartbeat_age <= MAXIMUM_FLIGHT_STATE_AGE_S
            and position_age is not None
            and 0.0 <= position_age <= MAXIMUM_FLIGHT_STATE_AGE_S
            and extended_state_age is not None
            and 0.0 <= extended_state_age <= MAXIMUM_FLIGHT_STATE_AGE_S
            and sample.get("telemetry_fresh") is True
            and sample.get("valid") is True
        )
    interval_sample_count = _strict_nonnegative_int(
        flight.get("flight_capture_interval_sample_count")
    )
    resolved_ready_path = flight_ready_path.resolve(strict=False)
    resolved_complete_path = flight_complete_path.resolve(strict=False)
    checks = {
        "schema": payload.get("schema") == "multidetect.px4-vtol-sitl-mission-acceptance",
        "schema_version": payload.get("schema_version") == 1,
        "gate_passed": payload.get("gate_passed") is True,
        "exact_container_id": _DOCKER_ID_RE.fullmatch(container_id) is not None
        and container.get("id") == container_id,
        "container_remained_parent_owned": container.get("created_by_this_helper") is False
        and container.get("stopped_by_this_helper") is False
        and ownership.get("exact_container_id") is True
        and ownership.get("running") is True,
        "transaction_id_bound": transaction.get("id") == barrier_transaction.get("id")
        and isinstance(transaction.get("id"), str)
        and bool(str(transaction.get("id")).strip()),
        "transaction_start_permitted": transaction.get("final_state") == "start_permitted",
        "protocol_committed": protocol.get("upload_acknowledged") is True
        and protocol.get("readback_hash_match") is True
        and protocol.get("int_only") is True
        and _valid_sha256(committed_hash)
        and committed_hash == barrier_transaction.get("committed_protocol_sha256")
        and protocol.get("uploaded_protocol_sha256") == committed_hash
        and protocol.get("readback_protocol_sha256") == committed_hash,
        "scenario_bound": scenario.get("scenario_id") == EXPECTED_SCENARIO_ID
        and scenario.get("manifest_sha256") == scenario_manifest_sha256
        and scenario.get("truth_loaded_by_runtime") is False
        and scenario.get("expectations_loaded_by_runtime") is False,
        "airframe_profile_bound": bindings.get("airframe_profile_hash") == airframe_profile_hash,
        "runtime_descriptor_bound": bindings.get("runtime_descriptor_hash") == descriptor_hash,
        "runtime_provenance_bound": bindings.get("runtime_provenance_hash") == provenance_hash,
        "evaluator_endpoint_bound": mavlink.get("endpoint") == f"udpin:127.0.0.1:{evaluator_port}"
        and mavlink.get("source_system") == EVALUATOR_SYSTEM_ID
        and mavlink.get("source_component") == EVALUATOR_COMPONENT_ID
        and mavlink.get("target_system") == 1
        and mavlink.get("target_component") == 1
        and mavlink.get("loopback_only") is True
        and mavlink.get("protected_udp_14550_used") is False,
        "fixed_wing": flight.get("fixed_wing_observed") is True,
        "multicopter_after_fixed_wing": flight.get("multicopter_observed") is True,
        "vtol_landed": flight.get("landed_observed") is True,
        "terminal_disarmed": flight.get("terminal_disarmed") is True,
        "distance": distance is not None and distance >= MINIMUM_FLIGHT_DISTANCE_M,
        "altitude": altitude is not None and altitude >= MINIMUM_FLIGHT_ALTITUDE_M,
        "flight_capture_ready_binding": flight.get("flight_capture_ready_written") is True
        and flight.get("flight_capture_ready_file") == str(resolved_ready_path),
        "flight_capture_complete_binding": flight.get("flight_capture_complete_observed") is True
        and flight.get("flight_capture_complete_file") == str(resolved_complete_path)
        and _valid_sha256(flight_complete_file_sha256)
        and flight.get("flight_capture_complete_file_sha256") == flight_complete_file_sha256
        and _mapping(flight.get("flight_capture_complete_document"))
        == dict(flight_complete_document),
        "flight_capture_complete_checks": set(completion_checks)
        == _FLIGHT_CAPTURE_COMPLETE_CHECK_KEYS
        and all(value is True for value in completion_checks.values()),
        "flight_capture_interval": flight.get("flight_capture_interval_state_source")
        == "live MAVLink state observer"
        and interval_sample_count is not None
        and interval_sample_count > 0
        and interval_sample_count == len(interval_samples)
        and len(interval_sample_checks) == len(interval_samples)
        and all(interval_sample_checks)
        and len(sample_times) == len(interval_samples)
        and sample_times == sorted(sample_times)
        and completion_started is not None
        and completion_completed is not None
        and barrier_ready_time is not None
        and barrier_ready_time <= completion_started <= completion_completed
        and sample_times[0] <= completion_started
        and sample_times[-1] >= completion_completed
        and flight.get("flight_capture_interval_violation") is False
        and flight.get("flight_capture_interval_qualified") is True,
        "no_qgc_visual_claim": safety.get("qgc_fly_view_visual_acceptance") == "not_claimed",
        "no_real_hardware": safety.get("real_pixhawk_contacted") is False
        and safety.get("real_jetson_contacted") is False
        and safety.get("real_camera_contacted") is False
        and safety.get("hardware_control_enabled") is False
        and safety.get("product_runtime_flight_control_enabled") is False
        and safety.get("product_runtime_actuator_output_enabled") is False,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VtolMissionSensorCaptureError(
            "completed VTOL mission evidence checks failed: " + ", ".join(failed)
        )
    return checks


def validate_mission_journal(
    path: Path,
    mission_payload: Mapping[str, Any],
    *,
    transaction_id: str,
    committed_protocol_sha256: str,
) -> dict[str, bool]:
    """Reopen and validate the durable hash-chained mission transaction journal."""

    expected_transaction_id = str(transaction_id).strip()
    if not expected_transaction_id:
        raise VtolMissionSensorCaptureError("mission journal requires a transaction ID")
    expected_protocol_hash = str(committed_protocol_sha256)
    if not _valid_sha256(expected_protocol_hash):
        raise VtolMissionSensorCaptureError("mission journal requires a committed protocol SHA-256")

    claim = _mapping(mission_payload.get("mission_journal"))
    documents = _mapping(mission_payload.get("documents"))
    protocol = _mapping(mission_payload.get("protocol"))
    transaction = _mapping(mission_payload.get("transaction"))
    document_bindings = _mapping(documents.get("bindings"))
    document_records = {
        name: _mapping(documents.get(name))
        for name in (
            "prompt",
            "task_spec",
            "context_snapshot",
            "platform_neutral_plan",
            "executable_mission",
            "validation_report",
            "operator_grant",
        )
    }
    document_payloads = {
        name: _mapping(record.get("document")) for name, record in document_records.items()
    }
    document_hashes = {name: record.get("sha256") for name, record in document_records.items()}
    journal: DurableMissionJournal | None = None
    try:
        resolved = _absolute_unsymlinked(path, label="mission_journal_path")
        if not resolved.is_file() or path.is_symlink():
            raise VtolMissionSensorCaptureError(
                f"mission journal is not a regular unsymlinked file: {resolved}"
            )
        file_sha256 = _sha256_path(resolved)
        journal = DurableMissionJournal.open(resolved)
        recovery = journal.recover()
        recovery_mapping = recovery.to_mapping()
        resume_plan = recovery.resume_plan.to_mapping()
        records = recovery.records
        last_record = records[-1] if records else None
        identity = recovery.identity
        identity_bindings = {
            "prompt_hash": identity.bindings.prompt_hash,
            "task_spec_hash": identity.bindings.task_spec_hash,
            "plan_hash": identity.bindings.plan_hash,
            "context_snapshot_hash": identity.bindings.context_snapshot_hash,
            "airframe_profile_hash": identity.bindings.airframe_profile_hash,
            "geofence_hash": identity.bindings.geofence_hash,
            "vehicle_identity_hash": identity.bindings.vehicle_identity_hash,
            "runtime_descriptor_hash": identity.bindings.runtime_descriptor_hash,
            "runtime_provenance_hash": identity.bindings.runtime_provenance_hash,
        }
        canonical_document_hashes = {
            name: (
                bool(document_payloads[name])
                and _valid_sha256(document_hashes[name])
                and canonical_sha256(document_payloads[name]) == document_hashes[name]
            )
            for name in document_records
        }
        executable_document = document_payloads["executable_mission"]
        validation_document = document_payloads["validation_report"]
        grant_document = document_payloads["operator_grant"]
        executable_items = _sequence(executable_document.get("items"))
        protocol_item_count = _strict_nonnegative_int(protocol.get("mission_item_count"))
        raw_wire_items = _sequence(protocol.get("wire_items"))
        parsed_wire_items: tuple[Px4MissionItemInt, ...] = ()
        recomputed_wire_hash: str | None = None
        try:
            parsed_wire_items = tuple(
                Px4MissionItemInt.from_mapping(item) for item in raw_wire_items
            )
            recomputed_wire_hash = px4_wire_hash(parsed_wire_items)
        except (Px4MissionError, TypeError, ValueError):
            # Keep malformed or missing serialized mission items inside the
            # named fail-closed check below instead of accepting the three
            # self-asserted protocol hash fields as sufficient evidence.
            parsed_wire_items = ()
            recomputed_wire_hash = None
        grant_binding_fields = {
            "prompt_hash": identity.bindings.prompt_hash,
            "task_spec_hash": identity.bindings.task_spec_hash,
            "plan_hash": identity.bindings.plan_hash,
            "executable_mission_hash": identity.executable_mission_hash,
            "validation_report_hash": identity.validation_report_hash,
            "context_snapshot_hash": identity.bindings.context_snapshot_hash,
            "airframe_profile_hash": identity.bindings.airframe_profile_hash,
            "geofence_hash": identity.bindings.geofence_hash,
            "vehicle_identity_hash": identity.bindings.vehicle_identity_hash,
            "runtime_descriptor_hash": identity.bindings.runtime_descriptor_hash,
            "runtime_provenance_hash": identity.bindings.runtime_provenance_hash,
        }
        checks = {
            "exact_path": claim.get("path") == str(resolved)
            and recovery.path == resolved
            and claim.get("exists") is True,
            "file_sha256": _valid_sha256(file_sha256) and claim.get("file_sha256") == file_sha256,
            "hash_chain_verified": bool(records),
            "transaction_id": identity.transaction_id == expected_transaction_id
            and transaction.get("id") == expected_transaction_id
            and all(record.transaction_id == expected_transaction_id for record in records),
            "committed_protocol_sha256": identity.protocol_hash == expected_protocol_hash
            and protocol.get("committed_protocol_sha256") == expected_protocol_hash
            and protocol.get("uploaded_protocol_sha256") == expected_protocol_hash
            and protocol.get("readback_protocol_sha256") == expected_protocol_hash,
            "identity_hash": claim.get("identity_hash") == identity.canonical_hash
            and recovery_mapping.get("identity_hash") == identity.canonical_hash,
            "canonical_document_hashes": all(canonical_document_hashes.values()),
            "identity_bindings": set(document_bindings) == set(identity_bindings)
            and dict(document_bindings) == identity_bindings
            and document_hashes["prompt"] == identity.bindings.prompt_hash
            and document_hashes["task_spec"] == identity.bindings.task_spec_hash
            and document_hashes["platform_neutral_plan"] == identity.bindings.plan_hash
            and document_hashes["context_snapshot"] == identity.bindings.context_snapshot_hash,
            "executable_mission_identity": document_hashes["executable_mission"]
            == identity.executable_mission_hash
            and executable_document.get("platform_plan_hash") == identity.bindings.plan_hash
            and executable_document.get("airframe_profile_hash")
            == identity.bindings.airframe_profile_hash
            and executable_document.get("airframe_adapter_id") == identity.airframe_adapter_id,
            "validation_report_identity": document_hashes["validation_report"]
            == identity.validation_report_hash
            and validation_document.get("executable_mission_hash")
            == identity.executable_mission_hash,
            "operator_grant_identity": grant_document.get("transaction_id")
            == expected_transaction_id
            and all(
                grant_document.get(name) == value for name, value in grant_binding_fields.items()
            ),
            "mission_item_count": protocol_item_count == identity.mission_item_count
            and len(executable_items) == identity.mission_item_count,
            "serialized_wire_items": protocol_item_count == identity.mission_item_count
            and len(raw_wire_items) == identity.mission_item_count
            and len(parsed_wire_items) == identity.mission_item_count
            and recomputed_wire_hash == identity.protocol_hash
            and recomputed_wire_hash == expected_protocol_hash,
            "airframe_adapter_id": isinstance(identity.airframe_adapter_id, str)
            and bool(identity.airframe_adapter_id.strip())
            and executable_document.get("airframe_adapter_id") == identity.airframe_adapter_id,
            "terminal_state": recovery.state is MissionTransactionState.START_PERMITTED
            and claim.get("state") == MissionTransactionState.START_PERMITTED.value,
            "record_count": len(records) > 0
            and claim.get("record_count") == len(records)
            and recovery_mapping.get("record_count") == len(records),
            "requested_sequences": claim.get("requested_sequences")
            == list(recovery.requested_sequences),
            "no_recovery_required": recovery.recovery_required is False
            and recovery.recovery_reason is None
            and claim.get("recovery_required") is False
            and claim.get("recovery_reason") is None,
            "no_repair_event": all(
                record.event_type != "journal_recovery_acknowledged" for record in records
            ),
            "terminal_record": last_record is not None
            and last_record.record_type == "event"
            and last_record.event_type == "start_permitted"
            and last_record.to_state is MissionTransactionState.START_PERMITTED
            and _valid_sha256(last_record.record_hash),
            "resume_plan": claim.get("resume_plan") == resume_plan
            and resume_plan.get("state") == MissionTransactionState.START_PERMITTED.value
            and resume_plan.get("next_action") == "start_permit_already_issued"
            and resume_plan.get("requires_operator_action") is True
            and resume_plan.get("auto_start_allowed") is False
            and resume_plan.get("requires_readback") is False
            and resume_plan.get("recovery_required") is False,
        }
    except (OSError, RuntimeError, MissionJournalError, ValueError) as exc:
        if isinstance(exc, VtolMissionSensorCaptureError):
            raise
        raise VtolMissionSensorCaptureError(
            f"mission journal validation failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if journal is not None:
            journal.close()

    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VtolMissionSensorCaptureError("mission journal checks failed: " + ", ".join(failed))
    return checks


def validate_sensor_capture(
    payload: Mapping[str, Any],
    *,
    frame_count: int,
    frame_directory: Path,
    scenario_id: str,
    scenario_manifest_hash: str,
    expected_width: int,
    expected_height: int,
    flight_ready_monotonic_s: float,
    capture_started_monotonic_s: float,
    capture_completed_monotonic_s: float,
) -> dict[str, bool]:
    """Verify the persisted wire-v2 frame set, not just in-memory claims."""

    persistence = _mapping(payload.get("pixel_persistence"))
    transport = _mapping(payload.get("transport"))
    manifest_record = _mapping(persistence.get("manifest"))
    frames = _sequence(payload.get("frames"))
    persisted_frames = _sequence(persistence.get("files"))
    expected_payload_bytes = expected_width * expected_height * 3
    expected_directory = frame_directory.resolve()
    expected_manifest = expected_directory / GAZEBO_BGR24_FRAME_SET_MANIFEST
    sensor_binding_hash = payload.get("sensor_binding_hash")

    output_directory_bound = False
    manifest_path_bound = False
    manifest_file_integrity = False
    manifest_canonical_integrity = False
    manifest_content_bound = False
    exact_directory_contents = False
    frame_files_integrity = False
    frame_records_bound = False
    frame_provenance_bound = False
    frame_timestamps_monotonic = False
    manifest_payload: Mapping[str, Any] = {}

    try:
        claimed_output = Path(str(persistence.get("output_directory"))).resolve(strict=True)
        output_directory_bound = (
            claimed_output == expected_directory
            and expected_directory.is_dir()
            and not expected_directory.is_symlink()
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        output_directory_bound = False

    try:
        claimed_manifest = Path(str(manifest_record.get("path"))).resolve(strict=True)
        resolved_manifest = expected_manifest.resolve(strict=True)
        manifest_path_bound = (
            claimed_manifest == resolved_manifest
            and resolved_manifest.parent == expected_directory
            and manifest_record.get("file") == GAZEBO_BGR24_FRAME_SET_MANIFEST
            and resolved_manifest.is_file()
            and not resolved_manifest.is_symlink()
        )
        if manifest_path_bound:
            manifest_bytes = resolved_manifest.stat().st_size
            manifest_file_hash = _sha256_path(resolved_manifest)
            manifest_file_integrity = (
                _strict_nonnegative_int(manifest_record.get("bytes")) == manifest_bytes
                and manifest_bytes > 0
                and manifest_record.get("file_sha256") == manifest_file_hash
                and _valid_sha256(manifest_file_hash)
            )
            loaded = json.loads(resolved_manifest.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                manifest_payload = loaded
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        manifest_path_bound = False

    if manifest_payload:
        manifest_without_hash = dict(manifest_payload)
        declared_manifest_hash = manifest_without_hash.pop("manifest_sha256", None)
        manifest_canonical_integrity = (
            _valid_sha256(declared_manifest_hash)
            and declared_manifest_hash == manifest_record.get("manifest_sha256")
            and canonical_sha256(manifest_without_hash) == declared_manifest_hash
        )
        source_attribution = _mapping(manifest_payload.get("source_attribution"))
        claims = _mapping(manifest_payload.get("claims"))
        manifest_content_bound = (
            manifest_payload.get("schema") == "multidetect.gazebo-bgr24-frame-set"
            and manifest_payload.get("schema_version") == 1
            and manifest_payload.get("scenario_id") == scenario_id
            and manifest_payload.get("scenario_manifest_hash") == scenario_manifest_hash
            and manifest_payload.get("sensor_binding_hash") == sensor_binding_hash
            and manifest_payload.get("wire_version") == 2
            and manifest_payload.get("pixel_format") == "BGR24"
            and manifest_payload.get("frame_count") == frame_count
            and manifest_payload.get("total_payload_bytes") == frame_count * expected_payload_bytes
            and _sequence(manifest_payload.get("frames")) == persisted_frames
            and source_attribution.get("pixels") == "actual_gazebo_camera_sensor_output"
            and source_attribution.get("environment") == "software_simulation"
            and source_attribution.get("wire") == "MDGZSIM2"
            and source_attribution.get("source_row_order") == GAZEBO_SIM_SOURCE_ROW_ORDER
            and source_attribution.get("wire_row_order") == GAZEBO_SIM_WIRE_ROW_ORDER
            and source_attribution.get("vertical_flip_applied") is GAZEBO_SIM_VERTICAL_FLIP_APPLIED
            and source_attribution.get("truth_loaded_by_runtime") is False
            and claims.get("actual_gazebo_pixels_persisted") is True
            and claims.get("qgc_rendering") == "not_claimed"
            and claims.get("manual_visual_acceptance") == "not_claimed"
            and claims.get("hardware") == "not_claimed"
            and claims.get("detector_evaluation") == "not_claimed"
            and claims.get("production_model_approval") == "not_claimed"
        )

    expected_names = {
        GAZEBO_BGR24_FRAME_SET_MANIFEST,
        *(f"frame-{index:06d}.bgr24" for index in range(frame_count)),
    }
    try:
        entries = list(expected_directory.iterdir())
        exact_directory_contents = {entry.name for entry in entries} == expected_names and all(
            not entry.is_symlink() for entry in entries
        )
    except OSError:
        exact_directory_contents = False

    file_checks: list[bool] = []
    record_checks: list[bool] = []
    provenance_checks: list[bool] = []
    captured_times: list[float] = []
    sensor_stamps: list[int] = []
    if len(frames) == frame_count and len(persisted_frames) == frame_count:
        for index, (raw_sensor_frame, raw_persisted_frame) in enumerate(
            zip(frames, persisted_frames, strict=True)
        ):
            sensor_frame = _mapping(raw_sensor_frame)
            persisted_frame = _mapping(raw_persisted_frame)
            provenance = _mapping(persisted_frame.get("provenance"))
            expected_name = f"frame-{index:06d}.bgr24"
            frame_path = expected_directory / expected_name
            payload_hash = persisted_frame.get("sha256")
            record_hash = persisted_frame.get("wire_record_sha256")
            try:
                file_checks.append(
                    frame_path.is_file()
                    and not frame_path.is_symlink()
                    and frame_path.stat().st_size == expected_payload_bytes
                    and _valid_sha256(payload_hash)
                    and _sha256_path(frame_path) == payload_hash
                )
            except OSError:
                file_checks.append(False)
            record_checks.append(
                persisted_frame.get("sequence") == index
                and persisted_frame.get("file") == expected_name
                and persisted_frame.get("bytes") == expected_payload_bytes
                and persisted_frame.get("wire_payload_sha256") == payload_hash
                and persisted_frame.get("raw_bytes_sha256_matches_wire_payload") is True
                and _valid_sha256(record_hash)
                and sensor_frame.get("sequence") == index
                and sensor_frame.get("frame_id") == persisted_frame.get("frame_id")
                and sensor_frame.get("width") == expected_width
                and sensor_frame.get("height") == expected_height
                and sensor_frame.get("stride") == expected_width * 3
                and tuple(_sequence(sensor_frame.get("bgr_shape")))
                == (expected_height, expected_width, 3)
                and sensor_frame.get("payload_bytes") == expected_payload_bytes
                and sensor_frame.get("payload_sha256") == payload_hash
                and sensor_frame.get("record_sha256") == record_hash
                and sensor_frame.get("persisted") is True
                and sensor_frame.get("persisted_file") == expected_name
                and sensor_frame.get("persisted_bytes") == expected_payload_bytes
                and sensor_frame.get("persisted_sha256") == payload_hash
                and sensor_frame.get("raw_bytes_sha256_matches_wire_payload") is True
            )
            captured_at = _finite_float(provenance.get("captured_at_s"))
            sensor_stamp = _strict_nonnegative_int(provenance.get("sensor_stamp_ns"))
            if captured_at is not None:
                captured_times.append(captured_at)
            if sensor_stamp is not None:
                sensor_stamps.append(sensor_stamp)
            provenance_checks.append(
                provenance.get("captured_at_clock") == "host_monotonic"
                and captured_at is not None
                and captured_at > 0.0
                and sensor_stamp is not None
                and sensor_stamp > 0
                and provenance.get("sequence") == index
                and provenance.get("frame_id") == persisted_frame.get("frame_id")
                and provenance.get("width") == expected_width
                and provenance.get("height") == expected_height
                and provenance.get("stride") == expected_width * 3
                and provenance.get("pixel_format") == "BGR24"
                and provenance.get("payload_bytes") == expected_payload_bytes
                and provenance.get("payload_sha256") == payload_hash
                and provenance.get("record_sha256") == record_hash
            )
    frame_files_integrity = len(file_checks) == frame_count and all(file_checks)
    frame_records_bound = len(record_checks) == frame_count and all(record_checks)
    frame_provenance_bound = len(provenance_checks) == frame_count and all(provenance_checks)
    frame_timestamps_monotonic = (
        len(captured_times) == frame_count
        and captured_times == sorted(captured_times)
        and len(set(captured_times)) == frame_count
        and len(sensor_stamps) == frame_count
        and sensor_stamps == sorted(sensor_stamps)
        and len(set(sensor_stamps)) == frame_count
    )

    ready_time = _finite_float(flight_ready_monotonic_s)
    started_time = _finite_float(capture_started_monotonic_s)
    completed_time = _finite_float(capture_completed_monotonic_s)
    checks = {
        "qualified": payload.get("qualified") is True,
        "wire_v2": payload.get("wire_version") == 2,
        "row_order_normalized": payload.get("source_row_order") == GAZEBO_SIM_SOURCE_ROW_ORDER
        and payload.get("wire_row_order") == GAZEBO_SIM_WIRE_ROW_ORDER
        and payload.get("vertical_flip_applied") is GAZEBO_SIM_VERTICAL_FLIP_APPLIED
        and transport.get("source_row_order") == GAZEBO_SIM_SOURCE_ROW_ORDER
        and transport.get("wire_row_order") == GAZEBO_SIM_WIRE_ROW_ORDER
        and transport.get("vertical_flip_applied") is GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
        "truth_free": payload.get("truth_free") is True,
        "full_odometry": payload.get("full_odometry_pose_velocity_fusion_claimed") is True,
        "frame_count": payload.get("requested_frame_count") == frame_count
        and len(frames) == frame_count,
        "capture_after_current_fw_barrier": ready_time is not None
        and started_time is not None
        and completed_time is not None
        and 0.0 <= started_time - ready_time <= MAXIMUM_FLIGHT_CAPTURE_START_DELAY_S
        and completed_time >= started_time,
        "sensor_binding": _valid_sha256(sensor_binding_hash)
        and transport.get("sensor_binding_sha256") == sensor_binding_hash,
        "transport_complete": transport.get("complete") is True
        and transport.get("helper_exit_code") == 0
        and transport.get("expected_frame_count") == frame_count
        and transport.get("received_frame_count") == frame_count,
        "pixels_persisted": persistence.get("requested") is True
        and persistence.get("status") == "passed"
        and persistence.get("persisted_frame_count") == frame_count
        and persistence.get("total_payload_bytes") == frame_count * expected_payload_bytes
        and persistence.get("actual_gazebo_pixels_persisted") is True
        and persistence.get("raw_bytes_sha256_matches_wire_payload") is True,
        "output_directory_bound": output_directory_bound,
        "manifest_path_bound": manifest_path_bound,
        "manifest_file_integrity": manifest_file_integrity,
        "manifest_canonical_integrity": manifest_canonical_integrity,
        "manifest_content_bound": manifest_content_bound,
        "exact_directory_contents": exact_directory_contents,
        "frame_files_integrity": frame_files_integrity,
        "frame_records_bound": frame_records_bound,
        "frame_provenance_bound": frame_provenance_bound,
        "frame_timestamps_monotonic": frame_timestamps_monotonic,
        "frame_timestamps_within_capture_interval": started_time is not None
        and completed_time is not None
        and len(captured_times) == frame_count
        and all(started_time <= captured_at <= completed_time for captured_at in captured_times),
        "no_qgc_render_claim": persistence.get("qgc_rendering") == "not_claimed",
        "no_manual_visual_claim": persistence.get("manual_visual_acceptance") == "not_claimed",
        "no_hardware_claim": persistence.get("hardware") == "not_claimed",
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VtolMissionSensorCaptureError(
            "Gazebo sensor capture checks failed: " + ", ".join(failed)
        )
    return checks


def validate_router_result(payload: Mapping[str, Any], *, ports: CapturePorts) -> dict[str, bool]:
    barrier = _mapping(payload.get("ready_barrier"))
    endpoints = _mapping(payload.get("endpoints"))
    identity = _mapping(payload.get("evaluator_identity"))
    expected_endpoints = {
        "px4_telemetry_input": f"127.0.0.1:{ports.px4_telemetry}",
        "px4_gcs_input": f"127.0.0.1:{ports.px4_input}",
        "qgc": f"127.0.0.1:{ports.qgc_sink}",
        "qgc_router_return": f"127.0.0.1:{ports.qgc_router_return}",
        "evaluator": f"127.0.0.1:{ports.evaluator}",
        "evaluator_router_return": f"127.0.0.1:{ports.evaluator_router_return}",
    }
    px4_forwarded = _strict_nonnegative_int(payload.get("px4_frames_forwarded_to_evaluator"))
    mission_forwarded = _strict_nonnegative_int(payload.get("evaluator_mission_frames_forwarded"))
    control_forwarded = _strict_nonnegative_int(payload.get("evaluator_control_commands_forwarded"))
    checks = {
        "schema_version": payload.get("schema_version") == 2,
        "finished_event": payload.get("event") == "px4_gazebo_qgc_router_finished",
        "graceful_stop": payload.get("termination_reason") == "stop_event_set",
        "ready": barrier.get("reached") is True
        and barrier.get("all_udp_sockets_bound_before_ready") is True
        and barrier.get("expected_udp_socket_count") == 3
        and barrier.get("bound_udp_socket_count") == 3
        and barrier.get("ready_event_supplied") is True
        and barrier.get("ready_event_set") is True
        and barrier.get("stop_event_supplied") is True,
        "endpoints_bound": dict(endpoints) == expected_endpoints,
        "evaluator_identity_bound": identity.get("system_id") == EVALUATOR_SYSTEM_ID
        and identity.get("component_id") == EVALUATOR_COMPONENT_ID
        and payload.get("evaluator_identity_enforced") is True,
        "telemetry_forwarded": px4_forwarded is not None and px4_forwarded > 0,
        "mission_frames_forwarded": mission_forwarded is not None and mission_forwarded > 0,
        "control_forwarded": control_forwarded is not None and control_forwarded > 0,
        "no_discarded_bytes": _strict_nonnegative_int(payload.get("px4_discarded_bytes")) == 0
        and _strict_nonnegative_int(payload.get("qgc_discarded_bytes")) == 0
        and _strict_nonnegative_int(payload.get("evaluator_discarded_bytes")) == 0,
        "loopback": payload.get("all_udp_endpoints_loopback") is True,
        "ports_distinct": payload.get("all_ports_distinct") is True
        and len(set(ports.values)) == PORT_COUNT,
        "protected_port_unused": payload.get("protected_udp_14550_used") is False
        and PROTECTED_GROUND_STATION_PORT not in ports.values,
        "bounded_evaluator_policy": payload.get("acknowledge_owned_disposable_sitl") is True
        and payload.get("evaluator_bounded_mission_transaction_allowed") is True
        and payload.get("evaluator_mission_clear_all_allowed") is False
        and payload.get("evaluator_set_mode_restricted_to_px4_auto_mission") is True
        and payload.get("evaluator_arbitrary_commands_allowed") is False
        and payload.get("qgc_parameter_writes_allowed") is False
        and payload.get("qgc_flight_commands_allowed") is False
        and payload.get("qgc_mission_writes_allowed") is False,
        "software_only": payload.get("software_only") is True
        and payload.get("real_pixhawk_contacted") is False
        and payload.get("physical_actuator_outputs_present") is False
        and payload.get("payload_commands_allowed") is False,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VtolMissionSensorCaptureError(
            "evaluator router result checks failed: " + ", ".join(failed)
        )
    return checks


def _terminate_owned_process(process: Any | None) -> dict[str, object]:
    if process is None:
        return {
            "started": False,
            "terminated": False,
            "killed": False,
            "exit_code": None,
            "cleanup_succeeded": True,
            "errors": [],
        }
    errors: list[str] = []
    try:
        exit_code = process.poll()
    except Exception as exc:
        exit_code = None
        errors.append(f"poll failed: {type(exc).__name__}: {exc}")
    terminated = False
    killed = False
    if exit_code is None:
        try:
            process.terminate()
            terminated = True
        except Exception as exc:
            errors.append(f"terminate failed: {type(exc).__name__}: {exc}")
        try:
            exit_code = process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                killed = True
            except Exception as exc:
                errors.append(f"kill failed: {type(exc).__name__}: {exc}")
            try:
                exit_code = process.wait(timeout=5.0)
            except Exception as exc:
                errors.append(f"post-kill wait failed: {type(exc).__name__}: {exc}")
        except Exception as exc:
            errors.append(f"wait failed: {type(exc).__name__}: {exc}")
    return {
        "started": True,
        "terminated": terminated,
        "killed": killed,
        "exit_code": exit_code,
        "cleanup_succeeded": not errors and exit_code is not None,
        "errors": errors,
    }


def _cleanup_exact_container(
    *,
    docker: str,
    container_id: str | None,
    runner: Callable[..., object] | None,
) -> dict[str, object]:
    if container_id is None:
        return {
            "attempted": False,
            "exact_id": None,
            "stop_exit_code": None,
            "remove_exit_code": None,
            "post_remove_inspect_exit_code": None,
            "post_remove_absence_proven": False,
            "removed": False,
            "cleanup_succeeded": True,
            "errors": [],
        }
    errors: list[str] = []
    stop_code: int | None = None
    remove_code: int | None = None
    post_code: int | None = None
    post_remove_absence_proven = False
    try:
        stop_result = _run(
            docker,
            ["stop", "--timeout", "2", container_id],
            timeout_s=15.0,
            runner=runner,
        )
        stop_code = _result_returncode(stop_result)
        if stop_code != 0:
            errors.append(f"docker stop exact ID returned {stop_code}")
    except Exception as exc:
        errors.append(f"docker stop exact ID failed: {type(exc).__name__}: {exc}")
    try:
        remove_result = _run(
            docker,
            ["rm", container_id],
            timeout_s=15.0,
            runner=runner,
        )
        remove_code = _result_returncode(remove_result)
        if remove_code != 0:
            errors.append(f"docker rm exact ID returned {remove_code}")
    except Exception as exc:
        errors.append(f"docker rm exact ID failed: {type(exc).__name__}: {exc}")
    try:
        post = _run(
            docker,
            ["container", "inspect", container_id],
            timeout_s=10.0,
            runner=runner,
        )
        post_code = _result_returncode(post)
        post_detail = (_result_text(post, "stdout") + "\n" + _result_text(post, "stderr")).lower()
        post_remove_absence_proven = post_code == 1 and (
            "no such container" in post_detail or "no such object" in post_detail
        )
        if post_code != 0 and not post_remove_absence_proven:
            errors.append(
                f"post-remove inspect did not prove exact container absence (exit {post_code})"
            )
    except Exception as exc:
        errors.append(f"post-remove inspect exact ID failed: {type(exc).__name__}: {exc}")
    removed = remove_code == 0 and post_remove_absence_proven
    if post_code == 0:
        errors.append("exact container ID still exists after docker rm")
    return {
        "attempted": True,
        "exact_id": container_id,
        "stop_exit_code": stop_code,
        "remove_exit_code": remove_code,
        "post_remove_inspect_exit_code": post_code,
        "post_remove_absence_proven": post_remove_absence_proven,
        "removed": removed,
        "cleanup_succeeded": removed and not errors,
        "errors": errors,
    }


def _validate_runtime_container(
    *,
    record: Mapping[str, Any],
    plan: DockerSimulatorPlan,
    container_id: str,
) -> dict[str, object]:
    if record.get("Id") != container_id:
        raise VtolMissionSensorCaptureError("Docker inspect record did not match the exact run ID")
    common_checks = _validate_owned_container(dict(record), plan)
    mission_checks = validate_owned_container_record(
        record,
        container_name=plan.container_name,
        expected_container_id=container_id,
        expected_label=EXPECTED_CONTAINER_LABEL,
    )
    return {"docker_acceptance": common_checks, "vtol_mission": mission_checks}


def _base_evidence(
    *,
    paths: CapturePaths,
    scenario: Path,
    ports: CapturePorts,
    execute: bool,
) -> dict[str, Any]:
    return {
        "schema": "multidetect.vtol-mission-sensor-capture",
        "schema_version": 1,
        "generated_at_utc": _utc_now(),
        "release_status": RELEASE_STATUS,
        "gate_passed": False,
        "failure": None,
        "execution": {
            "requested": execute,
            "status": "not_started" if execute else "planned_not_executed",
            "lifecycle_owner": "scripts/run_vtol_mission_sensor_capture.py",
            "headless": True,
            "qgc_process_started": False,
            "computer_use_invoked": False,
        },
        "scenario": {
            "manifest": str(scenario),
            "manifest_sha256": _sha256_path(scenario),
            "scenario_id_expected": EXPECTED_SCENARIO_ID,
            "runtime_channel_only": True,
            "truth_loaded": False,
            "expectations_loaded": False,
        },
        "paths": paths.to_mapping(),
        "ports": ports.to_mapping(),
        "docker_plan": None,
        "runtime_identity": None,
        "container": {
            "started": False,
            "id": None,
            "ownership_checks": None,
            "cleanup": None,
        },
        "router": None,
        "flight_capture_ready": None,
        "sensor_capture": None,
        "mission": None,
        "mission_journal": None,
        "final_integrity": None,
        "gates": {
            "docker_plan": False,
            "container_identity": False,
            "router_ready": False,
            "flight_capture_ready": False,
            "flight_capture_complete_marker": False,
            "flight_capture_interval": False,
            "same_container_sensor_capture": False,
            "wire_v2_pixels": False,
            "complete_vtol_mission": False,
            "mission_journal_integrity": False,
            "router_graceful_stop": False,
            "exact_id_cleanup": False,
            "final_artifact_integrity": False,
        },
        "safety": {
            "software_only": True,
            "sim_only": True,
            "qgc_started": False,
            "qgc_fly_view_visual_acceptance": "not_claimed",
            "operator_visual_acceptance": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "real_pixhawk_contacted": False,
            "real_jetson_contacted": False,
            "real_camera_contacted": False,
            "hardware": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
            "outdoor_gnss": "not_claimed",
            "ground_truth_used_for_runtime_or_capture": False,
        },
    }


def run_vtol_mission_sensor_capture(
    scenario: Path = DEFAULT_SCENARIO,
    evidence_path: Path = DEFAULT_EVIDENCE_PATH,
    *,
    run_directory: Path = DEFAULT_RUN_DIRECTORY,
    flight_ready_file: Path | None = None,
    flight_capture_complete_file: Path | None = None,
    mission_journal_path: Path | None = None,
    frame_output_directory: Path | None = None,
    container_name: str = DEFAULT_CONTAINER_NAME,
    execute: bool = False,
    frame_count: int = DEFAULT_FRAME_COUNT,
    frame_width: int = DEFAULT_FRAME_WIDTH,
    frame_height: int = DEFAULT_FRAME_HEIGHT,
    sensor_timeout_s: float = DEFAULT_SENSOR_TIMEOUT_S,
    mission_timeout_s: float = DEFAULT_MISSION_TIMEOUT_S,
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    router_ready_timeout_s: float = DEFAULT_ROUTER_READY_TIMEOUT_S,
    ports: CapturePorts | None = None,
    port_probe: Callable[[int], bool] = _is_unused_loopback_port,
    docker: str | None = None,
    command_runner: Callable[..., object] | None = None,
    process_factory: Callable[..., Any] = subprocess.Popen,
    router_runner: Callable[..., dict[str, object]] = run_router,
    sensor_capture: Callable[..., dict[str, object]] = _capture_gazebo_sensor_transport,
) -> dict[str, Any]:
    """Run or plan the headless, same-container VTOL sensor acceptance."""

    source = scenario.resolve(strict=True)
    if source.name != DEFAULT_SCENARIO.name:
        raise VtolMissionSensorCaptureError(
            "this first flight-pixel gate is bound to river_rescue_vtol_001"
        )
    if isinstance(frame_count, bool) or not 1 <= frame_count <= 32:
        raise VtolMissionSensorCaptureError("frame_count must be in 1..32")
    if not 1 <= frame_width <= 16_384 or not 1 <= frame_height <= 16_384:
        raise VtolMissionSensorCaptureError("frame dimensions must be in 1..16384")
    if not 1.0 <= sensor_timeout_s <= 300.0:
        raise VtolMissionSensorCaptureError("sensor_timeout_s must be in 1..300")
    if not 10.0 <= mission_timeout_s <= 600.0:
        raise VtolMissionSensorCaptureError("mission_timeout_s must be in 10..600")
    if not 1.0 <= ready_timeout_s <= mission_timeout_s:
        raise VtolMissionSensorCaptureError(
            "ready_timeout_s must be positive and no greater than mission_timeout_s"
        )
    if not 0.1 <= router_ready_timeout_s <= 60.0:
        raise VtolMissionSensorCaptureError("router_ready_timeout_s must be in 0.1..60")

    selected_ports = (
        allocate_capture_ports(port_probe=port_probe)
        if ports is None
        else validate_capture_ports(ports, port_probe=port_probe)
    )
    paths = prepare_capture_paths(
        run_directory,
        evidence_path,
        flight_ready_file=flight_ready_file,
        flight_capture_complete_file=flight_capture_complete_file,
        mission_journal_path=mission_journal_path,
        frame_output_directory=frame_output_directory,
    )
    evidence = _base_evidence(
        paths=paths,
        scenario=source,
        ports=selected_ports,
        execute=execute,
    )
    container_id: str | None = None
    router_handle: _RouterThread | None = None
    mission_process: Any | None = None
    mission_stdout: Any | None = None
    mission_stderr: Any | None = None
    runtime_failure: BaseException | None = None
    docker_executable = docker or "docker"
    captured: Mapping[str, Any] | None = None
    sensor_checks: dict[str, bool] | None = None
    flight_ready: dict[str, Any] | None = None
    flight_checks: dict[str, bool] | None = None
    capture_started_monotonic_s: float | None = None
    capture_completed_monotonic_s: float | None = None
    completion_payload: dict[str, object] | None = None
    completion_file_sha256: str | None = None
    mission_payload: dict[str, Any] | None = None
    mission_checks: dict[str, bool] | None = None
    journal_checks: dict[str, bool] | None = None
    descriptor_hash: str | None = None
    airframe_profile_hash: str | None = None
    provenance_hash: str | None = None

    try:
        resolved_docker = _resolve_docker_path(docker)
        if execute and resolved_docker is None:
            raise VtolMissionSensorCaptureError(
                "Docker CLI is unavailable for an execution request"
            )
        if resolved_docker is not None:
            docker_executable = resolved_docker
        plan = build_docker_simulator_plan(
            source,
            repository_root=ROOT,
            job_dir=paths.docker_job_directory,
            container_name=container_name,
            control_port=selected_ports.px4_input,
            telemetry_port=selected_ports.px4_telemetry,
            image=PINNED_IMAGE,
            dry_run=not execute,
        )
        plan_checks = validate_capture_plan(
            plan,
            ports=selected_ports,
            container_name=container_name,
        )
        evidence["docker_plan"] = {
            "document": plan.to_mapping(),
            "sha256": plan.evidence_hash,
            "checks": plan_checks,
        }
        evidence["gates"]["docker_plan"] = True

        descriptor_document = {
            "schema": "multidetect.vtol-flight-capture-runtime-descriptor",
            "schema_version": 1,
            "runtime_mode": "SIM",
            "scenario_id": plan.scenario_id,
            "scenario_manifest_sha256": _sha256_path(source),
            "stack_plan_sha256": plan.stack_plan_sha256,
            "docker_plan_sha256": plan.evidence_hash,
            "ports": selected_ports.to_mapping(),
            "headless": True,
            "qgc_process_started": False,
        }
        descriptor_hash = canonical_sha256(descriptor_document)
        airframe_profile_document = {
            "schema_version": 1,
            "airframe_profile_id": plan.airframe_profile_id,
            "vehicle_model": plan.vehicle_model,
            "model_name": plan.model_name,
            "stack_plan_sha256": plan.stack_plan_sha256,
        }
        airframe_profile_hash = canonical_sha256(airframe_profile_document)

        if not execute:
            evidence["runtime_identity"] = {
                "descriptor": descriptor_document,
                "descriptor_sha256": descriptor_hash,
                "airframe_profile": airframe_profile_document,
                "airframe_profile_sha256": airframe_profile_hash,
                "provenance": None,
                "provenance_sha256": None,
            }
            evidence["execution"]["status"] = "planned_not_executed"
        else:
            existing = _run(
                docker_executable,
                ["container", "inspect", container_name],
                timeout_s=10.0,
                runner=command_runner,
            )
            if _result_returncode(existing) == 0:
                raise VtolMissionSensorCaptureError(
                    f"container already exists; refusing replacement: {container_name}"
                )
            _run(
                docker_executable,
                ["image", "inspect", plan.image],
                timeout_s=30.0,
                check=True,
                runner=command_runner,
            )
            started = _run(
                docker_executable,
                list(plan.command[1:]),
                timeout_s=30.0,
                check=True,
                runner=command_runner,
            )
            container_id = _result_text(started, "stdout").strip().splitlines()[-1].strip()
            if _DOCKER_ID_RE.fullmatch(container_id) is None:
                raise VtolMissionSensorCaptureError(
                    "docker run did not return one exact 64-character lowercase container ID"
                )
            evidence["container"]["started"] = True
            evidence["container"]["id"] = container_id
            record = _container_inspect(
                docker_executable,
                container_id,
                runner=command_runner,
            )
            ownership_checks = _validate_runtime_container(
                record=record,
                plan=plan,
                container_id=container_id,
            )
            evidence["container"]["ownership_checks"] = ownership_checks
            evidence["gates"]["container_identity"] = True

            provenance_document = {
                "schema": "multidetect.vtol-flight-capture-runtime-provenance",
                "schema_version": 1,
                "container_id": container_id,
                "container_name": plan.container_name,
                "container_label": EXPECTED_CONTAINER_LABEL,
                "image": plan.image,
                "docker_plan_sha256": plan.evidence_hash,
                "scenario_id": plan.scenario_id,
                "stack_plan_sha256": plan.stack_plan_sha256,
                "ownership_checks": ownership_checks,
                "lifecycle_owner": "scripts/run_vtol_mission_sensor_capture.py",
                "headless": True,
            }
            provenance_hash = canonical_sha256(provenance_document)
            evidence["runtime_identity"] = {
                "descriptor": descriptor_document,
                "descriptor_sha256": descriptor_hash,
                "airframe_profile": airframe_profile_document,
                "airframe_profile_sha256": airframe_profile_hash,
                "provenance": provenance_document,
                "provenance_sha256": provenance_hash,
            }

            router_handle = _start_router_thread(
                ports=selected_ports,
                mission_timeout_s=mission_timeout_s,
                router_runner=router_runner,
            )
            _wait_router_ready(router_handle, router_ready_timeout_s)
            evidence["gates"]["router_ready"] = True

            mission_arguments = _process_arguments(
                paths=paths,
                scenario=source,
                container_name=plan.container_name,
                container_id=container_id,
                evaluator_port=selected_ports.evaluator,
                airframe_profile_sha256=airframe_profile_hash,
                runtime_descriptor_sha256=descriptor_hash,
                runtime_provenance_sha256=provenance_hash,
                mission_timeout_s=mission_timeout_s,
                docker_executable=docker_executable,
            )
            mission_process, mission_stdout, mission_stderr = _start_mission_process(
                mission_arguments,
                paths=paths,
                process_factory=process_factory,
            )
            flight_ready, flight_checks = _wait_for_flight_ready(
                path=paths.flight_ready,
                process=mission_process,
                router=router_handle,
                container_id=container_id,
                timeout_s=ready_timeout_s,
            )
            evidence["flight_capture_ready"] = {
                "document": flight_ready,
                "file_sha256": _sha256_path(paths.flight_ready),
                "checks": flight_checks,
            }
            evidence["gates"]["flight_capture_ready"] = True

            if mission_process.poll() is not None:
                raise VtolMissionSensorCaptureError(
                    "mission helper exited between flight barrier and sensor capture"
                )
            capture_record = _container_inspect(
                docker_executable,
                container_id,
                runner=command_runner,
            )
            _validate_runtime_container(
                record=capture_record,
                plan=plan,
                container_id=container_id,
            )
            # Docker accepts either a name or an immutable container ID as its
            # exec target. Use the exact inspected ID here so a later name
            # alias cannot redirect pixel capture to a different container.
            capture_plan = replace(plan, container_name=container_id)
            capture_started_monotonic_s = time.monotonic()
            captured = sensor_capture(
                plan=capture_plan,
                manifest_path=source,
                docker_executable=docker_executable,
                frame_count=frame_count,
                expected_width=frame_width,
                expected_height=frame_height,
                timeout_s=sensor_timeout_s,
                frame_output_directory=paths.frame_directory,
            )
            capture_completed_monotonic_s = time.monotonic()
            sensor_checks = validate_sensor_capture(
                captured,
                frame_count=frame_count,
                frame_directory=paths.frame_directory,
                scenario_id=plan.scenario_id,
                scenario_manifest_hash=plan.scenario_manifest_hash,
                expected_width=frame_width,
                expected_height=frame_height,
                flight_ready_monotonic_s=float(flight_ready["captured_at_monotonic_s"]),
                capture_started_monotonic_s=capture_started_monotonic_s,
                capture_completed_monotonic_s=capture_completed_monotonic_s,
            )
            persistence = _mapping(captured.get("pixel_persistence"))
            frame_manifest = _mapping(persistence.get("manifest"))
            completion_payload = {
                "schema": "multidetect.vtol-sensor-capture-complete",
                "schema_version": 1,
                "generated_at_utc": _utc_now(),
                "clock": "host_monotonic",
                "capture_started_at_monotonic_s": round(capture_started_monotonic_s, 6),
                "capture_completed_at_monotonic_s": round(capture_completed_monotonic_s, 6),
                "container_id": container_id,
                "transaction_id": _mapping(flight_ready.get("transaction")).get("id"),
                "committed_protocol_sha256": _mapping(flight_ready.get("transaction")).get(
                    "committed_protocol_sha256"
                ),
                "scenario_id": plan.scenario_id,
                "scenario_manifest_hash": plan.scenario_manifest_hash,
                "frame_count": frame_count,
                "sensor_binding_hash": captured.get("sensor_binding_hash"),
                "frame_set_manifest_sha256": frame_manifest.get("manifest_sha256"),
                "ground_truth_used_for_runtime_or_capture": False,
            }
            _atomic_write_new_json(paths.flight_capture_complete, completion_payload)
            completion_file_sha256 = _sha256_path(paths.flight_capture_complete)
            exact_capture_target = (
                capture_plan.container_name == container_id
                and _DOCKER_ID_RE.fullmatch(capture_plan.container_name) is not None
                and flight_ready.get("container", {}).get("id") == container_id
            )
            if not exact_capture_target:
                raise VtolMissionSensorCaptureError(
                    "sensor capture was not bound to the exact flight container ID"
                )
            evidence["sensor_capture"] = {
                "document": captured,
                "checks": sensor_checks,
                "container_id_before_capture": container_id,
                "docker_exec_target": capture_plan.container_name,
                "docker_exec_target_is_exact_id": True,
                "same_container_as_flight_barrier": True,
                "timing": {
                    "clock": "host_monotonic",
                    "flight_ready_at_s": flight_ready["captured_at_monotonic_s"],
                    "capture_started_at_s": round(capture_started_monotonic_s, 6),
                    "capture_completed_at_s": round(capture_completed_monotonic_s, 6),
                    "start_delay_s": round(
                        capture_started_monotonic_s
                        - float(flight_ready["captured_at_monotonic_s"]),
                        6,
                    ),
                    "maximum_start_delay_s": MAXIMUM_FLIGHT_CAPTURE_START_DELAY_S,
                    "per_frame_current_vtol_state_claimed": False,
                    "claim": "capture_started_after_current_fixed_wing_barrier",
                },
                "completion_marker": {
                    "path": str(paths.flight_capture_complete),
                    "file_sha256": completion_file_sha256,
                    "document": completion_payload,
                },
            }
            evidence["gates"]["flight_capture_complete_marker"] = True
            evidence["gates"]["same_container_sensor_capture"] = True
            evidence["gates"]["wire_v2_pixels"] = True

            mission_exit = mission_process.wait(timeout=mission_timeout_s + 30.0)
            if mission_exit != 0:
                raise VtolMissionSensorCaptureError(
                    f"VTOL mission helper returned non-zero after capture: {mission_exit}"
                )
            mission_stdout.close()
            mission_stderr.close()
            mission_stdout = None
            mission_stderr = None
            mission_payload = _read_json_object(
                paths.mission_evidence,
                label="VTOL mission evidence",
            )
            mission_checks = validate_mission_evidence(
                mission_payload,
                container_id=container_id,
                scenario_manifest_sha256=_sha256_path(source),
                airframe_profile_hash=airframe_profile_hash,
                descriptor_hash=descriptor_hash,
                provenance_hash=provenance_hash,
                evaluator_port=selected_ports.evaluator,
                barrier=flight_ready,
                flight_ready_path=paths.flight_ready,
                flight_complete_path=paths.flight_capture_complete,
                flight_complete_file_sha256=completion_file_sha256,
                flight_complete_document=completion_payload,
            )
            ready_transaction = _mapping(flight_ready.get("transaction"))
            journal_checks = validate_mission_journal(
                paths.mission_journal,
                mission_payload,
                transaction_id=str(ready_transaction.get("id") or ""),
                committed_protocol_sha256=str(
                    ready_transaction.get("committed_protocol_sha256") or ""
                ),
            )
            evidence["mission"] = {
                "document": mission_payload,
                "file_sha256": _sha256_path(paths.mission_evidence),
                "stdout_sha256": _sha256_path(paths.mission_stdout),
                "stderr_sha256": _sha256_path(paths.mission_stderr),
                "checks": mission_checks,
                "exit_code": mission_exit,
            }
            evidence["mission_journal"] = {
                "path": str(paths.mission_journal),
                "file_sha256": _sha256_path(paths.mission_journal),
                "document": _mapping(mission_payload.get("mission_journal")),
                "checks": journal_checks,
            }
            evidence["gates"]["complete_vtol_mission"] = True
            evidence["gates"]["flight_capture_interval"] = mission_checks["flight_capture_interval"]
            evidence["gates"]["mission_journal_integrity"] = True
            evidence["execution"]["status"] = "runtime_complete_pending_cleanup"
    except Exception as exc:
        runtime_failure = exc
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
        evidence["execution"]["status"] = "failed"
    finally:
        cleanup_failures: list[str] = []
        try:
            process_cleanup = _terminate_owned_process(mission_process)
        except Exception as exc:  # defensive: cleanup must continue to Docker removal
            cleanup_failures.append(f"mission process cleanup: {type(exc).__name__}: {exc}")
            process_cleanup = {
                "started": mission_process is not None,
                "terminated": False,
                "killed": False,
                "exit_code": None,
                "cleanup_succeeded": False,
                "errors": [cleanup_failures[-1]],
            }
        if process_cleanup.get("cleanup_succeeded") is not True:
            cleanup_failures.extend(str(item) for item in process_cleanup.get("errors", ()))
        for label, handle in (
            ("mission stdout", mission_stdout),
            ("mission stderr", mission_stderr),
        ):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception as exc:
                cleanup_failures.append(f"{label} close: {type(exc).__name__}: {exc}")
        try:
            router_record = _stop_router(router_handle)
        except Exception as exc:
            cleanup_failures.append(f"router cleanup: {type(exc).__name__}: {exc}")
            router_record = {
                "started": router_handle is not None,
                "ready": False,
                "stop_requested": False,
                "stopped": False,
                "result": None,
                "error": cleanup_failures[-1],
            }
        evidence["router"] = router_record
        if execute and router_handle is not None:
            try:
                if router_record.get("stopped") is not True:
                    raise VtolMissionSensorCaptureError("evaluator router did not stop")
                if router_record.get("error") is not None:
                    raise VtolMissionSensorCaptureError(str(router_record["error"]))
                router_result = router_record.get("result")
                if not isinstance(router_result, Mapping):
                    raise VtolMissionSensorCaptureError("evaluator router produced no result")
                router_checks = validate_router_result(router_result, ports=selected_ports)
                router_record["checks"] = router_checks
                evidence["gates"]["router_graceful_stop"] = True
            except Exception as exc:
                cleanup_failures.append(f"router validation: {type(exc).__name__}: {exc}")
        evidence["mission_process_cleanup"] = process_cleanup
        try:
            cleanup = _cleanup_exact_container(
                docker=docker_executable,
                container_id=container_id,
                runner=command_runner,
            )
        except Exception as exc:  # defensive: preserve a terminal evidence record
            cleanup_failures.append(f"container cleanup: {type(exc).__name__}: {exc}")
            cleanup = {
                "attempted": container_id is not None,
                "exact_id": container_id,
                "stop_exit_code": None,
                "remove_exit_code": None,
                "post_remove_inspect_exit_code": None,
                "post_remove_absence_proven": False,
                "removed": False,
                "cleanup_succeeded": False,
                "errors": [cleanup_failures[-1]],
            }
        evidence["container"]["cleanup"] = cleanup
        if (
            execute
            and container_id is not None
            and cleanup.get("removed") is True
            and cleanup.get("cleanup_succeeded") is True
        ):
            evidence["gates"]["exact_id_cleanup"] = True
        elif execute and container_id is not None:
            cleanup_failures.append("exact-ID Docker cleanup did not remove the owned container")
        evidence["cleanup_failures"] = cleanup_failures
        if cleanup_failures and runtime_failure is None:
            runtime_failure = VtolMissionSensorCaptureError("; ".join(cleanup_failures))
        if runtime_failure is not None:
            evidence["failure"] = f"{type(runtime_failure).__name__}: {runtime_failure}"
            evidence["execution"]["status"] = "failed"

    if execute and runtime_failure is None:
        try:
            required_runtime_values = {
                "captured": captured,
                "sensor_checks": sensor_checks,
                "flight_ready": flight_ready,
                "flight_checks": flight_checks,
                "capture_started_monotonic_s": capture_started_monotonic_s,
                "capture_completed_monotonic_s": capture_completed_monotonic_s,
                "completion_payload": completion_payload,
                "completion_file_sha256": completion_file_sha256,
                "mission_payload": mission_payload,
                "mission_checks": mission_checks,
                "journal_checks": journal_checks,
                "descriptor_hash": descriptor_hash,
                "airframe_profile_hash": airframe_profile_hash,
                "provenance_hash": provenance_hash,
            }
            missing_runtime_values = [
                name for name, value in required_runtime_values.items() if value is None
            ]
            if missing_runtime_values:
                raise VtolMissionSensorCaptureError(
                    "final artifact revalidation lacks runtime state: "
                    + ", ".join(missing_runtime_values)
                )

            assert captured is not None
            assert sensor_checks is not None
            assert flight_ready is not None
            assert flight_checks is not None
            assert capture_started_monotonic_s is not None
            assert capture_completed_monotonic_s is not None
            assert completion_payload is not None
            assert completion_file_sha256 is not None
            assert mission_payload is not None
            assert mission_checks is not None
            assert journal_checks is not None
            assert descriptor_hash is not None
            assert airframe_profile_hash is not None
            assert provenance_hash is not None

            final_ready = _read_json_object(
                paths.flight_ready,
                label="final flight-capture ready file",
            )
            final_completion = _read_json_object(
                paths.flight_capture_complete,
                label="final flight-capture completion marker",
            )
            final_mission = _read_json_object(
                paths.mission_evidence,
                label="final VTOL mission evidence",
            )
            if final_ready != flight_ready:
                raise VtolMissionSensorCaptureError(
                    "flight-capture ready document changed after initial validation"
                )
            if final_completion != completion_payload:
                raise VtolMissionSensorCaptureError(
                    "flight-capture completion marker changed after initial validation"
                )
            if final_mission != mission_payload:
                raise VtolMissionSensorCaptureError(
                    "VTOL mission evidence changed after initial validation"
                )

            final_ready_hash = _sha256_path(paths.flight_ready)
            final_completion_hash = _sha256_path(paths.flight_capture_complete)
            final_mission_hash = _sha256_path(paths.mission_evidence)
            final_journal_hash = _sha256_path(paths.mission_journal)
            final_stdout_hash = _sha256_path(paths.mission_stdout)
            final_stderr_hash = _sha256_path(paths.mission_stderr)
            initial_ready_hash = _mapping(evidence.get("flight_capture_ready")).get("file_sha256")
            initial_mission = _mapping(evidence.get("mission"))
            initial_journal = _mapping(evidence.get("mission_journal"))
            expected_hashes = {
                "flight_capture_ready": initial_ready_hash,
                "flight_capture_complete": completion_file_sha256,
                "mission_evidence": initial_mission.get("file_sha256"),
                "mission_journal": initial_journal.get("file_sha256"),
                "mission_stdout": initial_mission.get("stdout_sha256"),
                "mission_stderr": initial_mission.get("stderr_sha256"),
            }
            actual_hashes = {
                "flight_capture_ready": final_ready_hash,
                "flight_capture_complete": final_completion_hash,
                "mission_evidence": final_mission_hash,
                "mission_journal": final_journal_hash,
                "mission_stdout": final_stdout_hash,
                "mission_stderr": final_stderr_hash,
            }
            changed_hashes = [
                name
                for name, actual_hash in actual_hashes.items()
                if not _valid_sha256(expected_hashes.get(name))
                or expected_hashes.get(name) != actual_hash
            ]
            if changed_hashes:
                raise VtolMissionSensorCaptureError(
                    "one or more runtime artifacts changed before final publication: "
                    + ", ".join(changed_hashes)
                )

            final_flight_checks = validate_flight_capture_ready(
                final_ready,
                container_id=container_id or "",
            )
            final_sensor_checks = validate_sensor_capture(
                captured,
                frame_count=frame_count,
                frame_directory=paths.frame_directory,
                scenario_id=plan.scenario_id,
                scenario_manifest_hash=plan.scenario_manifest_hash,
                expected_width=frame_width,
                expected_height=frame_height,
                flight_ready_monotonic_s=float(final_ready["captured_at_monotonic_s"]),
                capture_started_monotonic_s=capture_started_monotonic_s,
                capture_completed_monotonic_s=capture_completed_monotonic_s,
            )
            final_mission_checks = validate_mission_evidence(
                final_mission,
                container_id=container_id or "",
                scenario_manifest_sha256=_sha256_path(source),
                airframe_profile_hash=airframe_profile_hash,
                descriptor_hash=descriptor_hash,
                provenance_hash=provenance_hash,
                evaluator_port=selected_ports.evaluator,
                barrier=final_ready,
                flight_ready_path=paths.flight_ready,
                flight_complete_path=paths.flight_capture_complete,
                flight_complete_file_sha256=final_completion_hash,
                flight_complete_document=final_completion,
            )
            final_ready_transaction = _mapping(final_ready.get("transaction"))
            final_journal_checks = validate_mission_journal(
                paths.mission_journal,
                final_mission,
                transaction_id=str(final_ready_transaction.get("id") or ""),
                committed_protocol_sha256=str(
                    final_ready_transaction.get("committed_protocol_sha256") or ""
                ),
            )
            if (
                final_flight_checks != flight_checks
                or final_sensor_checks != sensor_checks
                or final_mission_checks != mission_checks
                or final_journal_checks != journal_checks
            ):
                raise VtolMissionSensorCaptureError(
                    "final artifact checks differ from their initial validated state"
                )
            evidence["final_integrity"] = {
                "revalidated_after_mission_and_cleanup": True,
                "hashes": actual_hashes,
                "flight_capture_ready_checks": final_flight_checks,
                "sensor_capture_checks": final_sensor_checks,
                "mission_checks": final_mission_checks,
                "mission_journal_checks": final_journal_checks,
            }
            evidence["gates"]["final_artifact_integrity"] = True
        except Exception as exc:
            runtime_failure = exc
            evidence["failure"] = f"{type(exc).__name__}: {exc}"
            evidence["execution"]["status"] = "failed"

    required_gates = (
        "docker_plan",
        "container_identity",
        "router_ready",
        "flight_capture_ready",
        "flight_capture_complete_marker",
        "flight_capture_interval",
        "same_container_sensor_capture",
        "wire_v2_pixels",
        "complete_vtol_mission",
        "mission_journal_integrity",
        "router_graceful_stop",
        "exact_id_cleanup",
        "final_artifact_integrity",
    )
    gate_passed = bool(execute and all(evidence["gates"][name] is True for name in required_gates))
    evidence["gate_passed"] = gate_passed
    if gate_passed:
        evidence["failure"] = None
        evidence["execution"]["status"] = "passed"
    evidence["generated_at_utc"] = _utc_now()
    evidence_without_hash = dict(evidence)
    evidence["evidence_sha256"] = canonical_sha256(evidence_without_hash)
    _atomic_write_new_json(paths.evidence, evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument(
        "--evidence", "--out", dest="evidence", type=Path, default=DEFAULT_EVIDENCE_PATH
    )
    parser.add_argument("--run-directory", type=Path, default=DEFAULT_RUN_DIRECTORY)
    parser.add_argument("--flight-capture-ready-file", type=Path)
    parser.add_argument("--flight-capture-complete-file", type=Path)
    parser.add_argument("--mission-journal", type=Path)
    parser.add_argument("--frame-output-directory", type=Path)
    parser.add_argument("--container-name", default=DEFAULT_CONTAINER_NAME)
    parser.add_argument("--frame-count", type=int, default=DEFAULT_FRAME_COUNT)
    parser.add_argument("--frame-width", type=int, default=DEFAULT_FRAME_WIDTH)
    parser.add_argument("--frame-height", type=int, default=DEFAULT_FRAME_HEIGHT)
    parser.add_argument("--sensor-timeout-seconds", type=float, default=DEFAULT_SENSOR_TIMEOUT_S)
    parser.add_argument("--mission-timeout-seconds", type=float, default=DEFAULT_MISSION_TIMEOUT_S)
    parser.add_argument("--ready-timeout-seconds", type=float, default=DEFAULT_READY_TIMEOUT_S)
    parser.add_argument(
        "--router-ready-timeout-seconds",
        type=float,
        default=DEFAULT_ROUTER_READY_TIMEOUT_S,
    )
    parser.add_argument("--docker")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start the pinned disposable container and execute the full headless gate",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = run_vtol_mission_sensor_capture(
            args.scenario,
            args.evidence,
            run_directory=args.run_directory,
            flight_ready_file=args.flight_capture_ready_file,
            flight_capture_complete_file=args.flight_capture_complete_file,
            mission_journal_path=args.mission_journal,
            frame_output_directory=args.frame_output_directory,
            container_name=args.container_name,
            execute=args.execute,
            frame_count=args.frame_count,
            frame_width=args.frame_width,
            frame_height=args.frame_height,
            sensor_timeout_s=args.sensor_timeout_seconds,
            mission_timeout_s=args.mission_timeout_seconds,
            ready_timeout_s=args.ready_timeout_seconds,
            router_ready_timeout_s=args.router_ready_timeout_seconds,
            docker=args.docker,
        )
    except (OSError, TypeError, ValueError, VtolMissionSensorCaptureError) as exc:
        print(
            json.dumps(
                {"event": "vtol_mission_sensor_capture_failed", "error": str(exc)}, sort_keys=True
            )
        )
        return 2
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    if args.execute and not payload["gate_passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CapturePaths",
    "CapturePorts",
    "VtolMissionSensorCaptureError",
    "allocate_capture_ports",
    "main",
    "prepare_capture_paths",
    "run_vtol_mission_sensor_capture",
    "validate_capture_plan",
    "validate_capture_ports",
    "validate_flight_capture_ready",
    "validate_mission_evidence",
    "validate_router_result",
    "validate_sensor_capture",
]
