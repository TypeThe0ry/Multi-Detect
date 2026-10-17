"""Live Gazebo MDGZSIM3 and MAVLink readers for SIM teacher episodes."""

from __future__ import annotations

import math
import threading
import time
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO


class LatestWinsBuffer:
    """Thread-safe one-slot buffer for high-bandwidth sensor frames.

    A producer never waits for a slow consumer: publishing a new item replaces
    the previous item and increments ``dropped_count``.  Consumers can wait for
    an item satisfying a predicate (used by the SIM server for a minimum
    simulation timestamp).
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: Any | None = None
        self.published_count = 0
        self.dropped_count = 0

    @property
    def latest(self) -> Any | None:
        with self._condition:
            return self._latest

    def put(self, item: Any) -> None:
        with self._condition:
            if self._latest is not None:
                self.dropped_count += 1
            self._latest = item
            self.published_count += 1
            self._condition.notify_all()

    def wait_for(
        self,
        predicate: Callable[[Any], bool] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            while self._latest is None or (predicate is not None and not predicate(self._latest)):
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("latest-wins buffer predicate timed out")
                self._condition.wait(remaining)
            return self._latest


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
    segmentation: Any = None
    segmentation_info: dict[str, Any] | None = None


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
        request_stream: BinaryIO | None = None,
        depth_binding: Mapping[str, object] | None = None,
        camera_info: Mapping[str, object] | None = None,
        max_frames: int | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
        require_depth: bool = True,
        segmentation_topic: str | None = None,
    ) -> None:
        self.stream = stream
        self.request_stream = request_stream
        self._request_lock = threading.Lock()
        self.depth_binding = dict(depth_binding) if depth_binding else None
        self.camera_info = dict(camera_info or {})
        self.max_frames = max_frames
        self.clock = clock
        self.require_depth = require_depth
        self.segmentation_topic = segmentation_topic
        self.latest: SensorSample | None = None
        self.buffer = LatestWinsBuffer()
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
        # Close the request pipe first: the frame server sees EOF and exits, which ends
        # its stdout and unblocks the reader thread. Closing ``self.stream`` while the
        # reader thread is blocked in read() deadlocks on Windows (buffered-IO lock).
        if self.request_stream is not None:
            try:
                self.request_stream.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_s))
        if self._thread is None or not self._thread.is_alive():
            try:
                self.stream.close()
            except Exception:
                pass

    def request(self, min_sensor_stamp_ns: int = 0) -> None:
        """Ask a request/response sensor server for its newest frame.

        The legacy continuous stream leaves ``request_stream`` unset and is
        still supported for CPU tests and compatibility.  The collector's
        latest-wins server receives one ``GET <sim_stamp_ns>`` line per
        observation; flushing here is deliberate so the request reaches the
        in-container process before we poll ``latest``.
        """
        if self.request_stream is None:
            return
        stamp = int(min_sensor_stamp_ns)
        if stamp < 0:
            raise ValueError("min_sensor_stamp_ns must be non-negative")
        payload = f"GET {stamp}\n".encode("ascii")
        with self._request_lock:
            self.request_stream.write(payload)
            flush = getattr(self.request_stream, "flush", None)
            if callable(flush):
                flush()

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
        sample = self.buffer.latest
        if sample is None:
            raise RuntimeError("sensor stream has no decoded frame")
        if max_age_s is not None:
            age = (self.clock() - sample.received_monotonic_ns) / 1e9
            if age > max_age_s:
                raise RuntimeError(f"sensor frame is stale: {age:.3f}s > {max_age_s:.3f}s")
        return sample

    @property
    def frames_dropped(self) -> int:
        return self.buffer.dropped_count

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
                        if depth_frame.encoding == "UINT16_MM":
                            depth = depth.astype(np.float32) / 1000.0
                elif self.require_depth:
                    raise RuntimeError("MDGZSIM3 stream has no MDGZDEP1 depth binding")
                segmentation = segmentation_info = None
                if self.segmentation_topic is not None:
                    from scripts.gazebo_segmentation_wire import read_segmentation_wire_frame

                    segmentation_frame = read_segmentation_wire_frame(
                        self.stream, sequence=int(header.sequence),
                        stamp_ns=int(header.sensor_stamp_ns),
                        rgb_sha256=bytes(header.sha256).hex(), width=int(header.width),
                        height=int(header.height), source_topic=self.segmentation_topic,
                    )
                    if np is None:
                        raise RuntimeError("NumPy is required to record segmentation frames")
                    segmentation = (
                        np.frombuffer(segmentation_frame.payload, dtype=np.uint8)
                        .reshape((int(header.height), int(header.width), 3)).copy()
                    )
                    segmentation_info = dict(segmentation_frame.metadata)
                stamp = self.clock()
                sample = SensorSample(
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
                    segmentation=segmentation,
                    segmentation_info=segmentation_info,
                )
                self.buffer.put(sample)
                self.latest = sample
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
    pose_alignment: dict[str, Any] | None = None
    energy_telemetry: list[dict[str, Any]] | None = None

    def get(self, name: str) -> Any | None:
        return self.messages.get(name)


def _energy_telemetry(state: MavlinkState) -> list[dict[str, Any]]:
    """Copy wire samples with their real receive clocks, never a frame clock."""
    samples = []
    for name in (
        "BATTERY_STATUS", "SYS_STATUS", "WIND_COV", "HEARTBEAT",
        "LOCAL_POSITION_NED", "VFR_HUD", "EXTENDED_SYS_STATE",
    ):
        message = state.get(name)
        stamp = state.received_monotonic_ns.get(name)
        if message is None or stamp is None:
            continue
        row = MavlinkReader._mapping(message)
        for source_field, getter in (
            ("source_system", "get_srcSystem"),
            ("source_component", "get_srcComponent"),
        ):
            read_source = getattr(message, getter, None)
            if callable(read_source):
                row[source_field] = read_source()
        samples.append(
            {
                "message": {**row, "mavpackettype": name},
                "received_monotonic_s": stamp / 1e9,
            }
        )
    return sorted(samples, key=lambda sample: sample["received_monotonic_s"])


class MavlinkReader:
    POSE_TYPES = frozenset({"LOCAL_POSITION_NED", "ATTITUDE", "GLOBAL_POSITION_INT"})
    POSE_HISTORY_MS = 5_000
    POSE_HISTORY_MAX_SAMPLES = 1_024
    TRACKED = frozenset(
        {
            "LOCAL_POSITION_NED",
            "ATTITUDE",
            "GLOBAL_POSITION_INT",
            "GPS_RAW_INT",
            "DISTANCE_SENSOR",
            "ALTITUDE",
            "HEARTBEAT",
            "EXTENDED_SYS_STATE",
            "VFR_HUD",
            "COMMAND_ACK",
            "HOME_POSITION",
            "SYS_STATUS",
            "BATTERY_STATUS",
            "WIND_COV",
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
        self._pose_condition = threading.Condition()
        self._pose_history: dict[str, list[tuple[int, dict[str, Any], int, float]]] = {
            name: [] for name in self.POSE_TYPES
        }

    def _record_pose_message(
        self, name: str, message: Any, mono: int, wall: float
    ) -> None:
        """Copy timestamped pose data while the receiver owns the message."""
        raw_stamp = _field(message, "time_boot_ms", None)
        if isinstance(raw_stamp, bool) or not isinstance(raw_stamp, int) or raw_stamp < 0:
            return
        row = self._mapping(message)
        with self._pose_condition:
            history = self._pose_history[name]
            stamps = [sample[0] for sample in history]
            index = bisect_left(stamps, raw_stamp)
            sample = (raw_stamp, row, mono, wall)
            if index < len(history) and history[index][0] == raw_stamp:
                history[index] = sample
            else:
                history.insert(index, sample)
            newest = history[-1][0]
            cutoff = newest - self.POSE_HISTORY_MS
            while history and (
                history[0][0] < cutoff or len(history) > self.POSE_HISTORY_MAX_SAMPLES
            ):
                history.pop(0)
            self._pose_condition.notify_all()

    def aligned_state(
        self, frame_sensor_stamp_ns: int, *, timeout_s: float = 0.5
    ) -> MavlinkState:
        """Return a pose sampled at the frame's simulation time.

        A bounded wait lets PX4 telemetry catch up when Gazebo delivers a frame
        ahead of the newest MAVLink message. On timeout, the closest available
        sample is retained and its actual residual skew is reported.
        """
        target_ms = frame_sensor_stamp_ns / 1_000_000.0
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._pose_condition:
            while any(
                not samples or samples[-1][0] < target_ms
                for samples in self._pose_history.values()
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.done.is_set() or self._stop.is_set():
                    break
                self._pose_condition.wait(remaining)
            selected = {
                name: _select_pose_sample(samples, target_ms)
                for name, samples in self._pose_history.items()
            }
            # The rest of the state is read under the same lock as receiver updates.
            snapshot = MavlinkState(
                messages=dict(self.state.messages),
                received_monotonic_ns=dict(self.state.received_monotonic_ns),
                received_wall_time=dict(self.state.received_wall_time),
                count=self.state.count,
                last_preflight_failure_ns=self.state.last_preflight_failure_ns,
                energy_telemetry=_energy_telemetry(self.state),
            )
        for name, result in selected.items():
            if result is None:
                continue
            row, mono, wall, _, _ = result
            snapshot.messages[name] = row
            snapshot.received_monotonic_ns[name] = mono
            snapshot.received_wall_time[name] = wall
        local = selected["LOCAL_POSITION_NED"]
        if local is not None:
            _, _, _, method, source_ms = local
            snapshot.pose_alignment = {
                "method": method,
                "source_time_boot_ms": source_ms,
                "residual_skew_ms": 0.0
                if method == "interpolated"
                else target_ms - float(source_ms),
            }
        return snapshot

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
                    with self._pose_condition:
                        self.state.messages[name] = message
                        self.state.received_monotonic_ns[name] = mono
                        self.state.received_wall_time[name] = wall
                        if (
                            name == "STATUSTEXT"
                            and "preflight fail" in str(_field(message, "text", "")).lower()
                        ):
                            self.state.last_preflight_failure_ns = mono
                    if name in self.POSE_TYPES:
                        self._record_pose_message(name, message, mono, wall)
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
            with self._pose_condition:
                self._pose_condition.notify_all()


def _field(message: Any, name: str, default: Any = None) -> Any:
    if message is None:
        return default
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _select_pose_sample(
    history: list[tuple[int, dict[str, Any], int, float]], target_ms: float
) -> tuple[dict[str, Any], int, float, str, int | list[int]] | None:
    """Select/interpolate one message history at ``target_ms``."""
    if not history:
        return None
    stamps = [sample[0] for sample in history]
    index = bisect_left(stamps, target_ms)
    if index == 0:
        stamp, row, mono, wall = history[0]
        return dict(row), mono, wall, "nearest_after", stamp
    if index == len(history):
        stamp, row, mono, wall = history[-1]
        return dict(row), mono, wall, "nearest_before", stamp
    before = history[index - 1]
    after = history[index]
    if before[0] == after[0] or abs(target_ms - before[0]) <= 0.5:
        return dict(before[1]), before[2], before[3], "nearest_before", before[0]
    if abs(after[0] - target_ms) <= 0.5:
        return dict(after[1]), after[2], after[3], "nearest_after", after[0]
    ratio = (target_ms - before[0]) / (after[0] - before[0])
    row = dict(before[1])
    for key, value in before[1].items():
        other = after[1].get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if not isinstance(other, (int, float)) or isinstance(other, bool):
            continue
        if not math.isfinite(float(value)) or not math.isfinite(float(other)):
            continue
        if key == "yaw":
            delta = (float(other) - float(value) + math.pi) % (2.0 * math.pi) - math.pi
            row[key] = float(value) + ratio * delta
        else:
            row[key] = float(value) + ratio * (float(other) - float(value))
    mono = round(before[2] + ratio * (after[2] - before[2]))
    wall = before[3] + ratio * (after[3] - before[3])
    # The aligned pose represents the requested simulation instant.  Keep the
    # metadata field scalar (rather than exposing the two interpolation
    # brackets) so consumers can use it as a normal PX4 ``time_boot_ms``.
    return row, mono, wall, "interpolated", int(round(target_ms))


def simulation_stamp_skew_ms(
    frame_sensor_stamp_ns: int, pose_time_boot_ms: int | None
) -> float | None:
    """Return Gazebo-frame minus PX4 simulation-clock time in milliseconds."""
    if pose_time_boot_ms is None:
        return None
    if (
        isinstance(frame_sensor_stamp_ns, bool)
        or not isinstance(frame_sensor_stamp_ns, int)
        or frame_sensor_stamp_ns < 0
        or isinstance(pose_time_boot_ms, bool)
        or not isinstance(pose_time_boot_ms, int)
        or pose_time_boot_ms < 0
    ):
        raise ValueError("simulation stamps must be non-negative integers")
    return frame_sensor_stamp_ns / 1_000_000.0 - float(pose_time_boot_ms)


def _ground_agl(
    sensor: SensorSample, mavlink: MavlinkState, *, max_age_s: float
) -> dict[str, Any] | None:
    """Normalize optional MAVLink AGL, never relative_alt or barometric AMSL.

    DISTANCE_SENSOR must face body down (rotation 25); compensate vehicle tilt.
    ALTITUDE.altitude_terrain and bottom_clearance are already distances above
    terrain, not MSL ground elevations. Negative/NaN values mean unavailable.
    """
    def finite(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    for name in ("DISTANCE_SENSOR", "ALTITUDE"):
        message = mavlink.get(name)
        stamp = mavlink.received_monotonic_ns.get(name)
        if message is None or stamp is None:
            continue
        age = (sensor.received_monotonic_ns - stamp) / 1e9
        if not 0 <= age <= max_age_s:
            continue
        if name == "DISTANCE_SENSOR":
            if _field(message, "orientation") != 25 or _field(message, "signal_quality") == 1:
                continue
            distance = _field(message, "current_distance")
            minimum, maximum = _field(message, "min_distance"), _field(message, "max_distance")
            if not all(finite(value) for value in (distance, minimum, maximum)):
                continue
            if not (0 <= minimum <= distance <= maximum and distance > 0):
                continue
            attitude = mavlink.get("ATTITUDE")
            attitude_stamp = mavlink.received_monotonic_ns.get("ATTITUDE")
            if attitude_stamp is None:
                continue
            attitude_age = (sensor.received_monotonic_ns - attitude_stamp) / 1e9
            if not 0 <= attitude_age <= max_age_s:
                continue
            if abs(stamp - attitude_stamp) / 1e9 > min(max_age_s, 0.25):
                continue
            roll, pitch = _field(attitude, "roll"), _field(attitude, "pitch")
            if not all(finite(value) for value in (roll, pitch)):
                continue
            component = math.cos(roll) * math.cos(pitch)
            if component <= 1e-6:
                continue
            height = distance / 100.0 * component
        else:
            height = _field(message, "altitude_terrain")
            if not finite(height) or height <= 0:
                height = _field(message, "bottom_clearance")
            if not finite(height) or height <= 0:
                continue
        return {"height_m": height, "age_s": age, "source": name}
    return None


def build_observation(
    sensor: SensorSample, mavlink: MavlinkState, *,
    camera_info: Mapping[str, object] | None = None,
    ground_agl_max_age_s: float | None = None,
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
    extended_state = mavlink.get("EXTENDED_SYS_STATE")
    landed_state = _field(extended_state, "landed_state", None)
    vtol_state = _field(extended_state, "vtol_state", None)
    landed = (
        landed_state == 1
        if isinstance(landed_state, int)
        and not isinstance(landed_state, bool)
        and landed_state in {1, 2, 3, 4}
        else None
    )
    # VFR_HUD.airspeed is the PX4 air-relative speed in m/s.  Keep it
    # separate from LOCAL_POSITION_NED ground velocity; fixed-wing takeoff,
    # orbit and stall checks depend on the former.
    vfr_hud = mavlink.get("VFR_HUD")
    airspeed_raw = _field(vfr_hud, "airspeed", _field(vfr_hud, "airspeed_mps", None))
    try:
        airspeed_mps = float(airspeed_raw) if airspeed_raw is not None else None
    except (TypeError, ValueError):
        airspeed_mps = None
    if airspeed_mps is not None and (not math.isfinite(airspeed_mps) or airspeed_mps < 0):
        airspeed_mps = None
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
    depth_camera_info: dict[str, Any] | None = None
    if sensor.depth_frame is not None:
        metadata = getattr(sensor.depth_frame, "metadata", None)
        if isinstance(metadata, Mapping):
            depth_camera_info = {
                key: metadata[key]
                for key in (
                    "source_topic",
                    "calibration_id",
                    "calibration_sha256",
                    "encoding",
                    "width",
                    "height",
                    "minimum_depth_m",
                    "maximum_depth_m",
                    "fx_px",
                    "fy_px",
                    "cx_px",
                    "cy_px",
                )
                if key in metadata
            }
    pose_stamp = mavlink.received_monotonic_ns.get(
        "LOCAL_POSITION_NED", sensor.received_monotonic_ns
    )
    pose_time_boot_ms_raw = _field(local, "time_boot_ms", None)
    pose_time_boot_ms: int | None
    alignment = mavlink.pose_alignment
    if alignment is not None:
        # Interpolation uses the frame timestamp; nearest samples retain their
        # actual source timestamp so the residual is an honest diagnostic.
        source_time = alignment.get("source_time_boot_ms")
        if isinstance(source_time, (int, float)) and not isinstance(source_time, bool):
            pose_time_boot_ms = int(round(float(source_time)))
        else:
            pose_time_boot_ms = int(round(sensor.sensor_stamp_ns / 1_000_000.0))
        sim_skew_ms = simulation_stamp_skew_ms(sensor.sensor_stamp_ns, pose_time_boot_ms)
    elif (
        isinstance(pose_time_boot_ms_raw, (int, float))
        and not isinstance(pose_time_boot_ms_raw, bool)
        and math.isfinite(float(pose_time_boot_ms_raw))
        and float(pose_time_boot_ms_raw) >= 0
    ):
        pose_time_boot_ms = int(pose_time_boot_ms_raw)
        sim_skew_ms = simulation_stamp_skew_ms(sensor.sensor_stamp_ns, pose_time_boot_ms)
    else:
        pose_time_boot_ms = None
        sim_skew_ms = None
    result = {
        "rgb": rgb_value,
        "depth": sensor.depth,
        "depth_camera_info": depth_camera_info,
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
        # Pose alignment may interpolate or select historical position. Energy
        # learning needs the latest wire state and its unmodified receive time.
        "energy_telemetry": mavlink.energy_telemetry
        if mavlink.energy_telemetry is not None
        else _energy_telemetry(mavlink),
        "gps_fix": gps_fix,
        "landed": landed,
        "flight_state": {
            "vtol_state": vtol_state,
            "landed_state": landed_state,
            "airspeed_mps": airspeed_mps,
        },
        "airspeed_mps": airspeed_mps,
        "sensor_status": "live",
        "frame_sensor_stamp_ns": sensor.sensor_stamp_ns,
        "pose_time_boot_ms": pose_time_boot_ms,
        "pose_sim_stamp_ns": None
        if pose_time_boot_ms is None
        else pose_time_boot_ms * 1_000_000,
        "sim_skew_ms": sim_skew_ms,
        "pose_alignment": alignment
        or {
            "method": "legacy_latest",
            "source_time_boot_ms": pose_time_boot_ms,
            "residual_skew_ms": sim_skew_ms,
        },
        "frame_received_monotonic_ns": sensor.received_monotonic_ns,
        "pose_received_monotonic_ns": pose_stamp,
        "frame_pose_skew_ns": sensor.received_monotonic_ns - pose_stamp,
    }
    if sensor.segmentation is not None:
        result["segmentation"] = sensor.segmentation
        result["segmentation_info"] = sensor.segmentation_info
    if ground_agl_max_age_s is not None:
        ground_agl = _ground_agl(sensor, mavlink, max_age_s=ground_agl_max_age_s)
        if ground_agl is not None:
            result["ground_agl"] = ground_agl
    return result


__all__ = [
    "SensorSample",
    "LatestWinsBuffer",
    "SensorStreamReader",
    "MavlinkState",
    "MavlinkReader",
    "simulation_stamp_skew_ms",
    "build_observation",
]
