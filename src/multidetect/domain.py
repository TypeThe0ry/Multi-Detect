from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import hypot, isfinite, nan
from typing import Any


class ConfigurationError(ValueError):
    """Raised when a mission would violate the prototype's safety boundary."""


class StateTransitionError(RuntimeError):
    """Raised when an invalid mission or payload transition is requested."""


class SensorKind(StrEnum):
    RGB = "rgb"
    THERMAL = "thermal"
    FUSED = "fused"


class MissionPhase(StrEnum):
    STANDBY = "standby"
    NAVIGATING = "navigating"
    SEARCHING = "searching"
    TARGET_CONFIRMED = "target_confirmed"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    DEPLOYMENT_READY = "deployment_ready"
    DEPLOYING = "deploying"
    VERIFYING_RELEASE = "verifying_release"
    EGRESS = "egress"
    RETURN_REQUESTED = "return_requested"
    TERMINATED = "terminated"
    FAULT = "fault"


class PayloadState(StrEnum):
    LOCKED = "locked"
    ARMED = "armed"
    RELEASE_REQUESTED = "release_requested"
    RELEASED = "released"
    RELEASE_CONFIRMED = "release_confirmed"
    FAILED = "failed"


class Verdict(StrEnum):
    PASS = "pass"
    DENY = "deny"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Normalized XYXY bounding box."""

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        values = (self.x1, self.y1, self.x2, self.y2)
        if not all(isfinite(value) for value in values):
            raise ValueError("bounding box coordinates must be finite")
        if any(value < 0.0 or value > 1.0 for value in values):
            raise ValueError("bounding box coordinates must be normalized to [0, 1]")
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("bounding box must have positive area")

    @property
    def area(self) -> float:
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def iou(self, other: BoundingBox) -> float:
        x1 = max(self.x1, other.x1)
        y1 = max(self.y1, other.y1)
        x2 = min(self.x2, other.x2)
        y2 = min(self.y2, other.y2)
        intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        union = self.area + other.area - intersection
        return intersection / union if union > 0 else 0.0

    def expanded(self, margin: float) -> BoundingBox:
        return BoundingBox(
            max(0.0, self.x1 - margin),
            max(0.0, self.y1 - margin),
            min(1.0, self.x2 + margin),
            min(1.0, self.y2 + margin),
        )

    def intersects(self, other: BoundingBox) -> bool:
        return not (
            self.x2 <= other.x1 or other.x2 <= self.x1 or self.y2 <= other.y1 or other.y2 <= self.y1
        )

    def center_distance(self, other: BoundingBox) -> float:
        ax, ay = self.center
        bx, by = other.center
        return hypot(ax - bx, ay - by)

    def rounded(self, digits: int = 5) -> tuple[float, float, float, float]:
        return tuple(round(value, digits) for value in (self.x1, self.y1, self.x2, self.y2))


@dataclass(frozen=True, slots=True)
class Detection:
    label: str
    confidence: float
    bbox: BoundingBox
    sensor: SensorKind = SensorKind.RGB
    model_version: str = "unknown"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_label = self.label.strip().lower()
        if not normalized_label:
            raise ValueError("detection label cannot be empty")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("detection confidence must be in [0, 1]")
        object.__setattr__(self, "label", normalized_label)


@dataclass(frozen=True, slots=True)
class VehicleTelemetry:
    altitude_agl_m: float
    roll_deg: float
    pitch_deg: float
    ground_speed_mps: float
    in_allowed_zone: bool | None
    geofence_healthy: bool | None
    position_healthy: bool | None
    link_healthy: bool | None
    flight_mode_allows_deploy: bool | None
    release_zone_clear: bool | None
    person_detector_healthy: bool | None = None
    latitude_deg: float = nan
    longitude_deg: float = nan
    heading_deg: float = nan
    battery_remaining_pct: float = nan
    satellites_visible: int | None = None
    armed: bool | None = None
    flight_mode: str | None = None
    mission_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class FrameObservation:
    frame_id: str
    captured_at_s: float
    detections: tuple[Detection, ...]
    telemetry: VehicleTelemetry

    def __post_init__(self) -> None:
        if not self.frame_id:
            raise ValueError("frame_id cannot be empty")
        if not isfinite(self.captured_at_s) or self.captured_at_s < 0:
            raise ValueError("captured_at_s must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class TrackSnapshot:
    track_id: str
    revision: int
    label: str
    bbox: BoundingBox
    first_seen_at_s: float
    last_seen_at_s: float
    observation_count: int
    consecutive_observations: int
    confidence_floor: float
    confidence_mean: float
    maximum_gap_s: float
    area_growth_rate: float
    thermal_corroborated: bool
    confirmed: bool

    @property
    def duration_s(self) -> float:
        return max(0.0, self.last_seen_at_s - self.first_seen_at_s)


@dataclass(frozen=True, slots=True)
class FireAlert:
    """A confirmed fire observation ready for delivery over a data link."""

    alert_id: str
    mission_id: str
    target_id: str
    target_revision: int
    frame_id: str
    label: str
    confidence: float
    bbox: BoundingBox
    observed_at_s: float
    aircraft_latitude_deg: float = nan
    aircraft_longitude_deg: float = nan
    aircraft_altitude_agl_m: float = nan


@dataclass(frozen=True, slots=True)
class RuleCheck:
    rule_id: str
    verdict: Verdict
    reason: str


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    allowed: bool
    target_id: str
    target_revision: int
    frame_id: str
    scene_digest: str
    ruleset_version: str
    evaluated_at_s: float
    checks: tuple[RuleCheck, ...]
    priority_score: float = 0.0

    @property
    def denial_reasons(self) -> tuple[str, ...]:
        return tuple(check.reason for check in self.checks if check.verdict is not Verdict.PASS)


@dataclass(frozen=True, slots=True)
class AuthorizationChallenge:
    challenge_id: str
    nonce: str
    mission_id: str
    target_id: str
    target_revision: int
    payload_slot_id: str
    scene_digest: str
    ruleset_version: str
    created_at_s: float
    expires_at_s: float


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    challenge_id: str
    operator_id: str
    approved: bool
    granted_at_s: float


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    timestamp_s: float
    event_type: str
    details: Mapping[str, Any] = field(default_factory=dict)
