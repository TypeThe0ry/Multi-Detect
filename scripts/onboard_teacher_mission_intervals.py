"""Bounded receive-only intervals within ONE pre-uploaded teacher Mission.

Activation is an observed MISSION_CURRENT, never a new command submission.
Stable windows use original LOCAL_POSITION_NED metres and m/s, on one declared
observer clock/origin. PX4 boot milliseconds are retained, not used as host time.
Camera/pose observations remain separate: host-time proximity does not prove
sensor synchronization or equality of PX4 and Gazebo coordinate origins.

No filesystem, flight, transport, Actor, teacher label or ML operation exists
here. Source bindings are caller declarations; an outer collector must persist
original receive envelopes and independently verify bytes/JSON projections.
The frozen multirotor compiler has fly-through intermediate waypoints. Missing
stable windows are therefore valid INCOMPLETE evidence, not invented endpoints.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from math import dist, hypot

from multidetect.autonomy.onboard_executed_trajectory import ActualObservation, EvidenceBinding
from multidetect.autonomy.serialization import (
    canonical_json,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

CONTRACT = "multidetect.onboard-teacher-mission-intervals-v1"
MAX_EVENTS = 256
MAX_STATES = 2048
MAX_MESSAGE_BYTES = 4096


def _text(value, name):
    if type(value) is not str or require_text(value, name) != value or len(value) > 256:
        raise ValueError(f"{name} needs bounded canonical text")
    return value


def _source(value):
    if type(value) is not EvidenceBinding or type(value.json_pointer) is not str:
        raise TypeError("original JSON receive-envelope EvidenceBinding required")
    return value.to_mapping()


@dataclass(frozen=True, slots=True)
class MissionParentSubmission:
    """One declared actual upload lineage, not a per-leg send or authority."""

    mission_id: str
    transaction_id: str
    vehicle_id: str
    mission_sha256: str
    protocol_sha256: str
    item_count: int
    submitted_at_s: float
    clock_id: str
    expected_system_id: int
    expected_component_id: int
    local_origin_id: str
    source: EvidenceBinding

    def __post_init__(self):
        for name in ("mission_id", "transaction_id", "vehicle_id", "clock_id", "local_origin_id"):
            _text(getattr(self, name), name)
        for name in ("mission_sha256", "protocol_sha256"):
            value = getattr(self, name)
            if type(value) is not str or require_sha256(value, name) != value:
                raise ValueError(f"{name} needs lowercase SHA256")
        require_int(self.item_count, "item_count", minimum=1, maximum=64)
        require_real(self.submitted_at_s, "submitted_at_s", minimum=0)
        for name in ("expected_system_id", "expected_component_id"):
            require_int(getattr(self, name), name, minimum=1, maximum=255)
        _source(self.source)

    def to_mapping(self):
        return {**asdict(self), "source": self.source.to_mapping()}

    @classmethod
    def from_mapping(cls, value):
        raw = dict(strict_object(value, name=cls.__name__, required={f.name for f in fields(cls)}))
        raw["source"] = EvidenceBinding.from_mapping(raw["source"])
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class SettlementPolicy:
    """Precommitted observation thresholds, NOT flight success tolerances."""

    minimum_duration_s: float = 1.0
    minimum_samples: int = 5
    maximum_speed_mps: float = 0.2
    maximum_position_span_m: float = 0.15
    maximum_gap_s: float = 0.3
    maximum_endpoint_age_s: float = 0.3
    maximum_pose_skew_s: float = 0.1
    maximum_position_jump_m: float = 10.0

    def __post_init__(self):
        require_int(self.minimum_samples, "minimum_samples", minimum=2, maximum=128)
        bounds = {
            "minimum_duration_s": (0.2, 5.0),
            "maximum_speed_mps": (0.001, 1.0),
            "maximum_position_span_m": (0.001, 1.0),
            "maximum_gap_s": (0.001, 1.0),
            "maximum_endpoint_age_s": (0.001, 1.0),
            "maximum_pose_skew_s": (0.0, 1.0),
            "maximum_position_jump_m": (0.1, 100.0),
        }
        for name, (low, high) in bounds.items():
            require_real(getattr(self, name), name, minimum=low, maximum=high)


class MissionIntervalRecorder:
    """Single-caller ordered recorder. A relevant rejection latches invalid.

    Receive envelopes point to original JSON containing message, received_at_s,
    clock_id, transaction_id, local_origin_id (states), and packet_sha256 if
    available. Original packet bytes/hash are optional, never fabricated here.
    This object neither reads nor authenticates those original files.
    """

    def __init__(self, parent, tracked_sequences, *, policy=None):
        if type(parent) is not MissionParentSubmission:
            raise TypeError("typed parent Mission submission required")
        if type(tracked_sequences) is not tuple or not tracked_sequences:
            raise TypeError("nonempty immutable tracked_sequences required")
        for seq in tracked_sequences:
            require_int(seq, "tracked sequence", minimum=0, maximum=parent.item_count - 1)
        if tuple(sorted(set(tracked_sequences))) != tracked_sequences:
            raise ValueError("tracked sequences must be unique and ordered")
        self.parent = parent
        self.policy = policy if policy is not None else SettlementPolicy()
        if type(self.policy) is not SettlementPolicy:
            raise TypeError("typed SettlementPolicy required")
        self._events, self._states = [], []
        self._intervals = {
            seq: {
                "sequence": seq,
                "activation_event": None,
                "reached_event": None,
                "before_window": None,
                "after_window": None,
                "before": None,
                "after": None,
            }
            for seq in tracked_sequences
        }
        self._current = self._reached = self._last_time = self._boot_ms = None
        self._last_current_at = None
        self._telemetry_source_id = None
        self._failure = None
        self._closed = False

    def _check_open(self):
        if self._closed or self._failure is not None:
            raise ValueError("recorder closed or invalid; no reset/retry within one Mission")

    def _envelope(self, message, stamp, clock_id, transaction_id, source, packet_sha256):
        self._check_open()
        # Validate without replacing the original JSON number representation:
        # source-bound integer 2 must not silently become projected float 2.0.
        require_real(stamp, "received_at_s", minimum=self.parent.submitted_at_s)
        if clock_id != self.parent.clock_id or transaction_id != self.parent.transaction_id:
            raise ValueError("observer clock or parent transaction mismatch")
        if self._last_time is not None and stamp < self._last_time:
            raise ValueError("observer receive clock regressed")
        if not isinstance(message, Mapping):
            raise TypeError("original MAVLink message mapping required")
        raw = strict_json_loads(canonical_json(dict(message)))
        if len(canonical_json(raw).encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("message exceeds bounded raw metadata limit")
        for key, expected in (
            ("source_system_id", self.parent.expected_system_id),
            ("source_component_id", self.parent.expected_component_id),
        ):
            if require_int(raw.get(key), key, minimum=1, maximum=255) != expected:
                raise ValueError("MAVLink source identity mismatch")
        if packet_sha256 is not None:
            if (
                type(packet_sha256) is not str
                or require_sha256(packet_sha256, "packet") != packet_sha256
            ):
                raise ValueError("packet_sha256 must be lowercase SHA256 or null")
        binding = _source(source)
        if (
            self._telemetry_source_id is not None
            and binding["source_id"] != self._telemetry_source_id
        ):
            raise ValueError("original telemetry receive source changed")
        self._telemetry_source_id = binding["source_id"]
        return {
            "message": raw,
            "received_at_s": stamp,
            "clock_id": clock_id,
            "transaction_id": transaction_id,
            "packet_sha256": packet_sha256,
            "source": binding,
        }

    def record_feedback(
        self, message, *, received_at_s, clock_id, transaction_id, source, packet_sha256=None
    ):
        """Record only actual CURRENT/REACHED observations; unrelated types return False."""
        self._check_open()
        if isinstance(message, Mapping) and message.get("message_type") not in {
            "MISSION_CURRENT",
            "MISSION_ITEM_REACHED",
        }:
            return False
        try:
            if len(self._events) >= MAX_EVENTS:
                raise ValueError("Mission feedback capacity exhausted; no truncation")
            row = self._envelope(
                message, received_at_s, clock_id, transaction_id, source, packet_sha256
            )
            raw, stamp = row["message"], row["received_at_s"]
            seq = require_int(raw.get("seq"), "seq", minimum=0, maximum=self.parent.item_count - 1)
            kind = raw["message_type"]
            if kind == "MISSION_CURRENT":
                if (self._current is not None and seq < self._current) or (
                    self._reached is not None and seq < self._reached
                ):
                    raise ValueError("Mission CURRENT regressed or precedes reached item")
                if seq != self._current:
                    interval = self._intervals.get(seq)
                    if interval is not None:
                        interval["activation_event"] = len(self._events)
                        interval["before_window"] = self._window(stamp)
                    self._current = seq
                self._last_current_at = stamp
            else:
                if self._current is None or seq > self._current:
                    raise ValueError("Reached item ahead of observed CURRENT")
                if self._reached is not None and seq < self._reached:
                    raise ValueError("Reached sequence regressed")
                interval = self._intervals.get(seq)
                if interval is not None and interval["reached_event"] is None:
                    if interval["activation_event"] is None:
                        raise ValueError("Tracked item lacks actual activation")
                    interval["reached_event"] = len(self._events)
                self._reached = seq
            self._events.append(row)
            self._last_time = stamp
            return True
        except (ValueError, TypeError, KeyError) as exc:
            self._failure = str(exc)
            raise

    def record_state(
        self,
        message,
        *,
        received_at_s,
        clock_id,
        transaction_id,
        local_origin_id,
        source,
        packet_sha256=None,
    ):
        """Original local NED position+velocity, NOT an approved/planned endpoint."""
        self._check_open()
        try:
            if len(self._states) >= MAX_STATES:
                raise ValueError("state capacity exhausted; no truncation")
            row = self._envelope(
                message, received_at_s, clock_id, transaction_id, source, packet_sha256
            )
            raw = row["message"]
            if raw.get("message_type") != "LOCAL_POSITION_NED":
                raise ValueError("original LOCAL_POSITION_NED required")
            if local_origin_id != self.parent.local_origin_id:
                raise ValueError("PX4 local origin changed or unspecified")
            boot = require_int(
                raw.get("time_boot_ms"), "time_boot_ms", minimum=0, maximum=2**32 - 1
            )
            if self._boot_ms is not None and boot <= self._boot_ms:
                raise ValueError("PX4 boot clock duplicate/regressed/reset; no independent sample")
            for key in ("x", "y", "z", "vx", "vy", "vz"):
                require_real(raw.get(key), key, minimum=-1e6, maximum=1e6)
            if self._states:
                prior = self._states[-1]["message"]
                if (
                    dist([raw[k] for k in ("x", "y", "z")], [prior[k] for k in ("x", "y", "z")])
                    > self.policy.maximum_position_jump_m
                ):
                    raise ValueError("local position jump/reset exceeds declared observation bound")
            row["local_origin_id"] = local_origin_id
            self._states.append(row)
            self._boot_ms, self._last_time = boot, row["received_at_s"]
            interval = self._intervals.get(self._current)
            if (
                interval is not None
                and interval["reached_event"] is not None
                and interval["after_window"] is None
                and self._last_time - self._last_current_at <= 1.5
            ):
                reached_at = self._events[interval["reached_event"]]["received_at_s"]
                interval["after_window"] = self._window(self._last_time, earliest=reached_at)
            return True
        except (ValueError, TypeError, KeyError) as exc:
            self._failure = str(exc)
            raise

    def _window(self, now, *, earliest=None):
        selected = []
        for index in range(len(self._states) - 1, max(-1, len(self._states) - 129), -1):
            row = self._states[index]
            stamp = row["received_at_s"]
            if earliest is not None and stamp < earliest:
                break
            selected.append(index)
            if self._states[selected[0]]["received_at_s"] - stamp >= self.policy.minimum_duration_s:
                break
        selected.reverse()
        if len(selected) < self.policy.minimum_samples:
            return None
        rows = [self._states[index] for index in selected]
        start, end = rows[0]["received_at_s"], rows[-1]["received_at_s"]
        if (
            end > now
            or now - end > self.policy.maximum_endpoint_age_s
            or end - start < self.policy.minimum_duration_s
        ):
            return None
        gaps = [
            b["received_at_s"] - a["received_at_s"] for a, b in zip(rows, rows[1:], strict=False)
        ]
        boot_stamps = [r["message"]["time_boot_ms"] for r in rows]
        boot_duration_ms = boot_stamps[-1] - boot_stamps[0]
        boot_gaps = [b - a for a, b in zip(boot_stamps, boot_stamps[1:], strict=False)]
        speeds = [hypot(*(r["message"][k] for k in ("vx", "vy", "vz"))) for r in rows]
        points = [tuple(r["message"][k] for k in ("x", "y", "z")) for r in rows]
        span = max(dist(a, b) for a in points for b in points)
        if (
            max(gaps) > self.policy.maximum_gap_s
            or min(gaps) <= 0
            or boot_duration_ms < self.policy.minimum_duration_s * 1000
            or max(boot_gaps) > self.policy.maximum_gap_s * 1000
            or max(speeds) > self.policy.maximum_speed_mps
            or span > self.policy.maximum_position_span_m
        ):
            return None
        return {
            "state_indices": selected,
            "start_at_s": start,
            "end_at_s": end,
            "maximum_speed_mps": max(speeds),
            "position_span_m": span,
            "maximum_gap_s": max(gaps),
            "px4_boot_duration_ms": boot_duration_ms,
            "maximum_px4_boot_gap_ms": max(boot_gaps),
        }

    def attach_observation(self, sequence, endpoint, observation):
        """Attach already recorded camera+full-pose metadata inside a real stable window.

        This may be called after receiving CURRENT, using a retained earlier
        frame. It does not move the receive clock, synthesize an image, equate
        camera/PX4 positions or make a planned teacher label.
        """
        self._check_open()
        try:
            require_int(sequence, "sequence", minimum=0, maximum=self.parent.item_count - 1)
            if type(endpoint) is not str or endpoint not in {"before", "after"}:
                raise ValueError("endpoint must be before or after")
            if type(observation) is not ActualObservation:
                raise TypeError("actual source-bound camera/pose observation required")
            interval = self._intervals[sequence]
            window = interval[endpoint + "_window"]
            if window is None or interval[endpoint] is not None:
                raise ValueError("actual stable window required; endpoint is immutable")
            if (
                observation.clock_id != self.parent.clock_id
                or observation.pose.clock_id != self.parent.clock_id
                or abs(observation.at_s - observation.pose.at_s) > self.policy.maximum_pose_skew_s
                or not window["start_at_s"] <= observation.at_s <= window["end_at_s"]
                or not window["start_at_s"] <= observation.pose.at_s <= window["end_at_s"]
                or window["end_at_s"] - min(observation.at_s, observation.pose.at_s)
                > self.policy.maximum_endpoint_age_s
            ):
                raise ValueError("camera and full pose must share observer clock and stable window")
            other = interval["after" if endpoint == "before" else "before"]
            if other is not None:
                other = ActualObservation.from_mapping(other)
                before, after = (
                    (observation, other) if endpoint == "before" else (other, observation)
                )
                if (
                    before.at_s >= after.at_s
                    or before.sequence >= after.sequence
                    or before.frame_id == after.frame_id
                    or before.pose.world_origin_id != after.pose.world_origin_id
                    or before.pose.world_frame != after.pose.world_frame
                    or before.pose.origin != after.pose.origin
                    or before.pose.source.source_id != after.pose.source.source_id
                    or before.source.source_id != after.source.source_id
                    or before.image_source.source_id != after.image_source.source_id
                ):
                    raise ValueError("actual endpoint order/camera/pose-origin mismatch")
            interval[endpoint] = observation.to_mapping()
        except (ValueError, TypeError, KeyError) as exc:
            self._failure = str(exc)
            raise

    def snapshot(self):
        intervals = []
        for value in self._intervals.values():
            missing = [
                name
                for name in (
                    "activation_event",
                    "reached_event",
                    "before_window",
                    "after_window",
                    "before",
                    "after",
                )
                if value[name] is None
            ]
            intervals.append(
                {
                    **value,
                    "status": "incomplete" if missing else "settled_interval_observed",
                    "missing": missing,
                }
            )
        result = {
            "contract": CONTRACT,
            "schema_version": 1,
            "time_basis": "observer_monotonic",
            "parent_submission": self.parent.to_mapping(),
            "policy": asdict(self.policy),
            "closed": self._closed,
            "valid": self._failure is None,
            "failure": self._failure,
            "events": self._events,
            "states": self._states,
            "intervals": intervals,
            "counts": {
                "feedback_events": len(self._events),
                "state_observations": len(self._states),
                "settled_intervals": sum(not i["missing"] for i in intervals),
            },
            "claims": dict.fromkeys(
                (
                    "provenance_verified",
                    "actual_teacher_collected",
                    "teacher_expert_verified",
                    "train_admitted",
                    "quality_verified",
                    "execution_authority",
                    "mission_complete_verified",
                    "sensor_clock_mapping_verified",
                    "px4_gazebo_origin_mapping_verified",
                    "attitude_stability_verified",
                    "continuous_path_verified",
                    "final_release_allowed",
                ),
                False,
            ),
        }
        # Deep immutable copy to callers; no mutable raw source can change later evidence.
        return strict_json_loads(canonical_json(result))

    def close(self):
        self._closed = True
        return self.snapshot()
