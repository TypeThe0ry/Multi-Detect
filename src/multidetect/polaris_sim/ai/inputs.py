"""CPU-only camera frame capture and read-only SITL telemetry sampling."""

from __future__ import annotations

import importlib
import ipaddress
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field

_CURRENT_FRAME: ContextVar["FrameSample | None"] = ContextVar("polaris_sim_frame", default=None)
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(item)
    for item in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def is_private_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return any(
        address in network
        for network in _PRIVATE_NETWORKS
        if address.version == network.version
    )


@dataclass(frozen=True, slots=True)
class FrameSample:
    frame_id: str
    slot: int
    captured_at_s: float
    source: str
    image: object | None = None
    jpeg: bytes | None = None
    telemetry: Mapping[str, object] = field(default_factory=dict)
    warning: str | None = None


def current_frame() -> FrameSample | None:
    return _CURRENT_FRAME.get()


def bind_frame(sample: FrameSample):
    return _CURRENT_FRAME.set(sample)


def unbind_frame(token: object) -> None:
    _CURRENT_FRAME.reset(token)  # type: ignore[arg-type]


class MavlinkTelemetryReader:
    """Read-only cache. This client never sends MAVLink messages."""

    def __init__(self, endpoint: str, *, enabled: bool = True) -> None:
        self.endpoint = endpoint
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._by_system: dict[int, dict[str, object]] = {}
        self._status = "disabled" if not enabled else "not_started"

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="polaris-sim-mavlink-read",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def snapshot(self, slot: int) -> dict[str, object]:
        system_id = slot - 4
        with self._lock:
            result = dict(self._by_system.get(system_id, {}))
        if result:
            now = time.monotonic()
            position_time = result.pop("position_received_at_s", None)
            gps_time = result.pop("gps_received_at_s", None)
            position_age_ms = (
                None
                if position_time is None
                else max(0.0, (now - float(position_time)) * 1000.0)
            )
            gps_age_ms = (
                None if gps_time is None else max(0.0, (now - float(gps_time)) * 1000.0)
            )
            result["age_ms"] = position_age_ms
            result["fresh"] = position_age_ms is not None and position_age_ms <= 2_000.0
            result["gps_age_ms"] = gps_age_ms
            result["gps_fresh"] = gps_age_ms is not None and gps_age_ms <= 2_000.0
        return result

    def _run(self) -> None:
        try:
            mavutil = importlib.import_module("pymavlink.mavutil")
        except ImportError:
            with self._lock:
                self._status = "pymavlink_unavailable"
            return
        while not self._stop.is_set():
            connection = None
            try:
                connection = mavutil.mavlink_connection(self.endpoint, autoreconnect=False)
                with self._lock:
                    self._status = "connected"
                while not self._stop.is_set():
                    message = connection.recv_match(blocking=True, timeout=1.0)
                    if message is None:
                        continue
                    self._record(message)
            except Exception:
                with self._lock:
                    self._status = "disconnected"
                self._stop.wait(2.0)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

    def _record(self, message: object) -> None:
        kind = str(message.get_type())
        system_id = int(message.get_srcSystem())
        now = time.monotonic()
        with self._lock:
            row = self._by_system.setdefault(system_id, {})
            if kind == "GLOBAL_POSITION_INT":
                row["position_received_at_s"] = now
                row.update(
                    latitude_deg=float(message.lat) / 1e7,
                    longitude_deg=float(message.lon) / 1e7,
                    altitude_m=float(message.alt) / 1000.0,
                    relative_altitude_m=float(message.relative_alt) / 1000.0,
                    heading_deg=float(message.hdg) / 100.0 if int(message.hdg) != 65535 else None,
                )
            elif kind == "ATTITUDE":
                row.update(
                    roll_rad=float(message.roll),
                    pitch_rad=float(message.pitch),
                    yaw_rad=float(message.yaw),
                )
            elif kind == "GPS_RAW_INT":
                row["gps_received_at_s"] = now
                row.update(
                    gps_fix_type=int(message.fix_type),
                    satellites_visible=int(message.satellites_visible),
                    horizontal_accuracy_m=(
                        float(message.eph) / 100.0 if int(message.eph) != 65535 else None
                    ),
                )
            elif kind == "GIMBAL_DEVICE_ATTITUDE_STATUS":
                row["gimbal_quaternion"] = list(message.q)


class CameraFrameSource:
    def __init__(self, config: Mapping[str, object]) -> None:
        self.template = str(config.get("rtsp_template", "rtsp://127.0.0.1:8554/v{index}/cam"))
        rtsp_by_slot = config.get("rtsp_by_slot", {})
        self.rtsp_by_slot = (
            {int(slot): str(url) for slot, url in rtsp_by_slot.items()}
            if isinstance(rtsp_by_slot, Mapping)
            else {}
        )
        self.fallback_video = config.get("fallback_video")
        self.ffmpeg = str(config.get("ffmpeg", "ffmpeg"))
        self.timeout_s = max(0.2, min(10.0, float(config.get("timeout_s", 3.0))))
        self.fallback_fps = max(0.2, min(60.0, float(config.get("fallback_fps", 10.0))))
        self.fallback_loop = bool(config.get("fallback_loop", True))
        self._fallback_index = 0
        self._fallback_lock = threading.Lock()

    def capture(self, slot: int) -> tuple[bytes | None, str, str | None]:
        index = slot - 5
        url = self.rtsp_by_slot.get(slot)
        if url is None:
            url = self.template.format(index=index, slot=slot)
        jpeg, error = self._one_jpeg(url, rtsp=True)
        if jpeg is not None:
            return jpeg, "rtsp", None
        fallback = self.fallback_video
        if isinstance(fallback, str) and fallback:
            with self._fallback_lock:
                seek_s = self._fallback_index / self.fallback_fps
                self._fallback_index += 1
            jpeg, fallback_error = self._one_jpeg(fallback, rtsp=False, seek_s=seek_s)
            if jpeg is not None:
                return jpeg, "replay", error
            error = fallback_error or error
        return None, "unavailable", error

    def _one_jpeg(
        self,
        source: str,
        *,
        rtsp: bool,
        seek_s: float = 0.0,
    ) -> tuple[bytes | None, str | None]:
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if rtsp:
            command.extend(["-rtsp_transport", "tcp"])
        elif self.fallback_loop:
            command.extend(["-stream_loop", "-1"])
        if not rtsp and seek_s > 0:
            command.extend(["-ss", f"{seek_s:.3f}"])
        command.extend(
            ["-i", source, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError:
            return None, "ffmpeg_unavailable"
        except subprocess.TimeoutExpired:
            return None, "frame_timeout"
        if result.returncode != 0 or not result.stdout:
            return None, "frame_unavailable"
        if len(result.stdout) > 16 * 1024 * 1024:
            return None, "frame_too_large"
        return result.stdout, None


class SimulationInputs:
    def __init__(self, config: Mapping[str, object]) -> None:
        camera = config.get("camera", {})
        telemetry = config.get("telemetry", {})
        self.camera = CameraFrameSource(camera if isinstance(camera, Mapping) else {})
        telemetry_cfg = telemetry if isinstance(telemetry, Mapping) else {}
        self.telemetry = MavlinkTelemetryReader(
            str(telemetry_cfg.get("endpoint", "tcp:127.0.0.1:5770")),
            enabled=bool(telemetry_cfg.get("enabled", True)),
        )

    def start(self) -> None:
        self.telemetry.start()

    def close(self) -> None:
        self.telemetry.close()

    def capture(self, slot: int) -> FrameSample:
        jpeg, source, warning = self.camera.capture(slot)
        image: object | None = None
        if jpeg is not None:
            try:
                cv2 = importlib.import_module("cv2")
                numpy = importlib.import_module("numpy")
                image = cv2.imdecode(numpy.frombuffer(jpeg, dtype=numpy.uint8), cv2.IMREAD_COLOR)
            except ImportError:
                image = jpeg
        return FrameSample(
            frame_id=f"sim-{slot}-{uuid.uuid4().hex[:12]}",
            slot=slot,
            captured_at_s=time.monotonic(),
            source=source,
            image=image,
            jpeg=jpeg,
            telemetry=self.telemetry.snapshot(slot),
            warning=warning,
        )

