"""Bounded, receive-only attitude/HUD evidence; no inferred TAS or clock join."""

import copy
import math
from collections.abc import Sequence

from multidetect.simulation.timestamp_contract import build_timestamp_contract


class MissionDynamicsCapture:
    FIELDS = {
        "ATTITUDE": ("roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed"),
        "VFR_HUD": ("airspeed", "groundspeed", "alt", "climb"),
        "ATTITUDE_TARGET": ("body_roll_rate", "body_pitch_rate", "body_yaw_rate", "thrust"),
        "NAV_CONTROLLER_OUTPUT": (
            "nav_roll",
            "nav_pitch",
            "alt_error",
            "aspd_error",
            "xtrack_error",
        ),
    }

    def __init__(self, capacity_per_stream=32768):
        if type(capacity_per_stream) is not int or not 1 <= capacity_per_stream <= 32768:
            raise ValueError("capacity must be an integer in 1..32768")
        self.capacity = capacity_per_stream
        self.streams = {
            name: dict(samples=[], samples_seen=0, rejected_source_count=0) for name in self.FIELDS
        }

    def record(self, message, received_at_s):
        kind = message.get_type()
        if kind not in self.FIELDS:
            return
        if (
            type(received_at_s) not in (int, float)
            or not math.isfinite(received_at_s)
            or received_at_s < 0
        ):
            raise ValueError("finite nonnegative receipt time required")
        stream = self.streams[kind]
        source = (message.get_srcSystem(), message.get_srcComponent())
        if any(type(v) is not int or v != 1 for v in source):
            stream["rejected_source_count"] += 1
            return
        stream["samples_seen"] += 1
        if len(stream["samples"]) >= self.capacity:
            return
        sample = dict(
            received_at_s=received_at_s,
            source_system_id=source[0],
            source_component_id=source[1],
            invalid_or_missing_fields=[],
        )
        for name in self.FIELDS[kind]:
            value = getattr(message, name, None)
            if type(value) not in (int, float) or not math.isfinite(value):
                sample["invalid_or_missing_fields"].append(name)
                value = None
            sample[name] = value
        if kind in ("ATTITUDE", "ATTITUDE_TARGET"):
            boot = getattr(message, "time_boot_ms", None)
            if type(boot) is not int or not 0 <= boot <= 0xFFFFFFFF:
                sample["invalid_or_missing_fields"].append("time_boot_ms")
                boot = None
            sample["time_boot_ms"] = boot
        if kind == "ATTITUDE_TARGET":
            quaternion = getattr(message, "q", None)
            if (
                not isinstance(quaternion, Sequence)
                or isinstance(quaternion, (str, bytes))
                or len(quaternion) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in quaternion)
            ):
                sample["q"] = None
                sample["invalid_or_missing_fields"].append("q")
            else:
                sample["q"] = list(quaternion)  # preserve raw, never silently normalize
            mask = getattr(message, "type_mask", None)
            if type(mask) is not int or not 0 <= mask <= 255:
                sample["invalid_or_missing_fields"].append("type_mask")
                mask = None
            sample["type_mask"] = mask
        # VFR_HUD has no source timestamp. Never borrow ATTITUDE's timestamp
        # or relabel its generic airspeed field as calibrated/true airspeed.
        stream["samples"].append(sample)

    def snapshot(self):
        streams = copy.deepcopy(self.streams)
        for stream in streams.values():
            stream["samples_dropped"] = stream["samples_seen"] - len(stream["samples"])
            stream["truncated"] = stream["samples_dropped"] > 0
        return dict(
            schema_version=1,
            clock="observer_relative_host_receive_time",
            timestamp_contract=build_timestamp_contract(),
            capacity_per_stream=self.capacity,
            streams=streams,
            units={
                "ATTITUDE": dict(
                    time_boot_ms="source_boot_ms",
                    roll="rad",
                    pitch="rad",
                    yaw="rad",
                    rollspeed="rad/s",
                    pitchspeed="rad/s",
                    yawspeed="rad/s",
                ),
                "VFR_HUD": dict(
                    airspeed="m/s_unspecified_airspeed_kind",
                    groundspeed="m/s",
                    alt="m_MSL",
                    climb="m/s_up",
                ),
                "ATTITUDE_TARGET": dict(
                    time_boot_ms="source_boot_ms",
                    q="quaternion_wxyz",
                    type_mask="MAVLink_attitude_target_ignore_bits",
                    body_roll_rate="rad/s",
                    body_pitch_rate="rad/s",
                    body_yaw_rate="rad/s",
                    thrust="normalized",
                ),
                "NAV_CONTROLLER_OUTPUT": dict(
                    nav_roll="deg",
                    nav_pitch="deg",
                    alt_error="m",
                    aspd_error="m/s",
                    xtrack_error="m",
                ),
            },
            vfr_hud_source_timestamp_available=False,
            nav_controller_source_timestamp_available=False,
            true_airspeed_semantics_proven=False,
            source_clock_mapping_proven=False,
            continuous_coverage_proven=False,
            affects_acceptance_gate=False,
        )
