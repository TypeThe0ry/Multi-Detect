"""Bounded event journal shared by SSE, WebSocket and runtime adapters."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any


class EventJournal:
    def __init__(self, capacity: int = 2048) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next_id = 1
        self._condition = threading.Condition()
        self._context = threading.local()

    def bind(self, *, request_id: str, slot: int, started_at: float) -> None:
        self._context.value = (request_id, slot, started_at)

    def unbind(self) -> None:
        self._context.value = None

    def publish(self, name: str, payload: Mapping[str, object]) -> dict[str, Any]:
        context = getattr(self._context, "value", None)
        data = dict(payload)
        if context is not None:
            request_id, slot, _ = context
            data.setdefault("request_id", request_id)
            data.setdefault("vehicle_slot", slot)
        with self._condition:
            event = {
                "id": self._next_id,
                "event": name,
                "timestamp": time.time(),
                "data": data,
            }
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
            return dict(event)

    def latest_id(self) -> int:
        with self._condition:
            return self._next_id - 1

    def next_after(self, event_id: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events:
                    if event["id"] > event_id:
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    @staticmethod
    def encode_sse(event: Mapping[str, object]) -> bytes:
        payload = json.dumps(event["data"], separators=(",", ":"), ensure_ascii=False)
        return f"id: {event['id']}\nevent: {event['event']}\ndata: {payload}\n\n".encode()

