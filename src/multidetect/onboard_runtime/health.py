"""Synchronous onboard health state machine."""

from __future__ import annotations

import math
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HealthConfig:
    enabled: bool = False
    slow_failure_limit: int = 2
    probe_interval_s: float = 5.0
    probe_timeout_s: float = 0.5
    slow_latency_budget_s: float = 15.0
    slow_latency_samples: int = 5
    thermal_threshold_c: float = 85.0
    memory_pressure_threshold: float = 0.95
    disk_hard_floor_bytes: int = 256 * 1024 * 1024
    escalate_after_s: float = 60.0
    pressure_hysteresis: float = 0.1
    sampling_failure_limit: int = 10
    max_frame_delta_s: float = 5.0
    critical_temperature_c: float | None = None
    critical_memory_pressure: float | None = None

    def __post_init__(self):
        if (
            self.slow_failure_limit < 1
            or self.slow_latency_samples < 1
            or self.sampling_failure_limit < 1
        ):
            raise ValueError("health limits must be positive")
        vals = (
            self.probe_interval_s,
            self.probe_timeout_s,
            self.slow_latency_budget_s,
            self.thermal_threshold_c,
            self.escalate_after_s,
            self.pressure_hysteresis,
            self.max_frame_delta_s,
        )
        if any(not math.isfinite(float(v)) or float(v) <= 0 for v in vals):
            raise ValueError("health thresholds invalid")
        if not 0 < self.memory_pressure_threshold <= 1 or self.disk_hard_floor_bytes < 0:
            raise ValueError("health thresholds invalid")


@dataclass(frozen=True)
class HealthDecision:
    decision: str = "ok"
    reason: str | None = None
    detector_skip: bool = False
    stop_frame_writes: bool = False

    @property
    def action(self):
        return self.decision

    @property
    def kind(self):
        return self.decision


@dataclass
class _State:
    slow_available: bool = True
    consecutive_slow_failures: int = 0
    slow_latencies: list[float] = field(default_factory=list)
    unavailable_since: float | None = None
    probe_succeeded: bool = False
    last_probe: float = float("-inf")
    hold_since: dict[str, float] = field(default_factory=dict)
    active_reasons: set[str] = field(default_factory=set)
    pressure_active: bool = False
    pressure_toggle: bool = False
    low_disk: bool = False
    last_resources: dict[str, Any] = field(default_factory=dict)
    sample_failures: int = 0
    clock_raw: float | None = None
    elapsed_s: float = 0.0


class HealthMonitor:
    def __init__(
        self,
        *,
        config: HealthConfig | Mapping[str, Any] | None = None,
        runner: Any = None,
        slow_runner: Any = None,
        resources: Callable[[], Mapping[str, Any]] | None = None,
        source: Callable[[], Mapping[str, Any]] | None = None,
        disk_path: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        logger: Callable[[str, Mapping[str, Any]], object] | None = None,
    ):
        if isinstance(config, Mapping):
            config = HealthConfig(**dict(config))
        self.config = config or HealthConfig()
        self.runner = runner if runner is not None else slow_runner
        self.resources = resources or source or self.jetson_resources
        self.disk_path = disk_path
        self.clock = clock
        self.logger = logger
        self.state = _State()

    @staticmethod
    def jetson_resources():
        out = {}
        try:
            ts = {}
            for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
                v = float(p.read_text().strip()) / 1000
                if math.isfinite(v):
                    ts[p.parent.name] = v
            if ts:
                out.update(temperatures_c=ts, temperature_c=max(ts.values()))
        except (OSError, ValueError):
            pass
        try:
            vals = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, s, r = line.partition(":")
                if s and r.split():
                    vals[k] = float(r.split()[0]) * 1024
            if vals.get("MemTotal"):
                out["memory_pressure_ratio"] = 1 - vals.get("MemAvailable", 0) / vals["MemTotal"]
        except (OSError, ValueError, IndexError):
            pass
        return out

    @staticmethod
    def _num(v):
        return (
            float(v)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
            else None
        )

    def _log(self, r, a, n, **d):
        if self.logger:
            try:
                self.logger(r, {"active": a, "at_s": n, **d})
            except Exception:
                pass

    def _trans(self, r, a, n, **d):
        p = r in self.state.active_reasons
        if a and not p:
            self.state.active_reasons.add(r)
            self.state.hold_since.setdefault(r, n)
            self._log(r, True, n, **d)
        elif not a and p:
            self.state.active_reasons.remove(r)
            self.state.hold_since.pop(r, None)
            self._log(r, False, n, **d)

    def _probe(self, n):
        if self.state.slow_available or n - self.state.last_probe < self.config.probe_interval_s:
            return
        self.state.last_probe = n
        fn = getattr(self.runner, "probe_health", None) if self.runner else None
        if fn:
            try:
                ok = bool(fn(timeout_s=self.config.probe_timeout_s))
            except Exception:
                ok = False
            self.state.probe_succeeded = ok
        else:
            self.state.probe_succeeded = False

    def _time(self, raw):
        raw = float(raw)
        if self.state.clock_raw is None:
            self.state.clock_raw = raw
            return self.state.elapsed_s
        self.state.elapsed_s += min(
            self.config.max_frame_delta_s, max(0.0, raw - self.state.clock_raw)
        )
        self.state.clock_raw = raw
        return self.state.elapsed_s

    def on_frame(self, *, now=None, mission_requires_images=False):
        n = self._time(self.clock() if now is None else now)
        if not self.config.enabled:
            return HealthDecision()
        self._probe(n)
        try:
            rd = dict(self.resources() or {})
        except Exception as e:
            self._trans("health_source_error", True, n, error=str(e))
            rd = {}
            self.state.sample_failures += 1
        else:
            self._trans("health_source_error", False, n)
            self.state.sample_failures = 0
        self.state.last_resources = rd
        t = self._num(rd.get("temperature_c"))
        if t is None and isinstance(rd.get("temperatures_c"), Mapping):
            t = max(
                (x for x in (self._num(v) for v in rd["temperatures_c"].values()) if x is not None),
                default=None,
            )
        m = self._num(rd.get("memory_pressure_ratio", rd.get("memory_pressure")))
        thr = bool(rd.get("throttled", rd.get("thermal_throttled", False)))
        measured_pressure = (
            (t is not None and t >= self.config.thermal_threshold_c)
            or thr
            or (m is not None and m >= self.config.memory_pressure_threshold)
        )
        if t is None and m is None and not thr and rd:
            self.state.sample_failures += 1
            pressure = self.state.pressure_active
        else:
            self.state.sample_failures = 0
            pressure = measured_pressure
        if self.state.pressure_active:
            pressure = not (
                (
                    t is None
                    or t < self.config.thermal_threshold_c * (1 - self.config.pressure_hysteresis)
                )
                and (
                    m is None
                    or m
                    < self.config.memory_pressure_threshold * (1 - self.config.pressure_hysteresis)
                )
                and not thr
            )
        self.state.pressure_active = pressure
        if not pressure:
            self.state.pressure_toggle = False
        self._trans("resource_pressure", pressure, n, temperature_c=t, memory_pressure=m)
        sampling_bad = self.state.sample_failures >= self.config.sampling_failure_limit
        self._trans(
            "resource_sampling_failed", sampling_bad, n, failures=self.state.sample_failures
        )
        free = self._num(rd.get("disk_free_bytes", rd.get("disk_free")))
        if free is None and self.disk_path is not None:
            try:
                free = float(shutil.disk_usage(self.disk_path).free)
            except OSError:
                free = None
        low = free is not None and free < self.config.disk_hard_floor_bytes
        self.state.low_disk = low
        self._trans("low_disk", low, n, free_bytes=free)
        critical = (
            self.config.critical_temperature_c is not None
            and t is not None
            and t >= self.config.critical_temperature_c
        ) or (
            self.config.critical_memory_pressure is not None
            and m is not None
            and m >= self.config.critical_memory_pressure
        )
        holds = [
            r
            for r in ("slow_unavailable", "slow_latency_drift", "resource_pressure")
            if r in self.state.active_reasons
        ]
        if "resource_sampling_failed" in self.state.active_reasons:
            holds.append("resource_sampling_failed")
        if mission_requires_images and "low_disk" in self.state.active_reasons:
            holds.append("low_disk")
        if critical:
            return HealthDecision("land", "critical_resource_pressure", stop_frame_writes=low)
        if any(n - self.state.hold_since.get(r, n) >= self.config.escalate_after_s for r in holds):
            return HealthDecision("return_home", holds[0], stop_frame_writes=low)
        if low and mission_requires_images:
            return HealthDecision("hold", "low_disk", stop_frame_writes=True)
        if pressure:
            self.state.pressure_toggle = not self.state.pressure_toggle
            if self.state.pressure_toggle:
                return HealthDecision(
                    "hold", "resource_pressure", detector_skip=True, stop_frame_writes=low
                )
        for r in ("slow_unavailable", "slow_latency_drift"):
            if r in self.state.active_reasons:
                return HealthDecision("hold", r, stop_frame_writes=low)
        if "resource_sampling_failed" in self.state.active_reasons:
            return HealthDecision("hold", "resource_sampling_failed", stop_frame_writes=low)
        return HealthDecision("ok", stop_frame_writes=low)

    check = on_frame
    tick = on_frame

    def record_slow_call(self, latency_s, *, ok=True, now=None):
        if not self.config.enabled:
            return HealthDecision()
        n = self._time(self.clock() if now is None else now)
        latency = self._num(latency_s)
        if latency is not None:
            self.state.slow_latencies = (self.state.slow_latencies + [latency])[
                -self.config.slow_latency_samples :
            ]
        if not ok:
            self.state.consecutive_slow_failures += 1
            self.state.probe_succeeded = False
            if self.state.consecutive_slow_failures >= self.config.slow_failure_limit:
                self.state.slow_available = False
                self.state.unavailable_since = self.state.unavailable_since or n
                self._trans(
                    "slow_unavailable", True, n, failures=self.state.consecutive_slow_failures
                )
            return HealthDecision("hold", "slow_call_failed")
        self.state.consecutive_slow_failures = 0
        if self.state.probe_succeeded:
            self.state.slow_available = True
            self.state.probe_succeeded = False
            self.state.unavailable_since = None
            self._trans("slow_unavailable", False, n)
        drift = len(self.state.slow_latencies) >= self.config.slow_latency_samples and all(
            x > self.config.slow_latency_budget_s for x in self.state.slow_latencies
        )
        self._trans("slow_latency_drift", drift, n, latency_s=latency)
        return HealthDecision("hold", "slow_latency_drift") if drift else HealthDecision()


OnboardHealthMonitor = HealthMonitor
__all__ = ["HealthConfig", "HealthDecision", "HealthMonitor", "OnboardHealthMonitor"]
