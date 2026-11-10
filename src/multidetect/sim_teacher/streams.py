"""Live Gazebo MDGZSIM3 and MAVLink readers for SIM teacher episodes."""

from __future__ import annotations

import math
import threading
import time
from bisect import bisect_left
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from multidetect.camera_mount import CameraMount, compose_camera_pose
from multidetect.gimbal_controller import parse_gimbal_device_attitude_status

GIMBAL_FEEDBACK_MAX_AGE_S = 1.0


def gimbal_angles_from_status(
    status: Any,
    *,
    vehicle_yaw_deg: float | None = None,
    yaw_correction_deg: float = 0.0,
) -> dict[str, float] | None:
    q = status.get("q") if isinstance(status, Mapping) else _field(status, "q", None)
    if not isinstance(q, (list, tuple)) or len(q) < 4:
        return None
    try:
        w, x, y, z = (float(value) for value in q[:4])
        norm = math.sqrt(w * w + x * x + y * y + z * z)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(norm) or norm <= 1e-9:
        return None
    w, x, y, z = (value / norm for value in (w, x, y, z))
    yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    flags = int(_field(status, "flags", 0) or 0)
    if vehicle_yaw_deg is not None and flags & 64:
        yaw -= float(vehicle_yaw_deg)
    yaw = (yaw + float(yaw_correction_deg) + 180.0) % 360.0 - 180.0
    return {
        "roll": math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))),
        "pitch": math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))),
        "yaw": yaw,
    }


def gimbal_angles_from_joint_state(joint_state: Any) -> dict[str, float] | None:
    if not isinstance(joint_state, Mapping) or joint_state.get("fresh") is False:
        return None
    positions = joint_state.get("position_rad")
    if not isinstance(positions, Mapping):
        return None
    try:
        values = {axis: math.degrees(float(positions[axis])) for axis in ("yaw", "roll", "pitch")}
    except (KeyError, TypeError, ValueError):
        return None
    return values if all(math.isfinite(value) for value in values.values()) else None


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
    # Optional simulator joint sidecar.  Older MDGZSIM3 frames do not carry
    # this value and remain valid; newer collectors may provide gimbal and
    # propeller joint samples without changing the RGB wire contract.
    joint_state: dict[str, Any] | None = None


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
                        self.stream,
                        sequence=int(header.sequence),
                        stamp_ns=int(header.sensor_stamp_ns),
                        rgb_sha256=bytes(header.sha256).hex(),
                        width=int(header.width),
                        height=int(header.height),
                        source_topic=self.segmentation_topic,
                    )
                    if np is None:
                        raise RuntimeError("NumPy is required to record segmentation frames")
                    segmentation = (
                        np.frombuffer(segmentation_frame.payload, dtype=np.uint8)
                        .reshape((int(header.height), int(header.width), 3))
                        .copy()
                    )
                    segmentation_info = dict(segmentation_frame.metadata)
                joint_state = None
                if depth_frame is not None:
                    sidecar = depth_frame.metadata.get("joint_state")
                    if isinstance(sidecar, Mapping):
                        joint_state = dict(sidecar)
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
                    joint_state=joint_state,
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
    # Vehicle (PX4 boot) clock: the newest time_boot_ms on the wire, and its value
    # when each tracked message arrived.  In SITL this is the lockstep simulation
    # clock, which can run far slower than host time on a loaded machine.
    vehicle_time_boot_ms: int | None = None
    received_vehicle_ms: dict[str, int] = field(default_factory=dict)
    # Recent spans of the vehicle clock in which no message carrying time_boot_ms
    # arrived at all (a host stall overflowing the UDP buffer, a link drop),
    # as (last before, first after) in ms.  See MavlinkReader.VEHICLE_GAP_MS.
    vehicle_gaps_ms: list[tuple[int, int]] = field(default_factory=list)
    # Latest COMMAND_ACK per command id with its receive clock.  ACKs for two
    # commands sent back to back (e.g. RTL then VTOL transition) arrive within
    # milliseconds; the single latest COMMAND_ACK would hide the earlier one.
    command_acks: dict[int, tuple[Any, int]] = field(default_factory=dict)

    def get(self, name: str) -> Any | None:
        return self.messages.get(name)


def _energy_telemetry(state: MavlinkState) -> list[dict[str, Any]]:
    """Copy wire samples with their real receive clocks, never a frame clock."""
    samples = []
    for name in (
        "BATTERY_STATUS",
        "SYS_STATUS",
        "WIND_COV",
        "HEARTBEAT",
        "LOCAL_POSITION_NED",
        "VFR_HUD",
        "EXTENDED_SYS_STATE",
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
        sample = {
            "message": {**row, "mavpackettype": name},
            "received_monotonic_s": stamp / 1e9,
        }
        vehicle_ms = state.received_vehicle_ms.get(name)
        if vehicle_ms is not None:
            sample["received_vehicle_s"] = vehicle_ms / 1e3
        samples.append(sample)
    return sorted(samples, key=lambda sample: sample["received_monotonic_s"])


class MavlinkReader:
    POSE_TYPES = frozenset({"LOCAL_POSITION_NED", "ATTITUDE", "GLOBAL_POSITION_INT"})
    # PX4 streams pose at tens of Hz of its own clock; a second of that clock
    # with nothing on the wire is a delivery gap, not a slow stream.
    VEHICLE_GAP_MS = 1_000
    VEHICLE_GAPS_KEPT = 16
    # Keep enough simulation history for delayed Gazebo frames to be aligned to
    # the PX4 pose timestamp instead of silently falling back to the newest pose.
    POSE_HISTORY_MS = 30_000
    POSE_HISTORY_MAX_SAMPLES = 16_384
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
            "MISSION_REQUEST_INT",
            "MISSION_REQUEST",
            "MISSION_ACK",
            "MISSION_CURRENT",
            "HOME_POSITION",
            "SYS_STATUS",
            "BATTERY_STATUS",
            "WIND_COV",
            "HEALTH_REPORT",
            "STATUSTEXT",
            "GIMBAL_DEVICE_ATTITUDE_STATUS",
            "GIMBAL_MANAGER_STATUS",
            "ACTUATOR_OUTPUT_STATUS",
            "ESC_STATUS",
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
        self._vehicle_newest_ms: int | None = None
        self._gimbal_events = deque(maxlen=4096)
        self._gimbal_event_sequence = 0
        self.progress = progress
        self._health_check: Callable[[], None] | None = None

        self.message_counts: Counter[str] = Counter()
        self.last_message_type: str | None = None
        self.last_message_monotonic_ns: int | None = None
        self.last_message_vehicle_ms: int | None = None
        self.parse_errors = 0
        self.bad_crc = 0
        self.recent_messages: deque[dict[str, Any]] = deque(maxlen=16)
        self._pose_condition = threading.Condition()
        self._pose_history: dict[str, list[tuple[int, dict[str, Any], int, float]]] = {
            name: [] for name in self.POSE_TYPES
        }

    def gimbal_events_cursor(self) -> int:
        with self._pose_condition:
            return self._gimbal_event_sequence

    def gimbal_events_since(self, cursor: int):
        with self._pose_condition:
            if self._gimbal_events and cursor < self._gimbal_events[0][0] - 1:
                raise RuntimeError("gimbal telemetry event buffer overflow")
            events = [(name, message, stamp, vehicle_ms) for seq, name, message, stamp, vehicle_ms in self._gimbal_events if seq > cursor]
            return self._gimbal_event_sequence, events

    def _record_pose_message(self, name: str, message: Any, mono: int, wall: float) -> None:
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

    def aligned_state(self, frame_sensor_stamp_ns: int, *, timeout_s: float = 0.5) -> MavlinkState:
        """Return a pose sampled at the frame's simulation time.

        A bounded wait lets PX4 telemetry catch up when Gazebo delivers a frame
        ahead of the newest MAVLink message. On timeout, the closest available
        sample is retained and its actual residual skew is reported.
        """
        target_ms = frame_sensor_stamp_ns / 1_000_000.0
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._pose_condition:
            while any(
                not samples or samples[-1][0] < target_ms for samples in self._pose_history.values()
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
                vehicle_time_boot_ms=self.state.vehicle_time_boot_ms,
                received_vehicle_ms=dict(self.state.received_vehicle_ms),
                vehicle_gaps_ms=list(self.state.vehicle_gaps_ms),
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
        socket_state: dict[str, Any] = {"available": False}
        for attribute in ("port", "socket"):
            candidate = getattr(self.connection, attribute, None)
            if candidate is None:
                continue
            socket_state = {"available": True, "attribute": attribute}
            try:
                socket_state["closed"] = bool(candidate._closed)
            except Exception:
                pass
            try:
                socket_state["fileno"] = int(candidate.fileno())
            except Exception as exc:
                socket_state["fileno_error"] = repr(exc)
            break
        return {
            "received_messages": self.state.count,
            "message_counts": dict(self.message_counts),
            "parse_errors": self.parse_errors,
            "bad_crc": self.bad_crc,
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "error": None if self.error is None else repr(self.error),
            "last_exception": None if self.error is None else repr(self.error),
            "socket": socket_state,
            "recent_messages": list(self.recent_messages),
            "last_message_type": self.last_message_type,
            "last_message_monotonic_ns": self.last_message_monotonic_ns,
            "last_message_vehicle_ms": self.last_message_vehicle_ms,
        }

    def raise_if_failed(self) -> None:
        """Raise a reader failure in the owning episode thread."""
        if self._health_check is not None:
            self._health_check()
        if self.error is not None:
            raise RuntimeError(
                "MAVLink reader failed: "
                f"{self.error}; messages={self.state.count}; "
                f"parse_errors={self.parse_errors}; bad_crc={self.bad_crc}"
            ) from self.error

    def set_health_check(self, callback: Callable[[], None] | None) -> None:
        """Attach an owning-loop health check evaluated at existing poll points."""
        self._health_check = callback

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
                boot_ms = _field(message, "time_boot_ms", None)
                mono = self.clock()
                self.last_message_type = name
                self.last_message_monotonic_ns = mono
                self.last_message_vehicle_ms = (
                    boot_ms
                    if isinstance(boot_ms, int) and not isinstance(boot_ms, bool)
                    else None
                )
                self.recent_messages.append(
                    {
                        "type": name,
                        "time_boot_ms": self.last_message_vehicle_ms,
                        "receive_monotonic_ns": mono,
                    }
                )
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
                                "receive_monotonic_ns": mono,
                                "receive_wall_time": time.time(),
                            }
                        )
                        self.on_message(row)
                    continue
                wall = time.time()
                if isinstance(boot_ms, int) and not isinstance(boot_ms, bool) and boot_ms >= 0:
                    with self._pose_condition:
                        # Measured from the newest clock seen, so a stray older
                        # stamp (another component, a reboot) never opens a gap.
                        newest_ms = self._vehicle_newest_ms
                        if newest_ms is not None and boot_ms - newest_ms > self.VEHICLE_GAP_MS:
                            gaps = self.state.vehicle_gaps_ms
                            gaps.append((newest_ms, boot_ms))
                            del gaps[: -self.VEHICLE_GAPS_KEPT]
                        if newest_ms is None or boot_ms > newest_ms:
                            self._vehicle_newest_ms = boot_ms
                        self.state.vehicle_time_boot_ms = boot_ms
                if name in self.TRACKED:
                    with self._pose_condition:
                        self.state.messages[name] = message
                        if name in {"COMMAND_ACK", "GIMBAL_MANAGER_STATUS", "GIMBAL_DEVICE_ATTITUDE_STATUS"}:
                            self._gimbal_event_sequence += 1
                            self._gimbal_events.append((self._gimbal_event_sequence, name, message, mono, self.state.vehicle_time_boot_ms))
                        self.state.received_monotonic_ns[name] = mono
                        self.state.received_wall_time[name] = wall
                        if self.state.vehicle_time_boot_ms is not None:
                            self.state.received_vehicle_ms[name] = self.state.vehicle_time_boot_ms
                        if (
                            name == "STATUSTEXT"
                            and "preflight fail" in str(_field(message, "text", "")).lower()
                        ):
                            self.state.last_preflight_failure_ns = mono
                        if name == "COMMAND_ACK":
                            try:
                                self.state.command_acks[int(_field(message, "command", -1))] = (
                                    message,
                                    mono,
                                )
                            except (TypeError, ValueError):
                                pass
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


def _propeller_joint_state(mavlink: MavlinkState, timestamp_ns: int) -> dict[str, Any]:
    """Normalize ESC RPM when present; actuator outputs do not measure rotor spin."""
    message = mavlink.get("ESC_STATUS")
    if message is None:
        return {
            "timestamp_ns": int(timestamp_ns),
            "angles_rad": [],
            "spin_rad_s": [],
            "source": "unavailable",
        }
    getter = (
        message.get
        if isinstance(message, Mapping)
        else lambda key, default=None: getattr(message, key, default)
    )
    rpm = getter("rpm", None)
    if not isinstance(rpm, (list, tuple)):
        rpm = []
    spin = [
        float(value) * 2.0 * math.pi / 60.0
        for value in rpm
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    return {
        "timestamp_ns": int(timestamp_ns),
        "angles_rad": [],
        "spin_rad_s": [
            float(value)
            for value in spin
            if isinstance(value, (int, float)) and math.isfinite(float(value))
        ],
        "source": "ESC_STATUS.rpm" if spin else "unavailable",
    }


def validate_gimbal_recording_observation(observation: Mapping[str, Any]) -> None:
    """Reject gimbal observations that are not measured, complete RGB sidecars."""
    rgb_stamp = observation.get("frame_sensor_stamp_ns")
    if isinstance(rgb_stamp, bool) or not isinstance(rgb_stamp, int) or rgb_stamp <= 0:
        raise ValueError("gimbal recording requires a positive RGB sensor timestamp")

    gimbal = observation.get("gimbal_joint_state")
    if not isinstance(gimbal, Mapping) or gimbal.get("source") != "gazebo_joint_state":
        raise ValueError("gimbal recording requires Gazebo joint-state feedback")
    if gimbal.get("timestamp_ns") != rgb_stamp:
        raise ValueError("gimbal joint timestamp does not match RGB sensor timestamp")
    positions = gimbal.get("position_rad")
    velocities = gimbal.get("velocity_rad_s")
    axes = ("pitch", "yaw", "roll")
    if not isinstance(positions, Mapping) or not isinstance(velocities, Mapping):
        raise ValueError("gimbal recording requires position and velocity for all three axes")
    if not all(_finite_number(positions.get(axis)) and _finite_number(velocities.get(axis)) for axis in axes):
        raise ValueError("gimbal recording requires finite position and velocity for all three axes")

    propellers = observation.get("propeller_joint_state")
    if not isinstance(propellers, Mapping) or propellers.get("source") != "gazebo_joint_state":
        raise ValueError("gimbal recording requires Gazebo propeller joint-state feedback")
    if propellers.get("timestamp_ns") != rgb_stamp:
        raise ValueError("propeller joint timestamp does not match RGB sensor timestamp")
    angles = propellers.get("angles_rad")
    spin = propellers.get("spin_rad_s")
    if not isinstance(angles, (list, tuple)) or not isinstance(spin, (list, tuple)):
        raise ValueError("gimbal recording requires four propeller angles and spin rates")
    if len(angles) != 4 or len(spin) != 4:
        raise ValueError("gimbal recording requires exactly four propeller angles and spin rates")
    if not all(_finite_number(value) for value in (*angles, *spin)):
        raise ValueError("gimbal recording requires finite propeller angles and spin rates")
    if not any(float(value) != 0.0 for value in spin):
        raise ValueError("gimbal recording requires at least one nonzero rotor spin rate")


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


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
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
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
    sensor: SensorSample,
    mavlink: MavlinkState,
    *,
    camera_info: Mapping[str, object] | None = None,
    ground_agl_max_age_s: float | None = None,
    camera_mount: CameraMount | None = None,
    gimbal_yaw_correction_deg: float | None = None,
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
    gimbal_status = mavlink.get("GIMBAL_DEVICE_ATTITUDE_STATUS")
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
        # Vehicle-clock spans with no telemetry at all; energy freshness is not
        # charged for them (OnboardEnergyEstimator.note_delivery_gap).
        "vehicle_telemetry_gaps_s": [
            [start / 1e3, end / 1e3] for start, end in mavlink.vehicle_gaps_ms
        ],
        "gps_fix": gps_fix,
        "landed": landed,
        "flight_state": {
            "vtol_state": vtol_state,
            "landed_state": landed_state,
            "airspeed_mps": airspeed_mps,
            "gimbal_status": (
                MavlinkReader._mapping(gimbal_status) if gimbal_status is not None else None
            ),
        },
        "airspeed_mps": airspeed_mps,
        "sensor_status": "live",
        "frame_sensor_stamp_ns": sensor.sensor_stamp_ns,
        "pose_time_boot_ms": pose_time_boot_ms,
        "pose_sim_stamp_ns": None if pose_time_boot_ms is None else pose_time_boot_ms * 1_000_000,
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
    gimbal_angles = gimbal_angles_from_status(
        gimbal_status,
        vehicle_yaw_deg=(
            math.degrees(float(_field(attitude, "yaw", 0.0)))
            if gimbal_yaw_correction_deg is not None
            else None
        ),
        yaw_correction_deg=float(gimbal_yaw_correction_deg or 0.0),
    )
    sidecar = sensor.joint_state if isinstance(sensor.joint_state, Mapping) else {}
    sidecar_gimbal = sidecar.get("gimbal_joint_state")
    sidecar_propeller = sidecar.get("propeller_joint_state")
    joint_angles = gimbal_angles_from_joint_state(sidecar_gimbal)
    if joint_angles is not None:
        gimbal_angles = joint_angles
    if gimbal_angles is not None:
        result["gimbal_angles_deg"] = gimbal_angles
    result["gimbal"] = {
        "available": gimbal_status is not None or joint_angles is not None,
        "angles_deg": gimbal_angles,
        "observed_at_ns": mavlink.received_monotonic_ns.get(
            "GIMBAL_DEVICE_ATTITUDE_STATUS"
        ),
    }
    # Gimbal feedback is meaningful only for airframes that declare a gimbal.
    # Fixed mounts retain their legacy projection path; their fixed geometry
    # is represented by the airframe profile rather than a per-frame pose.
    gimbal = (
        mavlink.get("GIMBAL_DEVICE_ATTITUDE_STATUS")
        if camera_mount is not None and camera_mount.kind == "gimbal"
        else None
    )
    gimbal_state = parse_gimbal_device_attitude_status(gimbal) if gimbal is not None else None
    gimbal_received_ns = mavlink.received_monotonic_ns.get("GIMBAL_DEVICE_ATTITUDE_STATUS")
    gimbal_age_s = (
        None
        if gimbal_received_ns is None
        else (sensor.received_monotonic_ns - gimbal_received_ns) / 1e9
    )
    gimbal_fresh = gimbal_state is not None and (
        gimbal_age_s is None or 0.0 <= gimbal_age_s <= GIMBAL_FEEDBACK_MAX_AGE_S
    )
    if gimbal_state is not None and camera_mount is not None and camera_mount.kind == "gimbal":
        within_limits = all(
            lower <= value <= upper
            for value, lower, upper in (
                (
                    gimbal_state.pitch_rad,
                    camera_mount.limits.pitch_min_rad,
                    camera_mount.limits.pitch_max_rad,
                ),
                (
                    gimbal_state.yaw_rad,
                    camera_mount.limits.yaw_min_rad,
                    camera_mount.limits.yaw_max_rad,
                ),
                (
                    gimbal_state.roll_rad,
                    camera_mount.limits.roll_min_rad,
                    camera_mount.limits.roll_max_rad,
                ),
            )
        )
        result["gimbal_state"] = {
            "pitch_rad": gimbal_state.pitch_rad,
            "yaw_rad": gimbal_state.yaw_rad,
            "roll_rad": gimbal_state.roll_rad,
            "rotation": list(gimbal_state.rotation) if gimbal_state.rotation is not None else None,
            "timestamp_us": gimbal_state.timestamp_us,
            "yaw_frame": gimbal_state.yaw_frame,
            "age_s": gimbal_age_s,
            "fresh": gimbal_fresh,
            "within_limits": within_limits,
            "out_of_limits": not within_limits,
            "source": "GIMBAL_DEVICE_ATTITUDE_STATUS",
        }
        result["gimbal_joint_state"] = {
            "timestamp_ns": sensor.sensor_stamp_ns,
            "position_rad": {
                "pitch": gimbal_state.pitch_rad,
                "yaw": gimbal_state.yaw_rad,
                "roll": gimbal_state.roll_rad,
            },
            "velocity_rad_s": dict(sidecar_gimbal.get("velocity_rad_s", {}))
            if isinstance(sidecar_gimbal, Mapping)
            else {},
            "source": "GIMBAL_DEVICE_ATTITUDE_STATUS",
            "fresh": gimbal_fresh,
        }
        if gimbal_fresh:
            rotation_frame = (
                gimbal_state.yaw_frame
                if gimbal_state.yaw_frame in {"body", "world"}
                else ("world" if camera_mount.stabilization == "lock-horizon" else "body")
            )
            composed = compose_camera_pose(
                tuple(ned),
                vehicle_roll_rad=math.radians(result["attitude"]["roll_deg"]),
                vehicle_pitch_rad=math.radians(result["attitude"]["pitch_deg"]),
                vehicle_yaw_rad=math.radians(result["attitude"]["yaw_deg"]),
                mount=camera_mount,
                rotation_override=gimbal_state.rotation,
                rotation_frame=rotation_frame,
            )
            result["camera_pose"] = {
                "center_ned": list(composed.center_ned),
                "optical_to_world": list(composed.optical_to_world),
                "source": "composed",
                "gimbal_timestamp_us": gimbal_state.timestamp_us,
                "gimbal_feedback_fresh": True,
                "gimbal_feedback_out_of_limits": not within_limits,
            }
    elif camera_mount is not None:
        # Fixed-camera episodes use the same explicit state shape.  This makes
        # consumers independent of the airframe profile while preserving the
        # old fields and old readers.
        result["gimbal_joint_state"] = {
            "timestamp_ns": sensor.sensor_stamp_ns,
            "position_rad": {
                "pitch": camera_mount.pitch_rad,
                "yaw": camera_mount.yaw_rad,
                "roll": camera_mount.roll_rad,
            },
            "velocity_rad_s": {},
            "source": "fixed_mount" if camera_mount.kind == "fixed" else "unavailable",
            "fresh": camera_mount.kind == "fixed",
        }
    if camera_mount is not None and "gimbal_joint_state" not in result:
        result["gimbal_joint_state"] = {
            "timestamp_ns": sensor.sensor_stamp_ns,
            "position_rad": {},
            "velocity_rad_s": {},
            "source": "unavailable",
            "fresh": False,
        }
    if isinstance(sidecar_gimbal, Mapping):
        # The simulator joint sidecar is an independent measured state.  Do
        # not merge it with the MAVLink feedback-derived shape: a stale
        # high-level sample must never leak an angle, velocity, or freshness
        # field into the fresh joint record.
        result["gimbal_joint_state"] = dict(sidecar_gimbal)
        result["gimbal_joint_state"]["timestamp_ns"] = int(
            sidecar_gimbal.get("timestamp_ns", sensor.sensor_stamp_ns)
        )
    if isinstance(sidecar_propeller, Mapping):
        result["propeller_joint_state"] = dict(sidecar_propeller)
    else:
        # PX4 ESC feedback has RPM but no rotor phase. Simulator joint sidecars
        # can provide both values when the model publishes them.
        result["propeller_joint_state"] = _propeller_joint_state(mavlink, sensor.sensor_stamp_ns)
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
    "gimbal_angles_from_status",
    "gimbal_angles_from_joint_state",
    "validate_gimbal_recording_observation",
]
