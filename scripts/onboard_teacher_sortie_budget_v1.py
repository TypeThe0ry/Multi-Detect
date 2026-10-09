"""Finite owned-SIM lifecycle resources; a budget is never a flight Grant.

The declared ceilings must be enforced by the NEW writer/transport/executor.
They are not measurements or hardware-performance claims. Frozen 60-second
intents and V3 writer limits are not changed or renewed by this contract.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

from multidetect.autonomy.serialization import (
    canonical_sha256,
    require_int,
    require_real,
    require_text,
    strict_object,
)

CONTRACT = "multidetect.teacher-owned-SIM-sortie-resource-budget-v1"
PHASES = ("preflight", "transfer", "mission_mode", "normal_arm", "route", "rtl_land", "cleanup")
MAXIMUM_DURATION_S = 600.0
MAXIMUM_RECORDS = 262144
MAXIMUM_DATA_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TeacherSortieLifecycleBudget:
    clock_id: str
    started_at_s: float
    preflight_s: float
    transfer_s: float
    mission_mode_s: float
    normal_arm_s: float
    route_s: float
    rtl_land_s: float
    cleanup_s: float
    packet_rate_ceiling_hz: float
    protocol_packet_reserve: int
    maximum_raw_record_bytes: int
    maximum_projection_record_bytes: int
    maximum_receipt_record_bytes: int
    snapshot_bytes: int
    camera_bytes: int
    evidence_bytes: int
    start_camera_reserve_bytes: int
    start_evidence_reserve_bytes: int
    queue_capacity: int
    maximum_consumer_blocked_s: float
    maximum_dual_commit_s: float

    def __post_init__(self):
        object.__setattr__(self, "clock_id", require_text(self.clock_id, "clock_id"))
        for name in ("started_at_s", *[f"{phase}_s" for phase in PHASES]):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        if any(getattr(self, f"{phase}_s") <= 0 for phase in PHASES):
            raise ValueError(
                "Every lifecycle phase including return/cleanup needs positive reserve"
            )
        if not self.duration_s <= MAXIMUM_DURATION_S:
            raise ValueError("Owned development SIM lifecycle must fit ten minutes")
        for name, lower, upper in (
            ("packet_rate_ceiling_hz", 1, 1000),
            ("maximum_consumer_blocked_s", 0.001, 5),
            ("maximum_dual_commit_s", 0.001, 2),
        ):
            object.__setattr__(
                self, name, require_real(getattr(self, name), name, minimum=lower, maximum=upper)
            )
        for name, lower, upper in (
            ("protocol_packet_reserve", 32, 4096),
            ("maximum_raw_record_bytes", 512, 8192),
            ("maximum_projection_record_bytes", 512, 40960),
            ("maximum_receipt_record_bytes", 1024, 16384),
            ("snapshot_bytes", 65536, MAXIMUM_DATA_BYTES),
            ("camera_bytes", 1, MAXIMUM_DATA_BYTES),
            ("evidence_bytes", 65536, MAXIMUM_DATA_BYTES),
            ("start_camera_reserve_bytes", 1, MAXIMUM_DATA_BYTES),
            ("start_evidence_reserve_bytes", 65536, MAXIMUM_DATA_BYTES),
            ("queue_capacity", 16, 4096),
        ):
            object.__setattr__(
                self, name, require_int(getattr(self, name), name, minimum=lower, maximum=upper)
            )
        if self.record_limit > MAXIMUM_RECORDS or self.data_byte_limit > MAXIMUM_DATA_BYTES:
            raise ValueError(
                "Declared rate/record/byte ceilings exceed finite experiment resources"
            )
        if (
            self.start_camera_reserve_bytes > self.camera_bytes
            or self.start_evidence_reserve_bytes > self.evidence_bytes
        ):
            raise ValueError("Start reserves exceed total camera/evidence allocation")
        needed_queue = (
            math.ceil(
                self.packet_rate_ceiling_hz
                * (self.maximum_consumer_blocked_s + self.maximum_dual_commit_s)
            )
            + 16
        )
        if self.queue_capacity < needed_queue:
            raise ValueError("Queue cannot cover declared consumer/dual-commit blocking ceiling")

    @property
    def duration_s(self):
        return sum(getattr(self, f"{phase}_s") for phase in PHASES)

    @property
    def deadline_s(self):
        return self.started_at_s + self.duration_s

    @property
    def record_limit(self):
        return (
            math.ceil(self.duration_s * self.packet_rate_ceiling_hz) + self.protocol_packet_reserve
        )

    @property
    def telemetry_byte_limit(self):
        return (
            self.record_limit
            * (self.maximum_raw_record_bytes + self.maximum_projection_record_bytes)
            + self.snapshot_bytes
        )

    @property
    def data_byte_limit(self):
        return (
            self.telemetry_byte_limit
            + self.final_receipt_byte_limit
            + self.camera_bytes
            + self.evidence_bytes
        )

    @property
    def final_receipt_byte_limit(self):
        # Includes both retained packet metadata copies, batches, snapshots,
        # error/identity fields and the complete original budget document.
        return self.record_limit * self.maximum_receipt_record_bytes + self.snapshot_bytes + 65536

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    def to_mapping(self):
        return {"contract": CONTRACT, **asdict(self)}

    @classmethod
    def from_mapping(cls, raw):
        value = strict_object(
            raw,
            name="TeacherSortieLifecycleBudget",
            required=frozenset({"contract", *[f.name for f in fields(cls)]}),
        )
        if value["contract"] != CONTRACT:
            raise ValueError("Sortie resource contract differs")
        return cls(**{f.name: value[f.name] for f in fields(cls)})

    def remaining_s(self, now_s, *, clock_id):
        now = require_real(now_s, "lifecycle now", minimum=self.started_at_s)
        if clock_id != self.clock_id:
            raise ValueError("Lifecycle clock domain differs")
        return max(0.0, self.deadline_s - now)

    def assert_start_capacity(
        self,
        *,
        now_s,
        clock_id,
        route_duration_s,
        used_records,
        used_telemetry_bytes,
        remaining_camera_bytes,
        remaining_evidence_bytes,
        next_control="mission-mode",
    ):
        remaining = self.remaining_s(now_s, clock_id=clock_id)
        duration = require_real(route_duration_s, "approved complete route duration", minimum=0)
        if duration > self.route_s:
            raise ValueError("Approved complete route exceeds declared lifecycle allocation")
        if next_control not in {"mission-mode", "normal-arm"}:
            raise ValueError("Only the two finite start-capacity boundaries are admitted")
        required_time = (
            (self.mission_mode_s if next_control == "mission-mode" else 0)
            + self.normal_arm_s
            + self.route_s
            + self.rtl_land_s
            + self.cleanup_s
        )
        needed_records = (
            math.ceil(required_time * self.packet_rate_ceiling_hz) + self.protocol_packet_reserve
        )
        used = require_int(used_records, "used records", minimum=0)
        charged = require_int(used_telemetry_bytes, "used telemetry bytes", minimum=0)
        camera = require_int(remaining_camera_bytes, "remaining camera bytes", minimum=0)
        evidence = require_int(remaining_evidence_bytes, "remaining evidence bytes", minimum=0)
        needed_bytes = (
            needed_records * (self.maximum_raw_record_bytes + self.maximum_projection_record_bytes)
            + self.snapshot_bytes
        )
        if (
            remaining < required_time
            or self.record_limit - used < needed_records
            or self.telemetry_byte_limit - charged < needed_bytes
            or camera < self.start_camera_reserve_bytes
            or evidence < self.start_evidence_reserve_bytes
        ):
            raise ValueError("Insufficient lifecycle/return/cleanup reserve: no normal ARM")

    def phase_deadline_s(self, phase):
        if phase not in PHASES:
            raise ValueError("Unknown finite lifecycle phase")
        return self.started_at_s + sum(
            getattr(self, f"{p}_s") for p in PHASES[: PHASES.index(phase) + 1]
        )

    def assert_phase(self, phase, *, now_s, clock_id):
        self.remaining_s(now_s, clock_id=clock_id)
        if not now_s < self.phase_deadline_s(phase):
            raise ValueError("Fixed phase deadline exhausted; no lifecycle renewal")


__all__ = ["TeacherSortieLifecycleBudget"]
