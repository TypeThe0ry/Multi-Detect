"""Deterministic user-space packet shaping shared by MAVLink and RTP relays."""

from __future__ import annotations

import asyncio
import random
import threading
from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class NetemConfig:
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    bandwidth_kbps: float = 0.0

    def __post_init__(self) -> None:
        for name in ("delay_ms", "jitter_ms", "loss_pct", "bandwidth_kbps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.loss_pct > 100:
            raise ValueError("loss_pct must be at most 100")

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> NetemConfig:
        allowed = {"delay_ms", "jitter_ms", "loss_pct", "bandwidth_kbps"}
        extra = set(value) - allowed
        if extra:
            raise ValueError(f"unknown netem field(s): {', '.join(sorted(extra))}")
        return cls(**{key: float(raw) for key, raw in value.items()})

    def to_mapping(self) -> dict[str, float]:
        return {
            "delay_ms": float(self.delay_ms),
            "jitter_ms": float(self.jitter_ms),
            "loss_pct": float(self.loss_pct),
            "bandwidth_kbps": float(self.bandwidth_kbps),
        }


class NetemShaper:
    """Schedule packets with independent up/down delay, jitter, loss and rate limits.

    ``shape`` is synchronous and deterministic for a given seed and call order;
    callers drop packets when it returns ``None`` and otherwise await the
    returned delay. A zero bandwidth limit disables serialization shaping.
    """

    def __init__(
        self,
        up: NetemConfig | None = None,
        down: NetemConfig | None = None,
        *,
        seed: int = 1,
    ) -> None:
        self._configs: dict[Direction, NetemConfig] = {
            "up": up or NetemConfig(),
            "down": down or NetemConfig(),
        }
        self.seed = int(seed)
        self._randoms = {
            "up": random.Random(self.seed),
            "down": random.Random(self.seed ^ 0x9E3779B9),
        }
        self._next_send = {"up": 0.0, "down": 0.0}
        self._lock = threading.Lock()

    def configure(
        self,
        direction: Direction,
        config: NetemConfig,
        *,
        reseed: int | None = None,
    ) -> None:
        if direction not in self._configs:
            raise ValueError("direction must be 'up' or 'down'")
        with self._lock:
            self._configs[direction] = config
            self._next_send[direction] = 0.0
            if reseed is not None:
                self.seed = int(reseed)
                for name in ("up", "down"):
                    self._next_send[name] = 0.0
                    self._randoms[name].seed(self.seed if name == "up" else self.seed ^ 0x9E3779B9)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "seed": self.seed,
                "up": self._configs["up"].to_mapping(),
                "down": self._configs["down"].to_mapping(),
            }

    def shape(self, direction: Direction, packet: bytes, *, now: float | None = None) -> float | None:
        """Return seconds to wait, or ``None`` when the packet is dropped."""
        if direction not in self._configs:
            raise ValueError("direction must be 'up' or 'down'")
        clock = asyncio.get_running_loop().time() if now is None and _in_async_loop() else now
        if clock is None:
            import time

            clock = time.monotonic()
        with self._lock:
            config = self._configs[direction]
            rng = self._randoms[direction]
            if rng.random() * 100.0 < config.loss_pct:
                return None
            jitter = rng.uniform(-config.jitter_ms, config.jitter_ms)
            delay = max(0.0, config.delay_ms + jitter) / 1000.0
            ready = max(float(clock), self._next_send[direction])
            if config.bandwidth_kbps > 0:
                serialization = len(packet) * 8.0 / (config.bandwidth_kbps * 1000.0)
                ready += serialization
                self._next_send[direction] = ready
            return max(0.0, ready - float(clock)) + delay

    async def transmit(self, direction: Direction, packet: bytes, sender) -> bool:
        """Apply shaping then call ``sender(packet)``; suitable for RTP relays."""
        delay = self.shape(direction, packet)
        if delay is None:
            return False
        if delay:
            await asyncio.sleep(delay)
        result = sender(packet)
        if hasattr(result, "__await__"):
            await result
        return True


def _in_async_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


__all__ = ["Direction", "NetemConfig", "NetemShaper"]
