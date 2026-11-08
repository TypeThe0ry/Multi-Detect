"""Concurrent observation in one owned SIM; no flight or model authority.

The RX thread exclusively reads and sends observation requests. The main
thread owns durable telemetry journals. Camera helpers are constructed, read
and closed by a separate worker in fresh finite windows. Source timestamps
are never replaced with host-return timestamps.
"""

from __future__ import annotations

import copy
import hashlib
import queue
import threading
from pathlib import Path

from scripts.onboard_teacher_gazebo_session import (
    MAX_CAPTURE_BYTES,
    BoundedTeacherCamera,
    OwnedTeacherGazeboSession,
    TeacherSessionError,
    _integer,
    _real,
)

CONTRACT = "multidetect.onboard-teacher-gazebo-session-v3"
_BASE = Path(__file__).with_name("onboard_teacher_gazebo_session.py")
if len(_BASE.read_bytes()) != 31076 or hashlib.sha256(_BASE.read_bytes()).hexdigest() != (
    "80d6a00c817c1654d62d9d3ffb0ddd35ccdce9a4bc8c1e884ea924f947793b42"
):
    raise RuntimeError("Frozen owned teacher session source differs")


class FrozenReceivedMessage:
    """Original packet clone first; decoded getters are deferred until commit.

    This does not authenticate a source or repair a decoder. A decoder object
    that changes its underlying packet is rejected, not silently re-decoded.
    """

    def __init__(self, original):
        self._original = original
        packet = original.get_msgbuf()
        if type(packet) not in (bytes, bytearray) or not 0 < len(packet) <= 4096:
            raise ValueError("Bounded original MAVLink packet required")
        self._packet = bytes(packet)

    def get_msgbuf(self):
        return self._packet

    def __getattr__(self, name):
        if name not in {"get_type", "get_srcSystem", "get_srcComponent", "get_seq", "to_dict"}:
            raise AttributeError(name)

        def decoded(*args, **kwargs):
            if bytes(self._original.get_msgbuf()) != self._packet:
                raise RuntimeError("Received decoder packet changed before durable projection")
            return getattr(self._original, name)(*args, **kwargs)

        return decoded


class ConcurrentTelemetryReceiver:
    """Bounded single RX/send owner, with one main-thread queue consumer."""

    def __init__(self, reader, clock, *, request_sender=None, queue_capacity=256, batch_size=16):
        if not callable(reader) or not callable(clock):
            raise TypeError("Original reader and monotonic clock required")
        if request_sender is not None and not callable(request_sender):
            raise TypeError("Observation request sender must be callable")
        self.reader, self.clock, self.sender = reader, clock, request_sender
        self.capacity = _integer(queue_capacity, "queue_capacity", 1, 4096)
        self.batch_size = _integer(batch_size, "batch_size", 1, 16)
        self._queue = queue.Queue(self.capacity)
        self._commands = queue.Queue(1)
        self._stop = threading.Event()
        self._thread = None
        self._owner = threading.get_ident()
        self._lock = threading.Lock()
        self._report = {
            "contract": "multidetect.teacher-concurrent-rx-v3",
            "started": False,
            "stopped": False,
            "thread_exited": False,
            "queue_capacity": self.capacity,
            "queue_high_water": 0,
            "received_packets": 0,
            "consumed_packets": 0,
            "raw_packet_bytes": 0,
            "queue_overflow": False,
            "failure": None,
            "flight_authority": False,
            "request_API_calls": 0,
            "request_partial_API_calls_unknown": False,
            "decoded_on_RX_thread": False,
        }

    def _main(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Receiver queue/control accessed by another consumer")

    def _fail(self, error):
        with self._lock:
            if self._report["failure"] is None:
                self._report["failure"] = f"{type(error).__name__}: {error}"[:1024]
        self._stop.set()

    def start(self):
        self._main()
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("Receiver is single-use")
        self._thread = threading.Thread(target=self._run, name="teacher-v3-rx", daemon=True)
        self._report["started"] = True
        self._thread.start()
        return self

    def _run(self):
        previous = None
        try:
            while not self._stop.is_set():
                try:
                    names, done, returned = self._commands.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        if self.sender is None:
                            raise RuntimeError("No observation request sender")
                        rows = self.sender(names)
                        if type(rows) is not list or len(rows) != len(names):
                            raise ValueError("One API evidence row per stream required")
                        returned.append((True, rows))
                        with self._lock:
                            self._report["request_API_calls"] += len(rows)
                    except BaseException as error:
                        with self._lock:
                            self._report["request_partial_API_calls_unknown"] = True
                        returned.append((False, error))
                        raise
                    finally:
                        done.set()
                # No read while capacity is exhausted. No unrecorded decoded
                # packet is dropped to continue collecting newer observations.
                if self._queue.full():
                    with self._lock:
                        self._report["queue_overflow"] = True
                    raise RuntimeError("RX queue capacity exhausted; no silent telemetry drop")
                original = self.reader(0.05)
                if original is None:
                    continue
                received = _real(self.clock(), "receive clock", 0, 1e20)
                if previous is not None and received < previous:
                    raise ValueError("RX observer clock regressed")
                frozen = FrozenReceivedMessage(original)
                self._queue.put_nowait((frozen, received))
                previous = received
                with self._lock:
                    self._report["received_packets"] += 1
                    self._report["raw_packet_bytes"] += len(frozen.get_msgbuf())
                    self._report["queue_high_water"] = max(
                        self._report["queue_high_water"], self._queue.qsize()
                    )
        except BaseException as error:
            self._fail(error)
        finally:
            with self._lock:
                self._report["thread_exited"] = True

    def read_batch(self, timeout_s=0.02):
        self._main()
        timeout = _real(timeout_s, "batch timeout", 0, 1)
        rows = []
        try:
            rows.append(self._queue.get(timeout=timeout))
        except queue.Empty:
            return rows
        while len(rows) < self.batch_size:
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            self._report["consumed_packets"] += len(rows)
        return rows

    def request_streams(self, names):
        self._main()
        self.raise_if_failed()
        from scripts.onboard_teacher_observed_context import REQUIRED_MESSAGES

        allowed = (*REQUIRED_MESSAGES, "GPS_GLOBAL_ORIGIN", "ATTITUDE_QUATERNION")
        if (
            not isinstance(names, (tuple, list))
            or not names
            or any(n not in allowed for n in names)
        ):
            raise ValueError("Only allowlisted observation stream requests permitted")
        if len(names) > len(allowed) or len(set(names)) != len(names):
            raise ValueError("Bounded unique observation stream requests required")
        if self._thread is None or not self._thread.is_alive() or self._stop.is_set():
            raise RuntimeError("Live exclusive RX owner required")
        done, returned = threading.Event(), []
        self._commands.put_nowait((tuple(names), done, returned))
        if not done.wait(2):
            error = RuntimeError("Observation request execution timeout; delivery unknown")
            self._fail(error)
            raise error
        valid, result = returned[0]
        if not valid:
            raise result
        return copy.deepcopy(result)

    def raise_if_failed(self):
        with self._lock:
            error = self._report["failure"]
        if error is not None:
            raise RuntimeError(error)

    def stop(self):
        self._main()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
            if self._thread.is_alive():
                error = RuntimeError("RX owner did not exit before connection cleanup")
                self._fail(error)
                raise error
        with self._lock:
            self._report["stopped"] = True

    def to_mapping(self):
        with self._lock:
            report = copy.deepcopy(self._report)
        report["queued_packets"] = self._queue.qsize()
        report["thread_alive"] = self._thread is not None and self._thread.is_alive()
        return report


class OwnedTeacherGazeboSessionV3(OwnedTeacherGazeboSession):
    """Reuse exact owned Docker lifecycle; explicitly transfer the RX owner."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.receiver = None
        self._camera_thread = None
        self._camera_stop = threading.Event()
        self._camera_updates = queue.Queue(128)
        self._active_camera = None
        self._camera_lock = threading.Lock()
        self._window_report = {
            "started": False,
            "complete": False,
            "failure": None,
            "windows": [],
            "cleanup_errors": [],
        }

    def raw_receive(self, timeout_s):
        raise TeacherSessionError("V3 uses exclusive concurrent receiver, not legacy main RX")

    def start_receiver(self, queue_capacity=256):
        if self.receiver is not None or threading.get_ident() != self._owner:
            raise TeacherSessionError("Single main-thread receiver transfer required")
        if not self.execute or self._closed or self.connection is None:
            raise TeacherSessionError("Opened owned session required")

        def reader(timeout):
            if self.connection.recv_match != self._receive or self._closed:
                raise TeacherSessionError("Original transferred receive path unavailable")
            message = self._receive(blocking=True, timeout=min(timeout, self._remaining()))
            self._remaining()
            return message

        def sender(names):
            from pymavlink import mavutil

            rows = []
            for name in names:
                self._remaining()
                message_id = getattr(mavutil.mavlink, f"MAVLINK_MSG_ID_{name}")
                args = (1, 1, 511, 0, message_id, 250000, 0, 0, 0, 0, 0)
                now = self.clock.monotonic()
                evidence = {
                    "kind": "telemetry-stream-request",
                    "message_type": name,
                    "args": list(args),
                    "API_called_at_s": now,
                    "clock_id": self.clock.session_id,
                    "acknowledged": False,
                    "datagram_delivery_independently_verified": False,
                    "API_returned": False,
                    "error": None,
                }
                self._report.setdefault("stream_request_API_attempts", []).append(evidence)
                try:
                    self.connection.mav.command_long_send(*args)
                except BaseException as error:
                    evidence["error"] = f"{type(error).__name__}: {error}"[:1024]
                    raise
                evidence["API_returned"] = True
                rows.append(copy.deepcopy(evidence))
            return rows

        self.receiver = ConcurrentTelemetryReceiver(
            reader, self.clock.monotonic, request_sender=sender, queue_capacity=queue_capacity
        ).start()
        return self.receiver

    def start_camera_windows(self, directory, persist_callback, *, windows=3, interval_s=1):
        if self._camera_thread is not None or threading.get_ident() != self._owner:
            raise TeacherSessionError("Camera windows are single-use and main-thread started")
        windows = _integer(windows, "camera_windows", 1, 8)
        interval = _real(interval_s, "camera_interval_s", 0, 10)
        total = windows * self.plan.frame_count * self.plan.width * self.plan.height * 7
        if total > MAX_CAPTURE_BYTES or windows * self.plan.frame_count > 128:
            raise ValueError("All camera windows must fit original aggregate capture budget")
        if not callable(persist_callback) or not self.execute or self._closed:
            raise TeacherSessionError(
                "Opened owned session and camera persistence callback required"
            )
        self._window_report["started"] = True

        def acquire():
            try:
                for index in range(windows):
                    if self._camera_stop.is_set():
                        raise TeacherSessionError("Camera windows cancelled; not complete")
                    window = {"window_index": index, "frames": 0, "complete": False}
                    self._window_report["windows"].append(window)
                    backend = self._camera(self.plan, self.container_id, self.clock)
                    camera = None
                    primary = None
                    try:
                        camera = BoundedTeacherCamera(
                            backend, self.plan, self.clock, self.deadline_s
                        )
                        with self._camera_lock:
                            self._active_camera = camera
                        for frame_index in range(self.plan.frame_count):
                            if self._camera_stop.is_set():
                                raise TeacherSessionError("Camera window cancelled")
                            observation = camera.read()
                            target = (
                                Path(directory) / f"camera-window-{index:03d}-{frame_index:03d}"
                            )
                            target.mkdir(exist_ok=False)
                            binding = persist_callback(target, observation)
                            self._camera_updates.put_nowait(
                                {
                                    "window_index": index,
                                    "frame_index": frame_index,
                                    "camera": binding,
                                    "host_return_at_s": observation.host_return_stamp.value_ns
                                    / 1e9,
                                    "source_sensor_stamp_ns": (
                                        observation.provenance.sensor_stamp_ns
                                    ),
                                }
                            )
                            window["frames"] += 1
                        camera.close(require_complete=True)
                        window["complete"] = True
                    except BaseException as error:
                        primary = error
                        raise
                    finally:
                        try:
                            if camera is not None:
                                camera.close(require_complete=primary is None)
                                window["native_stats"] = camera.source.stats.to_mapping()
                            else:
                                backend.close()
                        except BaseException as close_error:
                            note = f"{type(close_error).__name__}: {close_error}"[:1024]
                            self._window_report["cleanup_errors"].append(note)
                            if primary is not None:
                                primary.add_note(f"Camera cleanup secondary: {note}")
                            else:
                                raise
                        finally:
                            with self._camera_lock:
                                self._active_camera = None
                    if index + 1 < windows and self._camera_stop.wait(interval):
                        raise TeacherSessionError("Camera windows cancelled between helpers")
                self._window_report["complete"] = True
            except BaseException as error:
                self._window_report["failure"] = f"{type(error).__name__}: {error}"[:1024]

        self._camera_thread = threading.Thread(
            target=acquire, name="teacher-v3-camera", daemon=True
        )
        self._camera_thread.start()

    def camera_updates(self):
        updates = []
        while True:
            try:
                updates.append(self._camera_updates.get_nowait())
            except queue.Empty:
                return updates

    def camera_finished(self):
        if self._window_report["failure"] is not None:
            raise TeacherSessionError(self._window_report["failure"])
        return self._window_report["complete"] is True

    def stop_camera(self):
        if self._camera_thread is None or not self._camera_thread.is_alive():
            return
        self._camera_stop.set()
        with self._camera_lock:
            camera = self._active_camera
        close_error = None
        try:
            if camera is not None:
                camera.close(require_complete=False)
        except BaseException as error:
            close_error = error
            self._window_report["cleanup_errors"].append(f"{type(error).__name__}: {error}"[:1024])
        finally:
            self._camera_thread.join(4)
        if self._camera_thread.is_alive():
            error = TeacherSessionError("Camera owner did not exit before container cleanup")
            if close_error is not None:
                error.add_note(f"Camera close also failed: {close_error}")
            raise error
        if close_error is not None:
            raise close_error

    def stop_receiver(self):
        if self.receiver is not None:
            self.receiver.stop()

    def _finish(self, primary):
        errors = []
        for stop in (self.stop_camera, self.stop_receiver):
            try:
                stop()
            except BaseException as error:
                errors.append(f"{type(error).__name__}: {error}")
                if primary is not None:
                    primary.add_note(errors[-1])
        if errors:
            self._failure = "; ".join(errors)[:1024]
        super()._finish(primary)
        if errors and primary is None:
            raise TeacherSessionError(self._failure)

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": CONTRACT,
            "receiver": None if self.receiver is None else self.receiver.to_mapping(),
            "camera_windows": copy.deepcopy(self._window_report),
            "camera_thread_alive": self._camera_thread is not None
            and self._camera_thread.is_alive(),
            "flight_authority": False,
        }
