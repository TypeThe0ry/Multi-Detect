"""Bounded userspace timing evidence, never a source-event or latency estimate."""

import copy
import math


class MissionLoopTiming:
    def __init__(self, identity):
        self.identity = copy.deepcopy(identity)
        self.phases = {}

    def record(self, phase, start, end):
        if phase not in ("pump", "shadow", "other"):
            raise ValueError("unknown timing phase")
        if (
            any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (start, end)
            )
            or end < start
        ):
            raise ValueError("invalid timing interval")
        bucket = self.phases.setdefault(phase, {"count": 0, "total_s": 0.0, "slowest": []})
        elapsed = end - start
        bucket["count"] += 1
        bucket["total_s"] += elapsed
        bucket["slowest"].append({"start_host_s": start, "end_host_s": end, "elapsed_s": elapsed})
        bucket["slowest"].sort(key=lambda row: row["elapsed_s"], reverse=True)
        del bucket["slowest"][32:]

    def snapshot(self):
        return copy.deepcopy(
            {
                "schema_version": 1,
                "host_clock_identity": self.identity,
                "phases": self.phases,
                "max_intervals_per_phase": 32,
                "scope": "userspace elapsed intervals include scheduler preemption; not CPU time",
                "source_send_timing_claimed": False,
                "queue_latency_claimed": False,
                "terminal_tail_included": False,
            }
        )
