"""Cheap onboard health checks and monotonic degradation decisions.

The monitor is deliberately dependency free.  Jetson readings are best effort
(``tegrastats``/sysfs); tests can inject a source returning a mapping.  A
missing reading is healthy/unknown, while explicit bad readings are latched.
Slow identity work is disabled after bounded restart attempts; navigation and
return-home steps may continue, but identity-dependent steps request RTL.
"""

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
    slow_timeout_s: float = 20.0
    health_probe_timeout_s: float = 1.0
    slow_latency_budget_s: float = 15.0
    slow_latency_samples: int = 5
    max_restarts: int = 3
    restart_backoff_s: float = 1.0
    restart_backoff_max_s: float = 30.0
    detector_latency_budget_s: float = 1.0
    temperature_limit_c: float = 85.0
    memory_pressure_ratio: float = 0.95
    disk_free_bytes: int = 256 * 1024 * 1024
    check_interval_s: float = 1.0

    def __post_init__(self) -> None:
        for value in (self.slow_timeout_s, self.slow_latency_budget_s,
                      self.health_probe_timeout_s, self.restart_backoff_s,
                      self.restart_backoff_max_s,
                      self.detector_latency_budget_s, self.temperature_limit_c,
                      self.check_interval_s):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("health thresholds must be positive and finite")
        if self.slow_latency_samples < 1 or self.max_restarts < 0:
            raise ValueError("health sample and restart limits are invalid")
        if not 0 < self.memory_pressure_ratio <= 1 or self.disk_free_bytes < 0:
            raise ValueError("health resource thresholds are invalid")


@dataclass
class HealthState:
    healthy: bool = True
    hold: bool = False
    slow_available: bool = True
    stop_frames: bool = False
    detector_rate_scale: float = 1.0
    fatal_reason: str | None = None
    reasons: list[str] = field(default_factory=list)
    restarts: int = 0
    slow_latencies: list[float] = field(default_factory=list)
    last: dict[str, Any] = field(default_factory=dict)


class OnboardHealthMonitor:
    """Observe model/sensor/resource health without blocking the flight loop."""

    def __init__(self, *, config: HealthConfig | None = None,
                 runner: Any = None, source: Callable[[], Mapping[str, Any]] | None = None,
                 disk_path: str | Path | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 logger: Callable[[str, Mapping[str, Any]], None] | None = None) -> None:
        self.config = config or HealthConfig()
        self.runner = runner
        self.source = source or self.jetson_source
        self.disk_path, self.clock, self.logger = disk_path, clock, logger
        self.state = HealthState()
        self._last_check = float("-inf")
        self._next_restart = 0.0
        self._detector_slow = False

    @staticmethod
    def jetson_source() -> Mapping[str, Any]:
        """Read cheap sysfs thermal/memory signals when running on Jetson."""
        result: dict[str, Any] = {}
        try:
            temps = {}
            for entry in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
                raw = float(entry.read_text().strip())
                if math.isfinite(raw):
                    temps[entry.parent.name] = raw / 1000.0
            if temps:
                result["temperatures_c"] = temps
        except (OSError, ValueError):
            pass
        try:
            info = Path("/proc/meminfo").read_text()
            values = {line.split(":", 1)[0]: float(line.split()[1]) * 1024
                      for line in info.splitlines()
                      if ":" in line and line.split()[1].replace('.', '', 1).isdigit()}
            total, avail = values.get("MemTotal"), values.get("MemAvailable")
            if total and avail is not None:
                result["memory_pressure_ratio"] = 1.0 - avail / total
        except (OSError, ValueError, IndexError):
            pass
        return result

    def _record(self, reason: str, **details: Any) -> None:
        if reason not in self.state.reasons:
            self.state.reasons.append(reason)
        self.state.healthy = False
        self.logger and self.logger(reason, details)

    def _restart(self, now: float) -> None:
        if self.runner is None or now < self._next_restart:
            return
        process = getattr(self.runner, "process", None)
        dead = process is not None and process.poll() is not None
        check = getattr(self.runner, "health_check", None)
        try:
            endpoint_ok = True if check is None else bool(
                check(timeout=self.config.health_probe_timeout_s)
            )
        except Exception:
            endpoint_ok = False
        if not dead and endpoint_ok:
            return
        if self.state.restarts >= self.config.max_restarts:
            self.state.slow_available = False
            self.state.hold = True
            self.state.fatal_reason = "slow_model_unavailable_after_restarts"
            self._record(self.state.fatal_reason, attempts=self.state.restarts)
            return
        restart = getattr(self.runner, "restart", None)
        if restart is None:
            self.state.slow_available = False
            self.state.hold = True
            self._record("slow_model_unavailable_no_restart", dead=dead)
            return
        self.state.hold = True
        self._record("slow_model_restart", attempt=self.state.restarts + 1)
        try:
            restart()
            self.state.restarts += 1
            delay = min(self.config.restart_backoff_max_s,
                        self.config.restart_backoff_s * 2 ** max(0, self.state.restarts - 1))
            self._next_restart = now + delay
            self.state.slow_available = True
            self.state.hold = False
        except Exception as exc:
            self.state.restarts += 1
            self._record("slow_model_restart_failed", error=str(exc))
            self._next_restart = now + self.config.restart_backoff_s

    def tick(self, *, now: float | None = None, camera_age_s: float | None = None,
             detector_latency_s: float | None = None, detector_ok: bool = True) -> HealthState:
        now = self.clock() if now is None else now
        if now - self._last_check < self.config.check_interval_s:
            return self.state
        self._last_check = now
        self._restart(now)
        readings = dict(self.source() or {}) if self.source is not None else {}
        self.state.last = readings
        temp = readings.get("temperature_c")
        if temp is None and isinstance(readings.get("temperatures_c"), Mapping):
            values = [v for v in readings["temperatures_c"].values()
                      if isinstance(v, (int, float)) and math.isfinite(v)]
            temp = max(values, default=None)
        if (isinstance(temp, (int, float)) and math.isfinite(temp)
                and temp >= self.config.temperature_limit_c):
            self.state.detector_rate_scale = min(self.state.detector_rate_scale, 0.5)
            self._record("thermal_throttle", temperature_c=temp)
        memory = readings.get("memory_pressure_ratio")
        if (isinstance(memory, (int, float)) and math.isfinite(memory)
                and memory >= self.config.memory_pressure_ratio):
            self._record("memory_pressure", ratio=memory)
        throttled = readings.get("throttled")
        if throttled:
            self.state.detector_rate_scale = min(self.state.detector_rate_scale, 0.5)
            self._record("hardware_throttling")
        if camera_age_s is not None and (not math.isfinite(camera_age_s) or camera_age_s > 0.75):
            self._record("camera_frame_stale", age_s=camera_age_s)
        if (not detector_ok or detector_latency_s is not None and
                (not math.isfinite(detector_latency_s)
                 or detector_latency_s > self.config.detector_latency_budget_s)):
            self._detector_slow = True
            self.state.detector_rate_scale = min(self.state.detector_rate_scale, 0.5)
            self._record("detector_unhealthy", latency_s=detector_latency_s)
        if self.disk_path is not None:
            try:
                if shutil.disk_usage(self.disk_path).free < self.config.disk_free_bytes:
                    self.state.stop_frames = True
                    self._record("low_disk", path=str(self.disk_path))
            except OSError:
                pass
        self.state.healthy = not self.state.reasons
        return self.state

    def record_slow_call(self, latency_s: float, *, ok: bool = True) -> HealthState:
        latency = float(latency_s)
        self.state.slow_latencies.append(latency)
        del self.state.slow_latencies[:-self.config.slow_latency_samples]
        if not ok or not math.isfinite(latency) or latency > self.config.slow_latency_budget_s:
            self._record("slow_call_timeout" if not ok else "slow_call_latency", latency_s=latency)
            if not ok:
                self.state.slow_available = False
        if len(self.state.slow_latencies) >= self.config.slow_latency_samples and all(
            x > self.config.slow_latency_budget_s for x in self.state.slow_latencies
        ):
            self.state.slow_available = False
            self._record("slow_latency_drift", latency_s=latency)
        return self.state

