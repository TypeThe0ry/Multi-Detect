"""Bounded raw position evidence for phase diagnostics, never a control input."""

import math


class MissionPositionCapture:
    def __init__(self, capacity=32768):
        if type(capacity) is not int or not 1 <= capacity <= 32768:
            raise ValueError("capacity must be an integer in 1..32768")
        self.capacity = capacity
        self.samples = []
        self.samples_seen = 0

    def record(self, message, received_at_s):
        if message.get_type() != "GLOBAL_POSITION_INT":
            return
        if (
            type(received_at_s) not in (int, float)
            or not math.isfinite(received_at_s)
            or received_at_s < 0
        ):
            raise ValueError("finite nonnegative receipt time required")
        self.samples_seen += 1
        if len(self.samples) >= self.capacity:
            return
        fields = ("time_boot_ms", "lat", "lon", "relative_alt", "vx", "vy", "vz")
        sample = {name: getattr(message, name, None) for name in fields}
        # MAVLink integer sentinels remain raw; missing/malformed fields are not
        # silently converted into zero altitude or zero vertical speed.
        missing = [name for name in fields if type(sample[name]) is not int]
        for name in missing:
            sample[name] = None
        sample.update(
            received_at_s=received_at_s,
            source_system_id=message.get_srcSystem(),
            source_component_id=message.get_srcComponent(),
            missing_integer_fields=missing,
        )
        self.samples.append(sample)

    def snapshot(self):
        return dict(
            schema_version=1,
            message_type="GLOBAL_POSITION_INT",
            clock="observer_relative_host_receive_time",
            units=dict(
                time_boot_ms="source_boot_ms",
                lat="degE7",
                lon="degE7",
                relative_alt="mm_relative_home",
                vx="cm/s_north",
                vy="cm/s_east",
                vz="cm/s_down",
            ),
            capacity=self.capacity,
            samples_seen=self.samples_seen,
            samples_dropped=self.samples_seen - len(self.samples),
            truncated=self.samples_seen > len(self.samples),
            samples=[
                dict(s, missing_integer_fields=list(s["missing_integer_fields"]))
                for s in self.samples
            ],
            source_clock_mapping_proven=False,
            affects_acceptance_gate=False,
        )
