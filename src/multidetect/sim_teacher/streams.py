"""Live Gazebo MDGZSIM3 and MAVLink readers for SIM teacher episodes."""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO


@dataclass(slots=True)
class SensorSample:
    rgb: bytes
    width: int
    height: int
    depth: Any | None
    camera_info: dict[str, Any]
    pose: dict[str, Any]
    velocity: list[float]
    sensor_stamp_ns: int
    received_monotonic_ns: int
    received_wall_time: float
    source_header: Any = None
    depth_frame: Any = None


def _import_wire_helpers() -> tuple[Any, Any, Any]:
    try:
        from scripts.gazebo_depth_wire import read_depth_wire_frame
        from scripts.gazebo_sensor_stream import read_wire_frame
    except ModuleNotFoundError:
        from gazebo_depth_wire import read_depth_wire_frame  # type: ignore
        from gazebo_sensor_stream import read_wire_frame  # type: ignore
    try:
        import numpy as np
    except ImportError:
        np = None
    return read_wire_frame, read_depth_wire_frame, np


def _header_camera_info(header: Any) -> dict[str, Any]:
    return {
        "width": int(header.width),
        "height": int(header.height),
        "camera_info_stamp_ns": int(header.camera_info_stamp_ns),
    }


class SensorStreamReader:
    """Continuously decode MDGZSIM3 (and optional MDGZDEP1) from a pipe."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        depth_binding: Mapping[str, object] | None = None,
        camera_info: Mapping[str, object] | None = None,
        max_frames: int | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
        require_depth: bool = True,
    ) -> None:
        self.stream = stream
        self.depth_binding = dict(depth_binding) if depth_binding else None
        self.camera_info = dict(camera_info or {})
        self.max_frames = max_frames
        self.clock = clock
        self.require_depth = require_depth
        self.latest: SensorSample | None = None
        self.frames_received = 0
        self.error: BaseException | None = None
        self.done = threading.Event()
        self.first_frame = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> SensorStreamReader:
        if self._thread is not None:
            raise RuntimeError("sensor reader already started")
        self._thread = threading.Thread(target=self._run, name="sim-teacher-sensor", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        try:
            self.stream.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_s))

    def wait_first_frame(self, timeout_s: float) -> SensorSample:
        if not self.first_frame.wait(timeout=max(0.0, timeout_s)):
            if self.error is not None:
                raise RuntimeError(f"sensor stream failed: {self.error}") from self.error
            raise TimeoutError("sensor stream produced no frame before timeout")
        if self.latest is None:
            raise RuntimeError("sensor stream signalled first frame without a sample")
        return self.latest

    def raise_if_failed(self) -> None:
        if self.error is not None:
            raise RuntimeError(f"sensor stream failed: {self.error}") from self.error
        if self.done.is_set() and not self._stop.is_set():
            raise RuntimeError("sensor stream ended unexpectedly")

    def snapshot(self, *, max_age_s: float | None = None) -> SensorSample:
        if self.latest is None:
            raise RuntimeError("sensor stream has no decoded frame")
        if max_age_s is not None:
            age = (self.clock() - self.latest.received_monotonic_ns) / 1e9
            if age > max_age_s:
                raise RuntimeError(f"sensor frame is stale: {age:.3f}s > {max_age_s:.3f}s")
        return self.latest

    def _run(self) -> None:
        read_wire_frame, read_depth_wire_frame, np = _import_wire_helpers()
        try:
            order = None
            try:
                from scripts.gazebo_sensor_stream import WireOrderValidator

                order = WireOrderValidator()
            except Exception:
                pass
            while not self._stop.is_set() and (
                self.max_frames is None or self.frames_received < self.max_frames
            ):
                header, payload = read_wire_frame(self.stream, order=order)
                depth_frame = depth = None
                if self.depth_binding is not None:
                    depth_frame = read_depth_wire_frame(
                        self.stream,
                        expected_sequence=int(header.sequence),
                        expected_rgb_sensor_stamp_ns=int(header.sensor_stamp_ns),
                        expected_rgb_record_sha256=bytes(header.sha256).hex(),
                        expected_source_topic=str(self.depth_binding["source_topic"]),
                        expected_calibration_id=str(self.depth_binding["calibration_id"]),
                        expected_calibration_sha256=str(self.depth_binding["calibration_sha256"]),
                    )
                    if np is not None:
                        dtype = np.float32 if depth_frame.encoding == "FLOAT32_M" else np.uint16
                        depth = (
                            np.frombuffer(depth_frame.payload, dtype=dtype)
                            .reshape((depth_frame.height, depth_frame.width))
                            .copy()
                        )
                elif self.require_depth:
                    raise RuntimeError("MDGZSIM3 stream has no MDGZDEP1 depth binding")
                stamp = self.clock()
                self.latest = SensorSample(
                    rgb=bytes(payload),
                    width=int(header.width),
                    height=int(header.height),
                    depth=depth,
                    camera_info={**_header_camera_info(header), **self.camera_info},
                    pose={
                        "local_enu_m": [
                            float(header.position_east_m),
                            float(header.position_north_m),
                            float(header.position_up_m),
                        ],
                        "lat_deg": float(header.latitude_deg),
                        "lon_deg": float(header.longitude_deg),
                        "alt_m": float(header.altitude_m),
                        "orientation_quaternion": [
                            float(header.orientation_x),
                            float(header.orientation_y),
                            float(header.orientation_z),
                            float(header.orientation_w),
                        ],
                    },
                    velocity=[
                        float(header.velocity_forward_mps),
                        float(header.velocity_left_mps),
                        float(header.velocity_up_mps),
                    ],
                    sensor_stamp_ns=int(header.sensor_stamp_ns),
                    received_monotonic_ns=stamp,
                    received_wall_time=time.time(),
                    source_header=header,
                    depth_frame=depth_frame,
                )
                self.frames_received += 1
                self.first_frame.set()
        except BaseException as exc:
            if not self._stop.is_set():
                self.error = exc
        finally:
            self.done.set()


@dataclass(slots=True)
class MavlinkState:
    messages: dict[str, Any] = field(default_factory=dict)
    received_monotonic_ns: dict[str, int] = field(default_factory=dict)
    received_wall_time: dict[str, float] = field(default_factory=dict)
    count: int = 0
    last_preflight_failure_ns: int = 0

    def get(self, name: str) -> Any | None:
        return self.messages.get(name)


class MavlinkReader:
    TRACKED = frozenset(
        {
            "LOCAL_POSITION_NED",
            "ATTITUDE",
            "GLOBAL_POSITION_INT",
            "GPS_RAW_INT",
            "HEARTBEAT",
            "EXTENDED_SYS_STATE",
            "COMMAND_ACK",
            "HOME_POSITION",
            "SYS_STATUS",
            "HEALTH_REPORT",
            "STATUSTEXT",
        }
    )

    def __init__(
        self,
        connection: Any,
        *,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.connection = connection
        self.on_message = on_message
        self.clock = clock
        self.state = MavlinkState()
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.progress = progress
        self.message_counts: Counter[str] = Counter()
        self.parse_errors = 0
        self.bad_crc = 0

    @property
    def stats(self) -> dict[str, Any]:
        """Return a snapshot suitable for progress logs and diagnostics."""
        thread = self._thread
        return {
            "received_messages": self.state.count,
            "message_counts": dict(self.message_counts),
            "parse_errors": self.parse_errors,
            "bad_crc": self.bad_crc,
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "error": None if self.error is None else repr(self.error),
        }

    def raise_if_failed(self) -> None:
        """Raise a reader failure in the owning episode thread."""
        if self.error is not None:
            raise RuntimeError(
                "MAVLink reader failed: "
                f"{self.error}; messages={self.state.count}; "
                f"parse_errors={self.parse_errors}; bad_crc={self.bad_crc}"
            ) from self.error

    def start(self) -> MavlinkReader:
        if self._thread is not None:
            raise RuntimeError("MAVLink reader already started")
        self._thread = threading.Thread(target=self._run, name="sim-teacher-mavlink", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_s))

    @staticmethod
    def _type(message: Any) -> str:
        getter = getattr(message, "get_type", None)
        if callable(getter):
            return str(getter())
        if isinstance(message, Mapping):
            return str(message.get("type", message.get("_type", "")))
        return str(getattr(message, "_type", ""))

    @staticmethod
    def _mapping(message: Any) -> dict[str, Any]:
        if isinstance(message, Mapping):
            return dict(message)
        try:
            return dict(message.to_dict())
        except Exception:
            return {"message": str(message)}

    def _run(self) -> None:
        raw_receiver = getattr(self.connection, "recv_msg", None)
        receiver = getattr(self.connection, "recv_match", None)
        if not callable(raw_receiver) and not callable(receiver):
            self.error = RuntimeError("MAVLink connection has no recv_msg or recv_match")
            self.done.set()
            return
        try:
            while not self._stop.is_set():
                try:
                    message = (
                        raw_receiver()
                        if callable(raw_receiver)
                        else receiver(blocking=True, timeout=0.05)
                    )
                except TypeError:
                    message = receiver(timeout=0.05)
                if message is None:
                    selector = getattr(self.connection, "select", None)
                    if callable(selector):
                        selector(0.01)
                    continue
                name = self._type(message)
                self.message_counts[name] += 1
                self.state.count += 1
                if name == "BAD_DATA":
                    self.parse_errors += 1
                    reason = str(_field(message, "reason", "")).lower()
                    if "crc" in reason:
                        self.bad_crc += 1
                    if self.on_message is not None:
                        row = self._mapping(message)
                        row.update(
                            {
                                "message_type": name,
                                "receive_monotonic_ns": self.clock(),
                                "receive_wall_time": time.time(),
                            }
                        )
                        self.on_message(row)
                    continue
                mono = self.clock()
                wall = time.time()
                if name in self.TRACKED:
                    self.state.messages[name] = message
                    self.state.received_monotonic_ns[name] = mono
                    self.state.received_wall_time[name] = wall
                    if (
                        name == "STATUSTEXT"
                        and "preflight fail" in str(_field(message, "text", "")).lower()
                    ):
                        self.state.last_preflight_failure_ns = mono
                if self.on_message is not None:
                    row = self._mapping(message)
                    row.update(
                        {
                            "message_type": name,
                            "receive_monotonic_ns": mono,
                            "receive_wall_time": wall,
                        }
                    )
                    self.on_message(row)
        except BaseException as exc:
            if not self._stop.is_set():
                if "crc" in str(exc).lower():
                    self.bad_crc += 1
                else:
                    self.parse_errors += 1
                self.error = exc
        finally:
            self.done.set()


def _field(message: Any, name: str, default: Any = None) -> Any:
    if message is None:
        return default
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def build_observation(
    sensor: SensorSample, mavlink: MavlinkState, *, camera_info: Mapping[str, object] | None = None
) -> dict[str, Any]:
    local, attitude, global_pos, gps = (
        mavlink.get(name)
        for name in ("LOCAL_POSITION_NED", "ATTITUDE", "GLOBAL_POSITION_INT", "GPS_RAW_INT")
    )
    if local is None or attitude is None or global_pos is None or gps is None:
        raise RuntimeError("MAVLink state is incomplete for live observation")
    ned = [float(_field(local, key, 0.0)) for key in ("x", "y", "z")]
    velocity = [float(_field(local, key, 0.0)) for key in ("vx", "vy", "vz")]
    if not all(math.isfinite(value) for value in (*ned, *velocity)):
        raise RuntimeError("MAVLink LOCAL_POSITION_NED contains non-finite values")
    gps_fix = {
        "fix_type": int(_field(gps, "fix_type", 0) or 0),
        "satellites": int(_field(gps, "satellites_visible", 0) or 0),
        "hdop": _field(gps, "eph", None),
    }
    if gps_fix["fix_type"] < 3:
        raise RuntimeError(f"GPS fix is not valid: {gps_fix['fix_type']}")
    rgb_value: Any = sensor.rgb
    if isinstance(sensor.rgb, (bytes, bytearray, memoryview)):
        try:
            import numpy as np

            rgb_value = (
                np.frombuffer(sensor.rgb, dtype=np.uint8)
                .reshape((sensor.height, sensor.width, 3))
                .copy()
            )
            rgb_value = rgb_value[:, :, ::-1]
        except Exception:
            rgb_value = sensor.rgb
    cam = dict(sensor.camera_info)
    cam.update(camera_info or {})
    pose_stamp = mavlink.received_monotonic_ns.get(
        "LOCAL_POSITION_NED", sensor.received_monotonic_ns
    )
    return {
        "rgb": rgb_value,
        "depth": sensor.depth,
        "camera_info": cam,
        "pose": {
            "local_ned_m": ned,
            "lat_deg": float(_field(global_pos, "lat", 0)) / 1e7,
            "lon_deg": float(_field(global_pos, "lon", 0)) / 1e7,
            "alt_m": float(_field(global_pos, "relative_alt", 0)) / 1000.0,
        },
        "attitude": {
            "roll_deg": math.degrees(float(_field(attitude, "roll", 0.0))),
            "pitch_deg": math.degrees(float(_field(attitude, "pitch", 0.0))),
            "yaw_deg": math.degrees(float(_field(attitude, "yaw", 0.0))),
        },
        "velocity": velocity,
        "gps_fix": gps_fix,
        "sensor_status": "live",
        "frame_sensor_stamp_ns": sensor.sensor_stamp_ns,
        "frame_received_monotonic_ns": sensor.received_monotonic_ns,
        "pose_received_monotonic_ns": pose_stamp,
        "frame_pose_skew_ns": sensor.received_monotonic_ns - pose_stamp,
    }


__all__ = [
    "SensorSample",
    "SensorStreamReader",
    "MavlinkState",
    "MavlinkReader",
    "build_observation",
]
