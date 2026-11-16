#!/usr/bin/env python3
"""End-to-end acceptance runner for the Polaris SITL integration."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import queue
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    from pymavlink import mavutil
except ImportError:  # optional: only the mavlink steps need it
    mavutil = None  # type: ignore[assignment]

DEFAULT_STEPS = ("discovery", "mavlink", "video", "netem", "ai", "doctor", "record", "multi")
EVENT_ORDER = (
    "query.parsed",
    "vega.detected",
    "vlm.reviewed",
    "mizar.located",
    "tracking.updated",
    "published",
)
SLOT_SYSID = {5: 1, 6: 2, 7: 3}
DEFAULT_QUERY = {
    "schema_version": "1.0",
    "categories": ["car"],
    "outputs": ["count"],
}
LANE_ROOT = ROOT.parent / "cx-psim-gateway"
AI_LANE_ROOT = ROOT.parent / "cx-psim-ai"
VIDEO_LANE_ROOT = (
    ROOT
    if (ROOT / "src/multidetect/polaris_sim/recording.py").is_file()
    else ROOT.parent / "cx-psim-video"
)


def lane_file(local: Path, sibling: Path) -> Path:
    return local if local.is_file() else sibling


class E2EFailure(RuntimeError):
    """A named acceptance check failed."""


@dataclass
class StepResult:
    status: str
    started_at: str
    ended_at: str
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    log: str | None = None


@dataclass
class Config:
    host: str
    http_port: int
    mavlink_tcp_port: int
    mavlink_udp_port: int
    rtsp_port: int
    ai_port: int
    slots: tuple[int, ...]
    world: str
    airframe: str
    steps: tuple[str, ...]
    output_dir: Path
    up_script: Path
    down_script: Path
    timeout_s: float
    lifecycle_timeout_s: float
    record_seconds: float
    min_width: int
    min_height: int
    min_fps: float
    max_video_startup_ms: float
    ai_query_file: Path | None
    ai_demo_script: Path
    video_root: Path

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.http_port}"

    @property
    def ai_url(self) -> str:
        return f"http://{self.host}:{self.ai_port}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def parse_slots(value: str) -> tuple[int, ...]:
    try:
        slots = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",") if item.strip()))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("slots must be comma-separated integers") from exc
    if slots not in {(5,), (5, 6), (5, 6, 7)}:
        raise argparse.ArgumentTypeError(
            "the lifecycle manager starts a contiguous set beginning at slot 5"
        )
    return slots


def parse_steps(value: str) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    unknown = set(selected) - set(DEFAULT_STEPS)
    if not selected or unknown:
        raise argparse.ArgumentTypeError(
            f"steps must be a comma-separated subset of {', '.join(DEFAULT_STEPS)}"
        )
    return selected


def parse_fraction(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        numerator, denominator = value.split("/", 1)
        divisor = float(denominator)
        return float(numerator) / divisor if divisor else 0.0
    except (ValueError, ZeroDivisionError):
        try:
            return float(value)
        except ValueError:
            return 0.0


def assert_latency_increased(
    baseline_ms: float, delayed_ms: float, minimum_increase_ms: float
) -> None:
    if delayed_ms < baseline_ms + minimum_increase_ms:
        raise E2EFailure(
            f"netem did not measurably increase latency: baseline={baseline_ms:.1f}ms, "
            f"delayed={delayed_ms:.1f}ms, required_delta={minimum_increase_ms:.1f}ms"
        )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise E2EFailure(f"cannot read JSON from {path}: {exc}") from exc


def _request_json(
    config: Config,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    write: bool = False,
    timeout: float | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("POLARIS_SIM_TOKEN")
    if write and token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Polaris-Sim-Token"] = token
    request = urllib.request.Request(
        f"{config.base_url}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or config.timeout_s) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise E2EFailure(f"HTTP {method} {path} returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise E2EFailure(f"HTTP {method} {path} failed: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure(f"HTTP {method} {path} did not return JSON") from exc


def _write_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_command(
    command: list[str],
    log_path: Path,
    *,
    timeout: float,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    display_command = list(command)
    for index, argument in enumerate(display_command[:-1]):
        if argument in {"--token", "--password"}:
            display_command[index + 1] = "<redacted>"
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        _write_log(
            log_path,
            f"$ {' '.join(display_command)}\ntimeout={timeout}s\n"
            f"stdout:\n{stdout or ''}\nstderr:\n{stderr or ''}\n",
        )
        raise E2EFailure(f"command timed out after {timeout:g}s; log: {log_path}") from exc
    except OSError as exc:
        _write_log(log_path, f"$ {' '.join(display_command)}\n{exc}\n")
        raise E2EFailure(f"command failed to run; log: {log_path}: {exc}") from exc
    _write_log(
        log_path,
        f"$ {' '.join(display_command)}\nexit={result.returncode}\n\n"
        f"{result.stdout}\n{result.stderr}",
    )
    if check and result.returncode:
        raise E2EFailure(f"command exited {result.returncode}; log: {log_path}")
    return result


def _walk_statuses(value: Any, path: str = "") -> list[tuple[str, Any]]:
    statuses: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            if key.lower() == "status" and isinstance(item, (str, bool)):
                statuses.append((child, item))
            elif key.lower() == "ok" and isinstance(item, bool):
                statuses.append((child, "pass" if item else "fail"))
            statuses.extend(_walk_statuses(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            statuses.extend(_walk_statuses(item, f"{path}[{index}]"))
    return statuses


def validate_doctor(document: Any) -> dict[str, Any]:
    statuses = _walk_statuses(document)
    if not statuses:
        raise E2EFailure("doctor response contains no status/ok checks")
    failed = [
        (name, value)
        for name, value in statuses
        if str(value).lower() in {"fail", "failed", "error", "false"}
    ]
    if failed:
        raise E2EFailure(f"doctor has FAIL checks: {failed}")
    return {"checks": [{"name": name, "status": value} for name, value in statuses]}


def validate_ai_response(response: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise E2EFailure("query_target response must be a JSON object")
    envelope = response.get("result", response)
    if not isinstance(envelope, dict):
        raise E2EFailure("query_target response is missing its Spica result envelope")
    if envelope.get("name") != "query_target":
        raise E2EFailure("query_target response name does not match Spica envelope")
    if envelope.get("id") != request_id:
        raise E2EFailure("query_target response id does not match request_id")
    if envelope.get("status") not in {"ok", "error", "unavailable"}:
        raise E2EFailure(f"query_target returned invalid status: {envelope.get('status')!r}")
    if envelope.get("status") == "ok":
        data = envelope.get("data")
        if not isinstance(data, dict) or data.get("schema_version") != "1.0":
            raise E2EFailure(
                "successful query_target response is missing Spica data schema_version"
            )
        if not isinstance(data.get("results"), list):
            raise E2EFailure("successful query_target response is missing results")
    elif envelope.get("data") not in (None, {}):
        raise E2EFailure("disabled/abstain query_target response must not claim result data")
    return {
        "status": envelope["status"],
        "latency_ms": envelope.get("latency_ms"),
        "api_version": response.get("api_version"),
        "runtime": response.get("runtime"),
    }


def validate_event_order(events: list[dict[str, Any]]) -> list[str]:
    names = [str(item.get("event") or item.get("type") or "") for item in events]
    cursor = 0
    for name in names:
        if cursor < len(EVENT_ORDER) and name == EVENT_ORDER[cursor]:
            cursor += 1
    if cursor != len(EVENT_ORDER):
        expected = EVENT_ORDER[cursor] if cursor < len(EVENT_ORDER) else ""
        raise E2EFailure(
            f"SSE event order incomplete: received {names}; next required event is {expected}"
        )
    return names


def _fps(stream: dict[str, Any]) -> float:
    return parse_fraction(stream.get("avg_frame_rate")) or parse_fraction(
        stream.get("r_frame_rate")
    )


def validate_video_probe(
    document: dict[str, Any], *, min_width: int, min_height: int, min_fps: float
) -> dict[str, Any]:
    streams = document.get("streams")
    if not isinstance(streams, list) or not streams:
        raise E2EFailure("ffprobe found no video stream")
    stream = streams[0]
    codec = str(stream.get("codec_name", "")).lower()
    width, height, fps = int(stream.get("width", 0)), int(stream.get("height", 0)), _fps(stream)
    if codec != "h264":
        raise E2EFailure(f"RTSP codec must be H.264, got {codec or 'unknown'}")
    if width < min_width or height < min_height:
        raise E2EFailure(f"video resolution {width}x{height} is below {min_width}x{min_height}")
    if fps < min_fps:
        raise E2EFailure(f"video frame rate {fps:.3f} fps is below {min_fps:.3f} fps")
    return {"codec": codec, "width": width, "height": height, "fps": fps}


def validate_discovery(
    document: Any,
    *,
    http_port: int,
    rtsp_port: int,
    mavlink_tcp_port: int,
    mavlink_udp_port: int,
    ai_port: int,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise E2EFailure("GET /v1/discovery did not return an object")
    expected = {
        "service": "polaris-sim",
        "version": 1,
        "http": http_port,
        "rtsp": rtsp_port,
        "mavlink_tcp": mavlink_tcp_port,
        "mavlink_udp": mavlink_udp_port,
        "ai": ai_port,
    }
    mismatches = {
        key: (document.get(key), value)
        for key, value in expected.items()
        if document.get(key) != value
    }
    if mismatches:
        raise E2EFailure(f"discovery document violates service contract: {mismatches}")
    for key in ("mavlink_tcp", "mavlink_udp", "ai"):
        if not isinstance(document.get(key), int):
            raise E2EFailure(f"discovery document is missing integer port {key}")
    if not isinstance(document.get("vehicles"), list):
        raise E2EFailure("discovery document is missing the vehicles list")
    return {
        key: document[key]
        for key in (*expected.keys(), "host", "mavlink_tcp", "mavlink_udp", "ai", "vehicles")
        if key in document
    }


class PolarisE2E:
    def __init__(
        self, config: Config, *, dry_run: bool = False, no_up: bool = False, no_down: bool = False
    ):
        self.config = config
        self.dry_run = dry_run
        self.no_up = no_up
        self.no_down = no_down
        self.results: dict[str, StepResult] = {}
        self.run_id = timestamp()
        self._up_attempted = False
        self._process_ids_before = self._polaris_process_ids()
        self._container_ids_before = self._polaris_containers()
        self._requested_slots = config.slots
        self._up_succeeded = False
        self._lifecycle_pids: set[int] = set()
        self._session_units: list[str] = []
        self._owned_processes: list[subprocess.Popen[bytes]] = []
        self._lifecycle_state_file = Path("/data/scratch/polaris-sim/state.json")

    def _log(self, step: str) -> Path:
        return self.config.output_dir / "logs" / f"{step}.log"

    def _polaris_process_ids(self) -> set[int] | None:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        pids = set()
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 1)
            markers = (
                "polaris-sim-gateway",
                "polaris-sim-ai",
                "scripts/polaris_sim/gateway.py",
                "scripts/polaris_sim/video.py",
                "scripts/polaris_sim/ai.py",
                "scripts/polaris_sim/lifecycle.py",
            )
            if len(fields) == 2 and any(marker in fields[1] for marker in markers):
                try:
                    pids.add(int(fields[0]))
                except ValueError:
                    pass
        return pids

    def _polaris_containers(self) -> set[str] | None:
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.ID}} {{.Names}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        return {
            line.split()[0]
            for line in result.stdout.splitlines()
            if len(line.split()) > 1
            and (
                "polaris-sim" in line.split(maxsplit=1)[1]
                or re.search(r"slot[-_]?([567])(?:[-_]|$)", line.split(maxsplit=1)[1])
            )
        }

    def _record_step(self, name: str, callback: Callable[[], dict[str, Any] | None]) -> None:
        started = utc_now()
        try:
            details = callback() or {}
        except Exception as exc:
            log = self._log(name)
            if not log.exists():
                _write_log(log, f"step={name}\nerror={type(exc).__name__}: {exc}\n")
            self.results[name] = StepResult(
                status="FAIL",
                started_at=started,
                ended_at=utc_now(),
                error=str(exc),
                log=str(log),
            )
            raise E2EFailure(f"step {name} failed: {exc}; log: {log}") from exc
        self.results[name] = StepResult(
            status="PASS",
            started_at=started,
            ended_at=utc_now(),
            details=details,
            log=str(self._log(name)) if self._log(name).exists() else None,
        )

    def _lifecycle_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("POLARIS_SIM_STATE_FILE", None)
        return env

    def _lifecycle_state(self) -> dict[str, Any]:
        path = self._lifecycle_state_file
        if not path.is_file():
            return {}
        value = _read_json(path)
        return value if isinstance(value, dict) else {}

    def start(self) -> dict[str, Any]:
        if self.no_up:
            return {"started": False, "attached": True, "slots": list(self.config.slots)}
        missing = [
            str(path)
            for path in (self.config.up_script, self.config.down_script)
            if not path.is_file()
        ]
        if missing:
            raise E2EFailure("missing lifecycle scripts: " + ", ".join(missing))
        active = subprocess.run(
            ["systemctl", "is-active", "psim-session"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if active.returncode == 0:
            raise E2EFailure("psim-session is already active; refusing to stop another run")
        self._up_attempted = True
        command = [
            "bash",
            str(self.config.up_script),
            "--world",
            self.config.world,
            "--vehicles",
            str(len(self.config.slots)),
            "--airframe",
            self.config.airframe,
            "--gpu",
            "auto",
        ]
        _run_command(
            command,
            self._log("up"),
            timeout=self.config.lifecycle_timeout_s,
            env=self._lifecycle_env(),
        )
        self._up_succeeded = True
        state = self._lifecycle_state()
        self._lifecycle_pids = {
            value for value in (state.get("pid"),) if isinstance(value, int)
        }
        self._session_units = [
            value for value in state.get("units", []) if isinstance(value, str)
        ]
        rows = _request_json(self.config, "GET", "/v1/vehicles").get("vehicles", [])
        actual_slots = tuple(
            sorted(
                item["slot"]
                for item in rows
                if isinstance(item, dict) and isinstance(item.get("slot"), int)
            )
        )
        required_slots = (
            self._requested_slots[:2]
            if len(self._requested_slots) == 3
            else self._requested_slots
        )
        if actual_slots not in {required_slots, self._requested_slots}:
            raise E2EFailure(
                f"up.sh started slots {actual_slots}; requested {self._requested_slots}"
            )
        self.config.slots = actual_slots
        return {
            "started": True,
            "requested_slots": list(self._requested_slots),
            "slots": list(actual_slots),
            "world": self.config.world,
            "airframe": self.config.airframe,
            "lifecycle_state": str(self._lifecycle_state_file),
        }

    def stop(self) -> dict[str, Any]:
        slots = self.config.slots
        _run_command(
            ["bash", str(self.config.down_script)],
            self._log("down"),
            timeout=self.config.lifecycle_timeout_s,
            env=self._lifecycle_env(),
        )
        time.sleep(0.2)
        pids_after = self._polaris_process_ids()
        containers_after = self._polaris_containers()
        new_pids = (pids_after or set()) - (self._process_ids_before or set())
        new_containers = (containers_after or set()) - (self._container_ids_before or set())
        lock_leftovers = []
        try:
            from scripts.sitl_lock import default_lock_path

            lock_root = default_lock_path()
            for slot in slots:
                lease = lock_root.with_name(f"{lock_root.name}.slot-{slot}")
                owner_path = lease / "owner"
                if owner_path.is_file():
                    owner = owner_path.read_text(encoding="utf-8").split()
                    if len(owner) > 1 and owner[0] == f"polaris-sim-slot-{slot}":
                        try:
                            if int(owner[1]) in self._lifecycle_pids:
                                lock_leftovers.append(str(lease))
                        except ValueError:
                            lock_leftovers.append(str(lease))
        except (ImportError, OSError):
            lock_leftovers.append("lease inspection unavailable")
        active_units = []
        units_to_check = set(self._session_units) | {"psim-session"}
        for unit in sorted(units_to_check):
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                active_units.append(unit)
        if (
            active_units
            or new_pids
            or new_containers
            or lock_leftovers
            or self._process_ids_before is None
            or pids_after is None
            or self._container_ids_before is None
            or containers_after is None
        ):
            process_check = self._process_ids_before is not None and pids_after is not None
            container_check = (
                self._container_ids_before is not None and containers_after is not None
            )
            raise E2EFailure(
                "cleanup incomplete: "
                f"active_units={active_units}, residual_pids={sorted(new_pids)}, "
                f"residual_container_ids={sorted(new_containers)}, leases={lock_leftovers}, "
                f"process_inspection={process_check}, container_inspection={container_check}"
            )
        return {
            "slots": slots,
            "residual_pids": [],
            "residual_containers": [],
            "residual_leases": lock_leftovers,
            "active_units": [],
        }

    def _vehicle_table(self) -> dict[int, int]:
        document = _request_json(self.config, "GET", "/v1/vehicles")
        vehicles = document.get("vehicles", document) if isinstance(document, dict) else document
        if not isinstance(vehicles, list):
            raise E2EFailure("GET /v1/vehicles did not return a vehicle list")
        table: dict[int, int] = {}
        for item in vehicles:
            if (
                isinstance(item, dict)
                and isinstance(item.get("slot"), int)
                and isinstance(item.get("sysid"), int)
            ):
                table[item["slot"]] = item["sysid"]
        for slot in self.config.slots:
            if table.get(slot) != SLOT_SYSID[slot]:
                raise E2EFailure(
                    f"vehicle slot/sysid contract mismatch for slot {slot}: "
                    f"expected sysid {SLOT_SYSID[slot]}, got {table.get(slot)}"
                )
        return table

    def _mav_connection(self, transport: str, expected_sysid: int | None = None) -> Any:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise E2EFailure("pymavlink is required; install the project pixhawk extra") from exc
        endpoint = (
            f"tcp:{self.config.host}:{self.config.mavlink_tcp_port}"
            if transport == "tcp"
            else f"udpout:{self.config.host}:{self.config.mavlink_udp_port}"
        )
        connection = mavutil.mavlink_connection(
            endpoint, source_system=255, source_component=190, autoreconnect=False
        )
        deadline = time.monotonic() + self.config.timeout_s
        heartbeat = None
        while time.monotonic() < deadline:
            connection.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0,
                0,
                mavutil.mavlink.MAV_STATE_ACTIVE,
            )
            heartbeat = connection.recv_match(type="HEARTBEAT", blocking=True, timeout=1.0)
            if heartbeat is None:
                continue
            if expected_sysid is None or heartbeat.get_srcSystem() == expected_sysid:
                break
            heartbeat = None
        if heartbeat is None:
            connection.close()
            raise E2EFailure(
                f"no MAVLink heartbeat over {transport} within {self.config.timeout_s:g}s"
            )
        return connection

    @staticmethod
    def _recv(
        connection: Any,
        message_type: str,
        timeout: float,
        predicate: Callable[[Any], bool] | None = None,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_heartbeat = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - last_heartbeat >= 1.0:
                dialect = mavutil.mavlink
                connection.mav.heartbeat_send(
                    dialect.MAV_TYPE_GCS,
                    dialect.MAV_AUTOPILOT_INVALID,
                    0,
                    0,
                    dialect.MAV_STATE_ACTIVE,
                )
                last_heartbeat = time.monotonic()
            message = connection.recv_match(
                type=message_type,
                blocking=True,
                timeout=min(1.0, max(0.05, deadline - time.monotonic())),
            )
            if message is not None and (predicate is None or predicate(message)):
                return message
        return None

    def _read_parameter(self, connection: Any, sysid: int) -> dict[str, Any]:
        connection.mav.param_request_read_send(sysid, 1, b"SYS_AUTOSTART", -1)
        value = self._recv(
            connection,
            "PARAM_VALUE",
            10.0,
            lambda msg: (
                msg.get_srcSystem() == sysid
                and (
                    msg.param_id.decode(errors="ignore")
                    if isinstance(msg.param_id, bytes)
                    else msg.param_id
                ).rstrip("\x00")
                == "SYS_AUTOSTART"
            ),
        )
        if value is None:
            raise E2EFailure(f"no SYS_AUTOSTART PARAM_VALUE from sysid {sysid}")
        name = (
            value.param_id.decode(errors="ignore")
            if isinstance(value.param_id, bytes)
            else value.param_id
        )
        return {"param_id": name.rstrip("\x00"), "value": value.param_value}

    def _command(
        self,
        connection: Any,
        sysid: int,
        command: int,
        params: tuple[float, ...],
        timeout: float = 10.0,
    ) -> Any:
        # A fresh SITL answers TEMPORARILY_REJECTED until EKF/home are ready.
        retry_until = time.monotonic() + 90.0
        while True:
            connection.mav.command_long_send(sysid, 1, command, 0, *params)
            ack = self._recv(
                connection,
                "COMMAND_ACK",
                timeout,
                lambda msg: msg.get_srcSystem() == sysid and msg.command == command,
            )
            if (
                ack is None
                or ack.result != mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED
                or time.monotonic() >= retry_until
            ):
                break
            time.sleep(2.0)
        if ack is None:
            raise E2EFailure(f"no COMMAND_ACK for command {command} from sysid {sysid}")
        accepted = {
            mavutil.mavlink.MAV_RESULT_ACCEPTED,
            mavutil.mavlink.MAV_RESULT_IN_PROGRESS,
        }
        if ack.result not in accepted:
            raise E2EFailure(f"command {command} rejected by sysid {sysid}: result={ack.result}")
        return ack

    def _wait_altitude(self, connection: Any, sysid: int, minimum: float, timeout: float) -> float:
        deadline = time.monotonic() + timeout
        last = None
        last_heartbeat = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - last_heartbeat >= 1.0:
                dialect = mavutil.mavlink
                connection.mav.heartbeat_send(
                    dialect.MAV_TYPE_GCS,
                    dialect.MAV_AUTOPILOT_INVALID,
                    0,
                    0,
                    dialect.MAV_STATE_ACTIVE,
                )
                last_heartbeat = time.monotonic()
            message = connection.recv_match(
                type="GLOBAL_POSITION_INT",
                blocking=True,
                timeout=min(1.0, max(0.05, deadline - time.monotonic())),
            )
            if message is None or message.get_srcSystem() != sysid:
                continue
            last = float(message.relative_alt) / 1000.0
            if last >= minimum:
                return last
        raise E2EFailure(
            f"sysid {sysid} did not reach {minimum:.1f} m; last relative altitude={last}"
        )

    def _wait_landed(self, connection: Any, sysid: int, timeout: float = 60.0) -> float:
        deadline = time.monotonic() + timeout
        last = None
        last_heartbeat = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - last_heartbeat >= 1.0:
                dialect = mavutil.mavlink
                connection.mav.heartbeat_send(
                    dialect.MAV_TYPE_GCS,
                    dialect.MAV_AUTOPILOT_INVALID,
                    0,
                    0,
                    dialect.MAV_STATE_ACTIVE,
                )
                last_heartbeat = time.monotonic()
            message = connection.recv_match(
                type="GLOBAL_POSITION_INT",
                blocking=True,
                timeout=min(1.0, max(0.05, deadline - time.monotonic())),
            )
            if message is None or message.get_srcSystem() != sysid:
                continue
            last = float(message.relative_alt) / 1000.0
            if last < 0.8:
                return last
        raise E2EFailure(f"sysid {sysid} did not land; last relative altitude={last}")

    def _flight_test(self, transport: str, slot: int, sysid: int) -> dict[str, Any]:
        from pymavlink import mavutil

        connection = self._mav_connection(transport, sysid)
        armed_or_airborne = False
        details: dict[str, Any] = {"transport": transport, "slot": slot, "sysid": sysid}
        try:
            details["heartbeat_source"] = connection.target_system
            details["parameter"] = self._read_parameter(connection, sysid)
            self._command(
                connection,
                sysid,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            )
            armed_or_airborne = True
            self._command(
                connection,
                sysid,
                mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                (0.0, 0.0, 0.0, float("nan"), 0.0, 0.0, 5.0),
            )
            details["takeoff_relative_altitude_m"] = self._wait_altitude(
                connection, sysid, 2.0, 45.0
            )
            if "gimbal" not in self.config.airframe:
                details["gimbal"] = "skipped: airframe has no gimbal"
            else:
                gimbal_configure = getattr(
                    mavutil.mavlink, "MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE", None
                )
                gimbal_pitchyaw = getattr(
                    mavutil.mavlink, "MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW", None
                )
                if gimbal_configure is None or gimbal_pitchyaw is None:
                    raise E2EFailure(
                        "installed pymavlink dialect lacks MAVLink Gimbal Manager v2 commands"
                    )
                details["gimbal_configure_ack"] = self._command(
                    connection,
                    sysid,
                    gimbal_configure,
                    (255.0, 190.0, -1.0, -1.0, 0.0, 0.0, 0.0),
                ).result
                details["gimbal_command_ack"] = self._command(
                    connection,
                    sysid,
                    gimbal_pitchyaw,
                    (-25.0, 0.0, float("nan"), float("nan"), 44.0, 0.0, 0.0),
                ).result
                gimbal = self._recv(
                    connection,
                    "GIMBAL_DEVICE_ATTITUDE_STATUS",
                    20.0,
                    lambda msg: msg.get_srcSystem() == sysid,
                )
                if gimbal is None:
                    raise E2EFailure(f"no gimbal attitude readback from sysid {sysid}")
                w, x, y, z = (float(value) for value in gimbal.q)
                pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x)))))
                yaw = math.degrees(
                    math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
                )
                pitch_error = abs((pitch + 25.0 + 180.0) % 360.0 - 180.0)
                yaw_error = abs((yaw + 180.0) % 360.0 - 180.0)
                if max(pitch_error, yaw_error) > 4.0:
                    raise E2EFailure(
                        "gimbal readback mismatch: requested pitch/yaw -25/0 deg, "
                        f"got {pitch:.1f}/{yaw:.1f} deg"
                    )
                details["gimbal_readback_deg"] = {"pitch": pitch, "yaw": yaw}
            self._command(
                connection,
                sysid,
                mavutil.mavlink.MAV_CMD_NAV_LAND,
                (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            )
            details["land_relative_altitude_m"] = self._wait_landed(connection, sysid)
            self._command(
                connection,
                sysid,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            )
            armed_or_airborne = False
            return details
        finally:
            if armed_or_airborne:
                try:
                    connection.mav.command_long_send(
                        sysid, 1, mavutil.mavlink.MAV_CMD_NAV_LAND, 0, 0, 0, 0, 0, 0, 0, 0
                    )
                except Exception:
                    pass
            connection.close()

    def mavlink(self) -> dict[str, Any]:
        table = self._vehicle_table()
        slot = self.config.slots[0]
        results = [self._flight_test(transport, slot, table[slot]) for transport in ("tcp", "udp")]
        return {"connections": results, "vehicle_sysids": table}

    def _video_probe(self, slot: int, camera: str = "cam") -> dict[str, Any]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise E2EFailure("ffprobe is not installed")
        index = slot - 5
        if camera not in {"cam", "gimbal"}:
            raise E2EFailure(f"unsupported RTSP camera mount: {camera}")
        url = f"rtsp://{self.config.host}:{self.config.rtsp_port}/v{index}/{camera}"
        command = [
            ffprobe,
            "-hide_banner",
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-rw_timeout",
            str(int(self.config.timeout_s * 1_000_000)),
            "-read_intervals",
            "%+1",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,nb_read_frames",
            "-show_frames",
            "-of",
            "json",
            url,
        ]
        started = time.monotonic()
        result = _run_command(
            command, self._log(f"ffprobe-slot-{slot}"), timeout=self.config.timeout_s
        )
        elapsed_ms = (time.monotonic() - started) * 1000
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise E2EFailure(
                f"ffprobe returned invalid JSON; log: {self._log(f'ffprobe-slot-{slot}')}"
            ) from exc
        metrics = validate_video_probe(
            document,
            min_width=self.config.min_width,
            min_height=self.config.min_height,
            min_fps=self.config.min_fps,
        )
        frames = document.get("frames", [])
        if not frames:
            raise E2EFailure(f"ffprobe decoded no frames from {url}")
        if elapsed_ms > self.config.max_video_startup_ms:
            raise E2EFailure(
                f"RTSP connect-to-first-decoded-frame time {elapsed_ms:.1f}ms exceeds "
                f"{self.config.max_video_startup_ms:.1f}ms"
            )
        return {
            "slot": slot,
            "camera": camera,
            "url": url,
            **metrics,
            "decoded_frames": len(frames),
            "connect_first_frame_ms": elapsed_ms,
        }

    @staticmethod
    def _free_port(kind: socket.SocketKind) -> int:
        with socket.socket(socket.AF_INET, kind) as probe:
            probe.bind(("0.0.0.0", 0))
            return int(probe.getsockname()[1])

    def _video_replay_paths(self) -> tuple[Path, Path, Path]:
        scripts = self.config.video_root / "scripts" / "polaris_sim"
        recording_module = (
            self.config.video_root
            / "src"
            / "multidetect"
            / "polaris_sim"
            / "recording.py"
        )
        replay_script = scripts / "replay.py"
        if not recording_module.is_file() or not replay_script.is_file():
            raise E2EFailure(
                "video lane recording.py/replay.py are missing; expected them under "
                f"{self.config.video_root}"
            )
        spec = importlib.util.spec_from_file_location(
            "_polaris_sim_recording", recording_module
        )
        if spec is None or spec.loader is None:
            raise E2EFailure(f"could not load video lane recording module: {recording_module}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return recording_module, replay_script, module.RecordingSession

    def _start_owned_process(
        self, command: list[str], log_path: Path
    ) -> subprocess.Popen[bytes]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("wb")
        try:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        finally:
            log_file.close()
        self._owned_processes.append(process)
        return process

    @staticmethod
    def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _probe_url(self, url: str, log_name: str) -> dict[str, Any]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise E2EFailure("ffprobe is not installed")
        command = [
            ffprobe,
            "-hide_banner",
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-rw_timeout",
            str(int(self.config.timeout_s * 1_000_000)),
            "-read_intervals",
            "%+1",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,nb_read_frames",
            "-show_frames",
            "-of",
            "json",
            url,
        ]
        started = time.monotonic()
        result = _run_command(command, self._log(log_name), timeout=self.config.timeout_s)
        elapsed_ms = (time.monotonic() - started) * 1000
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise E2EFailure(f"ffprobe returned invalid JSON for {url}") from exc
        metrics = validate_video_probe(
            document,
            min_width=self.config.min_width,
            min_height=self.config.min_height,
            min_fps=self.config.min_fps,
        )
        frames = document.get("frames", [])
        if not frames:
            raise E2EFailure(f"ffprobe decoded no frames from {url}")
        return {
            "url": url,
            **metrics,
            "decoded_frames": len(frames),
            "first_frame_ms": elapsed_ms,
        }

    def _probe_recorded_file(self, path: Path, index: int) -> dict[str, Any]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise E2EFailure("ffprobe is required to validate recorded media")
        command = [
            ffprobe,
            "-v",
            "error",
            "-threads",
            "2",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,nb_read_frames",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
        result = _run_command(
            command,
            self._log(f"record-file-ffprobe-{index}"),
            timeout=max(self.config.timeout_s, 60.0),
        )
        try:
            document = json.loads(result.stdout)
            stream = document["streams"][0]
            duration = float(document["format"]["duration"])
            frames = int(stream["nb_read_frames"])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise E2EFailure(f"ffprobe returned incomplete recording metadata for {path}") from exc
        metrics = validate_video_probe(
            {"streams": [stream]},
            min_width=self.config.min_width,
            min_height=self.config.min_height,
            min_fps=self.config.min_fps,
        )
        minimum_duration = min(8.0, self.config.record_seconds * 0.6)  # first keyframe + mux tail trim ~2 s
        if frames < 1 or duration < minimum_duration:
            raise E2EFailure(
                f"recorded video {path} has {frames} frames over {duration:.2f}s; "
                f"expected at least {minimum_duration:.2f}s"
            )
        return {"path": str(path), "duration_s": duration, "decoded_frames": frames, **metrics}

    def video(self) -> dict[str, Any]:
        cameras = ("cam", "gimbal") if "gimbal" in self.config.airframe else ("cam",)
        return {
            "streams": [
                self._video_probe(slot, camera)
                for slot in self.config.slots
                for camera in cameras
            ]
        }

    def _ping_samples(self, connection: Any, sysid: int, count: int = 8) -> list[float]:
        # PX4 does not answer MAVLink PING requests; a REQUEST_MESSAGE command round-trips instead.
        samples = []
        request_message = mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE
        for _ in range(count):
            sent_ns = time.monotonic_ns()
            connection.mav.command_long_send(
                sysid, 1, request_message, 0, 148.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
            )
            response = self._recv(
                connection,
                "COMMAND_ACK",
                3.0,
                lambda msg: msg.get_srcSystem() == sysid and msg.command == request_message,
            )
            if response is not None:
                samples.append((time.monotonic_ns() - sent_ns) / 1_000_000.0)
        if len(samples) < max(3, count // 2):
            raise E2EFailure(
                f"only {len(samples)}/{count} MAVLink command probes returned from sysid {sysid}"
            )
        return samples

    @staticmethod
    def _direction_config(document: Any, direction: str) -> dict[str, Any]:
        if isinstance(document, dict):
            nested = document.get(direction)
            if isinstance(nested, dict):
                return nested.copy()
            for key in ("directions", "config"):
                candidate = document.get(key)
                if isinstance(candidate, dict) and isinstance(candidate.get(direction), dict):
                    return candidate[direction].copy()
            if any(key in document for key in ("delay_ms", "jitter_ms", "loss_pct")):
                return {key: document.get(key, 0) for key in ("delay_ms", "jitter_ms", "loss_pct")}
        return {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0}

    def _put_netem(self, direction: str, values: dict[str, Any]) -> Any:
        payload = {
            "direction": direction,
            "delay_ms": values.get("delay_ms", 0),
            "jitter_ms": values.get("jitter_ms", 0),
            "loss_pct": values.get("loss_pct", 0),
        }
        return _request_json(self.config, "PUT", "/v1/netem", payload, write=True)

    def _video_latency(self, slot: int) -> float:
        from scripts.polaris_sim.measure_video_latency import measure

        result = measure(self.config.host, self.config.rtsp_port, f"/v{slot - 5}/cam", 6.0, 4.0)
        median = result.get("latency_ms_p50")
        if median is None:
            raise E2EFailure(f"no video latency samples for slot {slot}: {result}")
        return float(median)

    def netem(self) -> dict[str, Any]:
        table = self._vehicle_table()
        slot = self.config.slots[0]
        sysid = table[slot]
        connection = self._mav_connection("tcp", sysid)
        saved: dict[str, dict[str, Any]] = {}
        try:
            video_before = self._video_latency(slot)
            original = _request_json(self.config, "GET", "/v1/netem")
            saved = {
                direction: self._direction_config(original, direction)
                for direction in ("up", "down")
            }
            baseline = self._ping_samples(connection, sysid)
            for direction in ("up", "down"):
                self._put_netem(direction, {"delay_ms": 100, "jitter_ms": 0, "loss_pct": 5})
            delayed = self._ping_samples(connection, sysid, count=12)
            baseline_median = statistics.median(baseline)
            delayed_median = statistics.median(delayed)
            assert_latency_increased(baseline_median, delayed_median, 80.0)
            video_after = self._video_latency(slot)
            assert_latency_increased(video_before, video_after, 15.0)  # host load adds ±80 ms of noise to p50
            return {
                "direction_settings": {
                    "up": {"delay_ms": 100, "jitter_ms": 0, "loss_pct": 5},
                    "down": {"delay_ms": 100, "jitter_ms": 0, "loss_pct": 5},
                },
                "baseline_rtt_ms": baseline,
                "delayed_rtt_ms": delayed,
                "baseline_median_ms": baseline_median,
                "delayed_median_ms": delayed_median,
                "video_baseline_latency_p50_ms": video_before,
                "video_delayed_latency_p50_ms": video_after,
            }
        finally:
            restore_errors = []
            for direction in saved:
                try:
                    self._put_netem(direction, saved[direction])
                except E2EFailure as exc:
                    restore_errors.append(str(exc))
            connection.close()
            if restore_errors:
                raise E2EFailure(f"failed to restore original netem settings: {restore_errors}")

    def _sse_reader(
        self,
        events: queue.Queue[dict[str, Any]],
        ready: threading.Event,
        errors: list[str],
        stop: threading.Event,
        response_holder: list[Any],
    ) -> None:
        headers = {"Accept": "text/event-stream"}
        token = os.environ.get("POLARIS_SIM_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{self.config.ai_url}/v1/events", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
                response_holder.append(response)
                ready.set()
                event_name = ""
                data_lines: list[str] = []
                while not stop.is_set():
                    line = response.readline().decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            try:
                                payload = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError:
                                payload = {"data": "\n".join(data_lines)}
                            if isinstance(payload, dict):
                                events.put(
                                    {
                                        "event": event_name or payload.get("type", ""),
                                        "data": payload,
                                    }
                                )
                            data_lines.clear()
                            event_name = ""
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
        except Exception as exc:
            errors.append(str(exc))
        finally:
            ready.set()

    def discovery(self) -> dict[str, Any]:
        document = _request_json(self.config, "GET", "/v1/discovery")
        return validate_discovery(
            document,
            http_port=self.config.http_port,
            rtsp_port=self.config.rtsp_port,
            mavlink_tcp_port=self.config.mavlink_tcp_port,
            mavlink_udp_port=self.config.mavlink_udp_port,
            ai_port=self.config.ai_port,
        )

    def ai(self) -> dict[str, Any]:
        query = DEFAULT_QUERY
        if self.config.ai_query_file:
            query = _read_json(self.config.ai_query_file)
        if not isinstance(query, dict) or query.get("schema_version") != "1.0":
            raise E2EFailure("AI query must be a Spica TargetQuery object with schema_version 1.0")
        # Components are off by default (fail-closed); the e2e exercises the CPU-stub chain.
        import urllib.request

        enable = urllib.request.Request(
            f"{self.config.ai_url}/v1/components",
            data=json.dumps(
                {"components": {name: True for name in ("altair", "vega", "vlm", "mizar", "tracking", "publish_polaris")}}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(enable, timeout=5.0):
            pass
        query_path = self.config.output_dir / "ai-query.json"
        query_path.write_text(
            json.dumps(query, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        events: queue.Queue[dict[str, Any]] = queue.Queue()
        ready = threading.Event()
        stop = threading.Event()
        stream_errors: list[str] = []
        response_holder: list[Any] = []
        reader = threading.Thread(
            target=self._sse_reader,
            args=(events, ready, stream_errors, stop, response_holder),
            daemon=True,
        )
        reader.start()
        if not ready.wait(timeout=10.0):
            stop.set()
            for response_stream in response_holder:
                response_stream.close()
            reader.join(timeout=2.0)
            raise E2EFailure("SSE /v1/events did not open within 10 seconds")
        command = [
            sys.executable,
            str(self.config.ai_demo_script),
            "--base-url",
            self.config.ai_url,
            "--slot",
            str(self.config.slots[0]),
            "--query-json",
            str(query_path),
        ]
        token = os.environ.get("POLARIS_SIM_TOKEN")
        if token:
            command.extend(["--token", token])
        try:
            result = _run_command(
                command,
                self._log("ai-client-demo"),
                timeout=max(self.config.timeout_s, 90.0),
            )
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise E2EFailure("ai_client_demo did not print a JSON response") from exc
            envelope = response.get("result", response) if isinstance(response, dict) else {}
            request_id = envelope.get("id") if isinstance(envelope, dict) else None
            if not isinstance(request_id, str) or not request_id:
                raise E2EFailure("ai_client_demo response omitted Spica result id")
            response_summary = validate_ai_response(response, request_id)
            correlated: list[dict[str, Any]] = []
            event_wait_s = (
                max(self.config.timeout_s, 90.0)
                if response_summary["status"] == "ok"
                else min(max(self.config.timeout_s, 5.0), 10.0)
            )
            deadline = time.monotonic() + event_wait_s
            while time.monotonic() < deadline:
                try:
                    item = events.get(timeout=min(1.0, max(0.05, deadline - time.monotonic())))
                except queue.Empty:
                    if stream_errors:
                        break
                    continue
                data = item.get("data")
                if isinstance(data, dict) and data.get("request_id") == request_id:
                    correlated.append(item)
                    if item.get("event") == "published":
                        break
            if not correlated and stream_errors:
                raise E2EFailure(f"AI SSE stream failed: {stream_errors}")
            if not correlated and response_summary["status"] != "ok":
                raise E2EFailure(
                    "AI returned a fail-closed result without the required event pipeline: "
                    f"status={response_summary['status']}, runtime={response_summary['runtime']}"
                )
            names = validate_event_order(correlated)
        finally:
            stop.set()
            for response_stream in response_holder:
                response_stream.close()
            reader.join(timeout=2.0)
        return {
            "client": str(self.config.ai_demo_script),
            "request_id": request_id,
            "response": response_summary,
            "events": names,
            "event_count": len(correlated),
        }

    def doctor(self) -> dict[str, Any]:
        _request_json(self.config, "GET", "/health")
        document = _request_json(self.config, "GET", "/v1/doctor")
        return validate_doctor(document)

    def record(self) -> dict[str, Any]:
        record_dir = self.config.output_dir / "recordings"
        record_dir.mkdir(parents=True, exist_ok=True)
        _recording_module, replay_script, recording_session_type = self._video_replay_paths()
        vehicles = [
            {
                "slot": slot,
                "sysid": SLOT_SYSID[slot],
                "index": index,
                "cameras": ["cam", "gimbal"]
                if "gimbal" in self.config.airframe
                else ["cam"],
            }
            for index, slot in enumerate(self.config.slots)
        ]
        session = recording_session_type(
            gateway_host=self.config.host,
            rtsp_port=self.config.rtsp_port,
            mavlink_port=self.config.mavlink_udp_port,
            vehicles=vehicles,
            ai_events_url=f"{self.config.ai_url}/v1/events",
            root=record_dir,
            ffmpeg=shutil.which("ffmpeg") or "/usr/bin/ffmpeg",
        )
        session_started = False
        gateway_started = False
        gateway_start: dict[str, Any] = {}
        gateway_stop: dict[str, Any] = {}
        try:
            session.start()
            session_started = True
            gateway_start = _request_json(
                self.config, "POST", "/v1/record/start", {}, write=True
            )
            gateway_started = True
            time.sleep(self.config.record_seconds)
        finally:
            try:
                if gateway_started:
                    gateway_stop = _request_json(
                        self.config, "POST", "/v1/record/stop", {}, write=True
                    )
            finally:
                manifest = session.stop() if session_started else {}

        jsonl_path = Path(str(gateway_stop.get("path") or gateway_start.get("path") or ""))
        packet_count = gateway_stop.get("packets", 0)
        if not jsonl_path.is_file() or jsonl_path.suffix != ".jsonl":
            raise E2EFailure("gateway stop did not produce its MAVLink JSONL recording")
        if not isinstance(packet_count, int) or packet_count < 1:
            raise E2EFailure(f"gateway JSONL recording has no packets: {jsonl_path}")
        if manifest.get("recording_errors", {}).get("mavlink"):
            raise E2EFailure(
                "video lane MAVLink recorder failed: "
                f"{manifest['recording_errors']['mavlink']}"
            )
        video_rows = [
            item for item in manifest.get("files", []) if item.get("kind") == "video"
        ]
        tlog_rows = [
            item for item in manifest.get("files", []) if item.get("kind") == "mavlink_tlog"
        ]
        expected_video_count = len(self.config.slots) * (
            2 if "gimbal" in self.config.airframe else 1
        )
        if len(video_rows) < expected_video_count or not tlog_rows:
            raise E2EFailure(
                f"recording manifest has {len(video_rows)} video files and {len(tlog_rows)} tlogs"
            )
        for item in [*video_rows, *tlog_rows]:
            asset = session.path / str(item["path"])
            if not asset.is_file() or asset.stat().st_size != item.get("bytes"):
                raise E2EFailure(f"recorded asset is missing or size changed: {asset}")
        recorded_video = [
            self._probe_recorded_file(session.path / str(item["path"]), index)
            for index, item in enumerate(video_rows)
        ]

        replay_api = _request_json(
            self.config,
            "POST",
            "/v1/record/replay",
            {"recording_id": jsonl_path.name, "speed": 1.0},
            write=True,
        )
        if replay_api.get("status") != "replaying":
            raise E2EFailure(f"gateway MAVLink replay was not accepted: {replay_api}")

        replay_port = self._free_port(socket.SOCK_STREAM)
        replay_mavlink_port = self._free_port(socket.SOCK_DGRAM)
        command = [
            sys.executable,
            str(replay_script),
            str(session.path),
            "--host",
            "127.0.0.1",
            "--rtsp-port",
            str(replay_port),
            "--mavlink-host",
            "127.0.0.1",
            "--mavlink-port",
            str(replay_mavlink_port),
            "--loop",
        ]
        replay_process = self._start_owned_process(command, self._log("video-replay"))
        try:
            deadline = time.monotonic() + self.config.timeout_s
            replay_probes = []
            for item in video_rows:
                index = int(item["vehicle_index"])
                camera = str(item["camera"])
                replay_url = f"rtsp://127.0.0.1:{replay_port}/v{index}/{camera}"
                while True:
                    if replay_process.poll() is not None:
                        raise E2EFailure(
                            f"video replay exited with {replay_process.returncode}; log: "
                            f"{self._log('video-replay')}"
                        )
                    try:
                        replay_probes.append(
                            self._probe_url(replay_url, f"record-replay-ffprobe-v{index}")
                        )
                        break
                    except E2EFailure:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.25)
        finally:
            self._stop_owned_process(replay_process)
        return {
            "gateway_recording": str(jsonl_path),
            "gateway_packets": packet_count,
            "gateway_replay": replay_api,
            "video_recording_id": manifest.get("recording_id"),
            "video_recording_directory": str(session.path),
            "video_files": [item.get("path") for item in video_rows],
            "recorded_video": recorded_video,
            "recorded_seconds": self.config.record_seconds,
            "video_replay_probes": replay_probes,
        }

    def _wait_heartbeats(self, connection: Any, expected: set[int]) -> set[int]:
        from pymavlink import mavutil

        observed: set[int] = set()
        deadline = time.monotonic() + self.config.timeout_s
        while time.monotonic() < deadline and not expected.issubset(observed):
            connection.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0,
                0,
                mavutil.mavlink.MAV_STATE_ACTIVE,
            )
            heartbeat = connection.recv_match(type="HEARTBEAT", blocking=True, timeout=1.0)
            if heartbeat is not None and heartbeat.get_srcSystem() in expected:
                observed.add(heartbeat.get_srcSystem())
        if not expected.issubset(observed):
            raise E2EFailure(
                f"multi-vehicle link saw sysids {sorted(observed)}, expected {sorted(expected)}"
            )
        return observed

    def multi(self) -> dict[str, Any]:
        if len(self.config.slots) < 2:
            raise E2EFailure("multi step requires at least two configured slots (default: 5,6)")
        from pymavlink import mavutil

        table = self._vehicle_table()
        expected = {table[slot] for slot in self.config.slots[:2]}
        connection = self._mav_connection("tcp")
        try:
            observed = self._wait_heartbeats(connection, expected)
            command = mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE
            message_id = mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION
            acknowledgements = {}
            for sysid in sorted(expected):
                connection.mav.command_long_send(
                    sysid, 1, command, 0, float(message_id), 0, 0, 0, 0, 0, 0
                )
                ack = self._recv(
                    connection, "COMMAND_ACK", 10.0, lambda msg: msg.command == command
                )
                if ack is None or ack.get_srcSystem() != sysid:
                    source = ack.get_srcSystem() if ack is not None else None
                    raise E2EFailure(
                        f"target_system {sysid} command ACK came from {source}; "
                        f"expected only sysid {sysid}"
                    )
                extra_sources = set()
                drain_deadline = time.monotonic() + 0.5
                while time.monotonic() < drain_deadline:
                    extra_ack = connection.recv_match(
                        type="COMMAND_ACK",
                        blocking=True,
                        timeout=min(0.1, max(0.01, drain_deadline - time.monotonic())),
                    )
                    if extra_ack is not None and extra_ack.command == command:
                        extra_sources.add(extra_ack.get_srcSystem())
                cross_target_sources = extra_sources - {sysid}
                if cross_target_sources:
                    raise E2EFailure(
                        f"target_system {sysid} command also reached sysids "
                        f"{sorted(cross_target_sources)}"
                    )
                acknowledgements[str(sysid)] = True
            wrong_target = 42
            connection.mav.param_request_read_send(wrong_target, 1, b"SYS_AUTOSTART", -1)
            stray = self._recv(
                connection,
                "PARAM_VALUE",
                2.0,
                lambda msg: (
                    (
                        msg.param_id.decode(errors="ignore")
                        if isinstance(msg.param_id, bytes)
                        else msg.param_id
                    ).rstrip("\x00")
                    == "SYS_AUTOSTART"
                    and msg.get_srcSystem() in expected
                ),
            )
            if stray is not None:
                raise E2EFailure(
                    f"target_system=42 request leaked to sysid {stray.get_srcSystem()}"
                )
            return {
                "expected_sysids": sorted(expected),
                "observed_heartbeats": sorted(observed),
                "routed_commands": acknowledgements,
                "unknown_target_dropped": True,
            }
        finally:
            connection.close()

    def dry_run_plan(self) -> dict[str, Any]:
        commands = []
        if not self.no_up:
            commands.append(
                [
                    "bash",
                    str(self.config.up_script),
                    "--world",
                    self.config.world,
                    "--vehicles",
                    str(len(self.config.slots)),
                    "--airframe",
                    self.config.airframe,
                    "--gpu",
                    "auto",
                ]
            )
        actions = {
            "discovery": ["GET", f"{self.config.base_url}/v1/discovery"],
            "mavlink": ["MAVLink", "TCP :5770", "UDP :14550", "flight/gimbal/land"],
            "video": [
                "ffprobe",
                f"rtsp://{self.config.host}:{self.config.rtsp_port}/v{{index}}/cam",
            ],
            "netem": [
                "PUT",
                f"{self.config.base_url}/v1/netem",
                "100ms, 5% loss",
                "RTT and video-startup assertions",
            ],
            "ai": [
                str(self.config.ai_demo_script),
                "--base-url",
                self.config.ai_url,
                "--query-json",
                "Spica TargetQuery",
                "SSE request-id correlation",
            ],
            "doctor": ["GET", f"{self.config.base_url}/v1/doctor"],
            "record": [
                "HTTP",
                f"{self.config.base_url}/v1/record/start",
                "capture H.264 + MAVLink tlog",
                str(self.config.record_seconds),
                "POST /v1/record/stop and /v1/record/replay",
                "video replay CLI + ffprobe",
            ],
            "multi": ["MAVLink TCP", "check sysid isolation and target routing"],
        }
        commands.extend(actions[step] for step in self.config.steps)
        if not self.no_down:
            commands.append(["bash", str(self.config.down_script)])
        return {
            "commands": commands,
            "slots": list(self.config.slots),
            "steps": list(self.config.steps),
        }

    def execute(self) -> int:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        if self.dry_run:
            self.results["dry_run"] = StepResult(
                status="DRY RUN",
                started_at=utc_now(),
                ended_at=utc_now(),
                details=self.dry_run_plan(),
            )
            self._write_reports("DRY RUN")
            print(f"DRY RUN: plan and reports at {self.config.output_dir}")
            return 0
        failed: E2EFailure | None = None
        try:
            if not self.no_up:
                try:
                    self._record_step("up", self.start)
                except E2EFailure as exc:
                    failed = exc
            if failed is None:
                for name in self.config.steps:
                    try:
                        callback = getattr(self, name)
                        self._record_step(name, callback)
                    except E2EFailure as exc:
                        failed = failed or exc
                        if name == "mavlink" or "failed to restore original netem" in str(exc):
                            break
        except KeyboardInterrupt:
            failed = failed or E2EFailure("interrupted by user; owned SITL cleanup will run")
        finally:
            if self._up_succeeded and not self.no_down:
                try:
                    self._record_step("down", self.stop)
                except E2EFailure as exc:
                    failed = failed or exc
            elif self._up_attempted and not self.no_down:
                self.results["down"] = StepResult(
                    status="未验证",
                    started_at=utc_now(),
                    ended_at=utc_now(),
                    error=(
                        "up.sh did not report a successful owned session; no down.sh was issued "
                        "to avoid stopping a session not proven to belong to this run. "
                        f"See {self._log('up')} for lifecycle rollback details."
                    ),
                )
        complete_run = (
            set(self.config.steps) == set(DEFAULT_STEPS) and not self.no_up and not self.no_down
        )
        status = "FAIL" if failed else "PASS" if complete_run else "PARTIAL"
        self._write_reports(status)
        if failed:
            print(f"FAIL: {failed}\nReports: {self.config.output_dir}", file=sys.stderr)
            return 1
        print(f"{status}: {', '.join(self.config.steps)}\nReports: {self.config.output_dir}")
        return 0

    def _write_reports(self, status: str) -> None:
        document = {
            "schema_version": 1,
            "run_id": self.run_id,
            "created_at": utc_now(),
            "status": status,
            "host": self.config.host,
            "slots": list(self.config.slots),
            "world": self.config.world,
            "selected_steps": list(self.config.steps),
            "results": {name: asdict(result) for name, result in self.results.items()},
        }
        json_path = self.config.output_dir / "report.json"
        md_path = self.config.output_dir / "report.md"
        json_path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        rows = [
            "# Polaris SITL 端到端验收",
            "",
            f"- 结果：**{status}**",
            f"- 时间：`{document['created_at']}`",
            f"- 网关：`{self.config.host}`",
            f"- SITL slots：`{', '.join(map(str, self.config.slots))}`",
            f"- 世界：`{self.config.world}`",
            "",
            "| 步骤 | 状态 | 说明 / 日志 |",
            "|---|---|---|",
        ]
        all_names = ["up", *DEFAULT_STEPS, "down"]
        for name in dict.fromkeys(all_names):
            result = self.results.get(name)
            if result is None:
                rows.append(f"| `{name}` | 未验证 | 未运行 |")
                continue
            summary = result.error or result.log or json.dumps(result.details, ensure_ascii=False)
            safe_summary = summary.replace("|", "\\|")
            rows.append(f"| `{name}` | {result.status} | {safe_summary} |")
        rows.extend(["", f"JSON：`{json_path}`  ", f"Markdown：`{md_path}`", ""])
        md_path.write_text("\n".join(rows), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("POLARIS_SIM_HOST", "192.168.1.138"))
    parser.add_argument("--http-port", type=int, default=8780)
    parser.add_argument("--mavlink-tcp-port", type=int, default=5770)
    parser.add_argument("--mavlink-udp-port", type=int, default=14550)
    parser.add_argument("--rtsp-port", type=int, default=8554)
    parser.add_argument("--ai-port", type=int, default=8790)
    parser.add_argument("--slots", type=parse_slots, default=(5, 6))
    parser.add_argument("--world", default="rw_suburb_davis")
    parser.add_argument("--airframe", default="x500_gimbal")
    parser.add_argument("--steps", type=parse_steps, default=DEFAULT_STEPS)
    parser.add_argument("--output-root", type=Path, default=Path("/data/scratch/polaris-sim/e2e"))
    parser.add_argument(
        "--up-script",
        type=Path,
        default=lane_file(
            ROOT / "scripts/polaris_sim/up.sh",
            LANE_ROOT / "scripts/polaris_sim/up.sh",
        ),
    )
    parser.add_argument(
        "--down-script",
        type=Path,
        default=lane_file(
            ROOT / "scripts/polaris_sim/down.sh",
            LANE_ROOT / "scripts/polaris_sim/down.sh",
        ),
    )
    parser.add_argument(
        "--ai-demo-script",
        type=Path,
        default=lane_file(
            ROOT / "scripts/polaris_sim/ai_client_demo.py",
            AI_LANE_ROOT / "scripts/polaris_sim/ai_client_demo.py",
        ),
    )
    parser.add_argument("--video-root", type=Path, default=VIDEO_LANE_ROOT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--lifecycle-timeout", type=float, default=900.0)
    parser.add_argument("--record-seconds", type=float, default=10.0)
    parser.add_argument("--min-width", type=int, default=320)
    parser.add_argument("--min-height", type=int, default=240)
    parser.add_argument("--min-fps", type=float, default=1.0)
    parser.add_argument("--max-video-startup-ms", type=float, default=15000.0)
    parser.add_argument(
        "--ai-query-file",
        type=Path,
        help="Spica TargetQuery JSON (the request envelope is added by the client)",
    )
    parser.add_argument("--no-up", action="store_true", help="连接已运行的网关和 SITL，不启停服务")
    parser.add_argument(
        "--no-down", action="store_true", help="不运行 down.sh；仅允许与 --no-up 同用"
    )
    parser.add_argument("--dry-run", action="store_true", help="只生成计划和报告，不连接或启动服务")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.lifecycle_timeout <= 0 or args.record_seconds < 1:
        parser.error("timeouts must be positive and --record-seconds must be at least 1")
    if args.no_down and not args.no_up:
        parser.error("--no-down is allowed only with --no-up; started SITL must be cleaned up")
    run_dir = args.output_root / timestamp()
    config = Config(
        host=args.host,
        http_port=args.http_port,
        mavlink_tcp_port=args.mavlink_tcp_port,
        mavlink_udp_port=args.mavlink_udp_port,
        rtsp_port=args.rtsp_port,
        ai_port=args.ai_port,
        slots=args.slots,
        world=args.world,
        airframe=args.airframe,
        steps=args.steps,
        output_dir=run_dir,
        up_script=args.up_script,
        down_script=args.down_script,
        timeout_s=args.timeout,
        lifecycle_timeout_s=args.lifecycle_timeout,
        record_seconds=args.record_seconds,
        min_width=args.min_width,
        min_height=args.min_height,
        min_fps=args.min_fps,
        max_video_startup_ms=args.max_video_startup_ms,
        ai_query_file=args.ai_query_file,
        ai_demo_script=args.ai_demo_script,
        video_root=args.video_root,
    )
    return PolarisE2E(
        config, dry_run=args.dry_run, no_up=args.no_up, no_down=args.no_down
    ).execute()


if __name__ == "__main__":
    raise SystemExit(main())
