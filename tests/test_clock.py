from __future__ import annotations

from datetime import datetime, timezone

import pytest

from multidetect.clock import (
    Clock,
    HighResolutionSystemClock,
    HostClockStamp,
    ManualClock,
    SystemClock,
)


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


def test_high_resolution_clock_protocol_identity_and_integer_arithmetic(monkeypatch):
    from multidetect import clock as module

    values = iter([1788856937257446631, 1788856937257446632])
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: next(values))
    clock = HighResolutionSystemClock(session_id="sim-session")
    assert isinstance(clock, Clock)
    first, second = clock.read(), clock.read()
    assert second.elapsed_since(first) == 1e-9
    assert first.to_mapping()["value_ns"] == 1788856937257446631
    assert clock.identity()["source"] == "python.perf_counter"
    assert clock.identity()["resolution_ns"] > 0
    assert not clock.identity()["legacy_monotonic_equivalent"]
    assert not clock.identity()["accuracy_verified"]
    assert clock.utc_now().tzinfo is not None


@pytest.mark.parametrize(
    "source,session", [("python.monotonic", "a"), ("python.perf_counter", "b")]
)
def test_host_stamp_rejects_cross_domain_subtraction(source, session):
    start = HostClockStamp(100, "python.perf_counter", "a")
    with pytest.raises(ValueError, match="domain/session"):
        HostClockStamp(200, source, session).elapsed_since(start)


@pytest.mark.parametrize(
    "value,source,session",
    [
        (True, "python.perf_counter", "a"),
        (-1, "python.perf_counter", "a"),
        (1.5, "python.perf_counter", "a"),
        (1, "unknown", "a"),
        (1, "python.perf_counter", " "),
        (1, "python.perf_counter", None),
    ],
)
def test_host_stamp_rejects_invalid_identity(value, source, session):
    with pytest.raises(ValueError):
        HostClockStamp(value, source, session)


def test_stamp_rejects_rewind_and_untyped_previous():
    stamp = HostClockStamp(1, "python.perf_counter", "a")
    with pytest.raises(ValueError, match="backwards"):
        stamp.elapsed_since(HostClockStamp(2, "python.perf_counter", "a"))
    with pytest.raises(TypeError):
        stamp.elapsed_since(0)


def test_performance_clock_seconds_come_from_same_ns_source(monkeypatch):
    from multidetect import clock as module

    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: 1250000000)
    clock = HighResolutionSystemClock()
    assert clock.monotonic() == clock.monotonic_s() == 1.25
    assert clock.monotonic_ns() == 1250000000
    assert clock.session_id != HighResolutionSystemClock().session_id


def test_legacy_system_clock_is_not_silently_switched(monkeypatch):
    from multidetect import clock as module

    monkeypatch.setattr(module.time, "monotonic", lambda: 10)
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: 20000000000)
    assert SystemClock().monotonic() == 10
    assert HighResolutionSystemClock().monotonic() == 20
