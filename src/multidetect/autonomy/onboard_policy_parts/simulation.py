"""Simulation-only ground event validation and timeline support."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from multidetect.autonomy.mission_supervisor import MissionSupervisor


def validate_sim_ground_events(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the SIM-only timeline before constructing any model resources."""
    events = config.get("sim_ground_events", [])
    if not isinstance(events, list):
        raise ValueError("sim_ground_events must be a list")
    if "sim_ground_events" in config and config.get("mission_package") is None:
        raise ValueError("sim_ground_events requires a mission_package")
    result = []
    previous = -1.0
    for entry in events:
        if not isinstance(entry, dict) or set(entry) != {"t_s", "event"}:
            raise ValueError("SIM timeline entries require exactly t_s and event")
        stamp = entry["t_s"]
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
            or stamp < 0
            or stamp < previous
        ):
            raise ValueError("SIM timeline t_s must be finite, nonnegative and ordered")
        if not isinstance(entry["event"], str) or entry["event"] not in {
            "link_lost",
            "link_restored",
            "abort",
            "pause",
            "resume",
        }:
            raise ValueError("unknown SIM timeline event")
        previous = stamp
        result.append(dict(entry))
    return result


class SimGroundTimeline:
    """Drive supervisor time from SIM frames, never inference or startup latency.

    A nonempty timeline models a healthy link until link_lost stops heartbeats.
    An empty timeline emits no ground events at all. Loss becomes observable
    after heartbeat_timeout_s; the policy silence timer uses the last heartbeat.
    Events crossed between frames are delivered once, in order, at their time.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.now = 0.0
        self.origin_ns: int | None = None
        self.index = 0
        self.link_up = bool(events)

    def advance(
        self,
        observation: Mapping[str, Any],
        supervisor: MissionSupervisor,
        pose_ned: Sequence[float] | None = None,
    ) -> None:
        stamp = observation.get("frame_sensor_stamp_ns")
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0:
            raise ValueError("SIM timeline requires frame_sensor_stamp_ns")
        if self.origin_ns is None:
            self.origin_ns = stamp
        target = (stamp - self.origin_ns) / 1_000_000_000
        if target < self.now:
            raise ValueError("SIM frame clock moved backwards")

        def heartbeat():
            supervisor.ground_event(
                {"mission_id": supervisor.package.mission_id, "type": "heartbeat"}
            )

        while self.index < len(self.events) and self.events[self.index]["t_s"] <= target:
            entry = self.events[self.index]
            self.now = float(entry["t_s"])
            if self.link_up:
                heartbeat()
            # Latch an expired silence/pause before a later resume/reconnect.  The
            # vehicle's pose goes with it: without one the energy model has no return
            # geometry and orders an immediate return (every SIM safety mission did).
            supervisor.evaluate(pose_ned=pose_ned)
            kind = entry["event"]
            supervisor.log.append("sim_ground_event", self.now, event=kind, t_s=self.now)
            if kind == "link_lost":
                self.link_up = False
            elif kind == "link_restored":
                self.link_up = True
                heartbeat()
            else:
                supervisor.ground_event({"mission_id": supervisor.package.mission_id, "type": kind})
            self.index += 1
        self.now = target
        if self.link_up:
            heartbeat()
