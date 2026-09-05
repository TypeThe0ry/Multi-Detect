from __future__ import annotations

from datetime import datetime, timezone

import pytest

from multidetect.clock import Clock, ManualClock, SystemClock


def test_system_clock_satisfies_clock_protocol_and_returns_aware_utc() -> None:
    clock = SystemClock()

    assert isinstance(clock, Clock)
    assert clock.monotonic() >= 0.0
    assert clock.utc_now().tzinfo is not None
    assert clock.utc_now().utcoffset() is not None


def test_manual_clock_advances_monotonic_and_utc_together() -> None:
    clock = ManualClock(
        initial_monotonic_s=5.0,
        initial_utc=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    assert clock.advance(2.5) == 7.5
    assert clock.monotonic() == 7.5
    assert clock.utc_now() == datetime(2026, 8, 25, 0, 0, 2, 500000, tzinfo=timezone.utc)


def test_manual_clock_refuses_rewind_and_nonfinite_time() -> None:
    clock = ManualClock(initial_monotonic_s=10.0)

    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(9.0)
    with pytest.raises(ValueError, match="finite"):
        clock.advance(float("nan"))
    with pytest.raises(ValueError, match="non-negative"):
        clock.advance(-0.1)


def test_manual_clock_requires_aware_initial_utc() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(initial_utc=datetime(2026, 8, 25))
