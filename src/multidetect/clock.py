"""Injectable clocks used by real-time and deterministic runtimes.

The autonomy core must not read :mod:`time` or wall-clock time directly.  A
runtime owns one clock and passes it to code that needs time.  ``ManualClock``
is deliberately monotonic: replay and simulation can move time forwards, but
cannot silently rewind a running mission.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil, isfinite
from threading import RLock
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .compat import UTC


@runtime_checkable
class Clock(Protocol):
    """Minimal time source shared by Replay, SIM, HIL and REAL."""

    def monotonic(self) -> float:
        """Return monotonic seconds for freshness and deadline decisions."""

    def utc_now(self) -> datetime:
        """Return an aware UTC timestamp for durable audit records."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Production clock backed by the operating system."""

    def monotonic(self) -> float:
        return time.monotonic()

    def monotonic_s(self) -> float:
        """Explicit-unit alias used by new runtime code."""

        return self.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def now_utc(self) -> datetime:
        return self.utc_now()


@dataclass(frozen=True, slots=True)
class HostClockStamp:
    """An integer timestamp bound to an explicit source and runtime session.

    Session identity is supplied by the runtime, not inferred from similar
    numbers. It prevents accidental subtraction across reboots or captures.
    """

    value_ns: int
    source: str
    session_id: str

    def __post_init__(self) -> None:
        if type(self.value_ns) is not int or self.value_ns < 0:
            raise ValueError("timestamp must be a nonnegative integer")
        if self.source not in ("python.monotonic", "python.perf_counter"):
            raise ValueError("unknown host clock source")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("explicit clock session identity required")

    def elapsed_since(self, previous: HostClockStamp) -> float:
        if not isinstance(previous, HostClockStamp):
            raise TypeError("domain-bound timestamp required")
        if (self.source, self.session_id) != (previous.source, previous.session_id):
            raise ValueError("host clock domain/session mismatch")
        if self.value_ns < previous.value_ns:
            raise ValueError("host clock moved backwards")
        return (self.value_ns - previous.value_ns) / 1_000_000_000

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "value_ns": self.value_ns,
            "source": self.source,
            "session_id": self.session_id,
        }


@dataclass(frozen=True, slots=True)
class HighResolutionSystemClock:
    """Opt-in shared Clock backed by perf_counter, never silently epoch-shifted.

    Existing SystemClock and ManualClock retain their behavior. A runtime must
    inject this same clock into all event/freshness consumers before comparing
    its timestamps. Resolution metadata is not an accuracy certification.
    """

    session_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        HostClockStamp(0, "python.perf_counter", self.session_id)
        info = time.get_clock_info("perf_counter")
        if not info.monotonic or info.adjustable:
            raise ValueError("non-adjustable monotonic performance clock required")
        if not isfinite(info.resolution) or info.resolution <= 0:
            raise ValueError("positive finite clock resolution required")

    def monotonic_ns(self) -> int:
        return self.read().value_ns

    def read(self) -> HostClockStamp:
        return HostClockStamp(time.perf_counter_ns(), "python.perf_counter", self.session_id)

    def monotonic(self) -> float:
        return self.monotonic_ns() / 1_000_000_000

    def monotonic_s(self) -> float:
        return self.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def now_utc(self) -> datetime:
        return self.utc_now()

    def identity(self) -> dict[str, object]:
        info = time.get_clock_info("perf_counter")
        return {
            "schema_version": 1,
            "source": "python.perf_counter",
            "session_id": self.session_id,
            "implementation": info.implementation,
            "resolution_ns": ceil(info.resolution * 1_000_000_000),
            "accuracy_verified": False,
            "legacy_monotonic_equivalent": False,
        }


@dataclass(slots=True)
class ManualClock:
    """Thread-safe deterministic clock for replay and simulation tests."""

    initial_monotonic_s: float = 0.0
    initial_utc: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    _monotonic_s: float = field(init=False, repr=False)
    _utc: datetime = field(init=False, repr=False)
    _lock: RLock = field(init=False, repr=False, default_factory=RLock)

    def __post_init__(self) -> None:
        if not isfinite(self.initial_monotonic_s) or self.initial_monotonic_s < 0.0:
            raise ValueError("initial_monotonic_s must be finite and non-negative")
        if self.initial_utc.tzinfo is None or self.initial_utc.utcoffset() is None:
            raise ValueError("initial_utc must be timezone-aware")
        self._monotonic_s = float(self.initial_monotonic_s)
        self._utc = self.initial_utc.astimezone(UTC)

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic_s

    def monotonic_s(self) -> float:
        return self.monotonic()

    def utc_now(self) -> datetime:
        with self._lock:
            return self._utc

    def now_utc(self) -> datetime:
        return self.utc_now()

    def advance(self, seconds: float) -> float:
        """Advance both time domains and return the new monotonic value."""

        if not isfinite(seconds) or seconds < 0.0:
            raise ValueError("clock advance must be finite and non-negative")
        with self._lock:
            self._monotonic_s += float(seconds)
            self._utc += timedelta(seconds=float(seconds))
            return self._monotonic_s

    def advance_to(self, monotonic_s: float) -> float:
        """Advance to an absolute monotonic timestamp without allowing rewind."""

        if not isfinite(monotonic_s) or monotonic_s < 0.0:
            raise ValueError("target monotonic time must be finite and non-negative")
        with self._lock:
            if monotonic_s < self._monotonic_s:
                raise ValueError("manual clock cannot move backwards")
            delta = float(monotonic_s) - self._monotonic_s
            self._monotonic_s = float(monotonic_s)
            self._utc += timedelta(seconds=delta)
            return self._monotonic_s


def validate_host_identity(identity):
    fields = {
        "schema_version",
        "source",
        "session_id",
        "implementation",
        "resolution_ns",
        "accuracy_verified",
        "legacy_monotonic_equivalent",
    }
    if not isinstance(identity, dict) or set(identity) != fields:
        raise ValueError("invalid host clock identity fields")
    if (
        type(identity["schema_version"]) is not int
        or identity["schema_version"] != 1
        or identity["source"] != "python.perf_counter"
        or identity["accuracy_verified"] is not False
        or identity["legacy_monotonic_equivalent"] is not False
    ):
        raise ValueError("invalid host clock identity claims")
    for key in ("session_id", "implementation"):
        if not isinstance(identity[key], str) or not 1 <= len(identity[key].strip()) <= 256:
            raise ValueError("invalid host clock identity text")
    if type(identity["resolution_ns"]) is not int or not 0 < identity["resolution_ns"] <= 10**9:
        raise ValueError("invalid host clock resolution")
    return identity


__all__ = [
    "Clock",
    "ManualClock",
    "SystemClock",
    "HighResolutionSystemClock",
    "HostClockStamp",
    "validate_host_identity",
]
