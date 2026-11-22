"""Recording manager shared by the gateway control API and its CLI client."""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
DEFAULT_RECORDINGS_ROOT = Path("/data/scratch/polaris-sim/recordings")


def _mavlink_heartbeat() -> bytes:
    try:
        from pymavlink.dialects.v20 import common
    except ImportError as exc:
        raise RuntimeError(
            "MAVLink recording needs the existing pixhawk extra: pip install -e '.[pixhawk]'"
        ) from exc
    encoder = common.MAVLink(None, srcSystem=255, srcComponent=190)
    return common.MAVLink_heartbeat_message(
        type=common.MAV_TYPE_GCS,
        autopilot=common.MAV_AUTOPILOT_INVALID,
        base_mode=0,
        custom_mode=0,
        system_status=common.MAV_STATE_ACTIVE,
        mavlink_version=3,
    ).pack(encoder)


def split_mavlink_frames(datagram: bytes) -> list[bytes]:
    """Split complete MAVLink v1/v2 frames from one UDP datagram."""
    frames: list[bytes] = []
    offset = 0
    while offset < len(datagram):
        start = offset
        while start < len(datagram) and datagram[start] not in (0xFE, 0xFD):
            start += 1
        if start >= len(datagram):
            break
        magic = datagram[start]
        if magic == 0xFE and start + 2 <= len(datagram):
            size = datagram[start + 1] + 8
        elif magic == 0xFD and start + 3 <= len(datagram):
            size = datagram[start + 1] + 12 + (13 if datagram[start + 2] & 1 else 0)
        else:
            break
        end = start + size
        if end > len(datagram):
            break
        frames.append(datagram[start:end])
        offset = end
    return frames


class MavlinkTlogRecorder:
    def __init__(self, gateway_host: str, gateway_port: int, output: Path) -> None:
        self.gateway = (gateway_host, gateway_port)
        self.output = output
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        heartbeat = _mavlink_heartbeat()
        self.thread = threading.Thread(
            target=self._run, args=(heartbeat,), name="polaris-tlog", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def _run(self, heartbeat: bytes) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
                channel.connect(self.gateway)
                channel.settimeout(0.25)
                with self.output.open("wb") as logfile:
                    next_heartbeat = 0.0
                    while not self.stop_event.is_set():
                        now = time.monotonic()
                        if now >= next_heartbeat:
                            channel.send(heartbeat)
                            next_heartbeat = now + 1.0
                        try:
                            datagram = channel.recv(65_535)
                        except TimeoutError:
                            continue
                        stamp = int(time.time() * 1_000_000)
                        for frame in split_mavlink_frames(datagram):
                            logfile.write(struct.pack(">Q", stamp) + frame)
                        logfile.flush()
        except (OSError, RuntimeError) as exc:
            self.error = str(exc)
            LOG.error("MAVLink tlog recorder failed: %s", exc)


class AiEventRecorder:
    def __init__(self, url: str, output: Path) -> None:
        self.url, self.output = url, output
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="polaris-ai-events", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def _run(self) -> None:
        try:
            request = Request(self.url, headers={"Accept": "text/event-stream"})
            with (
                urlopen(request, timeout=3) as response,
                self.output.open("a", encoding="utf-8") as out,
            ):
                data_lines: list[str] = []
                while not self.stop_event.is_set():
                    line = response.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if text.startswith("data:"):
                        data_lines.append(text[5:].lstrip())
                    elif not text and data_lines:
                        payload = "\n".join(data_lines)
                        try:
                            record = json.loads(payload)
                        except json.JSONDecodeError:
                            record = {"raw": payload}
                        out.write(
                            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                        )
                        out.flush()
                        data_lines.clear()
        except (OSError, URLError, TimeoutError) as exc:
            self.error = str(exc)
            LOG.warning("AI event stream unavailable: %s", exc)


class RecordingSession:
    """One run of video remux, MAVLink tlog, and optional AI event capture."""

    def __init__(
        self,
        *,
        gateway_host: str,
        rtsp_port: int = 8554,
        mavlink_port: int = 14550,
        vehicles: list[dict[str, Any]],
        ai_events_url: str | None = None,
        root: Path = DEFAULT_RECORDINGS_ROOT,
        ffmpeg: str = "/usr/bin/ffmpeg",
    ) -> None:
        if not vehicles:
            raise ValueError("at least one vehicle is required")
        self.gateway_host, self.rtsp_port, self.mavlink_port = gateway_host, rtsp_port, mavlink_port
        self.vehicles = vehicles
        self.ai_events_url, self.root, self.ffmpeg = ai_events_url, root, ffmpeg
        self.started_at = datetime.now(UTC)
        self.path = root / self.started_at.strftime("%Y%m%dT%H%M%S.%fZ")
        self.path.mkdir(parents=True, exist_ok=False)
        self.processes: list[tuple[subprocess.Popen[bytes], Path, dict[str, Any], str]] = []
        self.tlog: MavlinkTlogRecorder | None = None
        self.events: AiEventRecorder | None = None
        self.stopped_at: datetime | None = None
        self.video_failures: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def start(self) -> dict[str, Any]:
        try:
            for vehicle in self.vehicles:
                index = int(vehicle["index"])
                kinds = vehicle.get("cameras", ["cam"])
                for kind in kinds:
                    if kind not in {"cam", "gimbal"}:
                        raise ValueError(f"unknown camera kind: {kind}")
                    output = self.path / f"v{index}-{kind}.mkv"
                    url = f"rtsp://{self.gateway_host}:{self.rtsp_port}/v{index}/{kind}"
                    command = [
                        self.ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "warning",
                        "-nostdin",
                        "-rtsp_transport",
                        "tcp",
                        "-i",
                        url,
                        "-map",
                        "0:v:0",
                        "-an",
                        "-c:v",
                        "copy",
                        "-f",
                        "matroska",
                        str(output),
                    ]
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.processes.append((process, output, vehicle, kind))
            self.tlog = MavlinkTlogRecorder(
                self.gateway_host, self.mavlink_port, self.path / "mavlink.tlog"
            )
            self.tlog.start()
            if self.ai_events_url:
                self.events = AiEventRecorder(self.ai_events_url, self.path / "ai-events.jsonl")
                self.events.start()
            return {
                "recording_id": self.path.name,
                "directory": str(self.path),
                "state": "recording",
            }
        except BaseException:
            self.stop()
            raise

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self.stopped_at is not None:
                return self._manifest()
            self.stopped_at = datetime.now(UTC)
            if self.tlog is not None:
                self.tlog.stop()
            if self.events is not None:
                self.events.stop()
            for process, _, _, _ in self.processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            self.video_failures = [
                {
                    "vehicle_index": int(vehicle["index"]),
                    "camera": kind,
                    "exit_code": process.returncode,
                }
                for process, _, vehicle, kind in self.processes
                if process.returncode not in (0, -15)
            ]
            manifest = self._manifest()
            (self.path / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return manifest

    def _manifest(self) -> dict[str, Any]:
        files = []
        for _process, path, vehicle, kind in self.processes:
            if path.is_file() and path.stat().st_size:
                files.append(
                    _file_record(
                        path, kind="video", vehicle_index=int(vehicle["index"]), camera=kind
                    )
                )
        tlog_path = self.path / "mavlink.tlog"
        if tlog_path.is_file() and tlog_path.stat().st_size:
            files.append(_file_record(tlog_path, kind="mavlink_tlog"))
        events_path = self.path / "ai-events.jsonl"
        if events_path.is_file() and events_path.stat().st_size:
            files.append(_file_record(events_path, kind="ai_events_jsonl"))
        return {
            "schema": "multidetect.polaris-sim.recording",
            "schema_version": 1,
            "recording_id": self.path.name,
            "started_at_utc": self.started_at.isoformat().replace("+00:00", "Z"),
            "ended_at_utc": (self.stopped_at or datetime.now(UTC))
            .isoformat()
            .replace("+00:00", "Z"),
            "vehicles": self.vehicles,
            "files": files,
            "recording_errors": {
                "mavlink": self.tlog.error if self.tlog else None,
                "ai_events": self.events.error if self.events else None,
                "video": self.video_failures,
            },
            "ai_events_status": (
                "not_configured"
                if self.events is None
                else "unavailable"
                if self.events.error
                else "captured"
            ),
        }


def _file_record(path: Path, **metadata: Any) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        **metadata,
    }
