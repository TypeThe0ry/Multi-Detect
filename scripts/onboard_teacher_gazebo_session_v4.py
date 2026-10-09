"""Typed, bounded read-only requests on the frozen V3 unique RX owner.

The exact owned SIM/camera/queue/cleanup lifecycle is inherited. This module
does not interpret responses, set parameters, ARM, upload Missions or grant
authority. Attempt evidence is in-memory before the API; the caller persists
it and separately consumes source-qualified dual-committed response records.
The inherited lifecycle receipt remains its original v1 schema; callers save
this session's to_mapping() for the V4 receiver and diagnostic observations.
"""

from __future__ import annotations

import copy
import hashlib
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_V3 = Path(__file__).with_name("onboard_teacher_gazebo_session_v3.py")
_raw = _V3.read_bytes()
if len(_raw) != 19604 or hashlib.sha256(_raw).hexdigest() != (
    "bc7d3479e4c5a0a7fb06180eb14b158f510cdef91bc51c3c5ddf0a6f863a8e0a"
):
    raise RuntimeError("Frozen teacher session V3 source differs")

from scripts.onboard_teacher_gazebo_session_v3 import (  # noqa: E402
    ConcurrentTelemetryReceiver,
    OwnedTeacherGazeboSessionV3,
    TeacherSessionError,
    _real,
)
from scripts.onboard_teacher_observed_context import REQUIRED_MESSAGES  # noqa: E402

CONTRACT = "multidetect.onboard-teacher-gazebo-session-v4"
PARAMETER_ALLOWLIST = (
    "SYS_AUTOSTART",
    "MAV_TYPE",
    "BAT1_SOURCE",
    "BAT1_N_CELLS",
    "BAT1_CAPACITY",
    "BAT1_A_PER_V",
    "SIM_BAT_DRAIN",
    "SIM_BAT_MIN_PCT",
    "COM_ARM_WO_GPS",
    "COM_RC_IN_MODE",
    "EKF2_GPS_CTRL",
    "SYS_HAS_GPS",
    "COM_RCL_EXCEPT",
    "NAV_RCL_ACT",
    "COM_RC_LOSS_T",
    "NAV_DLL_ACT",
    "COM_DL_LOSS_T",
    "BAT_AVRG_CURRENT",
)
STREAM_ALLOWLIST = (*REQUIRED_MESSAGES, "GPS_GLOBAL_ORIGIN", "ATTITUDE_QUATERNION")
MAX_REQUESTS = 32  # 11 streams + one version + 18 reads; no automatic retry.
STREAM_MESSAGE_IDS = {
    "HEARTBEAT": 0,
    "EXTENDED_SYS_STATE": 245,
    "HOME_POSITION": 242,
    "GPS_RAW_INT": 24,
    "GLOBAL_POSITION_INT": 33,
    "LOCAL_POSITION_NED": 32,
    "BATTERY_STATUS": 147,
    "SYS_STATUS": 1,
    "VFR_HUD": 74,
    "GPS_GLOBAL_ORIGIN": 49,
    "ATTITUDE_QUATERNION": 31,
}


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    kind: str
    name: str | None = None

    def __post_init__(self):
        if type(self.kind) is not str:
            raise TypeError("Request kind must be text")
        if self.kind == "autopilot-version":
            if self.name is not None:
                raise ValueError("Version request has no name")
        elif self.kind in {"parameter-read", "stream-interval"}:
            allowed = PARAMETER_ALLOWLIST if self.kind == "parameter-read" else STREAM_ALLOWLIST
            if type(self.name) is not str or self.name not in allowed:
                raise ValueError("Request name is not in the fixed allowlist")
            self.name.encode("ascii", errors="strict")
        else:
            raise ValueError("Only read-only typed observation requests permitted")

    def to_mapping(self):
        self.__post_init__()
        return {"kind": self.kind, "name": self.name}


def _requests(values):
    if type(values) not in (tuple, list) or not 1 <= len(values) <= MAX_REQUESTS:
        raise ValueError("A bounded nonempty typed request sequence required")
    for value in values:
        if type(value) is not DiagnosticRequest:
            raise TypeError("DiagnosticRequest required; no untyped command args")
        value.__post_init__()
    if len(set(values)) != len(values):
        raise ValueError("Duplicate requests in one batch rejected")
    return tuple(values)


class ReadOnlyDiagnosticSender:
    """One RX thread sends only the three exact API forms; no receive loop."""

    def __init__(self, connection, clock, clock_id, remaining):
        if not callable(clock) or not callable(remaining):
            raise TypeError("Actual clock and owned-deadline callback required")
        if type(clock_id) is not str or not clock_id:
            raise ValueError("Clock identity required")
        self.connection, self.clock, self.clock_id, self.remaining = (
            connection,
            clock,
            clock_id,
            remaining,
        )
        self._owner, self._last_at = None, None
        self._attempts, self._lock = [], threading.Lock()

    def _now(self):
        now = _real(self.clock(), "request clock", 0, 1e20)
        if self._last_at is not None and now < self._last_at:
            raise ValueError("Request observer clock regressed")
        self._last_at = now
        return now

    def to_mapping(self):
        with self._lock:
            return copy.deepcopy(self._attempts)

    def __call__(self, requests):
        requests = _requests(requests)
        owner = threading.get_ident()
        with self._lock:
            if self._owner is None:
                self._owner = owner
            if self._owner != owner:
                raise RuntimeError("Diagnostic sender has one exclusive RX thread")
        if len(self._attempts) + len(requests) > MAX_REQUESTS:
            raise ValueError("Cumulative request budget exhausted; no reset/retry")
        returned = []
        for request in requests:
            self.remaining()
            if request.kind == "parameter-read":
                method = "param_request_read_send"
                args = (1, 1, request.name.encode("ascii"), -1)
                reported_args = [1, 1, request.name, -1]
            else:
                method = "command_long_send"
                command, message = (
                    (512, 148)
                    if request.kind == "autopilot-version"
                    else (511, STREAM_MESSAGE_IDS[request.name])
                )
                interval = 250000 if request.kind == "stream-interval" else 0
                args = (1, 1, command, 0, message, interval, 0, 0, 0, 0, 0)
                reported_args = list(args)
            evidence = {
                "attempt_index": len(self._attempts),
                "request_id": uuid4().hex,
                **request.to_mapping(),
                "target_system_id": 1,
                "target_component_id": 1,
                "API_owner_thread_id": owner,
                "API": method,
                "args": reported_args,
                "message_type": (
                    request.name
                    if request.kind == "stream-interval"
                    else "AUTOPILOT_VERSION"
                    if request.kind == "autopilot-version"
                    else "PARAM_VALUE"
                ),
                "param_id_ascii_hex": (
                    request.name.encode("ascii").hex() if request.kind == "parameter-read" else None
                ),
                "API_called_at_s": self._now(),
                "clock_id": self.clock_id,
                "API_attempted": False,
                "API_returned": False,
                "API_returned_at_s": None,
                "error": None,
                "secondary_error": None,
                "attempt_evidence_saved_in_memory_before_API": True,
                "attempt_evidence_durable_before_API": False,
                "response_observed_here": False,
                "acknowledged": False,
                "datagram_delivery_independently_verified": False,
                "parameter_write_performed": False,
                "flight_authority": False,
            }
            with self._lock:
                self._attempts.append(evidence)
            try:
                api = getattr(self.connection.mav, method)
                with self._lock:
                    evidence["API_attempted"] = True
                api(*args)
                with self._lock:
                    evidence["API_returned"] = True
                self.remaining()
                with self._lock:
                    evidence["API_returned_at_s"] = self._now()
                returned.append(copy.deepcopy(evidence))
            except BaseException as error:
                with self._lock:
                    evidence["error"] = f"{type(error).__name__}: {error}"[:1024]
                try:
                    at = self._now()
                    with self._lock:
                        evidence["API_failed_or_refused_at_s"] = at
                except BaseException as secondary:
                    with self._lock:
                        evidence["secondary_error"] = f"{type(secondary).__name__}: {secondary}"[
                            :1024
                        ]
                raise
        return returned


class ConcurrentTelemetryReceiverV4(ConcurrentTelemetryReceiver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reserved_requests = 0
        self._report["contract"] = "multidetect.teacher-concurrent-rx-v4"

    def request_diagnostics(self, requests):
        self._main()
        self.raise_if_failed()
        requests = _requests(requests)
        if self._reserved_requests + len(requests) > MAX_REQUESTS:
            raise ValueError("Cumulative read-only request budget exhausted")
        if self._thread is None or not self._thread.is_alive() or self._stop.is_set():
            raise RuntimeError("Live exclusive RX owner required")
        done, returned = threading.Event(), []
        try:
            self._commands.put_nowait((requests, done, returned))
        except queue.Full as error:
            raise RuntimeError("Another bounded request batch is pending") from error
        self._reserved_requests += len(requests)
        if not done.wait(2):
            error = RuntimeError("Observation request timeout; delivery unknown, no retry")
            self._fail(error)
            raise error
        valid, result = returned[0]
        if not valid:
            raise result
        return copy.deepcopy(result)

    def request_streams(self, names):
        if type(names) not in (tuple, list):
            raise TypeError("Bounded stream name sequence required")
        return self.request_diagnostics(
            tuple(DiagnosticRequest("stream-interval", n) for n in names)
        )

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "request_slots_reserved": self._reserved_requests,
            "maximum_request_API_attempts": MAX_REQUESTS,
        }


class OwnedTeacherGazeboSessionV4(OwnedTeacherGazeboSessionV3):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._diagnostic_sender = None

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

        self._diagnostic_sender = ReadOnlyDiagnosticSender(
            self.connection, self.clock.monotonic, self.clock.session_id, self._remaining
        )
        self.receiver = ConcurrentTelemetryReceiverV4(
            reader,
            self.clock.monotonic,
            request_sender=self._diagnostic_sender,
            queue_capacity=queue_capacity,
        ).start()
        return self.receiver

    def to_mapping(self):
        report = super().to_mapping()
        attempts = [] if self._diagnostic_sender is None else self._diagnostic_sender.to_mapping()
        return {
            **report,
            "contract": CONTRACT,
            "diagnostic_request_API_attempts": attempts,
            "stream_request_API_attempts": [r for r in attempts if r["kind"] == "stream-interval"],
            "parameter_write_performed": False,
            "flight_authority": False,
        }
