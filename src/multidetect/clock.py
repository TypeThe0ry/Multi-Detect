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
from math import isfinite
from threading import RLock
from typing import Protocol, runtime_checkable

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


__all__ = ["Clock", "ManualClock", "SystemClock"]
