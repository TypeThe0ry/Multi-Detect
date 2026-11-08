"""Owned, bounded teacher SIM wiring; no policy, Mission, ARM or grant.

The CLI only plans. The inherited Docker planner may create ``job_dir`` but
does not start a process or open a socket. Execution requires an explicit
``OwnedTeacherGazeboSession(..., execute=True)`` in an externally supervised
worker. This is not a watchdog for arbitrary code in that worker.

Camera source stamps remain Gazebo stamps, not host perf_counter timestamps.
The backend's derived GPS-quality defaults are NOT PX4 qualification. Only
the caller's genuinely qualified raw telemetry can authorize attestation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-teacher-gazebo-session-v1"
OWNER_LABEL = "multidetect.teacher-session-owner"
PURPOSE_LABEL = "multidetect.teacher-session-purpose"
PURPOSE = "truth-free-teacher-observation"
MAX_CAPTURE_BYTES = 128 * 1024 * 1024
_ID = re.compile(r"[0-9a-f]{64}")



def _sim_udpin_endpoint(port: int) -> str:
    from multidetect.sim_network import sim_udpin_endpoint

    return sim_udpin_endpoint(port)


class TeacherSessionError(RuntimeError):
    pass


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _real(value, name, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric, not bool")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} outside {minimum}..{maximum}")
    return float(value)


def _write_new(path, row):
    payload = (json.dumps(row, sort_keys=True, allow_nan=False, indent=2) + "\n").encode()
    with Path(path).open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True, slots=True)
class TeacherSessionPlan:
    docker_plan: object
    source_manifest_path: Path
    evidence_dir: Path
    frame_count: int = 8
    camera_timeout_s: float = 120
    session_timeout_s: float = 600
    width: int = 1280
    height: int = 960

    def __post_init__(self):
        from scripts.run_simulator_docker import DockerSimulatorPlan
        from multidetect.simulator.px4_image import DEFAULT_GPU_IMAGE, PINNED_IMAGE

        if not isinstance(self.docker_plan, DockerSimulatorPlan):
            raise TypeError("Repository DockerSimulatorPlan required")
        qualified_image = self.docker_plan.image == PINNED_IMAGE or (
            self.docker_plan.gpu_render and self.docker_plan.image == DEFAULT_GPU_IMAGE
        )
        if not qualified_image:
            raise ValueError("Exact repository source image or qualified GPU runtime required")
        if self.docker_plan.airframe_profile_id != "px4_multirotor_gazebo_v1":
            raise ValueError("Teacher observation seam is multirotor-only")
        for port in (self.docker_plan.host_control_port, self.docker_plan.host_telemetry_port):
            _integer(port, "port", 1024, 65535)
            if port == 14550:
                raise ValueError("Protected UDP 14550 forbidden")
        if self.docker_plan.host_control_port == self.docker_plan.host_telemetry_port:
            raise ValueError("Distinct control/telemetry ports required")
        _integer(self.frame_count, "frame_count", 1, 128)
        _integer(self.width, "width", 1, 4096)
        _integer(self.height, "height", 1, 4096)
        _real(self.session_timeout_s, "session_timeout_s", 1, 600)
        _real(self.camera_timeout_s, "camera_timeout_s", 0.01, self.session_timeout_s)
        if self.frame_count * self.width * self.height * 7 > MAX_CAPTURE_BYTES:
            raise ValueError("RGB plus possible FLOAT32 depth exceeds capture budget")
        if not self.source_manifest_path.is_absolute() or not self.evidence_dir.is_absolute():
            raise ValueError("Absolute source/evidence paths required")
        if self.evidence_dir.is_symlink() or self.evidence_dir.exists():
            raise ValueError("Fresh, non-symlink session evidence directory required")

    def to_mapping(self):
        return {
            "contract": CONTRACT,
            "docker_plan": self.docker_plan.to_mapping_with_hash(),
            "source_manifest_path": self.source_manifest_path.as_posix(),
            "evidence_dir": self.evidence_dir.as_posix(),
            "frame_count": self.frame_count,
            "camera_timeout_s": self.camera_timeout_s,
            "session_timeout_s": self.session_timeout_s,
            "width": self.width,
            "height": self.height,
            "capture_byte_cap": MAX_CAPTURE_BYTES,
            "default_plan_only": True,
            "flight_authority": False,
            "automatic_commands": False,
            "external_worker_watchdog_required": True,
        }


def build_teacher_session_plan(
    source_manifest_path,
    job_dir,
    *,
    container_name="multidetect-teacher-gazebo",
    control_port=14581,
    telemetry_port=14582,
    frame_count=8,
    camera_timeout_s=120,
    session_timeout_s=600,
    width=1280,
    height=960,
    repository_root=ROOT,
):
    from scripts.run_simulator_docker import build_docker_simulator_plan

    source = Path(source_manifest_path).resolve(strict=True)
    job = Path(job_dir).absolute()
    if job.is_symlink():
        raise ValueError("job_dir cannot be a symlink")
    plan = build_docker_simulator_plan(
        source,
        repository_root=repository_root,
        job_dir=job,
        container_name=container_name,
        control_port=control_port,
        telemetry_port=telemetry_port,
        dry_run=True,
    )
    return TeacherSessionPlan(
        plan,
        source,
        job.resolve() / "teacher-owned-session",
        frame_count,
        camera_timeout_s,
        session_timeout_s,
        width,
        height,
    )


def _probe_ports(plan):
    # A prelaunch availability check, not a claim that the race is reserved.
    for port in (plan.host_control_port, plan.host_telemetry_port):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))


def _connect(endpoint, **kwargs):
    from pymavlink import mavutil

    return mavutil.mavlink_connection(endpoint, **kwargs)


def _camera_backend(plan, container_id, clock):
    from scripts import run_simulator_docker_acceptance as capture

    docker = plan.docker_plan
    scenario = capture.load_runtime_scenario(
        plan.source_manifest_path,
        mode=capture.RuntimeMode.SIM,
    )
    calibration = capture._camera_calibration_provenance(
        plan=docker,
        scenario_input=scenario,
        expected_width=plan.width,
        expected_height=plan.height,
    )
    assets = capture._model_assets_provenance(
        docker.model_root,
        docker.model_name,
        repository_root=docker.repository_root,
    )
    return capture.GazeboSimBackend(
        scenario_input=scenario,
        clock=clock,
        camera_calibration=capture.CameraCalibration(**calibration["calibration"]),
        docker_executable=docker.docker_executable,
        container_name=container_id,
        frame_count=plan.frame_count,
        airframe_profile_hash=capture.stable_airframe_profile_hash(
            {
                "schema_version": 1,
                "airframe_profile_id": docker.airframe_profile_id,
                "vehicle_model": docker.vehicle_model,
                "model_name": docker.model_name,
                "stack_plan_sha256": docker.stack_plan_sha256,
            }
        ),
        container_image_sha256=docker.image.rsplit("@sha256:", 1)[-1],
        stream_script_sha256=capture._sha256(docker.scripts_root / "gazebo_sensor_stream.py"),
        model_assets_sha256=assets["assets_sha256"],
        terrain_dem=capture.load_local_dem_grid(docker.dem_path),
        model_instance=f"{docker.model_name}_0",
        world="default",
        timeout_s=plan.camera_timeout_s,
        expected_width=plan.width,
        expected_height=plan.height,
    )


@dataclass(frozen=True, slots=True)
class TeacherCameraObservation:
    frame: object
    provenance: object
    depth: object
    rgb_bgr24: bytes
    host_return_stamp: object

    def to_mapping(self):
        return {
            "frame_id": self.frame.frame_id,
            "provenance": self.provenance.to_mapping(),
            "rgb_bgr24_bytes": len(self.rgb_bgr24),
            "rgb_bgr24_sha256": hashlib.sha256(self.rgb_bgr24).hexdigest(),
            "host_return_stamp": self.host_return_stamp.to_mapping(),
            "depth": None if self.depth is None else self.depth.to_mapping(),
            "transcoded": False,
            "source_host_epoch_equivalence_claimed": False,
            "px4_quality_qualification_claimed": False,
        }


class BoundedTeacherCamera:
    """One native camera helper, sequential reads, no reusable frame cache.

    Each read has a host deadline; a timeout closes only this camera helper.
    Frozen source close bounds its native wait/terminate/kill. An outer worker
    watchdog is still required if a native close itself stops responding.
    """

    def __init__(self, backend, plan, clock, session_deadline_s):
        if backend.clock is not clock or backend.frame_source.clock is not clock:
            raise TeacherSessionError("Camera must use the exact session clock object")
        self.backend, self.source = backend, backend.frame_source
        self.plan, self.clock = plan, clock
        self.deadline_s = min(session_deadline_s, clock.monotonic() + plan.camera_timeout_s)
        self.owner = threading.get_ident()
        self.count = 0
        self.frame_count = plan.frame_count
        self.opened = self.closed = False
        self.failure = None
        self.last_sequence = None

    def read(self, *, deadline_s=None):
        if threading.get_ident() != self.owner or self.closed or self.failure is not None:
            raise TeacherSessionError("Camera owner closed, failed or wrong thread")
        deadline = (
            self.deadline_s
            if deadline_s is None
            else min(
                self.deadline_s,
                _real(deadline_s, "deadline_s", 0, float("1e20")),
            )
        )
        if self.count >= self.plan.frame_count or self.clock.monotonic() >= deadline:
            raise TeacherSessionError("Camera frame/deadline budget exhausted")
        done, returned = threading.Event(), []

        def receive():
            try:
                returned.append((True, self.source.read()))
            except BaseException as error:
                returned.append((False, error))
            finally:
                done.set()

        try:
            if not self.opened:
                self.opened = True  # also close a partially opened helper
                self.source.open()
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                raise TeacherSessionError("Camera deadline exhausted during open")
            thread = threading.Thread(target=receive, name="teacher-camera-read", daemon=True)
            thread.start()
            if not done.wait(remaining):
                self.close(require_complete=False)
                thread.join(timeout=3)
                raise TeacherSessionError("Camera read deadline exceeded")
            valid, frame = returned[0]
            if not valid:
                raise frame
            provenance = self.source.latest_frame_provenance
            raw = frame.image_bgr.tobytes(order="C")
            stamp = self.clock.read()
            expected = self.plan.width * self.plan.height * 3
            if (
                stamp.value_ns / 1e9 >= deadline
                or (frame.width, frame.height) != (self.plan.width, self.plan.height)
                or frame.frame_id != provenance.frame_id
                or frame.captured_at_s != provenance.captured_at_s
                or dict(provenance.host_clock_identity) != self.clock.identity()
                or provenance.pixel_format != "BGR24"
                or (provenance.width, provenance.height, provenance.stride)
                != (self.plan.width, self.plan.height, self.plan.width * 3)
                or len(raw) != expected
                or provenance.payload_bytes != expected
                or hashlib.sha256(raw).hexdigest() != provenance.payload_sha256
                or hashlib.sha256(
                    bytes.fromhex(provenance.wire_unsigned_header_hex) + raw,
                ).hexdigest()
                != provenance.record_sha256
                or (self.last_sequence is not None and provenance.sequence <= self.last_sequence)
            ):
                raise TeacherSessionError("Camera wire/pixels/host/deadline binding failed")
            depth = self.source.latest_depth_frame
            if depth is not None and (
                depth.frame_id != frame.frame_id
                or depth.frame_sensor_stamp_ns != provenance.sensor_stamp_ns
                or depth.source_stamp_ns != provenance.depth_source_stamp_ns
                or hashlib.sha256(depth.payload).hexdigest() != provenance.depth_payload_sha256
            ):
                raise TeacherSessionError("Depth companion binding failed")
            stamp = self.clock.read()
            if stamp.value_ns / 1e9 >= deadline:
                raise TeacherSessionError("Camera deadline exhausted during validation")
            self.last_sequence = provenance.sequence
            self.count += 1
            return TeacherCameraObservation(
                frame,
                provenance,
                depth,
                raw,
                stamp,
            )
        except BaseException as error:
            self.failure = f"{type(error).__name__}: {error}"[:1024]
            raise

    def close(self, *, require_complete=False):
        if self.closed:
            if (
                require_complete
                and self.opened
                and (self.failure is not None or self.source.stats.complete is not True)
            ):
                raise TeacherSessionError("Camera did not complete its declared frame budget")
            return
        self.closed = True
        self.backend.close()
        if self.opened:
            stats = self.source.stats
            if type(stats.helper_exit_code) is not int:
                raise TeacherSessionError("Camera helper native exit unobserved")
            if require_complete and (self.failure is not None or stats.complete is not True):
                raise TeacherSessionError("Camera did not complete its declared frame budget")


class OwnedTeacherGazeboSession:
    def __init__(
        self,
        plan,
        *,
        execute=False,
        clock=None,
        runner=None,
        connection_factory=None,
        camera_factory=None,
        port_probe=None,
    ):
        from multidetect.clock import HighResolutionSystemClock

        if type(plan) is not TeacherSessionPlan or type(execute) is not bool:
            raise TypeError("Typed plan and explicit boolean execute required")
        self.plan, self.execute = plan, execute
        self.clock = HighResolutionSystemClock() if clock is None else clock
        if not isinstance(self.clock, HighResolutionSystemClock):
            raise TypeError("HighResolutionSystemClock required")
        self._runner = runner
        self._connect = _connect if connection_factory is None else connection_factory
        self._camera = _camera_backend if camera_factory is None else camera_factory
        self._probe = _probe_ports if port_probe is None else port_probe
        self.connection = self.camera = None
        self.container_id = None
        self.owner_token = uuid4().hex
        self._entered = self._closed = self._launch_attempted = False
        self._receive = None
        self._receiving = False
        self._failure = None
        self._report = {"contract": CONTRACT, "executed": False, "cleanup_verified": False}

    @property
    def report(self):
        return copy.deepcopy(self._report)

    def to_mapping(self):
        """Detached runtime evidence; native/PID proof stays with the supervisor."""
        return {
            **self.report,
            "clock": self.clock.identity(),
            "control_endpoint": self.control_endpoint,
            "telemetry_endpoint": _sim_udpin_endpoint(self.plan.docker_plan.host_telemetry_port),
            "flight_authority": False,
        }

    @property
    def control_endpoint(self):
        return f"udp:127.0.0.1:{self.plan.docker_plan.host_control_port}"

    def _run(self, arguments, *, cleanup=False):
        docker = self.plan.docker_plan.docker_executable
        if not docker:
            raise TeacherSessionError("Docker executable required for execute")
        timeout = 15 if cleanup else min(30, self._remaining())
        argv = [docker, *arguments]
        result = (
            subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
            if self._runner is None
            else self._runner(argv, timeout_s=timeout)
        )
        if type(result.returncode) is not int:
            raise TeacherSessionError("Native Docker exit code missing")
        if len(result.stdout or "") + len(result.stderr or "") > 256 * 1024:
            raise TeacherSessionError("Docker diagnostic output budget exceeded")
        self._report.setdefault("native_commands", []).append(
            {
                "operation": arguments[:3],
                "native_exit_code": result.returncode,
            }
        )
        return result

    def _remaining(self):
        remaining = self.deadline_s - self.clock.monotonic()
        if remaining <= 0:
            raise TeacherSessionError("Session deadline exhausted")
        return remaining

    def _inspect(self, target, *, cleanup=False):
        result = self._run(["container", "inspect", target], cleanup=cleanup)
        if result.returncode != 0:
            if result.returncode == 1 and re.search(
                rf"No such (?:container|object):\s*{re.escape(target)}(?:\s|$)",
                result.stderr or "",
                re.IGNORECASE,
            ):
                return None
            raise TeacherSessionError("Docker inspect failed, absence not proven")
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise TeacherSessionError("Docker inspect must return one object")
        return records[0]

    def _owned(self, record):
        docker = self.plan.docker_plan
        config = record.get("Config") or {}
        labels = config.get("Labels") or {}
        identity = record.get("Id", "")
        if (
            not isinstance(identity, str)
            or _ID.fullmatch(identity) is None
            or record.get("Name") != "/" + docker.container_name
            or config.get("Image") != docker.image
            or labels.get("multidetect.purpose") != "simulator-docker"
            or labels.get(OWNER_LABEL) != self.owner_token
            or labels.get(PURPOSE_LABEL) != PURPOSE
            or (self.container_id is not None and identity != self.container_id)
        ):
            raise TeacherSessionError("Container not exact owned ID/name/image/labels; not touched")
        return identity

    def __enter__(self):
        if self._entered:
            raise TeacherSessionError("Session is single-use")
        self._entered, self._owner = True, threading.get_ident()
        self.deadline_s = self.clock.monotonic() + self.plan.session_timeout_s
        if not self.execute:
            self._report["status"] = "planned_only"
            return self
        try:
            from scripts.run_simulator_docker_acceptance import _validate_owned_container

            docker = self.plan.docker_plan
            if self._inspect(docker.container_name) is not None:
                raise TeacherSessionError("Pre-existing container not owned; not touched")
            image = self._run(["image", "inspect", docker.image])
            if image.returncode != 0:
                raise TeacherSessionError("Pinned local image unavailable; no automatic pull")
            self._probe(docker)
            self.plan.evidence_dir.mkdir(parents=False, exist_ok=False)
            _write_new(
                self.plan.evidence_dir / "intent.json",
                {
                    "owner_token": self.owner_token,
                    "plan": self.plan.to_mapping(),
                    "clock": self.clock.identity(),
                },
            )
            self.connection = self._connect(
                _sim_udpin_endpoint(docker.host_telemetry_port),
                source_system=250,
                source_component=191,
                autoreconnect=False,
            )
            self._receive = self.connection.recv_match
            command = list(docker.command)
            if command[:3] != ["docker", "run", "-d"] or "--cidfile" in command:
                raise TeacherSessionError("Exact repository detached launch command required")
            cidfile = self.plan.evidence_dir / "native-container.cid"
            command[3:3] = [
                "--cidfile",
                str(cidfile),
                "--label",
                f"{OWNER_LABEL}={self.owner_token}",
                "--label",
                f"{PURPOSE_LABEL}={PURPOSE}",
            ]
            self._launch_attempted = True
            launched = self._run(command[1:])
            if launched.returncode != 0:
                raise TeacherSessionError("Owned Docker launch native failure")
            identity = launched.stdout.strip()
            if _ID.fullmatch(identity) is None or cidfile.read_text().strip() != identity:
                raise TeacherSessionError("Native stdout/cidfile container identity mismatch")
            self.container_id = identity
            record = self._inspect(identity)
            if record is None:
                raise TeacherSessionError("Started container missing")
            self._owned(record)
            checks = _validate_owned_container(record, docker)
            _write_new(
                self.plan.evidence_dir / "owned-container.json",
                {
                    "container_id": identity,
                    "owner_token": self.owner_token,
                    "launch_native_exit_code": launched.returncode,
                    "checks": checks,
                },
            )
            self._report.update(executed=True, container_id=identity, ownership_checks=checks)
            backend = self._camera(self.plan, identity, self.clock)
            try:
                self.camera = BoundedTeacherCamera(backend, self.plan, self.clock, self.deadline_s)
            except BaseException as error:
                try:
                    backend.close()
                except BaseException as close_error:
                    error.add_note(f"Camera construction cleanup failed: {close_error}")
                raise
            return self
        except BaseException as error:
            self._finish(error)
            raise

    def raw_receive(self, timeout_s):
        if (
            not self.execute
            or self._closed
            or self.connection is None
            or self._failure is not None
            or threading.get_ident() != self._owner
            or self._receiving
            or self.connection.recv_match != self._receive
        ):
            raise TeacherSessionError("Unique original receive owner unavailable")
        timeout = min(_real(timeout_s, "timeout_s", 0, 10), self._remaining())
        self._receiving = True
        try:
            # No probe, cached collector, message-type filter or source filter.
            message = self._receive(blocking=True, timeout=timeout)
            self._remaining()
            return message
        except BaseException as error:
            self._failure = f"{type(error).__name__}: {error}"[:1024]
            raise
        finally:
            self._receiving = False

    def attest_qualified(self, *, telemetry_qualified):
        from multidetect.px4_external_mission import attest_owned_px4_container
        from scripts.run_simulator_docker_acceptance import _expected_mounts

        if telemetry_qualified is not True or not self.execute or self._closed:
            raise TeacherSessionError("Real caller telemetry qualification required")
        record = self._inspect(self.container_id)
        if record is None:
            raise TeacherSessionError("Owned runtime missing")
        self._owned(record)
        docker = self.plan.docker_plan
        return attest_owned_px4_container(
            record,
            expected_container_id=self.container_id,
            expected_image=docker.image,
            vehicle_class="multirotor",
            scenario_id=docker.scenario_id,
            scenario_manifest_sha256=docker.scenario_manifest_hash,
            expected_model=dict(docker.environment)["PX4_SIM_MODEL"],
            expected_purpose_label="simulator-docker",
            control_port=docker.host_control_port,
            telemetry_qualified=telemetry_qualified,
            expected_readonly_mounts={k: v[0] for k, v in _expected_mounts(docker).items()},
        )

    def _finish(self, primary):
        if self._closed:
            return
        self._closed = True
        errors = []
        for operation in (
            lambda: (
                self.camera.close(require_complete=primary is None and self._failure is None)
                if self.camera is not None
                else None
            ),
            lambda: self.connection.close() if self.connection is not None else None,
        ):
            try:
                operation()
            except BaseException as error:
                errors.append(error)
        if self._launch_attempted:
            try:
                target = self.container_id or self.plan.docker_plan.container_name
                record = self._inspect(target, cleanup=True)
                if record is None:
                    raise TeacherSessionError("Owned container disappearance not a cleanup proof")
                identity = self._owned(record)
                self.container_id = identity
                try:
                    _write_new(
                        self.plan.evidence_dir / "cleanup-claim.json",
                        {
                            "container_id": identity,
                            "owner_token": self.owner_token,
                        },
                    )
                except BaseException as error:
                    errors.append(error)  # cleanup must still run for the verified owned ID
                for arguments in (["stop", "--timeout", "2", identity], ["rm", identity]):
                    try:
                        result = self._run(arguments, cleanup=True)
                        if result.returncode != 0:
                            errors.append(TeacherSessionError("Owned cleanup native failure"))
                    except BaseException as error:
                        errors.append(error)
                absent = self._inspect(identity, cleanup=True) is None
                self._report["container_absent_after"] = absent
                if not absent:
                    errors.append(TeacherSessionError("Owned container still present after rm"))
                self._report["cleanup_verified"] = absent and not errors
            except BaseException as error:
                errors.append(error)
        self._report.update(
            status="failed" if primary is not None or errors or self._failure else "closed",
            error=None if primary is None else f"{type(primary).__name__}: {primary}"[:1024],
            receive_error=self._failure,
            cleanup_errors=[f"{type(e).__name__}: {e}"[:1024] for e in errors],
        )
        if not self.execute:
            self._report["status"] = "planned_only"
        if self.camera is not None:
            self._report["camera"] = {
                "admitted_frame_count": self.camera.count,
                "expected_frame_count": self.camera.frame_count,
                "opened": self.camera.opened,
                "closed": self.camera.closed,
                "failure": self.camera.failure,
            }
            if self.camera.opened:
                try:
                    self._report["camera"]["native_stats"] = self.camera.source.stats.to_mapping()
                except BaseException as error:
                    errors.append(error)
                    self._report["status"] = "failed"
                    self._report["cleanup_errors"].append(
                        f"{type(error).__name__}: {error}"[:1024],
                    )
        if self.execute and self.plan.evidence_dir.is_dir():
            try:
                _write_new(self.plan.evidence_dir / "session-receipt.json", self._report)
            except BaseException as error:
                errors.append(error)
                self._report["status"] = "failed"
                self._report["cleanup_errors"].append(f"{type(error).__name__}: {error}"[:1024])
        if errors or self._failure:
            if primary is not None:
                primary.add_note(
                    "Teacher session cleanup/receive failure: " + str(errors or self._failure),
                )
            else:
                raise TeacherSessionError("Teacher session did not close cleanly") from (
                    errors[0] if errors else None
                )

    def __exit__(self, error_type, error, traceback):
        self._finish(error)
        return False


def main(argv=None):
    # Checkout CLI only; importing the reusable module does not alter sys.path.
    for path in (ROOT, ROOT / "src"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    parser = argparse.ArgumentParser(description="Plan only: owned teacher Gazebo observation")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--frame-count", type=int, default=8)
    args = parser.parse_args(argv)
    plan = build_teacher_session_plan(args.scenario, args.job_dir, frame_count=args.frame_count)
    print(json.dumps(plan.to_mapping(), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
